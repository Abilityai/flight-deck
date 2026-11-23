#!/usr/bin/env python3
"""Flight Deck feeder: serves the screensaver page and one JSON snapshot of this Mac.

  GET /            web/index.html (read from disk on every request, so edits show on reload)
  GET /<name>      web/<name>.html, another skin of the same page (e.g. /dusk)
  GET /state.json  machine + agents + usage snapshot (the monitor page)
  GET /lite.json   a few numbers for the screensaver, which draws natively and asks every 5 s
  GET /survey.json what the skill's survey found on this machine, for the page's first-run card
  POST /voice/start | /voice/stop   TALK mode: the voice agent, shown in a frame (voice.py)

Everything is collected lazily, on request: with no screensaver (or browser tab) asking,
this process sleeps in accept() and costs nothing. Bound to 127.0.0.1 only.

Sources
  machine  psutil, pmset
  agents   ~/.claude/sessions/<pid>.json (live Claude Code processes) joined with
           ~/.cache/cc-dash/<session>/dashboard.txt (cc-dash's one-line summaries)
  usage    ~/.claude/history.jsonl (every prompt, all accounts) + cc-dash log.jsonl (live feed)
  sessions transcripts in ~/.claude/projects: per-subscription stats, recent sessions and the
           topic graph (sessions.py; indexed in a background thread, never blocks a request)
  attention  focus blocks, switches, reply time from cc-dash's prompt/answer log, or from the
           transcripts where cc-dash is not installed (attention.py)
  runtimes the other agent runtimes of this machine: Codex, OpenCode, Gemini CLI sessions join the
           agents, the sessions, the prompt counts and attention (runtimes.py)
  fleet    `Tailscale status --json`
  remote   the remote machine over ssh, trinity = the Trinity instances over MCP (remote.py;
           both in a background thread)
  backup   the restic log named by the config's `backup_log`
"""
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

import attention
import config
import remote
import runtimes
import sessions
import voice

# The TRINITY view shows a desk that lives in another agent's skill folder (config key `desk`): its data
# module is loaded from there. Without one the view is absent and these routes answer null.
marvin = None
if config.CFG["desk"]:
    import sys
    sys.path.insert(0, str(config.CFG["desk"]["dir"]))
    import deskdata as marvin  # noqa: E402

PORT = config.CFG["port"]
HOME = Path.home()
WEB = Path(__file__).resolve().parent / "web"
CC_DASH = HOME / ".cache/cc-dash"
SESSIONS = HOME / ".claude/sessions"
HISTORY = HOME / ".claude/history.jsonl"
RESTIC_LOG = config.CFG["backup_log"]
TAILSCALE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
# Set FLIGHT_DECK_PRIVATE=1 to hide the per-agent summary lines (the saver shows on the lock screen).
PRIVATE = os.environ.get("FLIGHT_DECK_PRIVATE") == "1"

_cache = {}
_busy = set()
_lock = threading.Lock()


def cached(key, ttl, fn):
    """Return fn() at most once per ttl seconds; on failure keep serving the last good value.

    While one request is computing a key, the others get the previous value instead of starting
    the same slow call again, so a hanging source (a stuck `tailscale status`) delays one reply,
    not every reply until it returns.
    """
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and (now - hit[0] < ttl or key in _busy):
            return hit[1]
        _busy.add(key)
    try:
        val = fn()
    except Exception as e:  # a broken source must never blank the whole screen
        val = hit[1] if hit else {"error": str(e)}
    finally:
        with _lock:
            _busy.discard(key)
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def sh(cmd, timeout=4):
    # TERM: the Tailscale app binary only acts as a CLI when it sees a terminal-ish environment;
    # under launchd (no TERM) it tries to start the GUI and fails.
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "TERM": "xterm"}).stdout


# ---------------------------------------------------------------- machine

_net_prev = None
_static = {}


def static_info():
    if not _static:
        _static["chip"] = sh(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
        try:
            _static["pcores"] = int(sh(["sysctl", "-n", "hw.perflevel0.physicalcpu"]))
        except ValueError:
            _static["pcores"] = psutil.cpu_count()
        _static["os"] = "macOS " + sh(["sw_vers", "-productVersion"]).strip()
        _static["host"] = sh(["scutil", "--get", "ComputerName"]).strip() or socket.gethostname()
    return _static


def machine():
    global _net_prev
    s = static_info()
    cores = psutil.cpu_percent(interval=None, percpu=True)  # delta since the previous call
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    du = psutil.disk_usage(str(HOME))
    now = time.time()
    io = psutil.net_io_counters()
    up = down = 0.0
    if _net_prev and now > _net_prev[0]:
        span = now - _net_prev[0]
        up = max(0, io.bytes_sent - _net_prev[1]) / span
        down = max(0, io.bytes_recv - _net_prev[2]) / span
    _net_prev = (now, io.bytes_sent, io.bytes_recv)
    batt = psutil.sensors_battery()
    return {
        **s,
        "cores": cores,
        "cpu": sum(cores) / max(1, len(cores)),
        "load": os.getloadavg(),
        "mem": {"used": vm.total - vm.available, "total": vm.total, "pct": vm.percent,
                "swap": sw.used},
        "disk": {"used": du.used, "total": du.total, "free": du.free, "pct": du.percent},
        "net": {"up": up, "down": down},
        "battery": {"pct": batt.percent, "plugged": batt.power_plugged} if batt else None,
        "uptime": now - psutil.boot_time(),
        "procs": cached("procs", 4, top_procs),
        "nproc": len(psutil.pids()),
    }


_proc_objs = {}


def top_procs():
    """Top 5 by CPU. Process objects are kept between calls: cpu_percent() is a delta per object."""
    rows, seen = [], set()
    for p in psutil.process_iter(["pid", "name"]):
        seen.add(p.pid)
        obj = _proc_objs.setdefault(p.pid, p)
        try:
            c = obj.cpu_percent(None)
            if c > 0.5:
                rows.append({"name": (p.info["name"] or "?")[:22], "cpu": c})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    for pid in set(_proc_objs) - seen:
        del _proc_objs[pid]
    return sorted(rows, key=lambda r: -r["cpu"])[:5]


# ---------------------------------------------------------------- agents

def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def agents():
    out = []
    for f in SESSIONS.glob("*.json"):
        s = read_json(f)
        if not s or not alive(s.get("pid", -1)):
            continue
        sid = s.get("sessionId", "")
        d = CC_DASH / sid
        st = read_json(d / "state.json", {}) or {}
        title = status = ""
        if not PRIVATE:
            try:
                lines = (d / "dashboard.txt").read_text().splitlines()
                title = lines[0].strip() if lines else ""
                status = next((l[6:].strip() for l in lines if l.startswith("STATUS")), "")
            except OSError:
                pass
        busy = s.get("status") == "busy" if "status" in s else bool(st.get("busy"))
        out.append({
            "id": sid[:8],
            "sid": sid,
            "project": Path(s.get("cwd") or "?").name,
            "name": s.get("name", ""),
            "busy": busy,
            "since": (s.get("statusUpdatedAt") or 0) / 1000 or st.get("since") or 0,
            "started": (s.get("startedAt") or 0) / 1000,
            "title": title,
            "status": status,
            "model": (st.get("model") or ""),
            "rt": "claude",
        })
    if not PRIVATE and any(not a["title"] for a in out):       # no cc-dash summary: the transcript's own title
        names = {f["sid"]: f["title"] or f["first"] for f in list((sessions._index or {}).values()) if not f["sub"]}
        for a in out:
            a["title"] = a["title"] or names.get(a["sid"], "")
    more = cached("live_rt", 5, lambda: runtimes.live(PRIVATE))  # Codex, OpenCode, Gemini CLI sessions that are open
    out += more if isinstance(more, list) else []
    out.sort(key=lambda a: (not a["busy"], -a["since"]))
    return out


# ---------------------------------------------------------------- usage

def usage():
    """Activity = prompts typed, from Claude Code's prompt history (~/.claude/history.jsonl).

    That file is shared by every account dir, so it covers all subscriptions with no gaps. The
    stats cache used before belongs to one login and is recomputed rarely, which left holes.
    The live feed (asked / answered) still comes from cc-dash's turn logs.
    """
    daily, hours, hours_today, projects_today = {}, [0] * 24, [0] * 24, set()
    today = dt.date.today().isoformat()
    try:
        with open(HISTORY, errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    t = dt.datetime.fromtimestamp(e["timestamp"] / 1000)
                except (ValueError, KeyError, TypeError, OSError):
                    continue
                day = t.date().isoformat()
                daily[day] = daily.get(day, 0) + 1
                hours[t.hour] += 1
                if day == today:
                    hours_today[t.hour] += 1
                    projects_today.add(e.get("project") or "?")
    except OSError:
        pass
    for ts, project in runtimes.prompts():                   # what was typed into the other runtimes
        t = dt.datetime.fromtimestamp(ts)
        day = t.date().isoformat()
        daily[day] = daily.get(day, 0) + 1
        hours[t.hour] += 1
        if day == today:
            hours_today[t.hour] += 1
            projects_today.add(project)

    cutoff = time.time() - 2 * 86400
    feed = [(ts, kind, project) for ts, kind, project, _ in attention.extra(cutoff)]
    for log in CC_DASH.glob("*/log.jsonl"):
        try:
            if log.stat().st_mtime < cutoff:
                continue
            hook = read_json(log.parent / "hook.json", {}) or {}
            project = Path(hook.get("cwd") or "?").name
            for line in log.read_text().splitlines()[-30:]:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("kind") == "prompt" and str(e.get("text", "")).startswith("<task-notification"):
                    continue
                feed.append((e.get("ts", 0), e.get("kind"), project))
        except OSError:
            continue

    start = dt.date.today() - dt.timedelta(days=26 * 7 + dt.date.today().weekday())
    heat = []
    day = start
    while day <= dt.date.today():
        heat.append(daily.get(day.isoformat(), 0))
        day += dt.timedelta(days=1)

    feed.sort()
    return {
        "heat": heat,
        "heat_start": start.isoformat(),
        "hours": hours,
        "hours_today": hours_today,
        "today": {"prompts": daily.get(today, 0), "projects": len(projects_today)},
        "total": {"prompts": sum(daily.values()), "days": len(daily), "since": min(daily) if daily else "",
                  "week": sum(heat[-7:])},
        "feed": [{"ts": ts, "kind": k, "project": p} for ts, k, p in feed[-14:]],
    }


# ---------------------------------------------------------------- fleet + backup

def fleet():
    d = json.loads(sh([TAILSCALE, "status", "--json"], timeout=6))
    peers = [p for p in (d.get("Peer") or {}).values() if not p.get("ExitNodeOption")]
    servers = [{"name": p.get("HostName", "?"), "online": bool(p.get("Online"))}
               for p in peers if p.get("OS") == "linux"]
    servers.sort(key=lambda s: (not s["online"], s["name"]))
    exit_node = d.get("ExitNodeStatus") or {}
    return {
        "state": d.get("BackendState"),
        "online": sum(1 for p in peers if p.get("Online")),
        "total": len(peers),
        "servers": servers[:14],
        "exit_node": bool(exit_node.get("Online")),
    }


def backup():
    if not RESTIC_LOG:
        return None
    done = None
    ok = None
    for line in reversed(RESTIC_LOG.read_text(errors="replace").splitlines()[-200:]):
        m = re.match(r"=== (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) backup done (\w+)", line)
        if m:
            done = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            ok = m.group(2) == "OK"
            break
    return {"last": done, "ok": ok}


def parts():
    """Which optional sources this machine has: the page leaves out what has none."""
    return {"claude": next((HOME / ".claude/projects").glob("*/*.jsonl"), None) is not None, "cc_dash": attention.has_cc_dash(),
            "fleet": Path(TAILSCALE).is_file(), "backup": bool(RESTIC_LOG)}


FD = next((p for p in (WEB.parents[1] / "scripts/fd.py", WEB.parents[1] / ".claude/skills/flight-deck/scripts/fd.py") if p.is_file()), None)


def survey():
    """What the first-run card shows: the skill's own survey of this machine (reads only), as lines."""
    if not FD:
        return {"lines": []}
    r = subprocess.run([sys.executable, str(FD), "survey", "--json"], capture_output=True, text=True, timeout=40, env={**os.environ, "FLIGHT_DECK_BACKGROUND": "1"})     # no folder macOS would ask about
    d = json.loads(r.stdout)
    return {"lines": d.get("lines") or [], "agent": d.get("agent") or ""}


def welcomed():
    """The person closed the first-run card: it does not come back."""
    try:
        user = json.loads(config.FILE.read_text())
    except (OSError, ValueError):
        user = {}
    user["welcome"] = False
    tmp = config.FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(user, indent=2, ensure_ascii=False) + "\n")
    tmp.chmod(0o600)
    tmp.replace(config.FILE)
    config.CFG["welcome"] = False
    return {"ok": True}


def snapshot():
    live = cached("agents", 0.9, agents)
    return {
        "ts": time.time(),
        "machine": cached("machine", 0.7, machine),
        "agents": live,
        "sessions": sessions.snapshot([a["sid"] for a in live if isinstance(a, dict)], PRIVATE),
        "runtimes": cached("runtimes", 20, runtimes.summary),
        "parts": cached("parts", 60, parts),
        "usage": cached("usage", 45, usage),
        "fleet": cached("fleet", 30, fleet),
        "backup": cached("backup", 60, backup),
        "attention": cached("attention", 30, lambda: attention.build([x["sid"] for x in live if isinstance(x, dict)])),
        "remote": remote.machine(),
        "voice": cached("voice", 2, voice.status),
        "trinity": remote.trinity(PRIVATE),
        "trinities": remote.trinities(PRIVATE),
        "instances": remote.instances(),
    }


def lite_machine():
    vm = psutil.virtual_memory()
    batt = psutil.sensors_battery()
    return {"cpu": psutil.cpu_percent(interval=None), "load": os.getloadavg()[0], "mem": vm.percent,
            "battery": {"pct": batt.percent, "plugged": batt.power_plugged} if batt else None}


def lite():
    """The screensaver's snapshot: small, and nothing in it is expensive to collect."""
    live = cached("agents", 4, agents)
    live = [a for a in live if isinstance(a, dict)] if isinstance(live, list) else []
    sess = sessions.snapshot([a["sid"] for a in live], PRIVATE)
    return {
        "ts": time.time(),
        "machine": cached("lite_machine", 4, lite_machine),
        "agents": [{"project": a["project"], "busy": a["busy"], "since": a["since"],
                    "text": a["status"] or a["title"]} for a in live],
        "accounts": [{"name": a["name"], "ci": a["ci"], "live": a["live"],
                      "five": (a.get("limits") or {}).get("five"),
                      "week": (a.get("limits") or {}).get("week"),
                      "out": a["today"]["out"]} for a in sess.get("accounts", [])],
        "backup": cached("backup", 60, backup),
    }


def page(path):
    """The page, with what it needs from config.py as window.__config (set before its own script)."""
    cfg = json.dumps(config.for_page()).replace("<", "\\u003c")
    return path.read_text().replace("<script>", f"<script>window.__config = {cfg};</script>\n<script>", 1).encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Only this machine's own address may ask: a web page that points its own name at 127.0.0.1 (DNS
        # rebinding) arrives with that name as the Host, and is refused before it can read anything.
        if self.headers.get("Host") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            self.send_error(403)
            return
        path = self.path.split("?")[0]
        if path == "/state.json":
            body, ctype = json.dumps(snapshot()).encode(), "application/json"
        elif path == "/lite.json":
            body, ctype = json.dumps(lite()).encode(), "application/json"
        elif path == "/survey.json":
            body, ctype = json.dumps(cached("survey", 120, survey)).encode(), "application/json"
        elif path == "/stage.json":                       # the TRINITY view's stage: what Marvin's loop or Marvin put up (the desk's stage.py)
            f = marvin.DIR / "stage.json" if marvin else None
            body, ctype = (f.read_bytes() if f and f.is_file() and not PRIVATE else b"null"), "application/json"
        elif path == "/decisions.json":                   # the asks that wait for an answer, with their options (the desk's loop)
            f = marvin.DIR / "decisions.json" if marvin else None
            body, ctype = (f.read_bytes() if f and f.is_file() and not PRIVATE else b"null"), "application/json"
        elif path == "/marvin.json":                      # the Trinity view: asks, projects, services (the desk's deskdata.py)
            body, ctype = json.dumps(marvin.snapshot(PRIVATE) if marvin else None).encode(), "application/json"
        elif path in ("/", "/index.html"):
            body, ctype = page(WEB / "index.html"), "text/html; charset=utf-8"
        elif re.fullmatch(r"/[a-z0-9-]+(\.html)?", path) and (WEB / (path[1:].removesuffix(".html") + ".html")).is_file():
            # other skins of the page, e.g. /dusk -> web/dusk.html (same data, same origin)
            body, ctype = page(WEB / (path[1:].removesuffix(".html") + ".html")), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """TALK mode: start / stop the voice agent. These act, so only this page may call them:
        the Host and Origin must be this server's own and the page's header must be present
        (another web page cannot set it without a CORS preflight, which is never answered)."""
        hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if (self.headers.get("Host") not in hosts or self.headers.get("X-Flight-Deck") != "1"
                or self.headers.get("Origin") not in {f"http://{h}" for h in hosts}):
            self.send_error(403)
            return
        try:
            req = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)) or b"{}")
        except ValueError:
            req = {}
        if self.path == "/voice/start":
            out = voice.start(agent=str(req.get("agent") or ""), force=bool(req.get("force")))
        elif self.path == "/voice/stop":
            out = voice.stop()
        elif self.path == "/orb/start":
            out = voice.orb()
        elif self.path == "/focus" and marvin:
            out = marvin.screen(str(req.get("ask") or ""), str(req.get("view") or ""), int(req.get("n") or 0), int(req.get("of") or 0),
                                str(req.get("picked") or ""))
        elif self.path == "/welcome/done":
            out = welcomed()
        elif self.path == "/trinity/add":
            # the key comes from this page only (see the checks above), goes to the Keychain, and is never sent back
            out = remote.add(req.get("label"), req.get("url"), req.get("key"))
        elif self.path == "/trinity/main":
            out = remote.set_main(req.get("label"))
        elif self.path == "/trinity/remove":
            out = remote.remove(req.get("label"))
        elif self.path == "/ask/answer" and marvin and not PRIVATE:
            # The person at this page answers an ask: recorded on the platform as their own answer.
            out = marvin.answer(str(req.get("id") or ""), str(req.get("response") or ""), str(req.get("response_text") or ""))
        else:
            self.send_error(404)
            return
        with _lock:
            _cache.pop("voice", None)
        body = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    psutil.cpu_percent(interval=None, percpu=True)  # prime the delta counters
    psutil.cpu_percent(interval=None)
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

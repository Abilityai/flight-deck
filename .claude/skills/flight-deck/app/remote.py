"""Flight Deck: what is going on on the other machines.

  machine  the remote machine (a Linux box), over ssh (the config's `remote.ssh_host`):
           load, CPU, memory, disk, GPU, what holds the GPU, the busiest containers
  trinity  the Trinity instances of the config, each over its MCP endpoint (an entry of a Claude
           config, or a URL with its key in the Keychain): agents, the last 24 h of runs, operator queue

Both are slow (0.5-3 s), so they are collected in a background thread and a request only ever
gets the last finished value. Nothing is collected while nobody is asking. Read-only.
"""
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import config
from config import CFG

_state = {}
_lock = threading.Lock()


def _bg(key, ttl, fn):
    """Last value of fn(), refreshed in the background when older than ttl. Never blocks."""
    now = time.time()
    with _lock:
        e = _state.setdefault(key, {"v": None, "at": 0, "busy": False})
        if now - e["at"] < ttl or e["busy"]:
            return e["v"]
        e["busy"] = True

    def run():
        try:
            v = fn()
        except Exception as ex:  # noqa: BLE001  (shown on the page instead of data)
            v = {"ok": False, "err": f"{type(ex).__name__}: {ex}"[:90]}
        with _lock:
            e.update(v=v, at=time.time(), busy=False)

    threading.Thread(target=run, daemon=True).start()
    return e["v"]


# ---------------------------------------------------------------- the remote machine

HOST = (CFG["remote"] or {}).get("ssh_host")
SCRIPT = r"""
echo "@host $(hostname)"
echo "@up $(cut -d' ' -f1 /proc/uptime)"
echo "@load $(cut -d' ' -f1 /proc/loadavg)"
echo "@ncpu $(nproc)"
echo "@stat1 $(head -1 /proc/stat)"
echo "@mem $(awk '/^MemTotal/{t=$2} /^MemAvailable/{a=$2} END{print t*1024, a*1024}' /proc/meminfo)"
echo "@disk $(df -B1 --output=size,avail / | tail -1)"
echo "@gpu $(nvidia-smi --query-gpu=name,utilization.gpu,temperature.gpu,power.draw --format=csv,noheader,nounits 2>/dev/null | head -1)"
sleep 0.5
echo "@stat2 $(head -1 /proc/stat)"
"""


def _app_name(args):
    """A readable name for a GPU process from "<working dir>|<command line>"."""
    cwd, _, args = args.partition("|")
    m = re.search(r"-m\s+(\S+)", args)
    if "llama-server" in args and m:
        alias = re.search(r"--alias\s+(\S+)", args)
        model = Path(m.group(1)).name
        model = re.sub(r"(-\d{5}-of-\d{5})?\.gguf$", "", model)
        return f"llama · {model}" + (f" ({alias.group(1)})" if alias else "")
    port = re.search(r"--port\s+(\d+)", args)
    if "main.py" in args and port and port.group(1) == "8188":
        return "ComfyUI"
    parts = args.split()
    name = Path(parts[0]).name if parts else "?"
    if name.startswith("python") and len(parts) > 1:
        name = Path(parts[1]).name
        if cwd:                      # "server.py" says nothing; its folder usually does
            name = f"{cwd} · {name}"
    return name[:40]


def _machine():
    p = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", HOST, "bash -s"],
        input=SCRIPT, capture_output=True, text=True, timeout=25)
    if p.returncode != 0 and "@host" not in p.stdout:
        return {"ok": False, "err": (p.stderr.strip().splitlines() or ["ssh failed"])[-1][:90]}
    one, apps, cts, stats = {}, [], [], []
    for line in p.stdout.splitlines():
        if not line.startswith("@"):
            continue
        k, _, v = line[1:].partition(" ")
        v = v.strip()
        if k == "app":
            mem, _, args = v.partition(" ")
            if mem.isdigit():
                apps.append({"name": _app_name(args), "mem": int(mem) * 2 ** 20})
        elif k == "ct":
            cts.append(v)
        elif k == "cs":
            name, _, pct = v.rpartition(" ")
            try:
                stats.append({"name": name, "cpu": float(pct.rstrip("%"))})
            except ValueError:
                pass
        else:
            one[k] = v

    def jiffies(s):
        n = [int(x) for x in s.split()[1:9]]
        return sum(n), n[3] + n[4]          # total, idle + iowait

    cpu = None
    try:
        t1, i1 = jiffies(one["stat1"])
        t2, i2 = jiffies(one["stat2"])
        if t2 > t1:
            cpu = 100 * (1 - (i2 - i1) / (t2 - t1))
    except (KeyError, ValueError, IndexError):
        pass
    num = lambda s: float(s) if re.fullmatch(r"-?[\d.]+", s or "") else None   # noqa: E731
    mem = [num(x) for x in one.get("mem", "").split()] + [None, None]
    disk = [num(x) for x in one.get("disk", "").split()] + [None, None]
    gpu = [x.strip() for x in one.get("gpu", "").split(",")] + [""] * 4
    apps.sort(key=lambda a: -a["mem"])
    stats.sort(key=lambda s: -s["cpu"])
    return {
        "ok": True, "ts": time.time(),
        "host": one.get("host", "remote"), "uptime": num(one.get("up")), "load": num(one.get("load")),
        "ncpu": num(one.get("ncpu")), "cpu": cpu,
        "mem": {"total": mem[0], "used": mem[0] - mem[1]} if mem[0] and mem[1] is not None else None,
        "disk": {"total": disk[0], "free": disk[1]} if disk[0] else None,
        "gpu": {"name": gpu[0], "util": num(gpu[1]), "temp": num(gpu[2]), "watts": num(gpu[3])} if gpu[0] else None,
        "apps": apps[:4],
        "containers": {"total": len(cts), "agents": sum(c.startswith("agent-") for c in cts)},
        "busy": [s for s in stats if s["cpu"] >= 1][:4],
    }


def machine():
    if not HOST:
        return None
    return _bg("remote", 20, _machine)


# ---------------------------------------------------------------- Trinity

def endpoint(c):
    """{"url", "headers"} of one configured instance. Three forms: an entry of a Claude config
    ({"server", "claude_json"}), a URL whose key is in the macOS Keychain ({"url", "keychain"}; the item
    is service `flight-deck-trinity`, account = the keychain value), or {"url", "headers"} given outright."""
    if "url" not in c:
        f = Path(c.get("claude_json", "~/.claude.json")).expanduser()
        return json.loads(f.read_text())["mcpServers"][c.get("server", "trinity")]
    if c.get("keychain"):
        key = subprocess.run(["security", "find-generic-password", "-s", "flight-deck-trinity", "-a", c["keychain"], "-w"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if not key:
            raise RuntimeError("no key in the Keychain for this instance")
        return {"url": c["url"], "headers": {"Authorization": "Bearer " + key}}
    return c


class _Mcp:
    """Just enough of MCP over streamable HTTP to call a few read-only tools."""

    def __init__(self, c=None, timeout=20):
        self.timeout = timeout
        c = endpoint(c or CFG["trinity"][0])
        self.url = c["url"]
        self.headers = {**(c.get("headers") or {}), "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"}
        self.sid = None
        self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "flight-deck", "version": "1"}})
        try:
            self.rpc("notifications/initialized", notify=True)
        except Exception:  # noqa: BLE001
            pass

    def rpc(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if not notify:
            body["id"] = 1
        if params is not None:
            body["params"] = params
        h = dict(self.headers)
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        with urllib.request.urlopen(urllib.request.Request(self.url, json.dumps(body).encode(), h), timeout=self.timeout) as r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            text = r.read().decode()
            if "text/event-stream" in (r.headers.get("Content-Type") or ""):
                text = "".join(line[5:] for line in text.splitlines() if line.startswith("data:"))
        return json.loads(text) if text.strip() else None

    def call(self, tool, **args):
        r = self.rpc("tools/call", {"name": tool, "arguments": args})
        if not r or "result" not in r:
            raise RuntimeError(f"{tool}: {((r or {}).get('error') or {}).get('message', 'no result')}"[:120])
        return json.loads(r["result"]["content"][0]["text"])


_PRIO = {"critical": 0, "high": 1, "medium": 2, "normal": 2, "low": 3}


def _n(v):
    """A count from an instance, as a number whatever it sent: the page writes these into its markup."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _trinity(private, c=None):
    m = _Mcp(c)
    agents = m.call("list_agents")
    agents = agents if isinstance(agents, list) else agents.get("agents", [])
    day = m.call("get_agent_activity_summary", hours=24)
    hour = m.call("get_agent_activity_summary", hours=1)

    def tally(summary):
        out = {"done": 0, "failed": 0, "running": 0}
        for states in (summary.get("by_type") or {}).values():
            out["done"] += _n(states.get("completed"))
            out["failed"] += _n(states.get("failed"))
            out["running"] += _n(states.get("started"))
        return out

    queue = None
    try:
        q = m.call("list_operator_queue", status="pending")
        items = q.get("items", [])
        items.sort(key=lambda i: (_PRIO.get(i.get("priority"), 2), i.get("created_at") or ""))
        queue = {
            "pending": _n(q.get("total", len(items))),
            "critical": sum(i.get("priority") == "critical" for i in items),
            "more": bool(q.get("has_more")),
            "top": [] if private else [{"agent": str(i.get("agent_name") or "")[:60], "title": str(i.get("title") or "")[:90],
                                        "priority": str(i.get("priority") or "")[:20], "type": str(i.get("type") or "")[:20]}
                                       for i in items[:3]],
        }
    except Exception:  # noqa: BLE001  (the queue is a bonus; the rest still shows)
        pass
    by = sorted(((str(k)[:60], _n(v)) for k, v in (day.get("by_agent") or {}).items()), key=lambda kv: -kv[1])
    recent = {str(k)[:60]: _n(v) for k, v in (hour.get("by_agent") or {}).items()}
    return {
        "ok": True, "ts": time.time(),
        "host": re.sub(r"^https?://([^/:.]+).*", r"\1", m.url),
        "agents": {"total": len(agents), "running": sum(a.get("status") == "running" for a in agents)},
        "day": tally(day), "hour": tally(hour),
        "active": [{"name": n, "day": c, "hour": recent.get(n, 0)} for n, c in by[:6]],
        "active_n": len(by),
        "queue": queue,
    }


def label_of(c, n):
    return str(c.get("label") or ("Trinity" if n == 0 else f"Trinity {n + 1}"))


def _fresh():
    """Take the instance list again when the config file changed (fd.py, an editor): no restart needed."""
    try:
        m = config.FILE.stat().st_mtime
    except OSError:
        m = 0
    if m != _fresh.seen:
        if _fresh.seen is not None:
            CFG["trinity"] = config.load()["trinity"]
        _fresh.seen = m


_fresh.seen = None


def _data(c, n, private):
    # one cache per instance, by its label: making another one the main one shows it at once
    return _bg("trinity:" + label_of(c, n), 90 if n == 0 else 120, lambda: _trinity(private, c))


def trinity(private=False):
    """The main instance (the first of the config), in full."""
    _fresh()
    if not CFG["trinity"]:
        return None
    return _data(CFG["trinity"][0], 0, private)


def instances():
    """What the page lists and switches between: label, host, where the key lives. Never a key."""
    out = []
    for n, c in enumerate(CFG["trinity"]):
        try:
            url = c["url"] if "url" in c else endpoint(c)["url"]
        except Exception:  # noqa: BLE001  (a Claude config entry that is gone)
            url = ""
        out.append({"label": label_of(c, n), "host": re.sub(r"^https?://([^/]+).*", r"\1", url), "main": n == 0,
                    "kept": "keychain" if c.get("keychain") else "config" if "url" in c else "claude"})
    return out


KEYCHAIN = "flight-deck-trinity"


def _save(items):
    """Write the instance list to the config file, leaving every other key as it is, and use it from now."""
    try:
        user = json.loads(config.FILE.read_text())
    except (OSError, ValueError):
        user = {}
    user["trinity"] = items
    config.FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(user, indent=2, ensure_ascii=False) + "\n")
    tmp.chmod(0o600)                                   # it can hold a key in its `headers` form
    tmp.replace(config.FILE)
    CFG["trinity"] = items
    try:
        _fresh.seen = config.FILE.stat().st_mtime
    except OSError:
        pass


def _find(label):
    for n, c in enumerate(CFG["trinity"]):
        if label_of(c, n) == label:
            return n
    return -1


def add(label, url, key):
    """Connect an instance from its address and MCP key. It is tried first (one read-only call), and saved
    only when it answers: the key goes to the Keychain, the address and label to the config file."""
    label, url, key = str(label or "").strip()[:40], str(url or "").strip(), str(key or "").strip()
    if not url or not key:
        return {"ok": False, "error": "give the instance's address and its MCP key"}
    schemes = [""] if re.match(r"^https?://", url) else ["https://", "http://"]    # typed without one: try both
    url = schemes[0] + url
    if not re.match(r"^https?://[^/\s]+(/\S*)?$", url):
        return {"ok": False, "error": "that does not look like an address"}
    host = re.sub(r"^https?://([^/:]+).*", r"\1", url)
    label = label or (host if re.match(r"^[\d.]+$|^localhost$", host) else host.split(".")[0])   # a name from the address
    if _find(label) >= 0:
        return {"ok": False, "error": f"an instance called {label} is already connected"}
    bare = re.match(r"^https?://[^/]+/?$", url)                 # no path given: the MCP endpoint is usually /mcp
    tries = [url.rstrip("/") + "/mcp", url] if bare else [url]
    # Typed without a scheme: plain http is tried only for an address that is not on the public internet (this
    # machine, a private or tailnet address, a bare host name). Elsewhere a blocked https would hand the key over.
    private_net = re.match(r"^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|[^.]+$|.*\.ts\.net$|.*\.local$)", host)
    tries += [u.replace("https://", "http://", 1) for u in tries] if len(schemes) > 1 and private_net else []
    err = ""
    for u in tries:
        try:
            _Mcp({"url": u, "headers": {"Authorization": "Bearer " + key}}, timeout=12).call("list_agents")
            url, err = u, ""
            break
        except urllib.error.HTTPError as e:
            err = "the instance refused the key" if e.code in (401, 403) else f"the instance answered {e.code} at {u}"
            if e.code in (401, 403):
                break
        except Exception as e:  # noqa: BLE001
            reason = getattr(e, "reason", None) or e
            err = f"no answer from {host}: {reason}"[:140]
    if err:
        return {"ok": False, "error": err}
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN, "-a", label, "-w", key],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return {"ok": False, "error": "the Keychain did not take the key: " + r.stderr.strip()[:100]}
    _save(CFG["trinity"] + [{"label": label, "url": url, "keychain": label}])
    return {"ok": True, "label": label, "instances": instances()}


def set_main(label):
    n = _find(str(label))
    if n < 0:
        return {"ok": False, "error": f"no instance called {label}"}
    items = [dict(c, label=label_of(c, i)) for i, c in enumerate(CFG["trinity"])]   # labels stay with their instance
    _save([items[n]] + items[:n] + items[n + 1:])
    return {"ok": True, "label": str(label), "instances": instances()}


def remove(label):
    n = _find(str(label))
    if n < 0:
        return {"ok": False, "error": f"no instance called {label}"}
    items = [dict(c, label=label_of(c, i)) for i, c in enumerate(CFG["trinity"])]
    gone = items.pop(n)
    if gone.get("keychain"):
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN, "-a", gone["keychain"]], capture_output=True, timeout=15)
    _save(items)
    with _lock:
        _state.pop("trinity:" + str(label), None)
    return {"ok": True, "label": str(label), "instances": instances()}


def trinities(private=False):
    """The other instances, a short summary each (the page gives them one line)."""
    out = []
    for i, c in enumerate(CFG["trinity"][1:], 1):
        d = _data(c, i, private)
        label = label_of(c, i)
        if not d:
            out.append({"label": label, "ok": False, "wait": True})
        elif not d.get("ok"):
            out.append({"label": label, "ok": False, "err": d.get("err", "")})
        else:
            out.append({"label": label, "ok": True, "agents": d["agents"], "day": d["day"],
                        "queue": {"pending": d["queue"]["pending"]} if d.get("queue") else None})
    return out


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:                              # one line per configured instance: can it be reached?
        if not CFG["trinity"]:
            print("no Trinity instance is configured")
        for n, c in enumerate(CFG["trinity"]):
            label = label_of(c, n)
            try:
                d = _trinity(True, c)
                print(f"ok    {label}: {d['host']} · {d['agents']['running']}/{d['agents']['total']} agents running"
                      + (f" · {d['queue']['pending']} waiting in the queue" if d.get("queue") else "") + ("  (main)" if n == 0 else ""))
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}: {type(e).__name__}: {str(e)[:110]}")
    else:
        print(json.dumps({"remote": _machine() if HOST else None, "trinity": _trinity(False) if CFG["trinity"] else None}, indent=1))

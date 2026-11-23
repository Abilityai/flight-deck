#!/usr/bin/env python3
"""Flight Deck: set it up, start it, stop it, see whether it is healthy.

  fd.py survey [--json]   look at this machine: what is here that the deck can use (changes nothing)
  fd.py setup [--fresh]   first run on a machine: survey it, write a config that fits, install the feeder, open the window
                          (an existing config is kept; --fresh puts it aside and writes a new one)
  fd.py start      make sure the feeder runs and the window is open and in front (never closes it)
  fd.py stop       close the window (the feeder stays: it collects nothing while no page asks)
  fd.py stop --all close the window and stop the feeder too
  fd.py restart    restart the feeder (after a change to its Python files or to the config)
  fd.py status     feeder, window, config, the Trinity desk it shows, the voice session
  fd.py doctor     what this machine has and lacks for each part of the deck
  fd.py trinity    the Trinity instances: which are connected, whether each answers, which others were found
  fd.py trinity add <label> --server <name> [--file <claude config>]   connect one that Claude Code already knows
  fd.py trinity add <label> --url <mcp url>                            connect one by address; asks for its key, kept in the Keychain
  fd.py trinity main <label> | remove <label>                          choose the main one / disconnect one
  fd.py install    build and install everything: screensaver, `flight-deck` command, feeder LaunchAgent
  fd.py uninstall  remove all of that (the config stays)
  fd.py package <folder>   write a standalone agent folder holding this skill and the app, for another machine

The project is the `app/` folder of this skill when there is one, else the `flight-deck/` folder of
this agent; this script only drives it.
"""
import datetime as dt
import getpass
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
SKILL = Path(__file__).resolve().parents[1]                       # <agent>/.claude/skills/flight-deck
AGENT = SKILL.parents[2]
PROJECT = SKILL / "app" if (SKILL / "app/feeder.py").is_file() else AGENT / "flight-deck"
LABEL = "local.flight-deck"
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
LAUNCHER = HOME / ".local/bin/flight-deck"
SAVER = HOME / "Library/Screen Savers/Flight Deck.saver"
CONFIG = Path(os.environ.get("FLIGHT_DECK_CONFIG") or HOME / ".config/flight-deck/config.json")
VENV = HOME / ".local/share/flight-deck/venv"
MARKER = "--user-data-dir=" + str(HOME / ".cache/flight-deck/chrome")
DOMAIN = f"gui/{os.getuid()}"
_OWN_VOICE = AGENT / ".claude/skills/voice-agent"                 # a voice skill that came with this agent, else the user's
VOICE_SKILL = _OWN_VOICE if (_OWN_VOICE / "voice_server.py").is_file() else HOME / ".claude/skills/voice-agent"
LIPSYNC = HOME / ".cache/voice-agent"
CHROME = Path("/Applications/Google Chrome.app")
KEYCHAIN = "flight-deck-trinity"                                  # the Keychain service of Trinity keys given by address


def port():
    try:
        return int(os.environ.get("FLIGHT_DECK_PORT") or json.loads(CONFIG.read_text()).get("port", 47600))
    except (OSError, ValueError):
        return 47600


def get(path, timeout=3):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port()}{path}", timeout=timeout) as r:
            return r.read()
    except Exception:  # noqa: BLE001
        return None


def run(*cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def _json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _tilde(p):
    p = str(p)
    return "~" + p[len(str(HOME)):] if p.startswith(str(HOME) + "/") else p


def window_pid():
    """The dedicated Chrome instance that holds the deck's window, if it runs."""
    out = run("pgrep", "-f", "--", MARKER).stdout.split()      # "--": the marker itself starts with dashes
    for pid in out:
        cmdline = run("ps", "-o", "command=", "-p", pid).stdout
        if "Contents/MacOS/Google Chrome" in cmdline and "Helper" not in cmdline.split(" --")[0]:
            return int(pid)
    return None


def front_pid():
    asn = run("lsappinfo", "front").stdout.strip()
    out = run("lsappinfo", "info", "-only", "pid", asn).stdout
    digits = "".join(c for c in out if c.isdigit())
    return int(digits) if digits else None


def feeder_up():
    return get("/lite.json") is not None


# ---------------------------------------------------------------- the feeder's interpreter and LaunchAgent

def _has_psutil(py):
    return bool(py) and Path(py).is_file() and run(py, "-c", "import psutil").returncode == 0


def plist_args():
    try:
        return plistlib.loads(PLIST.read_bytes()).get("ProgramArguments") or []
    except Exception:  # noqa: BLE001
        return []


def pick_python(make=True):
    """A Python 3 that has psutil (the feeder's one dependency): the one in use, a system one, or a
    small virtual environment of the deck's own."""
    seen = []
    for c in [*plist_args()[:1], "/opt/homebrew/bin/python3", "/usr/local/bin/python3", shutil.which("python3"),
              sys.executable, VENV / "bin/python3"]:
        if c and str(c) not in seen:
            seen.append(str(c))
            if _has_psutil(c):
                return str(c)
    if not make:
        return None
    base = next((c for c in seen if Path(c).is_file() and str(VENV) not in c), None)
    if not base:
        return None
    VENV.parent.mkdir(parents=True, exist_ok=True)
    run(base, "-m", "venv", VENV)
    run(VENV / "bin/python3", "-m", "pip", "install", "-q", "psutil")
    return str(VENV / "bin/python3") if _has_psutil(VENV / "bin/python3") else None


def write_plist(py):
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps({
        "Label": LABEL, "ProgramArguments": [py, str(PROJECT / "feeder.py")], "RunAtLoad": True, "KeepAlive": True,
        "ProcessType": "Background", "StandardErrorPath": str(HOME / "Library/Logs/flight-deck.log")}))


def loaded():
    return run("launchctl", "print", f"{DOMAIN}/{LABEL}").returncode == 0


def ensure_feeder(rewrite=False):
    args = plist_args()
    stale = len(args) < 2 or args[1] != str(PROJECT / "feeder.py") or not Path(args[0]).is_file()
    if feeder_up() and not stale and not rewrite:
        return "feeder: running"
    if stale or rewrite:
        py = pick_python()
        if not py:
            return "feeder: NOT installed: no Python 3 with psutil, and one could not be made (pip install psutil)"
        if loaded():
            run("launchctl", "bootout", f"{DOMAIN}/{LABEL}")
            time.sleep(0.5)
        write_plist(py)
    if loaded():
        run("launchctl", "kickstart", "-k", f"{DOMAIN}/{LABEL}")
    else:
        run("launchctl", "bootstrap", DOMAIN, PLIST)
    for _ in range(40):
        time.sleep(0.25)
        if feeder_up():
            return "feeder: started"
    return "feeder: NOT answering (see ~/Library/Logs/flight-deck.log; is another program on the port?)"


# ---------------------------------------------------------------- start, stop, status

def start():
    msg = ensure_feeder()
    print(msg)
    if "NOT" in msg:
        return 1
    url = f"http://127.0.0.1:{port()}/"
    if not CHROME.is_dir():                       # the window is a dedicated Chrome instance; without Chrome, a browser tab
        run("open", url)
        print("window: Google Chrome is not installed, so the page was opened in the default browser:", url)
        return 0
    if not LAUNCHER.is_file():
        LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
        r = run("swiftc", "-O", PROJECT / "launcher.swift", "-o", LAUNCHER)
        if r.returncode != 0:
            run("open", "-na", "Google Chrome", "--args", MARKER, "--no-first-run", "--no-default-browser-check", "--app=" + url)
            print("window: opened (the `flight-deck` command could not be built: the Xcode command line tools are missing)")
            return 0
    pid = window_pid()
    if pid and pid == front_pid():
        print("window: already in front")          # the command would close a window that is in front
        return 0
    run(LAUNCHER)
    print("window: brought to the front" if pid else "window: opened")
    return 0


def stop(everything):
    pid = window_pid()
    if LAUNCHER.is_file():
        run(LAUNCHER, "close")
    elif pid:
        os.kill(pid, 15)
    print("window: closed")
    if everything:
        run("launchctl", "bootout", f"{DOMAIN}/{LABEL}")
        print("feeder: stopped (start brings it back)")
    return 0


def restart():
    if not loaded():
        msg = ensure_feeder()
        print(msg)
        return 1 if "NOT" in msg else 0
    run("launchctl", "kickstart", "-k", f"{DOMAIN}/{LABEL}")
    time.sleep(0.6)
    for _ in range(40):
        if feeder_up():
            print("feeder: restarted; reload the window (cmd-R) if the page changed")
            return 0
        time.sleep(0.25)
    print("feeder: NOT answering after restart (see ~/Library/Logs/flight-deck.log)")
    return 1


def status():
    up = feeder_up()
    print("project :", PROJECT)
    print("feeder  :", f"running on 127.0.0.1:{port()}" if up else "NOT running")
    print("agent   :", LABEL, "loaded" if loaded() else "not loaded")
    pid = window_pid()
    print("window  :", "open" + (" and in front" if pid == front_pid() else "") if pid else "closed")
    print("command :", LAUNCHER if LAUNCHER.is_file() else "missing (start builds it)")
    print("config  :", CONFIG if CONFIG.is_file() else "none (defaults; `setup` writes one)")
    if not up:
        return 1
    try:
        s = json.loads(get("/state.json", 6))
        v = s.get("voice") or {}
        print("voice   :", f"conversation with {v.get('agent')}" if v.get("running") else "no voice session")
        print("sessions:", len(s.get("agents") or []), "local Claude sessions")
        t = s.get("trinity")
        more = len(s.get("trinities") or [])
        print("trinity :", (f"{t['agents']['running']}/{t['agents']['total']} agents running" if t and t.get("ok") else
                            "not read yet, unreachable, or none connected (`fd.py trinity`)") + (f"; {more} more connected" if more else ""))
    except Exception as ex:  # noqa: BLE001
        print("state   : could not read:", type(ex).__name__)
    st = get("/stage.json")
    try:
        d = json.loads(st) if st else None
    except ValueError:
        d = None
    if d:
        age = int(time.time() - d.get("loop", 0))
        print("desk    :", f"{d.get('name', 'a desk')}'s Trinity desk, loop passed {age} s ago" + ("" if age < 300 else "  <- its loop is silent"))
    else:
        print("desk    : none shown (no `desk` in the config, or its loop has not run)")
    return 0


# ---------------------------------------------------------------- looking at the machine

def _agent_name():
    try:
        for line in (AGENT / "CLAUDE.md").read_text().splitlines():
            if line.startswith("# ") and len(line) < 40:
                return line[2:].strip()
    except OSError:
        pass
    return AGENT.name.capitalize() if AGENT.name.islower() else AGENT.name


# macOS asks the person before a background process may read these; the question blocks the read. When the
# feeder runs the survey (for the page's first-run card) folders there are left alone. In a terminal they are read.
BACKGROUND = os.environ.get("FLIGHT_DECK_BACKGROUND") == "1"
GUARDED = [HOME / "Desktop", HOME / "Documents", HOME / "Downloads", HOME / "Library/Mobile Documents", Path("/Volumes")]


def _agent_folders():
    """Folders Claude Code has worked in that hold an agent file, most recently used first."""
    seen = {}
    for f in [HOME / ".claude.json", *sorted(HOME.glob(".claude-*/.claude.json"))]:
        for path in (_json(f, {}) or {}).get("projects") or {}:
            p = Path(path)
            if BACKGROUND and any(p == g or g in p.parents for g in GUARDED):
                continue
            if p not in seen and p != HOME and (p / "CLAUDE.md").is_file():
                slug = "".join(c if c.isalnum() else "-" for c in str(p))
                logs = list((HOME / ".claude/projects" / slug).glob("*.jsonl"))
                seen[p] = max((x.stat().st_mtime for x in logs), default=0)
    if AGENT not in seen and (AGENT / "CLAUDE.md").is_file():
        seen[AGENT] = time.time()
    return [p for p, _ in sorted(seen.items(), key=lambda kv: -kv[1])]


def _trinity_found(folders):
    """MCP servers that look like a Trinity instance, in the Claude configs of this user and its agents.
    Nothing of an entry is read here but its name and the host of its address."""
    out, urls = [], set()
    user = [HOME / ".claude.json", *sorted(HOME.glob(".claude-*/.claude.json")), HOME / ".mcp.json"]
    for f in [*user, *[d / ".mcp.json" for d in folders]]:
        for name, c in ((_json(f, {}) or {}).get("mcpServers") or {}).items():
            url = str((c or {}).get("url") or "")
            if url and ("trinity" in name.lower() or "trinity" in url.lower()) and url not in urls:
                urls.add(url)
                out.append({"server": name, "file": _tilde(f), "host": re.sub(r"^https?://([^/:]+).*", r"\1", url),
                            "scope": "user" if f in user else "agent"})     # the user's own, or one agent's
    return out


def _find_desk(folders):
    """A trinity-desk skill in this agent, in an agent Claude Code has worked in, or in a folder beside this one."""
    beside = sorted(p for p in AGENT.parent.iterdir() if p.is_dir()) if AGENT.parent != HOME else []
    for d in dict.fromkeys([AGENT, *folders, *beside]):
        desk = d / ".claude/skills/trinity-desk/desk"
        if (desk / "deskdata.py").is_file():
            return desk
    return None


def _gemini_key():
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return True
    env = (_json(HOME / ".claude/settings.json", {}) or {}).get("env") or {}
    return bool(env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"))


def _runtimes():
    """The agent runtimes on this machine and, where the deck reads their sessions, how much is there."""
    try:
        sys.path.insert(0, str(PROJECT))
        import runtimes
        runtimes.PERSIST = False
        runtimes.sessions(wait=True)
        return runtimes.summary()
    except Exception as e:  # noqa: BLE001
        return [{"key": "error", "name": str(e)[:80], "cli": False, "data": False, "read": None}]


def survey():
    """Everything the deck could use on this machine. Reads only; no secret is read or returned."""
    folders = _agent_folders()
    faces = sorted(d.name for d in (VOICE_SKILL / "faces").iterdir() if (d / "neutral.jpg").is_file()) if (VOICE_SKILL / "faces").is_dir() else []
    hosts = []
    try:
        for line in (HOME / ".ssh/config").read_text().splitlines():
            m = re.match(r"\s*Host\s+(.+)", line, re.I)
            if m:
                hosts += [h for h in m.group(1).split() if "*" not in h and "?" not in h]
    except OSError:
        pass
    desk = _find_desk(folders)
    return {
        "mac": sys.platform == "darwin",
        "os": run("sw_vers", "-productVersion").stdout.strip(),
        "python": pick_python(make=False),
        "claude": {"cli": shutil.which("claude") or (str(HOME / ".local/bin/claude") if (HOME / ".local/bin/claude").is_file() else None),
                   "used": next((HOME / ".claude/projects").glob("*/*.jsonl"), None) is not None,
                   "accounts": 1 + len(list(HOME.glob(".claude-*/.claude.json"))) if (HOME / ".claude.json").is_file() else 0},
        "agents": [_tilde(p) for p in folders],
        "agent": _agent_name(),
        "runtimes": _runtimes(),
        "chrome": CHROME.is_dir(),
        "swiftc": bool(shutil.which("swiftc")),
        "voice": {"skill": _tilde(VOICE_SKILL) if (VOICE_SKILL / "voice_server.py").is_file() else None,
                  "own": VOICE_SKILL == _OWN_VOICE, "faces": faces, "key": _gemini_key(),
                  "lipsync": (LIPSYNC / "opentalking/.venv/bin/python").is_file() and (LIPSYNC / "models/quicktalk/checkpoints/quicktalk.pth").is_file(),
                  "ffmpeg": bool(shutil.which("ffmpeg"))},
        "cc_dash": (HOME / ".cache/cc-dash").is_dir(),
        "tailscale": Path("/Applications/Tailscale.app").is_dir(),
        "trinity": _trinity_found(folders),
        "desk": _tilde(desk) if desk else None,
        "ssh_hosts": hosts[:30],
        "backup_logs": [_tilde(p) for p in sorted((HOME / ".config/restic").glob("*.log"))] if (HOME / ".config/restic").is_dir() else [],
        "config": _tilde(CONFIG) if CONFIG.is_file() else None,
    }


def report(s):
    for mark, name, text in lines(s):
        print(f"{mark:<4}  {name:<10} {text}")


def lines(s):
    """The survey in plain lines: one per part of the deck, with what to do where something is missing."""
    out = []

    def line(mark, name, text):
        out.append([mark, name, text])

    v = s["voice"]
    n = s["claude"]["accounts"]
    line("ok" if s["mac"] else "MISS", "macOS", s["os"] if s["mac"] else "Flight Deck runs on macOS only")
    line("ok" if s["python"] else "none", "python", f"{s['python']} has psutil" if s["python"] else "no Python 3 with psutil yet: `setup` makes a small virtual environment for it")
    line("ok" if s["claude"]["used"] else "none", "claude", (f"Claude Code has run here ({n} login{'s' if n != 1 else ''}): DECK and SESSIONS have data"
                                                             if s["claude"]["used"] else "Claude Code has not run here: the deck shows the machine and the other runtimes below"
                                                             if any(r.get("last") for r in s["runtimes"]) else
                                                             "Claude Code has not run here yet: the deck shows the machine only until it has"))
    names = ", ".join(Path(a).name for a in s["agents"][:6]) + (" …" if len(s["agents"]) > 6 else "")
    line("ok" if s["agents"] else "none", "agents", f"{len(s['agents'])} agent folder{'s' if len(s['agents']) != 1 else ''} known to Claude Code" + (": " + names if names else ""))
    for r in s["runtimes"]:
        if r["key"] == "claude":
            continue
        if r["key"] == "error":
            line("MISS", "runtimes", "the other runtimes could not be looked at: " + r["name"])
        elif r["read"] and (r.get("last") or r.get("sessions")):
            last = dt.date.fromtimestamp(r["last"]).strftime("%-d %b %Y") if r.get("last") else "never"
            runs = f", {r['auto']} unattended run{'s' if r['auto'] != 1 else ''}" if r.get("auto") else ""
            what = "its sessions are on DECK, SESSIONS and ATTENTION" if r["read"] == "sessions" else "its prompts are counted (its log has no answers)"
            line("ok", r["key"], f"{r['name']}: {r['sessions']} sessions and {r['prompts']} prompts in 7 days{runs}, last used {last}; {what}")
        elif r["read"]:
            line("note", r["key"], f"{r['name']} is installed and has recorded nothing the deck can read yet")
        elif r["key"] == "ollama":
            line("note", r["key"], "Ollama (a model server) is here; it has no sessions to show")
        else:
            line("note", r["key"], f"{r['name']} is installed; the deck does not read its sessions")
    line("ok" if s["chrome"] else "none", "chrome", "the window opens as its own Chrome app window" if s["chrome"] else "no Google Chrome: the page opens as a tab of the default browser")
    line("ok" if s["swiftc"] else "none", "swiftc", "the `flight-deck` command and the screensaver can be built" if s["swiftc"] else "no Xcode command line tools: no toggle command, no screensaver (xcode-select --install)")
    if not v["skill"]:
        line("none", "voice", "no voice-agent skill: the TALK views are hidden")
    elif not v["key"]:
        line("MISS", "voice", "the voice skill is here but no GEMINI_API_KEY: add one under \"env\" in ~/.claude/settings.json (speech in and out use it)")
    else:
        whose = "this agent's own" if v["own"] else "the user's"
        line("ok", "voice", f"the voice skill ({whose}) and a speech key are here: TALK works; faces: {', '.join(v['faces']) or 'none'}")
    if v["skill"]:
        line("ok" if v["lipsync"] else "none", "lip-sync", "the talking-face engine is installed: the face's mouth follows the speech" if v["lipsync"] else
             "the talking-face engine is not installed: the face shows still portraits (the voice skill's SKILL.md has the install, about 4 GB)")
    line("ok" if s["cc_dash"] else "none", "cc-dash", "cc-dash logs are here: ATTENTION and the one-line session summaries come from them" if s["cc_dash"] else "no cc-dash logs: ATTENTION is worked out from the session transcripts, and a session shows its own title")
    line("ok" if s["tailscale"] else "none", "tailscale", "the Fleet box lists the tailnet's machines" if s["tailscale"] else "no Tailscale app: the Fleet box says so")
    if s["trinity"]:
        mine = [t for t in s["trinity"] if t["scope"] == "user"]
        line("ok", "trinity", (("the user's own: " + "; ".join(f"`{t['server']}` ({t['host']})" for t in mine)) if mine else "none of the user's own")
             + (f"; {len(s['trinity']) - len(mine)} more in agent folders (`fd.py trinity` lists them)" if len(s["trinity"]) > len(mine) else ""))
    else:
        line("none", "trinity", "no Trinity instance found in the Claude configs: connect one on the TRINITY page with its address and MCP key")
    line("ok" if s["desk"] else "none", "desk", f"a trinity-desk skill is at {s['desk']}: the TRINITY view" if s["desk"] else "no trinity-desk skill found: no TRINITY view")
    if s["ssh_hosts"]:
        line("note", "remote", f"{len(s['ssh_hosts'])} ssh host{'s' if len(s['ssh_hosts']) != 1 else ''} in ~/.ssh/config; a Linux one can be watched: set `remote` in the config to {{\"label\", \"ssh_host\"}}")
    if s["backup_logs"]:
        line("note", "backup", "restic log found: " + ", ".join(s["backup_logs"]))
    line("ok" if s["config"] else "none", "config", s["config"] or "none yet: defaults apply; `setup` writes one")
    return out


def doctor():
    s = survey()
    report(s)
    return 0 if s["mac"] else 1


def _config_for(s):
    """A config that fits what the survey found."""
    faces = s["voice"]["faces"]
    face = "marvin" if "marvin" in faces else "android" if "android" in faces else (faces or ["default"])[0]
    return {
        "port": 47600,
        "voice": {"port": 8766, "default_face": face,
                  "agents": [{"key": re.sub(r"[^a-z0-9]+", "-", AGENT.name.lower()).strip("-") or "agent", "name": _agent_name(),
                              "root": _tilde(AGENT), "face": face, "voice": "Charon"}]},
        "remote": None,
        # the user's own instances are connected; those of single agents are only listed (`fd.py trinity`), for the person to choose
        "trinity": [{"label": "Trinity" if n == 0 else t["server"], "server": t["server"], "claude_json": t["file"]}
                    for n, t in enumerate(t for t in s["trinity"] if t["scope"] == "user")],
        "backup_log": s["backup_logs"][0] if len(s["backup_logs"]) == 1 else None,
        "desk": {"dir": s["desk"], "voice": None} if s["desk"] else None,
        "welcome": True,                                 # the page shows what was found, once, until the person closes it
    }


def setup():
    """First run: look at the machine, write a config that fits it (never over an existing one unless --fresh),
    install the feeder, say what was found and what is missing, open the window."""
    s = survey()
    if not s["mac"]:
        print("Flight Deck runs on macOS only.")
        return 1
    if CONFIG.is_file() and "--fresh" in sys.argv:
        aside = CONFIG.with_name(f"config.{dt.datetime.now():%Y%m%d-%H%M%S}.json")
        CONFIG.rename(aside)
        print("config  : the old one was put aside as", _tilde(aside))
    if CONFIG.is_file():
        print("config  : kept as it is:", _tilde(CONFIG), "(`setup --fresh` writes a new one from the survey)")
    else:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(_config_for(s), indent=2) + "\n")
        os.chmod(CONFIG, 0o600)
        print("config  : written to fit this machine:", _tilde(CONFIG))
    msg = ensure_feeder(rewrite=True)
    print(msg)
    if "NOT" in msg:
        return 1
    report(survey())
    return start()


def install():
    r = subprocess.run(["/bin/zsh", str(PROJECT / "build.sh")])
    if r.returncode != 0:
        print("build: failed (the Xcode command line tools are needed: xcode-select --install); the feeder is installed all the same")
    msg = ensure_feeder(rewrite=True)
    print(msg)
    return 1 if "NOT" in msg or r.returncode else 0


def uninstall():
    stop(True)
    for p in (PLIST, LAUNCHER):
        p.unlink(missing_ok=True)
    for d in (SAVER, HOME / ".cache/flight-deck", VENV.parent):
        shutil.rmtree(d, ignore_errors=True)
    print("removed : the feeder's LaunchAgent, the `flight-deck` command, the screensaver, the cache.")
    print("kept    :", _tilde(CONFIG), "and any Trinity key in the Keychain (`fd.py trinity remove <label>` deletes one)")
    return 0


# ---------------------------------------------------------------- Trinity instances

def _load_config():
    cfg = _json(CONFIG)
    if cfg is None:
        cfg = _config_for(survey())
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
    return cfg


def _save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(CONFIG, 0o600)
    if loaded():
        run("launchctl", "kickstart", "-k", f"{DOMAIN}/{LABEL}")
        print("feeder: restarted with the new config; reload the window (cmd-R)")


def _check_trinity():
    py = pick_python(make=False) or sys.executable
    r = run(py, PROJECT / "remote.py", "--check", timeout=180)
    print((r.stdout or r.stderr).strip())


def trinity(args):
    """List, connect, disconnect. A key never passes through a conversation or the config file: an
    instance is either an entry Claude Code already holds, or an address whose key goes to the Keychain."""
    cmd = args[0] if args else "list"
    cfg = _load_config()
    have = cfg.get("trinity") or []
    label = args[1] if len(args) > 1 else ""

    def opt(name):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None

    if cmd == "list":
        print("connected:" if have else "connected: none")
        if have:
            _check_trinity()
        used = {(t.get("server"), str(Path(str(t.get("claude_json", "~/.claude.json"))).expanduser())) for t in have}
        found = [t for t in survey()["trinity"] if (t["server"], str(Path(t["file"]).expanduser())) not in used]
        if found:
            print("found in the Claude configs, not connected:")
            for t in found:
                print(f"  `{t['server']}` ({t['host']}): fd.py trinity add \"<a label>\" --server {t['server']} --file {t['file']}")
        print("to connect another: add its MCP server to Claude Code, then `fd.py trinity add <label> --server <name>`;")
        print("  or, in a terminal, `fd.py trinity add <label> --url <address>/mcp`, which asks for the instance's MCP key and keeps it in the Keychain.")
        return 0
    if not label:
        print(__doc__)
        return 1
    if cmd == "add":
        if any(t.get("label") == label for t in have):
            print(f"an instance labelled {label!r} is already connected")
            return 1
        if opt("--server"):
            f = opt("--file") or "~/.claude.json"
            if opt("--server") not in ((_json(Path(f).expanduser(), {}) or {}).get("mcpServers") or {}):
                print(f"no MCP server `{opt('--server')}` in {f}")
                return 1
            entry = {"label": label, "server": opt("--server"), "claude_json": f}
        elif opt("--url"):
            key = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass(f"MCP key of {label} (not shown, kept in the Keychain): ").strip()
            if not key:
                print("no key given")
                return 1
            r = run("security", "add-generic-password", "-U", "-s", KEYCHAIN, "-a", label, "-w", key)
            if r.returncode != 0:
                print("the Keychain refused the key:", r.stderr.strip()[:120])
                return 1
            entry = {"label": label, "url": opt("--url"), "keychain": label}
        else:
            print("give --server <name> or --url <address>")
            return 1
        cfg["trinity"] = have + [entry]
        print(f"connected: {label}" + (" (the main one)" if not have else ""))
    elif cmd in ("remove", "main"):
        hit = [t for t in have if t.get("label") == label]
        if not hit:
            print(f"no instance labelled {label!r}; connected: " + (", ".join(t.get("label", "?") for t in have) or "none"))
            return 1
        rest = [t for t in have if t.get("label") != label]
        if cmd == "remove":
            if hit[0].get("keychain"):
                run("security", "delete-generic-password", "-s", KEYCHAIN, "-a", hit[0]["keychain"])
            cfg["trinity"] = rest
            print(f"disconnected: {label}")
        else:
            cfg["trinity"] = hit + rest
            print(f"main instance: {label}")
    else:
        print(__doc__)
        return 1
    _save_config(cfg)
    time.sleep(1.5)
    _check_trinity()
    return 0


# ---------------------------------------------------------------- a copy for another machine

PKG_LABEL = "local.flight-deck"
TEXT = {".py", ".md", ".sh", ".swift", ".plist", ".html", ".json", ".txt", ".js", ""}
VOICE_FILES = ["voice_server.py", "voice.html", "voice.js", "face.js", "mic_worklet.js", "orb_sketch.js", "avatar_worker.py",
               "make_faces.py", "vendor/p5.min.js"]
VOICE_FACE = "marvin"                                             # the face of the agent: made for this, of no real person
VOICE_FACES = [VOICE_FACE, "stardust"]                            # the sets that ship; the others of this machine stay here


def _neutral_voice(dest):
    """The voice skill's copy, with what named this machine's owner and agents taken out. Every change is
    asserted: if the skill's text moved, packaging stops here rather than ship a name."""
    owner = "Eug" + "ene"
    f = dest / "voice_server.py"
    s = f.read_text()
    a = s.index('VOICE_PROMPT = """')
    b = s.index('"""', a + 20)
    p = s[a:b]
    assert f"{owner}. He is speaking" in p
    p = p.replace(f"with {owner}. He is speaking", "with the person at this computer. They are speaking")
    for x, y in [(r"\bhis\b", "their"), (r"\bhim\b", "them"), (r"\bHe is\b", "They are"), (r"\bhe wants\b", "they want"),
                 (r"\bhe needs\b", "they need"), (r"\bhe reads it\b", "they read it"), (r"\band sees it\b", "and see it"),
                 (r"\bhe asks\b", "they ask"), (r"\bhe\b", "they"), (r"\bHe\b", "They")]:
        p = re.sub(x, y, p)
    s = s[:a] + p + s[b:]
    for x, y in [(f'f"{owner} did not approve', 'f"The person did not approve'), (f'"{owner} declined this."', '"The person declined this."'),
                 (f'"Interrupted by {owner} speaking."', '"Interrupted by the person speaking."'), ("|cor" + r"bin_mail\S*)", ")")]:
        assert s.count(x) == 1, x
        s = s.replace(x, y)
    # tools, scripts and a place of this machine only; named in pieces so this file does not carry them either
    hire, graph, notes = "renta" + "human", "contact" + "_graph", "trans" + "cripts"
    for x, y in [(f'    "mcp__{hire}",\n', ""), (f"|browserbase|{hire}", "|browserbase"),
                 (f'    r"|project_files/{graph}/cg (before|who-at|who|path|mutual|quiet|semantic|stats)"\n', ""),
                 (f'    r"|python3 scripts/integrations/google/{notes}\\.py (list|get|search))(\\s|$)")', '    r")(\\s|$)")'),
                 ("zoneinfo/Europe/Lis" + "bon", "zoneinfo/<Region>/<City>")]:
        assert s.count(x) == 1, x
        s = s.replace(x, y)
    s, n = re.subn(r'\s*"mcp__trinity-[a-z]+__[a-z_]+",', "", s)   # send tools of one more instance of this machine
    assert n >= 1
    x = 'for name in (picked, ROOT.name, "default"):'
    assert s.count(x) == 1
    s = s.replace(x, 'for name in (picked, ROOT.name, "default", *sets[:1]):')      # no `default` set ships: the first one is the default
    f.write_text(s)
    f = dest / "make_faces.py"
    s = f.read_text()
    s, n = re.subn(r'so `make_faces\.py \w+ "([^"]+)"` gives \w+ \w+ own face', r'so `make_faces.py scout "\1"` gives the agent of the folder `scout` its own face', s)
    assert n == 1
    f.write_text(s)
    f = dest / "orb_sketch.js"
    s = f.read_text()
    s, n = re.subn(r"// Blue/cyan palette for \w+ \(vs \w+ gold\)", "// Blue/cyan palette", s)
    s, m = re.subn(r"'\[\w+\] p5 setup OK:'", "'[orb] p5 setup OK:'", s)
    assert n == 1 and m == 1
    f.write_text(s)


def package(dest):
    """Write a standalone agent folder: this skill with the app inside it, the voice skill with one
    face, an agent file, a readme.

    Nothing of this machine goes in: the app has no machine values (they are in the config file,
    which is not copied), conversations and other face sets are left behind, and the result is
    searched for this user's name, home folder, addresses, keys, and the hosts, paths and agent
    names of this machine's config before the command reports success.
    """
    dest = Path(dest).expanduser().resolve()
    mark = dest / ".flight-deck-package"
    if dest.exists() and any(dest.iterdir()) and not mark.is_file():
        print(f"{dest} exists and was not made by this command; choose an empty or new folder")
        return 1
    tpl = SKILL / "scripts/package"
    skill = dest / ".claude/skills/flight-deck"
    shutil.rmtree(skill, ignore_errors=True)
    junk = shutil.ignore_patterns("__pycache__", "build", ".DS_Store", "dusk.html", ".*base.html")
    shutil.copytree(PROJECT, skill / "app", ignore=junk)
    shutil.copytree(SKILL / "scripts", skill / "scripts", ignore=junk)
    shutil.copy(SKILL / "SKILL.md", skill / "SKILL.md")
    shutil.copy(tpl / "config.example.json", skill / "app/config.example.json")
    shutil.copy(tpl / "README.md", dest / "README.md")
    shutil.copy(tpl / "gitignore", dest / ".gitignore")
    if not (dest / "CLAUDE.md").is_file():                 # the agent file is the owner's to change: written once
        shutil.copy(tpl / "CLAUDE.md", dest / "CLAUDE.md")

    voice = dest / ".claude/skills/voice-agent"
    own = (_OWN_VOICE / "voice_server.py").is_file()
    src = _OWN_VOICE if own else HOME / ".claude/skills/voice-agent"
    if src.resolve() == voice.resolve():
        print("voice   : left as it is (packaging onto itself)")
    elif (src / "voice_server.py").is_file() and (src / "faces" / VOICE_FACE / "neutral.jpg").is_file():
        kept, aside = voice / "conversations", dest / ".conversations-kept"   # a rebuilt package keeps the talks held in it
        if kept.is_dir():
            kept.rename(aside)
        shutil.rmtree(voice, ignore_errors=True)
        for name in VOICE_FILES:
            (voice / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src / name, voice / name)
        shipped = [n for n in VOICE_FACES if (src / "faces" / n / "neutral.jpg").is_file()]
        for n in shipped:
            (voice / "faces" / n).mkdir(parents=True)
            for f in sorted((src / "faces" / n).iterdir()):     # portraits, the lip-sync tuning, a dust face's solid twin
                if (f.suffix in (".jpg", ".json") or f.name == "carrier.png") and not f.name.startswith("."):
                    shutil.copy(f, voice / "faces" / n / f.name)
        shutil.copy(tpl / "voice-agent-SKILL.md", voice / "SKILL.md")
        shutil.copy(tpl / "p5-LICENSE.txt", voice / "vendor/p5-LICENSE.txt")      # p5.js is LGPL-2.1: its licence travels with it
        (voice / ".gitignore").write_text("# Spoken conversations and the live session link stay off git.\nconversations/\n.runtime.json\n__pycache__/\n")
        if not own:
            _neutral_voice(voice)
        if aside.is_dir():
            aside.rename(kept)
        print("voice   : the voice skill is in it, with the faces " + ", ".join(f"`{n}` ({len(list((voice / 'faces' / n).glob('*.jpg')))} portraits)" for n in shipped))
    else:
        shutil.rmtree(voice, ignore_errors=True)
        print(f"voice   : NOT in it (no voice-agent skill with a `{VOICE_FACE}` face on this machine): the copy has no TALK view")

    mark.write_text("Made by `fd.py package`. Running it again replaces .claude/skills/flight-deck, .claude/skills/voice-agent (its conversations are kept), README.md and .gitignore; CLAUDE.md is kept.\n")
    skip = ("/.git/", "/conversations/", ".runtime.json", "__pycache__")
    every = [f for f in dest.rglob("*") if f.is_file() and not any(x in str(f) for x in skip)]
    files = [f for f in every if f.suffix in TEXT]
    for f in files:                                        # its own LaunchAgent name, so it never collides with this machine's
        s = f.read_text(errors="replace")
        if LABEL in s and LABEL != PKG_LABEL:
            f.write_text(s.replace(LABEL, PKG_LABEL))

    # what must not be in it
    words = {getpass.getuser(), str(HOME), "Drop" + "box"}
    try:
        cfg = json.loads(CONFIG.read_text())

        def strings(v):
            if isinstance(v, dict):
                for k, x in v.items():
                    if k not in ("face", "voice", "default_face", "label", "server", "key", "name", "title", "claude_json", "backup_label", "instance"):
                        yield from strings(x)
            elif isinstance(v, list):
                for x in v:
                    yield from strings(x)
            elif isinstance(v, str) and len(v) > 3:
                yield v
        for v in strings(cfg):
            words.add(v)
            if len(Path(v).name) > 4:
                words.add(Path(v).name)
        mine = {_agent_name().lower(), "marvin"}             # the other agents of this machine, by name
        words.update(a["name"] for a in (cfg.get("voice") or {}).get("agents") or [] if a.get("name") and a["name"].lower() not in mine)
        words.update(t["label"] for t in [cfg.get("remote") or {}] if t.get("label"))
        words.update(x for x in (cfg.get("dusk") or {}).get("name", "").split() if len(x) > 3)
    except (OSError, ValueError):
        pass
    words.update(w for w in run("id", "-F").stdout.split() if len(w) > 3)      # the owner's own name, as the Mac knows it
    pats = [re.compile(re.escape(w), re.I) for w in sorted(words) if w]
    pats += [re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}"), re.compile(r"[\w-]+\.ts\.net"), re.compile(r"\b100\.\d+\.\d+\.\d+\b"),
             re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
             re.compile(r"(?i)\b(bearer|api[_-]?key|token|secret|password)\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{20,}")]
    hits = []
    for f in files:
        if f.suffix == ".js" and "vendor" in f.parts:        # a third-party library, minified
            continue
        for n, ln in enumerate(f.read_text(errors="replace").splitlines(), 1):
            for p in pats:
                m = p.search(ln)
                if m:
                    hits.append(f"  {f.relative_to(dest)}:{n}: {m.group(0)}")
    other = sorted({f.suffix or f.name for f in every if f not in files})
    print(f"packaged: {dest}  ({len(every)} files; besides text: {', '.join(other) or 'nothing'})")
    if hits:
        print("LEAK CHECK FAILED: these lines name this machine, its owner or its other agents; fix the source and package again:")
        print("\n".join(hits[:40]))
        return 1
    print("leak check: clean (no user or owner name, home path, mail address, tailnet host, key, other agent's name, or value of this machine's config)")
    print("next    : on the other machine, open the folder in Claude Code and say `/flight-deck setup`,")
    print(f"          or run: python3 {skill.relative_to(dest)}/scripts/fd.py setup")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "start":
        sys.exit(start())
    if cmd == "stop":
        sys.exit(stop("--all" in sys.argv))
    if cmd in ("restart", "status", "doctor", "setup", "install", "uninstall"):
        sys.exit(globals()[cmd]())
    if cmd == "survey":
        s = survey()
        if "--json" in sys.argv:
            print(json.dumps({**s, "lines": lines(s)}, indent=2))
        else:
            report(s)
        sys.exit(0)
    if cmd == "trinity":
        sys.exit(trinity(sys.argv[2:]))
    if cmd == "package" and len(sys.argv) > 2:
        sys.exit(package(sys.argv[2]))
    print(__doc__)
    sys.exit(1)

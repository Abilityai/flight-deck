"""Flight Deck: the TALK mode. Starts and stops the /voice-agent server for the monitor page.

The page shows the voice agent's own page in a frame, so everything about the conversation
(mic, speech, the face, approvals) is that skill's code, unchanged. This module only:
  - starts `voice_server.py` for the chosen agent with `--embed-origin` (the one origin the
    voice page then lets frame it) and hands the page its session link,
  - reports whether a voice session is running and whose it is,
  - stops the one it started.
One voice server runs at a time on port 8766; a session started elsewhere (the HUD, a terminal)
cannot be framed and is never touched unless the page asks to take over.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from config import CFG

HOME = Path.home()
_V = CFG["voice"]
SKILL = _V["skill"]
RUNTIME = SKILL / ".runtime.json"
# One fixed character and voice per agent: the pickers are hidden in the frame. (The face is remembered
# per agent by the voice skill, so an agent's talks started elsewhere show the same one.)
# The agents come from config.py: the first is the TALK view, a second one gets its own view.
AGENTS = {a["key"]: a for a in _V["agents"]}
FIRST = _V["agents"][0]["key"]
_ORB = next((a for a in _V["agents"] if a.get("orb")), None)   # the agent with a live orb page (live_server.py), if any
ORB_PORT = _ORB["orb"]["port"] if _ORB else 0
ORB_DIR = _ORB["orb"]["dir"] if _ORB else None
ORIGIN = f"http://127.0.0.1:{CFG['port']}"
ORB_LOG = HOME / ".cache/flight-deck/orb.log"
STATE = HOME / ".cache/flight-deck/voice.json"          # the pid this module started
LOG = HOME / ".cache/flight-deck/voice.log"
PORT = int(_V["port"])
DEFAULT_FACE = _V["default_face"]
RESUME_WITHIN = 12 * 3600                               # continue the last talk if it is this recent


def _alive(pid):
    try:
        pid = int(pid)
        try:
            os.waitpid(pid, os.WNOHANG)                  # a server this feeder started and that has exited: collect it,
        except OSError:                                  # or it still answers the signal below as if alive
            pass
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _listening(port=PORT):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def status(with_url=False):
    """{running, ours, agent, key}; `url` (it carries the session token) only when asked and ours."""
    rt = _read(RUNTIME)
    running = _alive(rt.get("pid")) and _listening()
    ours = running and _read(STATE).get("pid") == rt.get("pid")
    root = Path(rt.get("root") or "?")
    out = {"running": bool(running), "ours": bool(ours), "agent": root.name if running else "",
           "key": next((k for k, a in AGENTS.items() if running and a["root"] == root), "")}
    if with_url and ours:
        out["url"] = rt.get("url")
    return out


def _data_dir(root):
    slug = "".join(c if c.isalnum() else "-" for c in str(root)).lstrip("-")
    return SKILL / "conversations" / slug


def start(agent="", force=False):
    agent = agent or FIRST
    """Start the voice server for one of AGENTS. One runs at a time: a session for the other agent,
    or one started elsewhere, is only ended when `force` is set (the page asks first)."""
    if agent not in AGENTS:
        return {"running": False, "ours": False, "agent": "", "error": f"unknown agent {agent!r}"}
    if not (SKILL / "voice_server.py").is_file():
        return {"running": False, "ours": False, "agent": "", "error": f"the voice-agent skill is not installed ({SKILL})"}
    root, face = AGENTS[agent]["root"], AGENTS[agent]["face"]
    if not root.is_dir():
        return {"running": False, "ours": False, "agent": "", "error": f"{root} is not there"}
    st = status(with_url=True)
    if st["ours"] and st["key"] == agent:
        return st
    if st["running"]:
        if not force:
            st.pop("url", None)
            return {**st, "busy": True}
        stop(any_owner=True)

    if _listening():                                     # the port is held by a server this deck knows nothing about
        return {"running": False, "ours": False, "agent": "",
                "error": f"port {PORT} is taken by another program (lsof -nP -iTCP:{PORT} -sTCP:LISTEN shows it)"}

    data = _data_dir(root)
    data.mkdir(parents=True, exist_ok=True)
    if not face and not (data / ".face_set").is_file():
        face = DEFAULT_FACE
    if face and (SKILL / "faces" / face / "neutral.jpg").is_file():
        (data / ".face_set").write_text(face)            # a fixed character: the picker is hidden in the frame

    cmd = [str(_V.get("python") or sys.executable), str(SKILL / "voice_server.py"), "--no-open", "--port", str(PORT),
           "--cwd", str(root), "--voice", AGENTS[agent]["voice"], "--lipsync", "--embed-origin", ORIGIN]
    if _V.get("yolo") is False:                          # approvals on: edits and commands wait for a spoken yes
        cmd += ["--no-yolo"]
    if CFG["desk"] and agent == CFG["desk"]["voice"]:     # the voice agent of the TRINITY view is told what is on the desk
        cmd += ["--context-file", str(Path(os.environ.get("TRINITY_DESK_STATE") or HOME / ".cache/trinity-desk") / "screen.txt")]
    last = data / ".last_session"
    try:
        # Continue only a talk that was really saved: a session ended before its first reply has no
        # transcript, and resuming it makes Claude Code fail every turn ("No conversation found").
        sid = last.read_text().strip()
        project = HOME / ".claude/projects" / "".join(c if c.isalnum() else "-" for c in str(root))
        if sid and time.time() - last.stat().st_mtime < RESUME_WITHIN and (project / f"{sid}.jsonl").is_file():
            cmd.append("--resume-last")
    except OSError:
        pass

    env = dict(os.environ)
    env["PATH"] = ":".join([f"{HOME}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                            "/usr/sbin", "/sbin", env.get("PATH", "")])
    for k, v in (_read(HOME / ".claude/settings.json").get("env") or {}).items():   # GEMINI_API_KEY lives there
        if isinstance(v, str):
            env.setdefault(k, v)
    env.pop("CLAUDECODE", None)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "ab") as log:
        p = subprocess.Popen(cmd, cwd=str(root), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)     # it outlives a feeder restart
    STATE.write_text(json.dumps({"pid": p.pid, "started": time.time()}))
    for _ in range(60):
        time.sleep(0.25)
        if p.poll() is not None:
            tail = LOG.read_text(errors="replace").strip().splitlines()[-1:] or ["no output"]
            return {"running": False, "ours": False, "agent": "", "error": tail[0][:200]}
        st = status(with_url=True)
        if st["ours"]:
            return st
    return {"running": False, "ours": False, "agent": "", "error": "the voice server did not come up in 15 s"}


def stop(any_owner=False):
    rt = _read(RUNTIME)
    pid = rt.get("pid")
    if _alive(pid) and (any_owner or _read(STATE).get("pid") == pid):
        os.kill(int(pid), signal.SIGTERM)                # the server logs session_end and exits
        for _ in range(40):
            time.sleep(0.1)
            if not _alive(pid):
                break
    return status()


def orb():
    """The live orb page of the agent that has one (driven by what its sessions are doing), started if it
    is not already up. The server is that agent's own, read-only and stdlib; it is left running."""
    import urllib.request
    if not _ORB:
        return {"ok": False, "error": "no agent with an orb is configured"}
    url = f"http://127.0.0.1:{ORB_PORT}/live.html"

    def healthy():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{ORB_PORT}/live/health", timeout=1.5) as r:
                return Path(json.loads(r.read()).get("agent_root", "")) == _ORB["root"]
        except (OSError, ValueError):
            return False

    if healthy():
        return {"ok": True, "url": url}
    if _listening(ORB_PORT):
        return {"ok": False, "error": f"port {ORB_PORT} is taken by something else"}
    if not (ORB_DIR / "live_server.py").is_file():
        return {"ok": False, "error": f"live_server.py is not in {ORB_DIR}"}
    ORB_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(ORB_LOG, "ab") as log:
        subprocess.Popen([sys.executable, str(ORB_DIR / "live_server.py"), str(ORB_PORT)], cwd=str(ORB_DIR),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(40):
        time.sleep(0.25)
        if healthy():
            return {"ok": True, "url": url}
    return {"ok": False, "error": "the orb server did not come up in 10 s"}

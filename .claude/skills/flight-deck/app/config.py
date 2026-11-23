"""Flight Deck: everything that differs from one machine to the next, in one place.

Read from ~/.config/flight-deck/config.json (or $FLIGHT_DECK_CONFIG). Every key is optional; without
the file the deck shows this Mac, the local sessions of Claude Code and of the other runtimes it can read, one voice agent (the folder this
project sits in) and the Trinity instance configured in ~/.claude.json, if there is one.

  port        the feeder's port (FLIGHT_DECK_PORT in the environment wins)
  voice       {"port", "skill", "python", "default_face", "agents": [{"key", "name", "root", "face", "voice",
               "orb": {"dir", "port"}}]}. The first agent is the TALK view; a second one, if any,
               gets its own view (beside its orb page, when it has one).
               "yolo": false starts voice with approvals on (edits and commands wait for a spoken yes).
  remote      {"label", "ssh_host"}: another machine watched over ssh, or null for none
  trinity     [{"label", "server", "claude_json"} or {"label", "url", "keychain"} or {"label", "url",
               "headers"}]: the Trinity instances. The first is the main one, shown in full; the others
               get a line each. [] for none. `fd.py trinity` lists, adds and removes them.
  dusk        {"name", "title", "face"}: a character the first voice agent appears as in the dusk skin, or null
  backup_log  a restic log to read the last backup from, or null (`backup_label` names it on the page)
  welcome     true: the page shows the first-run card (what the survey found). `setup` sets it in a new
               config, the card's button sets it back to false.
  desk        {"dir", "voice", "instance"}: a Trinity desk to show in the TRINITY view, or null for none
               (the view then shows the connected instances). `instance` is the label of the Trinity instance the
               desk belongs to: while another one is shown in full, the view shows the instances instead. `dir` is
               the code folder of the desk (an agent's trinity-desk skill, .../trinity-desk/desk); the
               feeder loads its data module from there and serves its stage and decision list.
               `voice` is the key of the voice agent of that view's voice corner.
"""
import json
import os
from pathlib import Path

HOME = Path.home()
FILE = Path(os.environ.get("FLIGHT_DECK_CONFIG") or HOME / ".config/flight-deck/config.json")


def _agent_root():
    """The agent folder this project sits in: the nearest folder above with a `.claude` in it (the
    project may be a folder of the agent, or the `app/` folder of the agent's flight-deck skill)."""
    here = Path(__file__).resolve().parent
    for d in here.parents:
        if d != HOME and (d / ".claude").is_dir():
            return d
    return here.parent


_HERE = _agent_root()


def agent_name():
    """What the agent of this folder is called: the first heading of its CLAUDE.md, else the folder's name."""
    try:
        for line in (_HERE / "CLAUDE.md").read_text().splitlines():
            if line.startswith("# ") and len(line) < 40:
                return line[2:].strip()
    except OSError:
        pass
    return _HERE.name.capitalize() if _HERE.name.islower() else _HERE.name


def _path(p):
    return Path(os.path.expanduser(str(p)))


def _trinity():
    """The Trinity instance of the Claude config, if one is connected there."""
    try:
        servers = json.loads((HOME / ".claude.json").read_text()).get("mcpServers") or {}
    except (OSError, ValueError):
        servers = {}
    return [{"label": "Trinity", "server": "trinity", "claude_json": "~/.claude.json"}] if "trinity" in servers else []


def _voice_skill():
    """The voice skill that came with this agent, else the user's own."""
    own = _HERE / ".claude/skills/voice-agent"
    return own if (own / "voice_server.py").is_file() else HOME / ".claude/skills/voice-agent"


def _defaults():
    name = agent_name()
    skill = _voice_skill()
    face = "marvin" if (skill / "faces/marvin/neutral.jpg").is_file() else "android"
    return {
        "port": 47600,
        "voice": {"port": 8766, "skill": str(skill), "default_face": face,
                  "agents": [{"key": "agent", "name": name, "root": str(_HERE), "face": face, "voice": "Charon"}]},
        "remote": None,
        "trinity": _trinity(),
        "backup_log": None,
        "backup_label": "Backup",
        "desk": None,
        "dusk": None,
        "welcome": False,
    }


def load():
    cfg = _defaults()
    try:
        user = json.loads(FILE.read_text())
    except (OSError, ValueError):
        user = {}
    for k, v in user.items():
        if k == "voice" and isinstance(v, dict):
            cfg["voice"].update(v)
        elif k in cfg:
            cfg[k] = v
    for a in cfg["voice"]["agents"]:
        a["root"] = _path(a["root"])
        a.setdefault("name", a["root"].name)
        a.setdefault("key", a["name"].lower())
        a.setdefault("face", cfg["voice"]["default_face"])
        a.setdefault("voice", "Charon")
        if a.get("orb"):
            a["orb"] = {"dir": _path(a["orb"]["dir"]), "port": int(a["orb"].get("port", 8790))}
    cfg["voice"]["skill"] = _path(cfg["voice"]["skill"])
    cfg["backup_log"] = _path(cfg["backup_log"]) if cfg["backup_log"] else None
    if cfg["desk"]:
        cfg["desk"] = {"dir": _path(cfg["desk"]["dir"]), "voice": cfg["desk"].get("voice") or cfg["voice"]["agents"][0]["key"],
                       "instance": cfg["desk"].get("instance") or None}
    cfg["port"] = int(os.environ.get("FLIGHT_DECK_PORT") or cfg["port"])
    return cfg


CFG = load()


def for_page():
    """What the page needs to know (no paths, no keys): injected into the HTML as window.__config."""
    return {"voice": [{"key": a["key"], "name": a["name"], "orb": bool(a.get("orb"))} for a in CFG["voice"]["agents"]],
            "talk": (CFG["voice"]["skill"] / "voice_server.py").is_file(),      # no voice skill: no TALK views
            "backup": {"label": str(CFG["backup_label"])} if CFG["backup_log"] else None,
            "dusk": CFG["dusk"] or None,
            "welcome": bool(CFG["welcome"]),                                     # the first-run card (setup turns it on, the page off)
            "remote": {"label": CFG["remote"].get("label", "Remote")} if CFG["remote"] else None,
            "trinity": {"label": CFG["trinity"][0].get("label", "Trinity")} if CFG["trinity"] else None,
            "desk": {"voice": CFG["desk"]["voice"], "instance": CFG["desk"]["instance"]} if CFG["desk"] else None}

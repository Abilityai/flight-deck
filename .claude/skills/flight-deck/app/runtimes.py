"""Flight Deck: the agent runtimes on this machine other than Claude Code.

Claude Code has its own readers (feeder.py for live sessions, sessions.py for transcripts and
subscriptions). People run other things beside it, or instead of it. This module finds them and,
where a runtime keeps its sessions in a form that can be read, turns them into one common shape:

  {"rt", "sid", "cwd", "project", "title", "model", "start", "end",
   "prompts": [ts], "answers": [ts], "open": a turn is running, "auto": not typed by a person}

  Codex       ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl   prompts, answers, model, folder
  OpenCode    ~/.local/share/opencode/opencode.db (SQLite)    prompts, answers, model, folder
  Gemini CLI  ~/.gemini/tmp/<project>/chats/*.json, else logs.json   prompts (answers from chats)
  others      found and named, their sessions are not read (KNOWN below says which)

Everything here only reads. Nothing is ever written into a runtime's own folder; a small index of
what was parsed is kept in ~/.cache/flight-deck/runtimes-index.json so a file is read once.
"""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path

try:
    import psutil
except ImportError:                                  # the survey runs this with any Python
    psutil = None

HOME = Path.home()
CACHE = HOME / ".cache/flight-deck"
INDEX = CACHE / "runtimes-index.json"
INDEX_VERSION = 1
HISTORY = 27 * 7 + 7                                 # days read: what the prompts-per-day chart covers
RECENT = 14                                          # days of sessions kept in full for the lists
PERSIST = True                                       # the survey turns this off: it changes nothing on disk

# key, name, command, where it keeps its data (under the home folder), what the deck reads of it
KNOWN = [
    ("claude", "Claude Code", "claude", [".claude"], "full"),
    ("codex", "Codex", "codex", [".codex"], "sessions"),
    ("opencode", "OpenCode", "opencode", [".local/share/opencode", ".config/opencode"], "sessions"),
    ("gemini", "Gemini CLI", "gemini", [".gemini"], "prompts"),
    ("cursor", "Cursor agent", "cursor-agent", [".cursor"], None),
    ("copilot", "Copilot CLI", "copilot", [".copilot"], None),
    ("goose", "Goose", "goose", [".config/goose"], None),
    ("aider", "Aider", "aider", [".aider"], None),
    ("ollama", "Ollama", "ollama", [".ollama"], None),          # a model server, not an agent: no sessions
]
NAMES = {k: n for k, n, *_ in KNOWN}

_lock = threading.Lock()
_state = {"sessions": [], "ts": 0.0, "ready": False}
_running = False
_index = None


def _epoch(ts):
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _short(text, n=90):
    return " ".join(str(text or "").split())[:n]


def _rec(rt, sid, cwd="", title="", model="", auto=False):
    return {"rt": rt, "sid": f"{rt}:{sid}", "cwd": cwd, "project": Path(cwd).name if cwd else NAMES[rt],
            "title": title, "model": model, "start": 0.0, "end": 0.0, "prompts": [], "answers": [],
            "open": False, "auto": auto}


def _close(r):
    """Fill start / end / open from the events of a record."""
    ts = r["prompts"] + r["answers"]
    r["start"] = r["start"] or (min(ts) if ts else 0.0)
    r["end"] = max([r["end"], *ts])
    r["open"] = bool(r["prompts"]) and (not r["answers"] or r["prompts"][-1] > r["answers"][-1])
    return r


# ---------------------------------------------------------------- Codex

def _codex_file(path):
    """One rollout file. Lines are large (the first holds the whole system prompt): only the few kinds
    that matter are parsed."""
    r = _rec("codex", path.stem[-36:])
    first, agent, waiting = "", 0.0, False
    with open(path, "rb") as fh:
        for line in fh:
            head = line[:160]
            if b'"event_msg"' in head:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                p, ts = e.get("payload") or {}, _epoch(e.get("timestamp"))
                kind = p.get("type")
                if not ts:
                    continue
                r["end"] = max(r["end"], ts)
                if kind == "user_message":
                    if waiting and agent:                 # an older Codex: no task_complete, the last message ends the turn
                        r["answers"].append(agent)
                    r["prompts"].append(ts)
                    first = first or _short(p.get("message"))
                    waiting, agent = True, 0.0
                elif kind in ("task_complete", "turn_aborted") and waiting:
                    r["answers"].append(ts)
                    waiting = False
                elif kind == "agent_message":
                    agent = ts
            elif b'"session_meta"' in head:
                try:
                    p = json.loads(line).get("payload") or {}
                except ValueError:
                    continue
                r["sid"] = "codex:" + str(p.get("id") or p.get("session_id") or path.stem[-36:])
                r["cwd"] = str(p.get("cwd") or "")
                r["project"] = Path(r["cwd"]).name if r["cwd"] else "Codex"
                r["start"] = _epoch(p.get("timestamp"))
                r["auto"] = p.get("source") == "exec" or "exec" in str(p.get("originator") or "") \
                    or str(p.get("thread_source") or "user") != "user"
            elif b'"turn_context"' in head and not r["model"]:
                try:
                    r["model"] = str((json.loads(line).get("payload") or {}).get("model") or "")
                except ValueError:
                    pass
    if waiting and agent and time.time() - agent > 120:
        r["answers"].append(agent)
    r["title"] = first
    return _close(r)


def _codex(since, index):
    root = HOME / ".codex/sessions"
    if not root.is_dir():
        return []
    names = {}
    try:
        for line in (HOME / ".codex/session_index.jsonl").read_text(errors="replace").splitlines():
            e = json.loads(line)
            names[str(e.get("id"))] = _short(e.get("thread_name"))
    except (OSError, ValueError):
        pass
    out = []
    for f in root.glob("*/*/*/*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime < since:
            continue
        key, was = str(f), index.get(str(f))
        if not was or was[0] != st.st_mtime or was[1] != st.st_size:
            try:
                was = index[key] = [st.st_mtime, st.st_size, _codex_file(f)]
            except OSError:
                continue
        r = dict(was[2])
        r["title"] = names.get(r["sid"][6:]) or r["title"]
        out.append(r)
    return out


# ---------------------------------------------------------------- OpenCode

def _opencode_db():
    base = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share")
    return base / "opencode/opencode.db"


def _opencode(since):
    db = _opencode_db()
    if not db.is_file():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.5)
    try:
        cols = {c[1] for c in con.execute("pragma table_info(session)")}
        if not {"id", "directory", "time_created", "time_updated"} <= cols:
            return []
        pick = [c if c in cols else "null" for c in ("id", "directory", "title", "parent_id", "time_created", "time_updated", "model")]
        recs = {}
        for sid, cwd, title, parent, t0, t1, model in con.execute(
                f"select {', '.join(pick)} from session where time_updated >= ?", (int(since * 1000),)):
            if parent:
                continue                                  # a subagent's session
            try:
                model = (json.loads(model) or {}).get("id") if model and str(model)[:1] == "{" else model
            except ValueError:
                model = ""
            r = recs[sid] = _rec("opencode", sid, str(cwd or ""), _short(title), str(model or ""))
            r["start"], r["end"] = t0 / 1000, t1 / 1000
        done = {}                                         # session -> when its last assistant message finished
        for sid, t, data in con.execute("select session_id, time_created, data from message where time_created >= ? order by time_created",
                                        (int(since * 1000),)):
            r = recs.get(sid)
            if not r:
                continue
            try:
                d = json.loads(data)
            except ValueError:
                continue
            if d.get("role") == "user":
                if done.get(sid):
                    r["answers"].append(done.pop(sid))
                r["prompts"].append(t / 1000)
            elif d.get("role") == "assistant":
                end = (d.get("time") or {}).get("completed")
                if end:
                    done[sid] = end / 1000
                else:
                    done.pop(sid, None)
                r["model"] = r["model"] or str(d.get("modelID") or "")
        for sid, ts in done.items():
            if recs[sid]["prompts"] and ts > recs[sid]["prompts"][-1] and time.time() - ts > 20:
                recs[sid]["answers"].append(ts)
        return [_close(r) for r in recs.values() if r["prompts"]]
    finally:
        con.close()


# ---------------------------------------------------------------- Gemini CLI

def _gemini(since, folders):
    root = HOME / ".gemini/tmp"
    if not root.is_dir():
        return []
    by_hash = {hashlib.sha256(str(p).encode()).hexdigest(): str(p) for p in folders}
    recs = {}
    for d in root.iterdir():
        if not d.is_dir():
            continue
        cwd = by_hash.get(d.name, "")
        name = Path(cwd).name if cwd else d.name if not re.fullmatch(r"[0-9a-f]{64}", d.name) else "Gemini CLI"
        chats = [f for f in d.glob("chats/*.json") if f.stat().st_mtime >= since]
        for f in chats:                                   # newer versions: one file per session, both sides
            try:
                c = json.loads(f.read_text(errors="replace"))
            except (OSError, ValueError):
                continue
            r = recs.setdefault(str(c.get("sessionId") or f.stem), _rec("gemini", c.get("sessionId") or f.stem, cwd))
            r["project"] = name
            for m in c.get("messages") or []:
                ts = _epoch(m.get("timestamp"))
                if ts and m.get("type") == "user":
                    r["prompts"].append(ts)
                    r["title"] = r["title"] or _short(m.get("content") if isinstance(m.get("content"), str) else "")
                elif ts and m.get("type") == "gemini":
                    if r["answers"] and r["prompts"] and r["answers"][-1] > r["prompts"][-1]:
                        r["answers"][-1] = ts
                    else:
                        r["answers"].append(ts)
        log = d / "logs.json"
        if chats or not log.is_file() or log.stat().st_mtime < since:
            continue
        try:
            entries = json.loads(log.read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        for e in entries if isinstance(entries, list) else []:
            ts = _epoch(e.get("timestamp"))
            if ts and e.get("type") == "user":
                r = recs.setdefault(str(e.get("sessionId")), _rec("gemini", e.get("sessionId"), cwd))
                r["project"] = name
                r["prompts"].append(ts)
                r["title"] = r["title"] or _short(e.get("message"))
    out = []
    for r in recs.values():
        r["prompts"].sort()
        only_prompts = not r["answers"]
        _close(r)
        if only_prompts:
            r["open"] = False                             # the log has no answers: never shown as working
        if r["end"] >= since:
            out.append(r)
    return out


# ---------------------------------------------------------------- all of them

def _folders():
    """Folders agents have worked in, as far as this machine says: used to name Gemini's hashed project folders."""
    out = set()
    for f in [HOME / ".claude.json", *HOME.glob(".claude-*/.claude.json")]:
        try:
            out.update(json.loads(f.read_text()).get("projects") or {})
        except (OSError, ValueError):
            pass
    return out


def _build():
    global _index
    if _index is None:
        try:
            d = json.loads(INDEX.read_text())
            _index = d["files"] if d.get("version") == INDEX_VERSION else {}
        except (OSError, ValueError, KeyError):
            _index = {}
    since = time.time() - HISTORY * 86400
    before = json.dumps(sorted((k, v[0], v[1]) for k, v in _index.items()))
    out = []
    for name, read in (("codex", lambda: _codex(since, _index)), ("opencode", lambda: _opencode(since))):
        try:
            out += read()
        except Exception:  # noqa: BLE001  (a runtime changed its format: the others still show)
            pass
    try:
        out += _gemini(since, _folders() | {r["cwd"] for r in out if r["cwd"]})
    except Exception:  # noqa: BLE001
        pass
    for k in [k for k in _index if not os.path.exists(k)]:
        del _index[k]
    if PERSIST and json.dumps(sorted((k, v[0], v[1]) for k, v in _index.items())) != before:
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            tmp = INDEX.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": INDEX_VERSION, "files": _index}))
            tmp.replace(INDEX)
        except OSError:
            pass
    out.sort(key=lambda r: r["start"])
    return out


def sessions(ttl=20, wait=False):
    """Every readable session of the other runtimes. Never blocks (unless wait): a stale list starts a refresh."""
    global _running

    def work():
        global _state, _running
        try:
            _state = {"sessions": _build(), "ts": time.time(), "ready": True}
        except Exception as e:  # noqa: BLE001
            _state = {**_state, "ts": time.time(), "error": repr(e)}
        finally:
            _running = False

    with _lock:
        start = not _running and time.time() - _state["ts"] > ttl
        if start:
            _running = True
    if start:
        if wait:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()
    return _state["sessions"]


def events(since):
    """[(ts, kind, project, session)] of what people typed and what came back, for attention and the feed."""
    out = []
    for r in sessions():
        if r["auto"] or r["end"] < since:
            continue
        out += [(t, "prompt", r["project"], r["sid"]) for t in r["prompts"] if t >= since]
        out += [(t, "answer", r["project"], r["sid"]) for t in r["answers"] if t >= since]
    return out


def prompts():
    """[(ts, project)] of every typed prompt in the history window, for the prompts-per-day charts."""
    return [(t, r["project"]) for r in sessions() if not r["auto"] for t in r["prompts"]]


def _procs():
    """Runtime -> the folders its running processes work in ('' where the folder cannot be read)."""
    out = {}
    if not psutil:
        return out
    for p in psutil.process_iter(["name", "cmdline"]):
        name = (p.info.get("name") or "").lower()
        heads = {Path(c).name.lower() for c in (p.info.get("cmdline") or [])[:2]}
        for rt in ("codex", "opencode", "gemini"):
            if name == rt or name.startswith(rt + "-") or rt in heads:
                try:
                    cwd = p.cwd()
                except (psutil.Error, OSError):
                    cwd = ""
                out.setdefault(rt, set()).add(cwd)
    return out


def live(private=False):
    """Sessions that are open in a running process, in the shape of the feeder's agent rows."""
    procs, now, out, seen = _procs(), time.time(), [], set()
    for r in sorted(sessions(), key=lambda r: -r["end"]):
        cwds = procs.get(r["rt"])
        if not cwds or r["auto"] or (r["rt"], r["cwd"]) in seen:
            continue
        if not ((r["cwd"] and r["cwd"] in cwds and now - r["end"] < 12 * 3600) or ("" in cwds and now - r["end"] < 900)):
            continue
        seen.add((r["rt"], r["cwd"]))                      # the latest session of a folder is the one that is open
        busy = r["open"] and now - r["end"] < 1800
        out.append({"id": r["sid"].split(":")[1][:8], "sid": r["sid"], "project": r["project"], "name": "", "busy": busy,
                    "since": (r["prompts"][-1] if busy else r["end"]), "started": r["start"],
                    "title": "" if private else r["title"], "status": "", "model": r["model"], "rt": r["rt"]})
    return out


def recent(private=False):
    """The sessions of the last RECENT days that a person drove, in the shape of the SESSIONS view's list."""
    since, on = time.time() - RECENT * 86400, {a["sid"] for a in live()}
    return [{"sid": r["sid"], "start": r["start"], "acct": "", "rt": r["rt"], "project": r["project"], "title": "" if private else r["title"],
             "theme": "", "out": 0, "live": r["sid"] in on, "switched": False, "end": r["end"]}
            for r in sessions() if not r["auto"] and r["end"] >= since]


def detect():
    """Which runtimes this machine has. Reads folder names and modification times only."""
    out = []
    for key, name, cmd, homes, read in KNOWN:
        cli = shutil.which(cmd) or next((str(p) for p in (HOME / ".local/bin" / cmd, Path("/opt/homebrew/bin") / cmd) if p.is_file()), None)
        dirs = [HOME / h for h in homes if (HOME / h).exists()]
        if not cli and not dirs:
            continue
        out.append({"key": key, "name": name, "cli": bool(cli), "data": bool(dirs), "read": read})
    return out


def summary():
    """detect() with numbers for the runtimes whose sessions are read: sessions and prompts of 7 days, last use."""
    now = time.time()
    week, today = now - 7 * 86400, dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
    rows, on = detect(), {}
    for a in live():
        on[a["rt"]] = on.get(a["rt"], 0) + 1
    for row in rows:
        mine = [r for r in sessions() if r["rt"] == row["key"]]
        if row["read"] in ("sessions", "prompts"):
            typed = [r for r in mine if not r["auto"]]
            row.update({"sessions": sum(1 for r in typed if r["end"] >= week),
                        "prompts": sum(1 for r in typed for t in r["prompts"] if t >= week),
                        "today": sum(1 for r in typed for t in r["prompts"] if t >= today),
                        "auto": sum(1 for r in mine if r["auto"] and r["end"] >= week),
                        "last": max((r["end"] for r in mine), default=0), "live": on.get(row["key"], 0)})
    return rows


if __name__ == "__main__":                              # python3 runtimes.py  -> what was found, no titles
    t = time.time()
    all_ = sessions(wait=True)
    for row in summary():
        print(row)
    print(f"{len(all_)} sessions read in {time.time() - t:.2f}s; live: {[(a['rt'], a['project'], a['busy']) for a in live()]}")

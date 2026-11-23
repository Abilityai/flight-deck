"""Flight Deck sessions: who used which Claude subscription, on what.

Reads Claude Code transcripts (~/.claude/projects/<project>/<session>.jsonl, shared by every
account dir) and returns, for the last DAYS days:

  accounts  per subscription: sessions, prompts, output tokens (today / 7 days / per day),
            live sessions, and the last rate-limit reading the status line saw
  recent    the latest interactive sessions: start, subscription, project, title
  graph     topics (words and word pairs from the AI titles) and projects as nodes,
            linked when the same sessions share them

Which subscription a turn ran on comes from the transcript itself: Claude Code writes a
`session-owner` record (account uuid) every time a session starts or resumes, so a session that
was continued after `/login` or under another account dir is split correctly. The account dir a
shell uses says nothing reliable (main gets re-logged-in), hence uuids, named from the
`oauthAccount` of every ~/.claude*/.claude.json seen so far (remembered in accounts.json).

Parsing is incremental: a per-file summary is kept in ~/.cache/flight-deck/sessions-index.json and
only files whose size or mtime changed are read again. Forked sessions copy their parent's
history into a new file, so messages are de-duplicated by message id across files.
"""
import bisect
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

import runtimes

HOME = Path.home()
PROJECTS = HOME / ".claude/projects"
CACHE = HOME / ".cache/flight-deck"
INDEX = CACHE / "sessions-index.json"
REGISTRY = CACHE / "accounts.json"
LIMITS = CACHE / "limits"                       # written by ~/.claude/statusline.sh
ALIASES = HOME / ".config/flight-deck/accounts.json"   # optional {"<uuid prefix>": "name"}
DAYS = 14            # history kept and charted
GRAPH_DAYS = 7       # window for the topic graph and the "7 days" numbers
INDEX_VERSION = 2
UNKNOWN = "?"

_index = None        # path -> per-file summary
_state = {"ready": False}
_running = False
_last = 0.0
_lock = threading.Lock()


# ---------------------------------------------------------------- parsing one transcript

def _epoch(ts):
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _prompt_text(d):
    """The text of a human prompt, or '' for tool results, meta and harness-made user records."""
    if d.get("isMeta") or d.get("isSidechain"):
        return ""
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(b.get("type") == "tool_result" for b in c if isinstance(b, dict)):
            return ""
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str):
        return ""
    c = c.strip()
    return "" if c.startswith("<") else c


def parse(path, sub):
    """Summarise one transcript. sub=True for a subagent file (no owner records, no prompts)."""
    owners, seg, msgs, prompts = [], [], {}, []
    cur, pending = -1, False
    title = first = cwd = entry = ""
    start = end = 0.0
    with open(path, "rb") as fh:
        for line in fh:
            if line.startswith(b'{"type":"'):
                if line.startswith(b'{"type":"session-owner"'):
                    u = json.loads(line).get("accountUuid") or UNKNOWN
                    if u not in owners:
                        owners.append(u)
                    cur, pending = owners.index(u), True    # stamped by the next dated record
                elif line.startswith(b'{"type":"ai-title"'):
                    title = json.loads(line).get("aiTitle") or title
                continue                                    # api-request blobs etc: large, unused
            is_a = b'"type":"assistant"' in line
            if not is_a and b'"type":"user"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            kind = d.get("type")
            if kind not in ("assistant", "user"):
                continue
            ts = _epoch(d.get("timestamp"))
            if not ts:
                continue
            start = start or ts
            end = max(end, ts)
            cwd = cwd or d.get("cwd") or ""
            entry = entry or d.get("entrypoint") or ""
            if pending:
                seg.append([ts, cur])
                pending = False
            if kind == "assistant":
                m = d.get("message") or {}
                out = (m.get("usage") or {}).get("output_tokens") or 0
                mid = (m.get("id") or d.get("uuid") or "")[-14:]
                if mid and out >= msgs.get(mid, (0, 0, -1))[2]:     # one id spans several lines
                    msgs[mid] = (int(ts), cur, out)
            elif not sub:
                text = _prompt_text(d)
                if text:
                    prompts.append([int(ts * 1000), cur])
                    first = first or " ".join(text.split())[:90]
    return {"sid": path.stem, "sub": sub, "parent": path.parent.parent.name if sub else "",
            "proj": path.parent.parent.parent.name if sub else path.parent.name,
            "cwd": cwd, "entry": entry, "title": title, "first": first,
            "start": start, "end": end, "owners": owners, "seg": seg,
            "msgs": [[k, *v] for k, v in msgs.items()], "prompts": prompts}


def reindex():
    """Bring the index in line with the transcripts on disk; returns True when anything changed."""
    global _index
    if _index is None:
        try:
            raw = json.loads(INDEX.read_text())
            _index = raw["files"] if raw.get("v") == INDEX_VERSION else {}
        except (OSError, ValueError, KeyError):
            _index = {}
    cutoff = time.time() - (DAYS + 1) * 86400
    seen, changed = set(), False
    for pattern, sub in (("*/*.jsonl", False), ("*/*/subagents/*.jsonl", True)):
        for p in PROJECTS.glob(pattern):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            key = str(p)
            seen.add(key)
            old = _index.get(key)
            if old and old["m"] == st.st_mtime and old["s"] == st.st_size:
                continue
            try:
                _index[key] = {"m": st.st_mtime, "s": st.st_size, **parse(p, sub)}
                changed = True
            except OSError:
                pass
    for key in set(_index) - seen:
        del _index[key]
        changed = True
    if changed:
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = INDEX.with_suffix(".tmp")
        tmp.write_text(json.dumps({"v": INDEX_VERSION, "files": _index}, separators=(",", ":")))
        tmp.replace(INDEX)
    return changed


# ---------------------------------------------------------------- accounts

def registry():
    """uuid -> name for every account ever seen logged in here, in first-seen order (stable colours)."""
    try:
        reg = json.loads(REGISTRY.read_text())
    except (OSError, ValueError):
        reg = {"order": [], "names": {}}
    before = json.dumps(reg, sort_keys=True)
    for p in [HOME / ".claude.json", *sorted(HOME.glob(".claude-*/.claude.json"))]:
        try:
            o = json.loads(p.read_text()).get("oauthAccount") or {}
        except (OSError, ValueError):
            continue
        u = o.get("accountUuid")
        if u:
            if u not in reg["order"]:
                reg["order"].append(u)
            reg["names"][u] = o.get("emailAddress") or reg["names"].get(u) or u[:8]
    if json.dumps(reg, sort_keys=True) != before:
        CACHE.mkdir(parents=True, exist_ok=True)
        REGISTRY.write_text(json.dumps(reg, indent=1))
    return reg


def limits(last_owner):
    """Newest rate-limit reading per account, from the per-session files the status line writes."""
    best, now = {}, time.time()
    if not LIMITS.is_dir():
        return best
    for f in LIMITS.glob("*.json"):
        try:
            if now - f.stat().st_mtime > 8 * 86400:
                f.unlink()
                continue
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        acct = last_owner.get(f.stem)
        if acct and d.get("ts", 0) > best.get(acct, {}).get("ts", 0):
            best[acct] = d
    for d in best.values():                     # a window that has rolled over is back at zero
        for k in ("five", "week"):
            if d.get(k + "_at") and d[k + "_at"] < now:
                d[k] = 0
    return best


# ---------------------------------------------------------------- topics

STOP = set("""a an and are as at be been both but by can did do does done for from get had has have how i if in into
is it its just let make me more my need new no not now of on one only or our out over per same should so some than
that the their them then there these this those to too two up use used using via vs was we were what when where
which while who why will with within without would you your
add added adding all also any ask asks answer answers build check checks command create created current
debug fix fixed fixes help implement investigation issue issues list open overview question questions quick
change request requests review run running session sessions set setup show slash status support test tests
testing update updated updates work working""".split())
KEEP_SHORT = {"pr", "ai", "ui", "ci", "qa", "db", "ux", "os"}
WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
PULL = re.compile(r"pull[- ]requests?", re.I)


def _stem(w):
    if w[:-1] in KEEP_SHORT and w.endswith("s"):
        return w[:-1]
    if len(w) > 4 and w.endswith("s") and not w.endswith(("ss", "us", "is", "ops")):
        return w[:-1]
    return w


def terms(title):
    """Ordered content words of a title; '' marks a break so pairs never span a dropped word."""
    out = []
    for w in WORD.findall(PULL.sub("PR", title.replace("_", " "))):
        w = _stem(w.lower())
        ok = w not in STOP and (len(w) > 2 or w in KEEP_SHORT)
        out.append(w if ok else "")
    return out


def project_name(f):
    if f["cwd"]:
        return Path(f["cwd"]).name
    return f["proj"].rsplit("-", 1)[-1]


def graph(sess, private):
    """Topic and project nodes for the given sessions, linked by the sessions they share."""
    docs = []
    for s in sess:
        ws = [] if private else terms(s["title"])
        uni = {w for w in ws if w}
        bi = {f"{a} {b}" for a, b in zip(ws, ws[1:]) if a and b}
        docs.append((s, uni, bi))
    udf = Counter(w for _, u, _ in docs for w in u)
    bdf = Counter(b for _, _, bs in docs for b in bs)
    # a pair that recurs and accounts for most uses of its words replaces them ("flight deck")
    pairs = {b for b, n in bdf.items()
             if n >= 2 and n >= .6 * min(udf[w] for w in b.split())}
    topics_of = []
    for s, uni, bi in docs:
        mine = bi & pairs
        covered = {w for b in mine for w in b.split()}
        topics_of.append((s, (uni - covered) | mine))
    df = Counter(t for _, ts in topics_of for t in ts)
    n = max(1, len(sess))
    # a word in nearly half of all titles is the background, not a topic
    top = [t for t, c in df.most_common() if c >= 2 and (c / n <= .45 or n < 8)][:18]
    projects = [p for p, _ in Counter(s["project"] for s in sess).most_common(8)]
    top = [t for t in top if t not in {p.lower() for p in projects}]   # the project node says it already

    nodes, idx = [], {}

    def node(kind, label):
        key = kind[0] + ":" + label
        if key not in idx:
            idx[key] = len(nodes)
            nodes.append({"id": key, "label": label, "kind": kind, "n": 0, "live": 0, "_a": Counter()})
        return idx[key]

    for t in top:
        node("topic", t)
    for p in projects:
        node("project", p)
    edges = Counter()
    for s, ts in topics_of:
        here = [idx["t:" + t] for t in ts if "t:" + t in idx]
        if "p:" + s["project"] in idx:
            here.append(idx["p:" + s["project"]])
        for i in here:
            nodes[i]["n"] += 1
            nodes[i]["live"] += 1 if s["live"] else 0
            nodes[i]["_a"][s["acct"]] += 1
        for a in range(len(here)):
            for b in range(a + 1, len(here)):
                edges[tuple(sorted((here[a], here[b])))] += 1
    # keep the graph readable: every node's two strongest links, plus anything seen three times
    keep, per = set(), defaultdict(list)
    for (a, b), w in edges.items():
        per[a].append((w, a, b))
        per[b].append((w, a, b))
        if w >= 3:
            keep.add((a, b))
    for lst in per.values():
        for w, a, b in sorted(lst, reverse=True)[:2]:
            keep.add((a, b))
    for nd in nodes:
        nd["acct"] = nd["_a"].most_common(1)[0][0] if nd["_a"] else UNKNOWN
        del nd["_a"]
    links = sorted(([a, b, edges[(a, b)]] for a, b in keep), key=lambda e: -e[2])[:48]
    return {"nodes": nodes, "edges": links}


# ---------------------------------------------------------------- themes

THEMES = CACHE / "themes.json"
THEME_EVERY = 1800       # regroup at most every 30 min, and only when new sessions appeared
CLAUDE = shutil.which("claude") or str(HOME / ".local/bin/claude")
_theme_try = 0.0

PROMPT = """Below are my Claude Code sessions from the last {days} days, one per line:
<number> | <project folder> | <session title>

Group them into 8 to 14 themes that say what the work was ABOUT: a workstream or goal, the way I
would describe my week to a colleague ("SOC 2 audit evidence", "PR merge train", "Docs learning
portal"). Rules:
- theme names are 2-4 words, specific, in Title Case; never a bare project name, never generic
  words like "Development", "Misc tasks", "Agent work"
- every session goes into exactly one theme; a one-off goes into the closest theme
- then list up to 10 pairs of themes that are genuinely related (one feeds the other, same goal,
  same system), each with a reason of at most 4 words
{previous}
Reply with JSON only, no prose, no code fence:
{{"themes":[{{"name":"...","sessions":[1,2]}}],"links":[["Theme A","Theme B","reason"]]}}

{lines}
"""


def themes(sess):
    """sid -> theme and theme links, from a small-model grouping of the session titles.

    Cached in themes.json; refreshed in the caller's background thread when sessions it has not
    seen exist and the last grouping is older than THEME_EVERY. Returns the cache (maybe None).
    """
    global _theme_try
    try:
        cache = json.loads(THEMES.read_text())
    except (OSError, ValueError):
        cache = None
    if not any(s.get("rt", "claude") == "claude" for s in sess):
        return cache                               # nobody uses Claude Code here: it is not started for the theme names either
    titled = [s for s in sess if s["title"]]
    known = set(cache["sid"]) if cache else set()
    fresh = [s for s in titled if s["sid"] not in known]
    now = time.time()
    if not titled or not fresh or now - _theme_try < 600 or (cache and now - cache["ts"] < THEME_EVERY):
        return cache
    _theme_try = now
    prev = sorted(set(cache["sid"].values())) if cache else []
    prompt = PROMPT.format(
        days=GRAPH_DAYS,
        previous=("- keep these existing theme names where they still fit: " + "; ".join(prev) + "\n") if prev else "",
        lines="\n".join(f"{i + 1} | {s['project']} | {s['title'][:90]}" for i, s in enumerate(titled)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE_")}
    env["CC_DASH_NESTED"] = "1"
    try:
        r = subprocess.run(
            [CLAUDE, "-p", "--model", "haiku", "--output-format", "text", "--no-session-persistence",
             "--tools", "", "--strict-mcp-config", "--settings", '{"disableAllHooks": true}'],
            input=prompt, capture_output=True, text=True, timeout=150, env=env, cwd=str(HOME))
        raw = r.stdout[r.stdout.index("{"):r.stdout.rindex("}") + 1]
        data = json.loads(raw)
        sid = {}
        for t in data["themes"]:
            for n in t["sessions"]:
                if isinstance(n, int) and 0 < n <= len(titled):
                    sid[titled[n - 1]["sid"]] = str(t["name"])[:40]
        names = set(sid.values())
        links = [[a, b, str(w)[:32]] for a, b, w in (l[:3] for l in data.get("links", []) if len(l) >= 3)
                 if a in names and b in names and a != b][:12]
        if len(names) < 3:
            raise ValueError("too few themes")
    except Exception as e:
        (CACHE / "themes.err").write_text(f"{time.ctime()} {e!r}\n")
        return cache
    cache = {"ts": now, "sid": sid, "links": links}
    THEMES.write_text(json.dumps(cache, indent=1))
    return cache


def theme_graph(sess, cache):
    """Themes (what the work was about) and projects (where it happened) as one graph."""
    nodes, idx = [], {}

    def node(kind, label):
        key = kind[0] + ":" + label
        if key not in idx:
            idx[key] = len(nodes)
            nodes.append({"id": key, "label": label, "kind": kind, "n": 0, "live": 0, "_a": Counter()})
        return idx[key]

    by_theme = Counter(cache["sid"].get(s["sid"]) for s in sess)
    by_theme.pop(None, None)
    for t, _ in by_theme.most_common(14):
        node("topic", t)
    for p, _ in Counter(s["project"] for s in sess).most_common(8):
        node("project", p)
    edges = Counter()
    for s in sess:
        here = [idx[k] for k in ("t:" + str(cache["sid"].get(s["sid"])), "p:" + s["project"]) if k in idx]
        for i in here:
            nodes[i]["n"] += 1
            nodes[i]["live"] += 1 if s["live"] else 0
            nodes[i]["_a"][s["acct"]] += 1
        if len(here) == 2:
            edges[(here[0], here[1])] += 1
    keep, per = set(), defaultdict(list)
    for (a, b), w in edges.items():
        per[a].append((w, a, b))
        per[b].append((w, a, b))
        if w >= 3:
            keep.add((a, b))
    for lst in per.values():                       # every theme stays tied to its main projects
        for w, a, b in sorted(lst, reverse=True)[:2]:
            keep.add((a, b))
    links = sorted(([a, b, edges[(a, b)], ""] for a, b in keep), key=lambda e: -e[2])[:40]
    for a, b, why in cache["links"]:
        if "t:" + a in idx and "t:" + b in idx:
            links.append([idx["t:" + a], idx["t:" + b], 2, why])
    for nd in nodes:
        nd["acct"] = nd["_a"].most_common(1)[0][0] if nd["_a"] else UNKNOWN
        del nd["_a"]
    return {"nodes": nodes, "edges": links, "themed": True}


# ---------------------------------------------------------------- the snapshot

def build(live_sids, private):
    files = sorted(_index.values(), key=lambda f: (f["start"], f["sub"], f["entry"] != "cli"))
    tops = {f["sid"]: f for f in files if not f["sub"]}
    now = time.time()
    today = dt.date.today()
    day0 = today - dt.timedelta(days=DAYS - 1)
    week0 = today - dt.timedelta(days=GRAPH_DAYS - 1)

    def owner_at(f, i, ts):
        """Account uuid for owner slot i of file f; subagents borrow their parent's timeline."""
        if f["sub"]:
            p = tops.get(f["parent"])
            if not p or not p["seg"]:
                return UNKNOWN
            k = bisect.bisect_right([s[0] for s in p["seg"]], ts) - 1
            return p["owners"][p["seg"][max(0, k)][1]]
        return f["owners"][i] if i >= 0 else UNKNOWN

    stat = defaultdict(lambda: {"out": [0] * DAYS, "prompts": [0] * DAYS,
                                "sids": [set() for _ in range(DAYS)]})
    seen_msg, seen_prompt = set(), set()
    per_session = defaultdict(Counter)             # sid -> account -> output tokens
    last_owner = {}

    def day_of(ts):
        return (dt.date.fromtimestamp(ts) - day0).days

    for f in files:
        sid = f["parent"] if f["sub"] else f["sid"]
        interactive = not f["sub"] and f["entry"] == "cli"
        for mid, ts, i, out in f["msgs"]:
            if mid in seen_msg:
                continue
            seen_msg.add(mid)
            acct, d = owner_at(f, i, ts), day_of(ts)
            per_session[sid][acct] += out
            if 0 <= d < DAYS:
                stat[acct]["out"][d] += out
        for ms, i in f["prompts"]:
            if ms in seen_prompt:
                continue
            seen_prompt.add(ms)
            acct, d = owner_at(f, i, ms / 1000), day_of(ms / 1000)
            if interactive and 0 <= d < DAYS:
                stat[acct]["prompts"][d] += 1
                stat[acct]["sids"][d].add(sid)
        if not f["sub"] and f["owners"]:
            last_owner[f["sid"]] = f["owners"][f["seg"][-1][1]] if f["seg"] else f["owners"][-1]

    sess = []
    for f in tops.values():
        if f["entry"] != "cli" or not f["prompts"]:
            continue                               # headless runs and forks are not "sessions"
        mix = per_session.get(f["sid"])
        acct = mix.most_common(1)[0][0] if mix else last_owner.get(f["sid"], UNKNOWN)
        sess.append({"sid": f["sid"], "start": f["start"], "end": f["end"], "acct": acct,
                     "project": project_name(f), "title": "" if private else (f["title"] or f["first"]),
                     "out": sum(mix.values()) if mix else 0, "live": f["sid"] in live_sids,
                     "switched": len(mix or ()) > 1})
    sess += runtimes.recent(private)               # what was driven in Codex, OpenCode, Gemini CLI: no subscription, no tokens
    sess.sort(key=lambda s: s["start"])

    reg = registry()
    try:
        alias = json.loads(ALIASES.read_text())
    except (OSError, ValueError):
        alias = {}

    def name(u):
        for k, v in alias.items():
            if u.startswith(k):
                return v
        if u == UNKNOWN:
            return "unattributed"
        return reg["names"].get(u) or "account " + u[:4]

    lim = limits(last_owner)
    live_by = Counter(last_owner[s] for s in live_sids if s in last_owner)
    w = GRAPH_DAYS
    accounts = []
    for u in set(stat) | set(lim) | set(live_by):
        s = stat[u]
        row = {"id": u[:8], "name": name(u),
               "ci": reg["order"].index(u) if u in reg["order"] else -1,
               "live": live_by.get(u, 0), "limits": lim.get(u),
               "today": {"sessions": len(s["sids"][-1]), "prompts": s["prompts"][-1], "out": s["out"][-1]},
               "week": {"sessions": len(set().union(*s["sids"][-w:])), "prompts": sum(s["prompts"][-w:]),
                        "out": sum(s["out"][-w:])},
               "days": s["out"]}
        if u == UNKNOWN and not row["week"]["out"] and not row["live"]:
            continue
        accounts.append(row)
    accounts.sort(key=lambda a: (a["id"] == UNKNOWN, -a["week"]["out"]))
    extra = len(reg["order"])                      # colours for accounts not in the registry
    for a in accounts:
        if a["ci"] < 0 and a["id"] != UNKNOWN:
            a["ci"], extra = extra, extra + 1

    short = {u: u[:8] for u in stat}
    week_start = dt.datetime.combine(week0, dt.time()).timestamp()
    recent = [s for s in sess if s["end"] >= week_start]
    th = None if private else themes(recent)
    g = theme_graph(recent, th) if th else graph(recent, private)   # word graph until themes exist
    for nd in g["nodes"]:
        nd["acct"] = short.get(nd["acct"], nd["acct"][:8])
    return {
        "ready": True, "ts": now, "day0": day0.isoformat(), "days": DAYS, "window": GRAPH_DAYS,
        "accounts": accounts,
        "recent": [{"start": s["start"], "acct": s["acct"][:8], "rt": s.get("rt", "claude"), "project": s["project"], "title": s["title"],
                    "theme": (th or {}).get("sid", {}).get(s["sid"], "") if th else "",
                    "out": s["out"], "live": s["live"], "switched": s["switched"]}
                   for s in sess[-16:][::-1]],
        "sessions_week": len(recent),

        "graph": g,
    }


def snapshot(live_sids, private=False, ttl=60):
    """Latest sessions state. Never blocks: a stale state triggers a background refresh."""
    global _running, _last

    def work():
        global _state, _running, _last
        try:
            reindex()
            _state = build(set(live_sids), private)
        except Exception as e:                     # keep serving the last good state
            _state = {**_state, "error": repr(e)}
        finally:
            _last = time.time()
            _running = False

    with _lock:
        if not _running and time.time() - _last > ttl:
            _running = True
            threading.Thread(target=work, daemon=True).start()
    return _state


if __name__ == "__main__":                         # python3 sessions.py  -> print the state
    import sys
    t = time.time()
    reindex()
    out = build(set(), "--private" in sys.argv)
    print(json.dumps(out, indent=1))
    print(f"{time.time() - t:.2f}s, {len(_index)} files", file=sys.stderr)

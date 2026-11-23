"""Flight Deck: attention analytics.

Where did the attention go? Read from two kinds of event per session: a `prompt` when the person
sends something and an `answer` when the agent finishes. Three sources, nothing new is recorded:

  cc-dash      a hook layer, where it is installed: ~/.cache/cc-dash/<session>/log.jsonl
  transcripts  Claude Code's own, on a machine without cc-dash (the index sessions.py keeps): a
               prompt is a typed prompt, its answer the last assistant message before the next one
  runtimes     Codex, OpenCode, Gemini CLI sessions (runtimes.py)

From those two events per session:
  focus block   consecutive prompts to the same project (a block ends when he prompts another
                project, or after BREAK minutes of silence)
  switch        a prompt that goes to a different project than the previous one
  at the keys   time covered by prompts less than BREAK minutes apart
  reply time    how long an agent that had answered waited for his next prompt
  working       prompt -> answer spans, per session (so several can overlap)
"""
import bisect
import datetime as dt
import json
import statistics
import time
from pathlib import Path

import runtimes
import sessions

CC_DASH = Path.home() / ".cache/cc-dash"
DAYS = 14
BREAK = 12 * 60          # silence longer than this ends a block / counts as away
DEEP = 10 * 60           # a block this long counts as focused
TAIL = 90                # seconds credited after the last prompt of a block
CAP = 3 * 3600           # ignore work / wait spans longer than this (session left overnight)


def has_cc_dash():
    return next(CC_DASH.glob("*/log.jsonl"), None) is not None


def _claude(since):
    """Prompt and answer events from Claude Code's transcripts, for a machine without cc-dash."""
    out, now = [], time.time()
    for f in list((sessions._index or {}).values()):
        if f["sub"] or f["entry"] != "cli" or f["end"] < since:
            continue                                          # subagents and headless runs: nobody typed
        project = Path(f["cwd"] or "?").name
        ps, ms = sorted(p[0] / 1000 for p in f["prompts"]), sorted(m[1] for m in f["msgs"])
        for i, p in enumerate(ps):
            last = i + 1 == len(ps)
            if p >= since:
                out.append((p, "prompt", project, f["sid"]))
            k = (len(ms) if last else bisect.bisect_left(ms, ps[i + 1])) - 1
            if k >= 0 and ms[k] > p and ms[k] >= since and (not last or now - ms[k] > 120):
                out.append((ms[k], "answer", project, f["sid"]))
    return out


def extra(since):
    """The events that do not come from cc-dash, unsorted (the deck's feed adds them to cc-dash's)."""
    return ([] if has_cc_dash() else _claude(since)) + runtimes.events(since)


def _events(since):
    """[(ts, kind, project, session)] for all sessions touched since `since`, oldest first."""
    out = extra(since)
    for log in CC_DASH.glob("*/log.jsonl"):
        try:
            if log.stat().st_mtime < since:
                continue
            try:
                hook = json.loads((log.parent / "hook.json").read_text())
            except (OSError, ValueError):
                hook = {}
            project = Path(hook.get("cwd") or "?").name
            sid, prev = log.parent.name, None
            for line in log.read_text(errors="replace").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                ts, kind, text = e.get("ts", 0), e.get("kind"), str(e.get("text", ""))[:80]
                if kind not in ("prompt", "answer") or ts < since:
                    continue
                if kind == "prompt":
                    if text.startswith("<task-notification"):
                        continue                              # the harness talking, not him
                    if prev and prev[0] == text and ts - prev[1] < 20:
                        continue                              # the hook fired twice
                    prev = (text, ts)
                out.append((ts, kind, project, sid))
        except OSError:
            continue
    out.sort()
    return out


def _spans(events):
    """Per session: work spans (prompt -> answer) and wait spans (answer -> next prompt)."""
    work, wait, last = [], [], {}
    for ts, kind, project, sid in events:
        p = last.get(sid)
        if kind == "answer" and p and p[1] == "prompt" and ts - p[0] < CAP:
            work.append((p[0], ts, project, sid))
        elif kind == "prompt" and p and p[1] == "answer" and ts - p[0] < CAP:
            wait.append((p[0], ts, project, sid))
        if kind == "prompt" and p and p[1] == "prompt":
            pass                                              # follow-up while working: keep the first start
        else:
            last[sid] = (ts, kind)
    return work, wait, last


def _blocks(prompts):
    """Focus blocks from the time-ordered prompts: [start, end, project, prompts]."""
    blocks = []
    for ts, project in prompts:
        b = blocks[-1] if blocks else None
        if b and b[2] == project and ts - b[1] <= BREAK:
            b[1], b[3] = ts, b[3] + 1
        else:
            blocks.append([ts, ts, project, 1])
    return blocks


def _present(prompts):
    """Seconds at the keys: gaps between prompts shorter than BREAK, plus a tail per stretch."""
    total, prev = 0.0, None
    for ts, _ in prompts:
        total += (ts - prev) if prev is not None and ts - prev <= BREAK else TAIL
        prev = ts
    return total


def _day(prompts, work, wait):
    blocks = _blocks(prompts)
    present = _present(prompts)
    deep = sum(b[1] - b[0] + TAIL for b in blocks if b[1] - b[0] >= DEEP)
    switches = sum(1 for a, b in zip(prompts, prompts[1:]) if a[1] != b[1] and b[0] - a[0] <= BREAK)
    longest = max(blocks, key=lambda b: b[1] - b[0], default=None)
    waits = [b - a for a, b, _, _ in wait]
    worked = sum(b - a for a, b, _, _ in work)
    return {
        "prompts": len(prompts),
        "present": present,
        "deep": min(deep, present),
        "switches": switches,
        "per_hour": switches / (present / 3600) if present >= 900 else None,
        "blocks": len(blocks),
        "longest": {"s": longest[1] - longest[0] + TAIL, "project": longest[2]} if longest else None,
        "reply": statistics.median(waits) if waits else None,
        "worked": worked,
        "parallel": worked / present if present >= 900 else None,
    }


def build(live_sids=()):
    now = time.time()
    today0 = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
    start = today0 - (DAYS - 1) * 86400
    ev = _events(start - 86400)
    work, wait, last = _spans(ev)

    days = []
    for i in range(DAYS):
        a, b = start + i * 86400, start + (i + 1) * 86400
        d = _day([(t, p) for t, k, p, _ in ev if k == "prompt" and a <= t < b],
                 [w for w in work if a <= w[1] < b], [w for w in wait if a <= w[1] < b])
        d["date"] = dt.date.fromtimestamp(a).isoformat()
        days.append(d)
    today = days[-1]
    past = [d for d in days[:-1] if d["prompts"] >= 10]

    def avg(key):
        vals = [d[key] for d in past if d[key] is not None]
        return sum(vals) / len(vals) if vals else None

    tp = [(t, p) for t, k, p, _ in ev if k == "prompt" and t >= today0]
    week0 = today0 - 6 * 86400
    share_day, share_week = {}, {}
    for t, k, p, _ in ev:
        if k != "prompt":
            continue
        if t >= today0:
            share_day[p] = share_day.get(p, 0) + 1
        if t >= week0:
            share_week[p] = share_week.get(p, 0) + 1
    hours = [0] * 24
    for a, b in zip(tp, tp[1:]):
        if a[1] != b[1] and b[0] - a[0] <= BREAK:
            hours[dt.datetime.fromtimestamp(b[0]).hour] += 1

    # the timeline: today, from the first prompt (at most 14 h back) to now
    t0 = max(tp[0][0] - 300, now - 14 * 3600) if tp else now - 3600
    lanes = sorted(share_day, key=lambda p: -share_day[p])[:8]
    live = set(live_sids)
    waiting = [{"project": next((p for t, k, p, s in reversed(ev) if s == sid), "?"), "since": ts}
               for sid, (ts, kind) in last.items() if kind == "answer" and sid in live]
    waiting.sort(key=lambda w: w["since"])
    open_work = [{"project": next((p for t, k, p, s in reversed(ev) if s == sid), "?"), "since": ts}
                 for sid, (ts, kind) in last.items() if kind == "prompt" and sid in live]
    return {
        "ready": True, "ts": now, "t0": t0,
        "today": today,
        "usual": {k: avg(k) for k in ("per_hour", "reply", "parallel", "present", "deep", "switches")},
        "days": [{"date": d["date"], "present": d["present"], "deep": d["deep"], "switches": d["switches"],
                  "per_hour": d["per_hour"]} for d in days],
        "lanes": lanes,
        "blocks": [{"a": b[0], "b": b[1] + TAIL, "project": b[2], "n": b[3]} for b in _blocks(tp)],
        "work": [{"a": max(a, t0), "b": b, "project": p} for a, b, p, _ in work if b >= t0 and p in lanes]
                + [{"a": max(w["since"], t0), "b": now, "project": w["project"], "open": True}
                   for w in open_work if w["project"] in lanes],
        "wait": [{"a": max(a, t0), "b": b, "project": p} for a, b, p, _ in wait if b >= t0 and p in lanes]
                + [{"a": max(w["since"], t0), "b": now, "project": w["project"], "open": True}
                   for w in waiting if w["project"] in lanes],
        "prompts": [{"t": t, "project": p} for t, p in tp if t >= t0],
        "share": [{"project": p, "day": share_day.get(p, 0), "week": n}
                  for p, n in sorted(share_week.items(), key=lambda kv: (-share_day.get(kv[0], 0), -kv[1]))[:8]],
        "switch_hours": hours,
        "waiting": waiting[:6],
    }


if __name__ == "__main__":
    d = build()
    print(json.dumps({k: v for k, v in d.items() if k not in ("work", "wait", "prompts", "blocks")}, indent=1))
    print(len(d["work"]), len(d["wait"]), len(d["prompts"]), len(d["blocks"]))

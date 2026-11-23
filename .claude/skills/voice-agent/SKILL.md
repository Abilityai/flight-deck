---
name: voice-agent
description: Talk to a real Claude Code session by voice, with a face that shows its expression and lip-syncs. Every spoken reply comes from Claude Code with its full tools, skills and CLAUDE.md, not from a separate voice model. Use when the person wants to work by voice or says "talk to you", "voice agent", "start a conversation".
allowed-tools: Bash, Read
user-invocable: true
argument-hint: "[new|resume|stop]"
---

# Voice Agent

```
mic -> speech-to-text -> persistent `claude -p` (stream-json) -> text-to-speech -> speaker
```

No other model sits in the path. It costs real Claude usage per turn, and a reply takes a couple of seconds. One voice server runs at a time on port 8766. Flight Deck's TALK view starts and stops it by itself (key **V**); the steps below are for using it without the deck.

## What it needs

- **A Gemini API key** (speech in and out): `GEMINI_API_KEY` in the environment, or under `"env"` in `~/.claude/settings.json`. Never paste it into a conversation.
- `ffmpeg` on the PATH (only for the fallback voice).
- For the talking face, the lip-sync engine below. Without it the face shows still portraits.

## Start

1. If `lsof -nP -iTCP:8766 -sTCP:LISTEN` shows a listener, a session is already running. Say so and stop.
2. Launch in the background from the agent's own folder, then tell the person it is open and to press **Start talking**:
   ```bash
   python3 .claude/skills/voice-agent/voice_server.py [--resume <session id> | --resume-last]
   ```
   Options: `--cwd <folder>` (talk to another agent), `--voice <name>`, `--model <model>`, `--stt whisper`, `--no-open`, `--no-yolo` (start with approvals on), `--speech-budget <words>` (default 60), `--lipsync` (load the talking face at start).
   It opens the browser itself. Never print the link: it carries the session token.

## Stop (`stop`)

Read `.runtime.json` in this folder, `kill <pid>`, then summarise the log named there from its `user`, `spoken` and `permission` events and give the reported cost (`session_end`). **End session** on the page is a pause: Start again continues the same conversation. With no browser connected for 3 minutes the server exits by itself.

## The face

`faces/<set>/<expression>.jpg`: one portrait per expression of the same character, crossfaded. The model opens every reply with `<face>NAME</face>` (never spoken), choosing the expression that honestly matches its stance; while the person speaks the face is attentive, while Claude Code works it is thinking, then focused.

The set that ships is `marvin`: a gloomy robot with a large, glossy, round white head on a small stooped body, made for this agent with image generation. It is its own design: no real person, and not a copy of any film or television robot. Its `tune.json` (`{"mouth_dy": -10, "mouth_scale": 0.7, "mouth_top": 0.5}`) fits the lip-sync to it: the engine sizes a face from the eyes, which are wide-set on a round head, so the mouth is drawn smaller (`mouth_scale`), nudged onto the lips (`mouth_dy`, `mouth_dx`), and only the lower half of the face is repainted (`mouth_top`), which keeps the eyes still; `delighted` is a copy of `amused` (the generated one drifted, and he is rarely delighted). `make_faces.py <set> "character description" [--look photo|creature|3d]` generates another (about 18 images; needs the Gemini key; look at the results, and redo a refused or drifted one with `--only <name>`). The lip-sync engine needs a face with a human-shaped mouth.

**A face of dust (`stardust`).** A second set ships: a face formed by blue and violet nebula dust, for an agent that is not a robot or a person. Its portraits are stills like any other set, and `face.js` draws a live layer for the sets named in its `FX` table: stars that drift and twinkle, dust leaving the edges, faint haze around it, a slow float and waver, and edges that thin out instead of a round frame (the round light and the orb behind are off for it). The voice brightens the face itself and speeds the dust; nothing is drawn over the face while it speaks. A change of expression is a two-second morph: the face loosens into haze, changes, and gathers again. The lip-sync engine cannot find a face in dust, so the set carries `carrier.png`, a solid twin of the neutral portrait with the eyes, nose and mouth in the same places; the worker takes the face points from it for every portrait. To make another such face: generate the neutral portrait, make its twin by an image edit ("a solid, clearly lit human face in exactly the same position"), save it as `carrier.png`, and add the set's name to `FX`.

**Lip-sync.** The mouth is generated from the reply's own audio by OpenTalking / QuickTalk (`datascale-ai/opentalking`, Apache-2.0), run locally on the Mac's GPU by `avatar_worker.py`. Install (about 4 GB, nothing in this folder; needs `git` and `uv`):

```bash
git clone --depth 1 https://github.com/datascale-ai/opentalking ~/.cache/voice-agent/opentalking
cd ~/.cache/voice-agent/opentalking && UV_DEFAULT_INDEX=https://pypi.org/simple uv sync --extra models --extra quicktalk-cpu --python 3.11
.venv/bin/hf download datascale-ai/quicktalk --local-dir ~/.cache/voice-agent/models/quicktalk/checkpoints
.venv/bin/python <this folder>/avatar_worker.py --bench <this folder>/faces/marvin <a 16 kHz mono wav>
```

The last line is the check: it prints frames per second against the 20 needed and writes short clips to look at. Audio is the clock, so sync cannot drift; only the mouth region moves.

## Approvals

YOLO is on by default: tools that reach the permission prompt are approved and logged. The **YOLO** button turns it off; then reads run freely, edits and ordinary commands ask for a spoken yes or no, and anything risky (interpreters, network, deleting, pushing, credentials, anything outside the project) needs a click. Sending tools (mail, chat, Trinity messages) are removed from voice sessions entirely: nothing is ever sent in the person's name by voice.

## Files

`voice_server.py` (server, policy, speech) · `voice.html`, `voice.js`, `mic_worklet.js`, `orb_sketch.js`, `face.js` (page) · `faces/` · `make_faces.py` · `avatar_worker.py` (lip-sync worker, runs in the engine's virtualenv) · `vendor/p5.min.js` · `conversations/` (logs and resume state per agent; never committed).

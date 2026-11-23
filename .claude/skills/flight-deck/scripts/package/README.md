# Flight Deck

A local-first operator console for a Mac where coding agents work. It works with Claude Code, Codex and OpenCode, and connects to Trinity instances if you have them.

It comes as a Claude Code agent folder. The agent inside is **Marvin**: he watches the machine and the agents on it, shows it all on the Flight Deck window, and can be talked to by voice, with a face.

| View | Shows | Needs |
|---|---|---|
| DECK | CPU, memory, disk, network, the live agent sessions, a prompt heat map, usage by hour, the Trinity instances | sessions of Claude Code, Codex or OpenCode |
| SESSIONS | recent sessions and a graph of what they were about; Claude subscriptions and their limits; a row per other runtime in use | the same; the subscriptions need Claude Code |
| ATTENTION | focus blocks, switches between projects, reply times | the same (worked out from the session records) |
| TALK | a voice conversation with Marvin: every reply is Claude Code itself, spoken, on a face that shows its expression and lip-syncs | a Gemini API key; the lip-sync engine for the moving mouth |
| TRINITY | the Trinity instances you connect, with their agents, runs and queue; with a `trinity-desk` skill, its stage and the asks waiting for a decision | an instance's address and MCP key |

Also: several Trinity instances side by side (`/flight-deck trinity`), a remote Linux machine over ssh, the machines of a Tailscale network, the last backup, a light native screensaver, three looks (`night`, `day`, `dusk`; key `K`).

Marvin looks at what the machine has and the deck leaves out what has no source. Besides Claude Code it reads the sessions of **Codex** and **OpenCode** (and the prompts of Gemini CLI); it finds Cursor agent, Copilot CLI, Goose, Aider and Ollama and names them without reading them. The deck works with any one of the three; Marvin himself is a Claude Code agent, so talking to him and asking him for changes needs Claude Code.

## Start

Open this folder in Claude Code and say:

```
/flight-deck setup
```

Marvin looks at the machine first (which agent runtimes are in use, which Claude Code logins and agent folders are here, whether voice can work, which Trinity instances Claude Code already knows), writes a config that fits, installs the feeder and opens the window. Then he tells you what is missing and offers to add it. `/flight-deck survey` only looks and changes nothing.

Without Claude Code: `python3 .claude/skills/flight-deck/scripts/fd.py setup`.

## Trinity

The easy way is on the page: open the TRINITY view (key **M**) and press **Connect an instance**, or **INSTANCES ▾** beside the logo (the Trinity box on the DECK view has the same button), give the instance's address and its MCP key, and it is connected; the key is kept in the Keychain of this Mac. The same menu shows another instance in full or removes one, and a click on an instance's line in the box switches to it.

```
/flight-deck trinity                                   what is connected, whether it answers, what else was found
/flight-deck trinity add Work --server trinity         an instance Claude Code already has as an MCP server
fd.py trinity add Lab --url https://host/mcp           by address, in a terminal: asks for the key, kept in the Keychain
/flight-deck trinity main Lab                          which one is shown in full
/flight-deck trinity remove Lab
```

Provisioning a new instance from here is planned, not built.

## Voice and the face

Put a Gemini API key under `"env"` in `~/.claude/settings.json` (`"GEMINI_API_KEY": "..."`), reload, press **V**. The face is an original character made for this agent. A second face ships for another agent you may add: `stardust`, a face of drifting nebula dust that moves while it waits. For the moving mouth, install the lip-sync engine (about 4 GB, runs locally): the steps are in `.claude/skills/voice-agent/SKILL.md`.

> **Voice runs Claude Code with approvals off by default.** In a voice conversation, tools that would normally ask for permission are approved and logged without asking, and whatever the microphone hears becomes a prompt. Use it where you trust what is said near the machine. To have edits and commands wait for a spoken yes, set `"yolo": false` under `"voice"` in the config (or start the voice skill with `--no-yolo`).

## Third-party code

`.claude/skills/voice-agent/vendor/p5.min.js` is [p5.js](https://p5js.org) 1.9.0, unmodified, under the LGPL-2.1 (`vendor/p5-LICENSE.txt`; source at github.com/processing/p5.js). The lip-sync engine (OpenTalking / QuickTalk, Apache-2.0) is not included; you install it yourself.

## Requirements

macOS, Python 3 (the one dependency, `psutil`, goes into a small virtual environment if no Python has it), Claude Code. Optional: Google Chrome (the deck's own window; without it, a browser tab), the Xcode command line tools (the toggle command and the screensaver).

## What it touches

- Reads: `~/.claude` (sessions, transcripts, prompt history), the names and addresses of MCP servers in the Claude configs, `~/.cache/cc-dash` if present, the machine's own counters. The feeder listens on 127.0.0.1 only and answers only requests addressed to that name.
- What leaves the machine: calls to the Trinity instances you connect; the titles of recent sessions, sent through your own Claude Code (`claude -p`) to name the themes on SESSIONS; during a voice conversation, your speech and the text of each reply, sent to Gemini; and for the voice page's weather line, the city of your time zone, sent to open-meteo.com.
- Writes: `~/.config/flight-deck/` (config), `~/.cache/flight-deck/` (index, window profile, logs), `~/Library/LaunchAgents/local.flight-deck.plist`, `~/.local/bin/flight-deck`, voice conversations inside `.claude/skills/voice-agent/conversations/` (ignored by git), and with `install` the screensaver in `~/Library/Screen Savers/`.
- `uninstall` removes all of it except the config and the conversations.

The config keys are described at the top of `.claude/skills/flight-deck/app/config.py`; `app/config.example.json` shows every one.

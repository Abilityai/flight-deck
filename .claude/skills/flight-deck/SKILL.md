---
name: flight-deck
description: Set up, start, stop, check or change Flight Deck, the monitor window of this Mac (DECK, SESSIONS, ATTENTION, TRINITY, TALK views) and its light screensaver. Use for "start flight deck", "open the deck", "set up the deck", "is the deck running", "restart the feeder", "the deck is blank", "package the deck for another machine", and for any change to the page, the feeder or its config.
allowed-tools: [Bash, Read, Write, Edit]
argument-hint: "[survey | setup [--fresh] | start | stop [--all] | restart | status | doctor | trinity [add|main|remove] | install | uninstall | package <folder>]"
user-invocable: true
metadata:
  version: "1.6"
  created: 2026-11-20
  changelog:
    - "1.6: 2026-11-23 - the product is named Flight Deck (Marvin is the agent inside it); a second face set ships, `stardust`, a moving face of dust with expressions and lip-sync"
    - "1.5: 2026-11-22 - other runtimes: the survey finds Codex, OpenCode, Gemini CLI, Cursor agent and others; Codex and OpenCode sessions (Gemini CLI prompts) are read into DECK, SESSIONS and ATTENTION; ATTENTION works without cc-dash, from the transcripts; parts without a source are left out; a first-run card on the page shows what the survey found"
    - "1.4: 2026-11-22 - Trinity instances from the page: the Trinity box's label opens a menu to connect one with its address and MCP key (tried first, key to the Keychain), show another in full, or remove one; the box stays when none is connected; changes apply without a feeder restart; the top menu keeps its buttons in place"
    - "1.3: 2026-11-22 - survey (looks at the machine) and a setup that fits the config to it; trinity list / add / main / remove, several instances on the page; the package is the agent Marvin and carries the voice skill with its own face; no machine or agent of one Mac named in the code"
    - "1.2: 2026-11-22 - portable: setup, doctor, uninstall, and package <folder> (a standalone agent folder for another machine); a third skin, day"
    - "1.1: 2026-11-21 - the dusk copy became a skin of the one page (night / dusk, button and key K); dusk.html is a redirect"
    - "1.0: 2026-11-20 - Flight Deck packaged as a skill: scripts/fd.py start / stop / restart / status / install drives the project"
---

# Flight Deck

> First print one line: `flight-deck v<version> - recent: <top changelog entry, short>`.

This skill is how Flight Deck is run and where its working knowledge lives. The project (the app) is the `app/` folder of this skill when there is one, else the `flight-deck/` folder of this agent; `status` prints which. One script does the running:

```bash
python3 .claude/skills/flight-deck/scripts/fd.py <command>
```

| Command | Does |
|---|---|
| `survey` (`--json`) | looks at the machine and changes nothing: Claude Code and its logins, the agent folders it has worked in, the voice skill, speech key and lip-sync engine, Trinity instances in the Claude configs, a trinity-desk skill, ssh hosts, backup logs |
| `setup` (`--fresh`) | first run on a machine: surveys it, writes a config that fits (never over an existing one; `--fresh` puts the old one aside), installs the feeder, prints the survey, opens the window |
| `trinity` | the connected instances and whether each answers, and those found in the Claude configs that are not connected |
| `trinity add <label> --server <name> [--file <claude config>]` | connects an instance Claude Code already holds as an MCP server |
| `trinity add <label> --url <address>/mcp` | connects one by address: the key is asked for in the terminal (or read from standard input) and kept in the Keychain, never in the config or a conversation |
| `trinity main <label>`, `trinity remove <label>` | which instance is shown in full; disconnect one |
| `start` (default, also with no argument) | makes sure the feeder runs, then opens the window or brings it to the front; never closes it |
| `stop` | closes the window; the feeder stays (it collects nothing while no page asks) |
| `stop --all` | closes the window and stops the feeder |
| `restart` | restarts the feeder: needed after a change to its Python files or to the config |
| `status` | project folder, feeder, LaunchAgent, window, config, voice session, local sessions, Trinity, the desk shown and whether its loop is alive |
| `doctor` | the survey's lines: one per part of the deck, what it needs and whether this machine has it |
| `install` | builds and installs the screensaver and the `flight-deck` command (`build.sh`), and the feeder's LaunchAgent |
| `uninstall` | removes the LaunchAgent, the command, the screensaver and the cache; the config stays |
| `package <folder>` | writes a standalone agent folder for another machine (the agent Marvin): this skill with the app inside it, the voice skill with the `marvin` face, an agent file, a readme; ends with a leak check |

Run the command asked for and report its lines as they came out. `/flight-deck` with no argument means `start`.

Other ways in, all the same window: `flight-deck` in a shell (that one toggles: it closes a window that is already in front), `http://127.0.0.1:47600/` in any browser.

## What is where

| Piece | Place |
|---|---|
| Feeder (HTTP on 127.0.0.1, collects only while a page asks) | `feeder.py`, LaunchAgent `local.flight-deck`, log `~/Library/Logs/flight-deck.log`. One dependency, `psutil`; `fd.py` picks a Python that has it or makes a virtual environment in `~/.local/share/flight-deck/venv` |
| The page (all views, all skins) | `web/index.html` |
| Collectors | `sessions.py` (subscriptions, themes), `attention.py`, `remote.py` (the remote box over ssh, the Trinity summary), `voice.py` (starts and stops the voice server) |
| Window | `launcher.swift` → `~/.local/bin/flight-deck`: a dedicated Chrome instance, profile `~/.cache/flight-deck/chrome`. Without Chrome the page opens in the default browser |
| Screensaver | `saver/FlightDeckView.swift`: native drawing from `/lite.json`, no web view. It assumes port 47600 |
| This machine's values | `~/.config/flight-deck/config.json` (read by `config.py`, whose docstring lists every key): port, voice agents, the remote ssh host, Trinity instances, backup log and its label, `desk` |
| The Trinity desk it displays | not part of this project: an agent's `trinity-desk` skill, named by `desk.dir`; its loop is `local.trinity-desk.loop` |

Trinity: several instances, managed from the page or with `fd.py trinity`. On the page the label of the Trinity box opens a menu: **+ Add instance** takes an address and the instance's MCP key (a name is optional; `/mcp` and the scheme are tried when left out), the feeder makes one read-only call with them and saves only an instance that answers, the key in the Keychain; **Show** makes another instance the one shown in full (so does a click on its line in the box); × then **Remove?** disconnects one. With no instance the box says so and offers **Connect an instance**. The routes are `POST /trinity/add | main | remove` in `feeder.py`, the logic `add / set_main / remove / instances` in `remote.py`; the page reloads itself when the main instance changes (after a running conversation ends), also when the change was made by `fd.py` or in the config file, which the feeder notices without a restart. The same menu is on the TRINITY view, under the instance's name beside the logo (**INSTANCES ▾**), and on the DECK view in the Trinity box. The TRINITY tab is always there: with a desk that belongs to the instance shown in full (config `desk.instance`, the instance's label; without it the desk shows for any instance) it is the desk; otherwise it shows the connected instances as cards (`tvInst()` in the page, `TVPLAIN`), with **Show in full** on each and a card to connect another. The config's `trinity` list holds them. The first instance is the main one (the Trinity box in full, the queue); each other gets one line under it (agents running, runs, queue, or "unreachable"). An entry is `{label, server, claude_json}` (read from a Claude config), `{label, url, keychain}` (key in the Keychain, service `flight-deck-trinity`) or `{label, url, headers}`. `setup` connects the user's own entries and only lists those of single agents. Provisioning a new instance is not built.

Runtimes: the deck is not only for Claude Code. `runtimes.py` finds the agent runtimes of the machine (`KNOWN` lists them) and reads the sessions of those that keep them in a readable form into one shape (prompts, answers, folder, model, title): Codex from `~/.codex/sessions/**/rollout-*.jsonl`, OpenCode from its SQLite file `~/.local/share/opencode/opencode.db`, Gemini CLI from `~/.gemini/tmp/*/chats/*.json` or, on older versions, `logs.json` (prompts only). Cursor agent, Copilot CLI, Goose, Aider and Ollama are found and named, not read. It only reads; its index is `~/.cache/flight-deck/runtimes-index.json`. Those sessions show as live rows in Agents with the runtime's name (a session is live when a process of that runtime runs in its folder), in SESSIONS (the list, the graph, a row per runtime used in 7 days), in ATTENTION and in the prompt counts. Runs nobody typed (`codex exec`, subagents) are counted apart and kept out of attention. A new runtime is one reader function that returns that shape plus a line in `KNOWN`; test it against a made-up home folder (`HOME=<folder>`, `FLIGHT_DECK_CONFIG`, `FLIGHT_DECK_PORT`), never by writing into a real runtime's folder. The state's `parts` says which optional sources the machine has (`claude`, `cc_dash`, `fleet`, `backup`): without Claude Code the Subscriptions panel becomes Runtimes and Claude is never started for theme names; without Tailscale and a backup log the Fleet box is left out.

First run: `setup` writes `"welcome": true` into a new config. The page then shows a card with the survey's lines (`GET /survey.json`, which runs `fd.py survey --json`) until the person presses **Got it** (`POST /welcome/done` sets it to false). `?welcome` in the address shows the card again. The feeder runs that survey with `FLIGHT_DECK_BACKGROUND=1`, which leaves out agent folders in places macOS guards (Desktop, Documents, Downloads, iCloud Drive, other volumes): a background process that opens a file there blocks on a permission question, and the survey then times out. Anything else the feeder is made to read must keep to that rule.

Optional parts and what each needs: TALK views, a `voice-agent` skill (the agent's own in `.claude/skills`, else the user's in `~/.claude/skills`; hidden without one) and a Gemini key; the one-line session summaries, cc-dash logs in `~/.cache/cc-dash` (ATTENTION uses them when they exist and the transcripts when they do not; a session then shows its own title); the Trinity box, a `trinity` MCP server in `~/.claude.json`; the TRINITY view, a `desk`; the Fleet box, the Tailscale app; the remote box, `remote.ssh_host`.

Views and keys: DECK `D`/`1`, SESSIONS `S`/`2`, ATTENTION `T`/`3`, TRINITY `M`/`6`, TALK `4`, the second voice agent's view `5`/`C`; `V` starts or stops the conversation of the voice view in front; `A` returns to automatic cycling; `K` switches the skin.

## Changing it

- **Nothing of one machine in the project.** Names, hosts, paths and keys go in the config file; the page and the collectors read them from there. `package` fails its leak check otherwise.
- **The page**: edit `web/index.html`, then reload the window (⌘R). No restart. Copy the file aside first for any large edit when the folder is not a git repository.
- **Skins.** The page has three looks in one file: `night` (the original), `day` (light, warm sand, not white) and `dusk` (sunset). The switch cycles night → day → dusk. The CSS half of a skin is a `<style id="skin-<name>" media="not all">` block switched on in the head; what the canvas draws with (accent colours, heat shades, halo, grid lines, fonts) is the `SKIN` table at the top of the script. Chosen by `?skin=`, else the last choice (`localStorage` `fd-skin`); switched with the ◐ button at the end of the tab row or key `K`, which reloads the page and is refused while a conversation runs. A new element needs no skin work unless it hard-codes a colour: use the CSS variables, and add a rule to a skin block only where that look should differ. To add a skin: a style block, a `SKIN` entry, its name in `SKINS`.
- **The feeder or a collector**: edit, then `fd.py restart`.
- **The config**: edit `~/.config/flight-deck/config.json`, then `fd.py restart` and reload the window.
- **Test** in a headless browser before saying it works, once per saved view (the page restores the last view from local storage at start-up, and code that runs then must not touch state declared further down). Fake the voice page and any route that acts (`/voice/*`, `/ask/answer`): the real ones may hold a live conversation or record a real answer. A second feeder for a test: `HOME=<an empty folder> FLIGHT_DECK_PORT=<another port> python3 feeder.py` shows what a new machine sees.
- Never `POST /voice/stop` without checking the running session is your own test's.
- Scripts that edit the page are written to a file and run, not passed as a shell heredoc: template literals with a dollar sign before a brace trip the shell safety check.
- **The voice skill in a package** is a copy of the user's `voice-agent` skill with one face set (`marvin`, generated for this, no real person and no existing character) and its text made neutral by asserted replacements in `_neutral_voice()`: if the skill's wording changes, packaging stops there and the replacement is updated. Conversations and other faces never go in.
- **This project is the source of the open-source product, Flight Deck** (since 2026-11-23; Marvin is the agent, voice and face inside it, not the product's name): the deck, this skill and the templates in `scripts/package/` are what the public version is built from, so a change is finished only when the release is rebuilt (`package <the release folder>`, leak check clean, the `CLAUDE.md` template copied over the build's) and the template readme matches. License, public name, `git init` and publishing are the owner's decisions.
- **A packaged copy** is a build of this project, not a second source: change the project here and run `package` again on the same folder (it replaces the skill, the readme and `.gitignore`, and keeps that folder's `CLAUDE.md` and git history). The copy uses the LaunchAgent name `local.flight-deck`.

## When it looks wrong

| Symptom | Look at |
|---|---|
| Blank window, "--:--" clock | a script error at start-up: open `http://127.0.0.1:47600/?debug`, read `window.__err`; usually a view restored before its state exists |
| "FEEDER OFFLINE" | `fd.py status`, then `fd.py restart`; the log file |
| The feeder does not start | `fd.py doctor`: no Python with psutil, or another program holds the port (change `port` in the config) |
| TALK tab missing | the `voice-agent` skill is not in `~/.claude/skills` (or `voice.skill` points elsewhere) |
| TRINITY tab missing | no `desk` in the config, or `desk.dir` does not exist |
| TRINITY shows old data, rail says the loop is silent | the desk's loop: `python3 <desk.dir>/deskctl.py doctor` |
| Trinity box says UNREACHABLE | the network path to the instance is down (a VPN or Tailscale), or the MCP entry in the Claude config changed |
| The `flight-deck` command is missing | `fd.py start` rebuilds it (needs the Xcode command line tools) |

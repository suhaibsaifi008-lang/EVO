# E.V.O — Personal Intelligence

A local personal assistant in the JARVIS tradition. Hears the wake word anywhere, transcribes commands **fully offline**, speaks with neural voices, sees your screen, remembers everything across restarts, learns permanent skills, runs resumable background missions, guards your system with watchers, texts you on Telegram, drafts your email, drives your mouse (with permission), answers from your own documents, builds professional websites, and pushes alerts to your phone.

## The complete ability map

**Senses** — wake phrase ("wake up evo", configurable in `.env`) · offline Vosk transcription · screen-free voice loop (speak → it answers aloud) · neural TTS with alert/calm/warm tones · screen vision · ambient window awareness · opt-in ambient vision · welcome-back greeting after long absences

**Mind** — agentic brain (~50 tools) · Deep Thought ensemble · Deep Mode self-critique · fast/strong model routing · feedback memory ("from now on always...") · habit engine with skill proposals · 3-layer persistent memory · relevant-knowledge injection

**Body & work** — app/web launching (Start Menu + Microsoft Store apps) · screenshots · volume/media · GUI control incl. vision-guided clicking *(gated)* · coding sandbox w/ self-debugging · AI worker teams · resumable missions (up to 200 steps) · watchers (battery/disk/web/news) · reminders/timers/alarms · briefing v2 (calendar + schedule + weather + battery)

**Reach** — Telegram presence · ntfy phone push (auto on watcher/project events) · email draft-first · calendar via iCal feed · YouTube lecture summaries · Home Assistant smart-home hook · file RAG over your folders · Websmith site builder · native chart/PDF generation

## Run on Windows

Prerequisites: [Python 3.11+](https://www.python.org/downloads/) and Chrome/Edge for speech input.

```powershell
cd EVO
scripts\start.bat        # first time: installs deps, starts tray + ear + console
scripts\stop-evo.bat     # stop everything
```

`start.bat` installs everything, starts the **tray app**, the **wake-word ear**, and opens the console.
Manual server run: `uvicorn main:app --port 8420`.

### Scripts

| File | Purpose |
|---|---|
| `scripts/start.bat` | One-time setup + launch tray/ear/console |
| `scripts/stop-evo.bat` | Stop all EVO processes |
| `scripts/install_autostart.bat` | Launch EVO automatically at login (run as admin) |
| `scripts/remove_autostart.bat` | Remove autostart entries |
| `scripts/build_exe.bat` | Build a standalone `dist\EVO\EVO.exe` |
| `scripts/evo_silent.vbs` | Start the server with no visible window |

Entry points live at the repo root: `main.py` (FastAPI server), `evo_app.pyw` (desktop app), `evo_tray.pyw` (system tray), `evo_ear.pyw` (wake-word ear).

Configure optional powers in `.env` (see `.env.example`): Telegram token, mail credentials, ntfy topic, Home Assistant, fast model, satellite ear URL. Calendar: paste your secret iCal URL in Setup.

## Apps & search behaviour

- **Open anything**: "open Valorant", "open Copilot", "open Spotify" — EVO resolves apps from its built-in alias table, every Start Menu shortcut (all users), Microsoft Store / packaged apps, and PATH executables, then verifies the window appeared.
- **No forced search engine**: searches are handed to **your default browser** (from Windows' own default-app setting) as plain text, so they run through that browser's configured search engine — Brave Search, Google, whatever you chose. EVO never forces Bing.

## Trust model

Code execution, GUI control and real email sending are each separately gated (voice approval and/or Setup toggles). Every tool action lands in the audit ledger (System tab). Shutdown/restart always asks. Telegram replies only to your paired chat ID.

## Voice commands to try

**How talking works (ChatGPT-style):** say **"wake up evo"** once — EVO chimes and opens a live conversation. Then just talk: every sentence is answered without repeating the wake word. Say "stop listening", "goodbye", or stay silent for 45 seconds and EVO goes back to standby until you wake it again. Works the same through the desktop ear and the web console's mic (with Wake mode enabled).

- "wake up evo, open Valorant" — command rides in the same breath
- Then keep chatting: "what's the weather", "search for cheap flights to Tokyo", "open Copilot"
- "Wake this product page and tell me when the price changes"
- "Read my unread mail and draft replies"
- "Click the accept button" (with GUI control enabled)
- "Build me a website for my bakery with about and contact pages"

## Tests

```powershell
.\.venv\Scripts\Activate.ps1
pytest tests -q
```

180+ tests cover every subsystem: intents, app discovery/opening, agent loop, skills forge, senses, offline voice, missions/resume, watchers, telegram pairing, mail gating, GUI gating, RAG, websmith, audit, failover routing, tones, corrections, habits, routing, calendar parsing, push, reports.

## Notes

- Server binds localhost only; data lives in `data/` — delete `data/evo.db` to factory-reset.
- `.env` holds secrets; keep it private (it's git-ignored).
- Wake phrases are set with `JARVIS_WAKE_PHRASES` in `.env` (default: "wake up evo"). Leave it empty to use the pretrained "hey jarvis" audio model instead.
- First server start downloads a ~40MB local speech model once (used for the wake phrase, the mic's offline fallback, and command transcription).

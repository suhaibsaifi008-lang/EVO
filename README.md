# E.V.O — Final

A local personal assistant in the JARVIS tradition. Hears "Hey Jarvis" anywhere, transcribes commands **fully offline**, speaks with emotional neural voices, sees your screen, remembers everything across restarts, learns permanent skills, runs resumable background missions, guards your system with watchers, texts you on Telegram, drafts your email, drives your mouse (with permission), answers from your own documents, builds professional websites, and pushes alerts to your phone.

## The complete ability map

**Senses** — wake-word ear (local model) · offline Vosk transcription · screen-free voice loop (speak → he answers aloud) · neural TTS with alert/calm/warm tones · screen vision · ambient window awareness · opt-in ambient vision · welcome-back greeting after long absences

**Mind** — agentic brain (~50 tools) · Deep Thought ensemble · Deep Mode self-critique · fast/strong model routing · feedback memory ("from now on always...") · habit engine with skill proposals · 3-layer persistent memory · relevant-knowledge injection

**Body & work** — app/web launching · screenshots · volume/media · GUI control incl. vision-guided clicking *(gated)* · coding sandbox w/ self-debugging · AI worker teams · resumable missions (up to 200 steps) · watchers (battery/disk/web/news) · reminders/timers/alarms · briefing v2 (calendar + schedule + weather + battery)

**Reach** — Telegram presence · ntfy phone push (auto on watcher/project events) · email draft-first · calendar via iCal feed · YouTube lecture summaries · Home Assistant smart-home hook · file RAG over your folders · Websmith site builder · native chart/PDF generation

## Run

```powershell
start.bat        # first time: installs deps, starts tray + ear + HUD
stop-evo.bat  # stop everything
```

Configure optional powers in `.env` (see `.env.example`): Telegram token, mail credentials, ntfy topic, Home Assistant, fast model, satellite ear URL. Calendar: paste your secret iCal URL in Setup.

## Trust model

Code execution, GUI control and real email sending are each separately gated (voice approval and/or Setup toggles). Every tool action lands in the audit ledger (System tab). Shutdown/restart always asks. Telegram replies only to your paired chat ID.

## Tests

```powershell
.\.venv\Scripts\Activate.ps1
pytest tests -q
```

163 tests cover every subsystem: intents, agent loop, skills forge, senses, missions/resume, watchers, telegram pairing, mail gating, GUI gating, RAG, websmith, audit, failover routing, tones, corrections, habits, routing, calendar parsing, push, reports, welcome-back.

## Notes

- Server binds localhost only; data lives in `data/` — delete `jarvis.db` to factory-reset.
- The spoken call sign remains "hey jarvis" (pretrained audio model); custom-phrase training possible later.
- First ear run downloads a ~40MB local speech model once.

## The seven powers (Mark III)

| Power | What it means |
|---|---|
| **Phone presence** | Text your bot on Telegram from anywhere; EVO answers and acts on your PC |
| **Watchers** | Background sentinels: *"warn me when battery hits 20%"*, *"watch this page"*, *"keep an eye on AI news"* — announced aloud when they trigger |
| **GUI control** *(gated)* | Moves the mouse, clicks, types, hotkeys — plus vision-guided clicking: *"click the Export button"* |
| **Email** *(draft-first)* | Reads your inbox, drafts replies; real sending is double-gated (your approval + Setup toggle) |
| **Long missions** | Up to 200-step background projects with checkpoint/resume — pause overnight, continue tomorrow |
| **File RAG** | *"index_folder D:\Documents"* → EVO answers questions from *your* files |
| **Offline brain** | If the primary LLM router is down, falls back to local Ollama automatically |

Plus everything from Mark II: agentic brain, skill forge, deep thought ensemble, deep-mode critique, persistent 3-layer memory, screen vision, ambient awareness, neural voice, wake word ear, coding sandbox with self-debugging, AI worker teams, scheduling + daily briefing, routines.

## Run on Windows

Prerequisites: [Python 3.11+](https://www.python.org/downloads/) and Chrome/Edge for speech input.

```powershell
cd evo   (folder may still be named jarvis)
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
start.bat equivalent: double-click start.bat
```

`start.bat` installs everything, starts the **tray app**, the **wake-word ear**, and opens the HUD.
`stop-evo.bat` stops it all. Manual: `uvicorn main:app --port 8420`.

## Configuration (.env)

Copy `.env.example` → `.env`. Sections: primary LLM (FreeLLMAPI or any OpenAI-compatible), Ollama fallback, identity (`JARVIS_NAME=EVO`), Telegram token, mail credentials. Everything optional except a brain (router *or* Ollama).

## Autonomy & trust

- Code execution: asks per-run unless "run own code" enabled in Setup
- GUI control: fully disabled until enabled in Setup
- Email sending: needs your spoken approval AND the Setup toggle; drafts always shown first
- Every tool action is written to an audit ledger (System tab shows recent actions)
- Shutdown/restart always requires explicit confirmation
- Telegram responds only to your paired chat ID

## Voice commands to try

- "Hey Jarvis" → chime → speak naturally (agent mode has no fixed phrases)
- "Index my documents folder, then what did I write about the insurance claim?"
- "Start a project: research the 5 best budget monitors, compare them, save a report"
- "Learn the skill: check my public IP and log it"
- "Watch this product page and tell me when the price changes"
- "Read my unread mail and draft replies"
- "Click the accept button" (with GUI control enabled)
- "Build me a website for my bakery with about and contact pages"

## Tests

```powershell
.\.venv\Scripts\Activate.ps1
pytest tests -q
```

139 tests cover intents, agent loop, skills, senses, forge, missions, watchers, telegram pairing, mail gating, GUI gating, RAG, websmith, audit, and failover routing.

## Notes

- Server binds localhost only. Data in `data/` (SQLite) — delete `jarvis.db` to factory-reset.
- `.env` holds secrets; keep it private (it's git-ignored).
- The spoken call sign remains "hey jarvis" (pretrained model); rename training is possible later via openWakeWord custom models.

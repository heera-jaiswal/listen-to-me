# ListenToMe

A local web app that records speech for an hour or longer and produces an accurate, timestamped transcript. Everything runs on your machine: no cloud APIs, no API keys, and no account. Transcription uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (Whisper via CTranslate2) on the CPU.

The UI is plain HTML, CSS, and JavaScript with no build step. The backend is a single Python process using the standard-library HTTP server.

## Features

- **Live transcription** while you record (typically every 20–30 seconds), with timestamps
- **Start / Pause / Stop**, recorded-time timer, mic level meter, and recording indicator
- **Microphone picker** (choice is remembered in the browser)
- **Session history**: create, reopen, rename, finalize unfinished sessions, delete
- **Exports**: plain text, SRT subtitles, Markdown; copy to clipboard; download audio (`.m4a` when ffmpeg is available, otherwise `.wav`)
- **Improve accuracy**: optional second pass over the full recording with a larger Whisper model
- **Resilience**: numbered audio chunks with retry, duplicate-safe uploads, screen wake lock while recording, warning before closing the tab
- **Privacy by default**: server binds to `127.0.0.1`; Hugging Face model loading defaults to offline mode once models are cached

## Requirements

| Requirement | Notes |
|-------------|--------|
| **macOS** (primary target) | Developed and tested on Apple Silicon; should work on Intel Macs with Python wheels available |
| **Python 3.9+** | 3.13 recommended; 3.9+ is supported |
| **Chrome** (recommended) | Microphone and AudioWorklet support; `http://localhost` is treated as a secure context |
| **Disk space** | ~2 GB for default models (`base.en` + `medium`) plus session audio (~115 MB/hour raw PCM on disk) |
| **RAM** | ~1 GB for live model; ~4 GB peak during “Improve accuracy” with `medium` |
| **ffmpeg** (optional) | For `.m4a` audio export; without it, exports fall back to `.wav` |

## Quick start

From the project directory:

```bash
./start.sh
```

`start.sh` will:

1. Create `.venv` and install dependencies from `requirements.txt` if needed
2. Start `server.py` on port **8765** (override with `LTM_PORT`)
3. Open **http://localhost:8765** in your default browser (after a short delay)

Manual start:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
```

Stop the server with **Ctrl+C** in the terminal where it is running.

## First-time setup: Whisper models

On first run, faster-whisper needs the model weights on disk. By default the server sets `HF_HUB_OFFLINE=1`, so **models must already be cached** or loading will fail with a message like “outgoing traffic has been disabled.”

Download the default models once (requires network):

```bash
cd /path/to/ListenToMe
HF_HUB_OFFLINE=0 .venv/bin/python -c "
from faster_whisper import WhisperModel
for name in ('base.en', 'medium'):
    print(f'Downloading {name}…')
    WhisperModel(name, device='cpu', compute_type='int8')
    print(f'{name} ready')
"
```

Approximate download sizes:

| Model | Role | Size (approx.) |
|-------|------|----------------|
| `base.en` | Live transcription while recording | ~145 MB |
| `medium` | “Improve accuracy” pass after Stop | ~1.5 GB |

After caching, normal runs use offline mode and do not contact Hugging Face.

Install **ffmpeg** for smaller audio downloads (optional):

```bash
brew install ffmpeg
```

## Using the app

1. Open **http://localhost:8765** in Chrome.
2. Choose a **title** and the correct **microphone** (prefer your built-in Mac mic, not virtual devices such as “Microsoft Teams Audio Device”).
3. Click **Start** and speak. The status line and transcript header show recording state and any transcription backlog.
4. Click **Pause** to pause recording (timer counts only active recording time).
5. Click **Stop** to flush remaining audio and finalize the transcript.
6. Use **TXT / SRT / Markdown / Audio / Copy** to export, or **Improve accuracy** for a full re-transcription with the refine model (slower, higher quality).
7. Past sessions appear in the **Sessions** sidebar; select one to reopen or delete.

If the server restarts while a session was finalizing or refining, open that session and use **Finalize** or run **Improve accuracy** again as needed.

## Configuration

Environment variables read by `server.py`:

| Variable | Default | Purpose |
|----------|---------|---------|
| `LTM_HOST` | `127.0.0.1` | Bind address (keep localhost for local-only use) |
| `LTM_PORT` | `8765` | HTTP port |
| `LTM_SESSIONS_DIR` | `./sessions` | Directory for session data |
| `LTM_LIVE_MODEL` | `base.en` | Model for live transcription |
| `LTM_REFINE_MODEL` | `medium` | Model for “Improve accuracy” |
| `LTM_LANGUAGE` | `en` | Language code for multilingual models; use `auto` to detect |
| `LTM_CPU_THREADS` | `8` | CPU threads passed to Whisper |
| `HF_HUB_OFFLINE` | `1` (set in code) | Set to `0` to allow downloading missing models |

Example:

```bash
LTM_PORT=9000 LTM_LIVE_MODEL=small.en ./start.sh
```

Session data is stored under `sessions/<id>/`:

- `audio.pcm` — 16 kHz mono 16-bit PCM
- `meta.json` — title, status, segments, transcription cursor, errors
- `audio.m4a` — created on export when ffmpeg is available

## How it works

```
Browser (public/)
  getUserMedia → AudioWorklet (16 kHz Int16) → every 5 s POST /audio?seq=N
  polls GET /api/sessions/:id every 3 s → renders transcript
        │
        ▼
server.py (127.0.0.1:8765)
  ├─ appends PCM to sessions/<id>/audio.pcm, state in meta.json
  ├─ transcription worker (one job at a time, models kept in memory)
  │    live:   when ≥20 s of new audio, transcribe up to 60 s window;
  │            commit all segments except the last (avoids cutting mid-sentence)
  │    final:  on Stop, transcribe remainder and commit all
  │    refine: optional full re-pass with refine model; live segments kept as backup
  └─ exports: .txt / .srt / .md; audio via ffmpeg (.m4a) or wave (.wav)
```

Silence is handled with faster-whisper’s VAD (`vad_filter=True`) to reduce hallucinated text during quiet periods.

## HTTP API

| Method | Route | Purpose |
|--------|-------|---------|
| `GET` | `/api/config` | Active models and sample rate |
| `GET` / `POST` | `/api/sessions` | List sessions / create session |
| `GET` / `DELETE` | `/api/sessions/:id` | Session JSON (transcript, status, backlog) / delete |
| `POST` | `/api/sessions/:id/audio?seq=N` | Upload raw PCM chunk (duplicates ignored) |
| `POST` | `/api/sessions/:id/status` | Set paused or recording |
| `POST` | `/api/sessions/:id/finish` | Finalize transcription |
| `POST` | `/api/sessions/:id/refine` | Run refine model on full audio |
| `POST` | `/api/sessions/:id/rename` | Rename session |
| `GET` | `/api/sessions/:id/export.txt` | Download transcript as text |
| `GET` | `/api/sessions/:id/export.srt` | Download SRT |
| `GET` | `/api/sessions/:id/export.md` | Download Markdown |
| `GET` | `/api/sessions/:id/audio.file` | Download audio file |

Static UI is served from `public/` at `/`.

## Project layout

```
ListenToMe/
├── README.md
├── PLAN.md                 # Implementation notes and roadmap
├── server.py               # HTTP server, sessions, transcription engine
├── requirements.txt
├── start.sh
├── tests/
│   └── smoke_test.py       # End-to-end automated test
├── sessions/               # Runtime data (gitignored)
└── public/
    ├── index.html
    ├── app.js
    ├── recorder-worklet.js
    └── styles.css
```

## Testing

Automated smoke test (synthetic speech, chunked upload, live + finish, exports):

```bash
.venv/bin/python tests/smoke_test.py
```

Skip the refine pass (faster):

```bash
.venv/bin/python tests/smoke_test.py --no-refine
```

Optional test tuning: `LTM_TEST_PORT`, `LTM_TEST_SPEEDUP`.

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| `[whisper] could not load 'base.en'` | Run the first-time model download with `HF_HUB_OFFLINE=0` (see above). |
| No microphone in browser | Use Chrome; allow mic for localhost; pick a real device in the dropdown. |
| Transcript lags behind speech | Check backlog in the transcript header; close heavy apps; use a smaller live model via `LTM_LIVE_MODEL`. |
| Audio export is `.wav` only | Install ffmpeg (`brew install ffmpeg`) and restart the server. |
| Port already in use | `LTM_PORT=8766 ./start.sh` |
| Empty or wrong transcript | Ensure you clicked **Stop** to finalize; check `meta.json` in the session folder for `error`. |

## Known limitations

- **Segment end times** can extend across long silences after speech (start times are usually correct). SRT caption durations may look too long; word-level timestamps may fix this in a future version.
- **Long sessions** (60+ minutes): monitor memory and backlog; see `PLAN.md` for extended testing notes.
- **Browser speech recognition** is intentionally not used (cloud-dependent and unreliable for long recordings).

## Privacy and security

- The server listens on **127.0.0.1** only by default; it is not exposed to your LAN unless you change `LTM_HOST`.
- Audio and transcripts stay under `sessions/` on disk.
- After models are cached, transcription does not require network access.

## Further reading

See [PLAN.md](./PLAN.md) for design decisions, benchmarks on Apple Silicon, implementation status, and planned enhancements (speaker labels, summaries, PWA, etc.).

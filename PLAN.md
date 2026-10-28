# ListenToMe — Implementation Plan

A simple web app that records you for up to an hour (or longer) and gives you an accurate, timestamped transcript. Everything runs on your Mac: no cloud, no API keys, and no model downloads.

## 1. Decisions (updated)

| Topic | Decision | Why |
|---|---|---|
| Speech engine | **faster-whisper** (Python, CTranslate2) | Your models are already cached, so nothing needs downloading |
| Live model | **`base.en`** (145 MB, cached) | Measured at **6.2× real time** and 1.0 GB RAM on your M4 Pro |
| "Improve accuracy" model | **`medium`** (1.5 GB, cached), run after you press Stop | Measured at 1.4× real time and 3.65 GB RAM. Too close to the limit for live use, fine as a second pass |
| Silence handling | faster-whisper's built-in Silero voice detection (`vad_filter=True`) | Stops Whisper from inventing text during silence |
| Backend | **Python standard-library HTTP server** (no Node, no npm) | npm isn't installed (the only Node is the one bundled inside Wibey). Python is needed for Whisper anyway, so this is one process in one language |
| Frontend | Plain HTML, CSS and JS, no build step | Simple. Living Design styling is optional later |
| Audio capture | AudioWorklet → **16 kHz mono 16-bit PCM**, uploaded every 5 s | Whisper's native format, so no ffmpeg in the transcription path. The raw file can be read from any point, so there are no chunk-boundary problems |
| Network | Model loading is forced offline (`HF_HUB_OFFLINE=1`); server listens on `127.0.0.1` only | Private by default |

Not used: the browser's built-in speech recognition (sends audio to Google and is unreliable for an hour), the whisper.cpp CLI (the only real model on disk for it is `tiny.en`), and the old `~/.polish-my-demo` Python environment (the project now has its own `.venv`).

## 2. System check (done)

M4 Pro (14 cores) · 48 GB RAM · 80 GB free disk · macOS 26.6.2 · Chrome · ffmpeg 7.1.1 · Python 3.13.5 → ✅ all requirements met.
An hour of audio takes about 115 MB as raw PCM, or about 30 MB as an `.m4a` export.

## 3. Architecture

```
Browser (public/)
  getUserMedia → AudioWorklet (16 kHz Int16) → every 5 s POST /audio?seq=N
  polls GET /api/sessions/:id every 3 s → renders transcript
        │
        ▼
server.py (one process, 127.0.0.1:8765)
  ├─ appends PCM to sessions/<id>/audio.pcm, state in meta.json
  ├─ transcription worker thread (one job at a time, models stay loaded)
  │    live:   when ≥20 s of new audio, transcribe [cursor → now] (60 s max window)
  │            commit all segments except the last; move the cursor to the start of the last one
  │            (the unfinished sentence is re-transcribed next pass, so words aren't cut at boundaries)
  │    final:  on Stop, transcribe the rest and commit everything
  │    refine: optional full re-pass with `medium`, which replaces the segments (live version kept as backup)
  └─ exports: .txt / .srt / .md, audio as .m4a (via ffmpeg) or .wav fallback
```

## 4. API

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/config` | Models in use |
| GET / POST | `/api/sessions` | List sessions / create one |
| GET / DELETE | `/api/sessions/:id` | Transcript, status and backlog / delete a session |
| POST | `/api/sessions/:id/audio?seq=N` | Upload a raw PCM chunk (duplicate uploads are ignored, so retries are safe) |
| POST | `/api/sessions/:id/status` | Mark the session paused or recording |
| POST | `/api/sessions/:id/finish` | Flush and finalize |
| POST | `/api/sessions/:id/refine` | Re-transcribe with `medium` |
| POST | `/api/sessions/:id/rename` | Rename a session |
| GET | `/api/sessions/:id/export.{txt,srt,md}` | Download the transcript |
| GET | `/api/sessions/:id/audio.file` | Download the audio |

## 5. Files

```
ListenToMe/
├─ PLAN.md
├─ server.py               # HTTP server, session store, transcription engine
├─ requirements.txt        # faster-whisper, numpy
├─ start.sh                # creates .venv if missing, starts the server, opens the browser
├─ .gitignore
├─ .venv/                  # Python 3.13 + faster-whisper 1.2.1 (installed)
├─ sessions/<id>/          # audio.pcm, meta.json, audio.m4a (created at runtime)
└─ public/
   ├─ index.html
   ├─ app.js               # recorder, upload queue with retry, polling, UI
   ├─ recorder-worklet.js  # mic → 16 kHz Int16 frames + level meter
   └─ styles.css           # light/dark theme, responsive, keyboard-accessible
```

## 6. Features

- Start / Pause / Stop, a timer (counts only recorded time), a mic level meter and a recording indicator
- Microphone picker, remembered between visits (use **MacBook Pro Microphone**, not "Microsoft Teams Audio Device")
- Transcript that fills in as you talk, with timestamps; the in-progress sentence is shown in grey italics
- Keeps the screen awake while recording, warns before you close the tab, and retries failed uploads (audio stays queued in the tab)
- Session history: open, finalize unfinished sessions, delete
- Export TXT, SRT or Markdown, copy to clipboard, download the audio
- "Improve accuracy" button that re-runs the whole session with `medium`, with a progress bar
- Recovery after a server restart: unfinished finalizing is resumed; an interrupted refine is marked so you can retry it

## 7. Status

| # | Step | Status |
|---|---|---|
| 1 | System check and benchmark of cached models | ✅ Done |
| 2 | Project `.venv` with faster-whisper | ✅ Done |
| 3 | `server.py` (API, storage, live/final/refine engine, exports) | ✅ Done, passes the smoke test |
| 4 | Frontend (`index.html`, `app.js`, worklet, styles) | ✅ Written. Verified with a simulated mic (start, stop, timer, final transcript, exports, Improve accuracy). **Not yet seen in the UI:** live lines while recording, the grey partial line, pause/resume. Check these in step 7. |
| 5 | `start.sh`, `requirements.txt`, `.gitignore` | ✅ Written |
| 6 | Automated smoke test: `.venv/bin/python tests/smoke_test.py` (synthetic speech, paced and burst upload, finish, exports, refine) | ✅ Passing |
| 7 | Manual browser test with your real microphone in Chrome (the simulated-mic run passed) | ⏳ Needs you |
| 8 | 60-minute test: watch memory, backlog (`transcribed to` vs `recorded`) and wake lock | ⏳ |
| 9 | Fixes so far: `[hidden]` buttons were still visible; favicon 404 | ✅ Done, more may come from steps 7–8 |

**Known issue:** the end time of a segment that is followed by a long silence can stretch over the silence (seen at 31.9 s instead of about 27 s). Start times are correct, but SRT caption durations can be too long. A fix may be word-level timestamps.

## 8. Settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `LTM_PORT` | `8765` | Server port |
| `LTM_LIVE_MODEL` | `base.en` | Live model |
| `LTM_REFINE_MODEL` | `medium` | Model for the "Improve accuracy" pass |
| `LTM_LANGUAGE` | `en` | Language for multilingual models (`auto` to detect) |
| `LTM_CPU_THREADS` | `8` | Whisper CPU threads |
| `HF_HUB_OFFLINE` | `1` | Set to `0` to allow downloading models that aren't cached |

## 9. Risks & mitigations

- **Laptop sleeps or the tab is closed:** the app holds a wake lock, warns before closing, and audio already uploaded is safe on disk.
- **Network or server hiccup:** chunks are numbered, retried with backoff, and the server ignores duplicates.
- **Transcription falls behind:** the backlog is shown in the transcript header, and `base.en` has about 6× headroom.
- **Whisper invents text during silence:** voice detection, plus `condition_on_previous_text=False`.
- **Microphone access needs a secure page:** `http://localhost` counts as secure.

## 10. Later (optional)

Speaker labels (diarization), AI summary and action items, search across sessions, edit the transcript in place, Living Design UI, install as an app (PWA).

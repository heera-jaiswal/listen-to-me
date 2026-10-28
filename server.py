#!/usr/bin/env python3
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import traceback
import uuid
import wave
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
SESSIONS_DIR = Path(os.environ.get("LTM_SESSIONS_DIR", ROOT / "sessions"))
HOST = os.environ.get("LTM_HOST", "127.0.0.1")
PORT = int(os.environ.get("LTM_PORT", "8765"))
LIVE_MODEL = os.environ.get("LTM_LIVE_MODEL", "base.en")
REFINE_MODEL = os.environ.get("LTM_REFINE_MODEL", "medium")
LANGUAGE = os.environ.get("LTM_LANGUAGE", "en")
CPU_THREADS = int(os.environ.get("LTM_CPU_THREADS", "8"))

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2
MIN_WINDOW_SEC = 20.0
RETRY_STEP_SEC = 10.0
MAX_WINDOW_SEC = 60.0
MAX_UPLOAD_BYTES = 4 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
ID_RE = re.compile(r"^[0-9a-f]{32}$")
ROUTE_RE = re.compile(r"^/api/sessions/([^/]+)(?:/([a-z0-9.]+))?$")
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_ts(sec, srt=False):
    ms = int(round(max(sec, 0) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}" if srt else f"{h:02d}:{m:02d}:{s:02d}"


def slugify(text):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug[:60] or "transcript"


class Transcriber:
    def __init__(self):
        self._models = {}
        self._lock = threading.Lock()

    def _model(self, name):
        if name not in self._models:
            from faster_whisper import WhisperModel

            print(f"[whisper] loading model '{name}'…", flush=True)
            self._models[name] = WhisperModel(
                name, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS
            )
            print(f"[whisper] model '{name}' ready", flush=True)
        return self._models[name]

    def warm(self, name):
        with self._lock:
            self._model(name)

    def transcribe(self, audio, model_name, on_progress=None):
        if model_name.endswith(".en"):
            language = "en"
        else:
            language = None if LANGUAGE == "auto" else LANGUAGE
        with self._lock:
            segments, _ = self._model(model_name).transcribe(
                audio,
                language=language,
                beam_size=5,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,
            )
            out = []
            for seg in segments:
                text = seg.text.strip()
                if text:
                    out.append({"start": float(seg.start), "end": float(seg.end), "text": text})
                if on_progress:
                    on_progress(float(seg.end))
            return out


class Session:
    def __init__(self, path, meta):
        self.dir = path
        self.meta = meta
        self.lock = threading.RLock()
        self.queued = False
        self.deleted = False

    @property
    def pcm_path(self):
        return self.dir / "audio.pcm"

    def audio_sec(self):
        return self.meta["bytes"] / BYTES_PER_SEC

    def save(self):
        tmp = self.dir / "meta.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=1))
        os.replace(tmp, self.dir / "meta.json")

    def read_audio(self, start, end):
        first = int(start * SAMPLE_RATE)
        count = max(int(end * SAMPLE_RATE) - first, 0)
        with open(self.pcm_path, "rb") as f:
            f.seek(first * 2)
            raw = f.read(count * 2)
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def summary(self):
        m = self.meta
        preview = " ".join(s["text"] for s in m["segments"][:3])[:120]
        return {
            "id": m["id"],
            "title": m["title"],
            "createdAt": m["createdAt"],
            "status": m["status"],
            "durationSec": round(self.audio_sec(), 1),
            "segmentCount": len(m["segments"]),
            "preview": preview,
        }

    def detail(self):
        m = dict(self.meta)
        m.pop("liveSegments", None)
        audio = self.audio_sec()
        m["audioSec"] = round(audio, 1)
        m["transcribedSec"] = round(m["cursor"], 1)
        m["backlogSec"] = round(max(audio - m["cursor"], 0), 1)
        m["liveModel"] = LIVE_MODEL
        m["refineModel"] = REFINE_MODEL
        return m


class Store:
    def __init__(self):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sessions = {}
        for meta_path in SESSIONS_DIR.glob("*/meta.json"):
            try:
                meta = json.loads(meta_path.read_text())
                if ID_RE.match(meta.get("id", "")):
                    self._sessions[meta["id"]] = Session(meta_path.parent, meta)
            except (OSError, ValueError) as exc:
                print(f"[store] skipping {meta_path}: {exc}", flush=True)

    def all(self):
        with self._lock:
            return list(self._sessions.values())

    def get(self, sid):
        if not ID_RE.match(sid or ""):
            return None
        with self._lock:
            return self._sessions.get(sid)

    def create(self, title):
        sid = uuid.uuid4().hex
        path = SESSIONS_DIR / sid
        path.mkdir(parents=True)
        (path / "audio.pcm").touch()
        meta = {
            "id": sid,
            "title": title or f"Session {datetime.now().strftime('%b %d, %Y %I:%M %p')}",
            "createdAt": now_iso(),
            "finishedAt": None,
            "status": "recording",
            "sampleRate": SAMPLE_RATE,
            "bytes": 0,
            "nextSeq": 0,
            "cursor": 0.0,
            "lastTriedEnd": 0.0,
            "segments": [],
            "partial": "",
            "refinedWith": None,
            "refineProgress": None,
            "error": None,
        }
        session = Session(path, meta)
        session.save()
        with self._lock:
            self._sessions[sid] = session
        return session

    def delete(self, session):
        with self._lock:
            self._sessions.pop(session.meta["id"], None)
        with session.lock:
            session.deleted = True
        shutil.rmtree(session.dir, ignore_errors=True)


def plan_commit(segs, start, end, final):
    abs_segs = [
        {"start": round(start + s["start"], 2), "end": round(start + s["end"], 2), "text": s["text"]}
        for s in segs
    ]
    window_full = (end - start) >= MAX_WINDOW_SEC - 0.5
    if final:
        return abs_segs, end, ""
    if not abs_segs:
        return [], max(start, end - 1.0), ""
    if len(abs_segs) >= 2:
        commit, cursor, partial = abs_segs[:-1], abs_segs[-1]["start"], abs_segs[-1]["text"]
    elif window_full:
        commit, cursor, partial = abs_segs, abs_segs[0]["end"], ""
    else:
        commit, cursor, partial = [], start, abs_segs[0]["text"]
    if window_full and cursor - start < 1.0:
        return abs_segs, end, ""
    return commit, cursor, partial


class Engine:
    def __init__(self, transcriber):
        self.t = transcriber
        self.jobs = queue.Queue()
        threading.Thread(target=self._run, name="transcriber", daemon=True).start()

    def maybe_schedule_live(self, s):
        with s.lock:
            m = s.meta
            if s.queued or s.deleted or m["status"] not in ("recording", "paused"):
                return
            audio = s.audio_sec()
            if audio - m["cursor"] >= MIN_WINDOW_SEC and audio - m["lastTriedEnd"] >= RETRY_STEP_SEC:
                s.queued = True
                self.jobs.put(("live", s))

    def schedule(self, kind, s):
        self.jobs.put((kind, s))

    def _run(self):
        while True:
            kind, s = self.jobs.get()
            try:
                if not s.deleted:
                    getattr(self, f"_{kind}")(s)
            except Exception as exc:
                traceback.print_exc()
                with s.lock:
                    s.meta["error"] = f"{kind} transcription failed: {exc}"
                    if kind in ("final", "refine"):
                        s.meta["status"] = "done"
                        s.meta["refineProgress"] = None
                    if not s.deleted:
                        s.save()
            finally:
                if kind == "live":
                    with s.lock:
                        s.queued = False
                    self.maybe_schedule_live(s)

    def _apply(self, s, commit, cursor, partial, tried_end):
        with s.lock:
            if s.deleted:
                return
            s.meta["segments"].extend(commit)
            s.meta["cursor"] = round(max(cursor, s.meta["cursor"]), 2)
            s.meta["partial"] = partial
            s.meta["lastTriedEnd"] = round(tried_end, 2)
            s.meta["error"] = None
            s.save()

    def _live(self, s):
        with s.lock:
            start = s.meta["cursor"]
            audio_end = s.audio_sec()
        end = min(audio_end, start + MAX_WINDOW_SEC)
        if end - start < 1.0:
            return
        segs = self.t.transcribe(s.read_audio(start, end), LIVE_MODEL)
        commit, cursor, partial = plan_commit(segs, start, end, final=False)
        self._apply(s, commit, cursor, partial, end)

    def _final(self, s):
        while True:
            with s.lock:
                if s.deleted:
                    return
                start = s.meta["cursor"]
                audio_end = s.audio_sec()
            if audio_end - start < 0.3:
                break
            end = min(audio_end, start + MAX_WINDOW_SEC)
            last = end >= audio_end
            segs = self.t.transcribe(s.read_audio(start, end), LIVE_MODEL)
            commit, cursor, partial = plan_commit(segs, start, end, final=last)
            self._apply(s, commit, cursor, partial, end)
            if last:
                break
        with s.lock:
            if s.deleted:
                return
            s.meta["status"] = "done"
            s.meta["partial"] = ""
            s.meta["finishedAt"] = now_iso()
            s.save()

    def _refine(self, s):
        with s.lock:
            duration = s.audio_sec()
            s.meta["refineProgress"] = 0.0
            s.save()
        audio = s.read_audio(0, duration)

        def progress(t):
            with s.lock:
                s.meta["refineProgress"] = round(min(t / duration, 1.0), 3) if duration else 1.0

        segs = self.t.transcribe(audio, REFINE_MODEL, progress)
        with s.lock:
            if s.deleted:
                return
            if not s.meta.get("liveSegments"):
                s.meta["liveSegments"] = s.meta["segments"]
            s.meta["segments"] = [
                {"start": round(x["start"], 2), "end": round(x["end"], 2), "text": x["text"]} for x in segs
            ]
            s.meta["refinedWith"] = REFINE_MODEL
            s.meta["refineProgress"] = None
            s.meta["status"] = "done"
            s.meta["error"] = None
            s.save()


def render_export(meta, fmt):
    segs = meta["segments"]
    if fmt == "txt":
        return "\n".join(f"[{fmt_ts(s['start'])}] {s['text']}" for s in segs) + "\n"
    if fmt == "srt":
        blocks = [
            f"{i}\n{fmt_ts(s['start'], True)} --> {fmt_ts(s['end'], True)}\n{s['text']}\n"
            for i, s in enumerate(segs, 1)
        ]
        return "\n".join(blocks)
    duration = fmt_ts(meta["bytes"] / BYTES_PER_SEC)
    lines = [f"# {meta['title']}", "", f"*Recorded {meta['createdAt']} · Duration {duration}*", ""]
    lines += [f"**[{fmt_ts(s['start'])}]** {s['text']}  " for s in segs]
    return "\n".join(lines) + "\n"


def build_audio_file(session):
    pcm = session.pcm_path
    if shutil.which("ffmpeg"):
        out = session.dir / "audio.m4a"
        if not out.exists() or out.stat().st_mtime < pcm.stat().st_mtime:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                 "-i", str(pcm), "-c:a", "aac", "-b:a", "64k", str(out)],
                check=True,
            )
        return out, "audio/mp4", "m4a"
    out = session.dir / "audio.wav"
    if not out.exists() or out.stat().st_mtime < pcm.stat().st_mtime:
        with wave.open(str(out), "wb") as w, open(pcm, "rb") as src:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            while chunk := src.read(1 << 20):
                w.writeframes(chunk)
    return out, "audio/wav", "wav"


class Handler(BaseHTTPRequestHandler):
    server_version = "ListenToMe/1.0"
    store: Store = None
    engine: Engine = None

    def log_message(self, fmt, *args):
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode())

    def _error(self, code, message, **extra):
        self._json(code, {"error": message, **extra})

    def _read_body(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > limit:
            raise ValueError("body too large")
        return self.rfile.read(length) if length else b""

    def _read_json(self):
        raw = self._read_body(MAX_JSON_BYTES)
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def _send_file(self, path, ctype, download_name=None):
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        if download_name:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(download_name)}")
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def _route(self):
        path = urlparse(self.path).path
        match = ROUTE_RE.match(path)
        if not match:
            return path, None, None
        session = self.store.get(match.group(1))
        return path, session, match.group(2)

    def do_GET(self):
        path, session, action = self._route()
        if path == "/api/config":
            return self._json(200, {"liveModel": LIVE_MODEL, "refineModel": REFINE_MODEL, "sampleRate": SAMPLE_RATE})
        if path == "/favicon.ico":
            return self._send(204)
        if path == "/api/sessions":
            items = sorted((s.summary() for s in self.store.all()), key=lambda x: x["createdAt"], reverse=True)
            return self._json(200, items)
        if path.startswith("/api/"):
            if session is None:
                return self._error(404, "Session not found")
            with session.lock:
                meta = json.loads(json.dumps(session.meta))
                detail = session.detail()
            if action is None:
                return self._json(200, detail)
            if action in ("export.txt", "export.srt", "export.md"):
                fmt = action.split(".")[1]
                ctype = {"txt": "text/plain", "srt": "application/x-subrip", "md": "text/markdown"}[fmt]
                body = render_export(meta, fmt).encode()
                name = f"{slugify(meta['title'])}.{fmt}"
                return self._send(200, body, f"{ctype}; charset=utf-8",
                                  {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
            if action == "audio.file":
                if meta["bytes"] == 0:
                    return self._error(404, "No audio recorded")
                try:
                    with session.lock:
                        out, ctype, ext = build_audio_file(session)
                except subprocess.CalledProcessError:
                    return self._error(500, "ffmpeg failed to build the audio file")
                return self._send_file(out, ctype, f"{slugify(meta['title'])}.{ext}")
            return self._error(404, "Unknown action")
        return self._static(path)

    def do_POST(self):
        path, session, action = self._route()
        try:
            if path == "/api/sessions":
                data = self._read_json()
                title = str(data.get("title") or "").strip()[:120]
                return self._json(201, self.store.create(title).detail())
            if session is None:
                self._read_body(MAX_UPLOAD_BYTES)
                return self._error(404, "Session not found")
            if action == "audio":
                return self._upload(session)
            data = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, str(exc))

        if action == "status":
            status = data.get("status")
            if status not in ("recording", "paused"):
                return self._error(400, "status must be 'recording' or 'paused'")
            with session.lock:
                if session.meta["status"] not in ("recording", "paused"):
                    return self._error(409, "Session is no longer recording")
                session.meta["status"] = status
                session.save()
                return self._json(200, session.detail())
        if action == "finish":
            with session.lock:
                if session.meta["status"] not in ("recording", "paused"):
                    return self._json(200, session.detail())
                session.meta["status"] = "finishing"
                session.save()
                detail = session.detail()
            self.engine.schedule("final", session)
            return self._json(202, detail)
        if action == "refine":
            with session.lock:
                if session.meta["status"] != "done":
                    return self._error(409, "Finish the session before refining")
                if session.meta["bytes"] == 0:
                    return self._error(409, "No audio recorded")
                session.meta["status"] = "refining"
                session.meta["refineProgress"] = 0.0
                session.save()
                detail = session.detail()
            self.engine.schedule("refine", session)
            return self._json(202, detail)
        if action == "rename":
            title = str(data.get("title") or "").strip()[:120]
            if not title:
                return self._error(400, "title is required")
            with session.lock:
                session.meta["title"] = title
                session.save()
                return self._json(200, session.detail())
        return self._error(404, "Unknown action")

    def _upload(self, session):
        try:
            seq = int(parse_qs(urlparse(self.path).query).get("seq", [""])[0])
        except ValueError:
            self._read_body(MAX_UPLOAD_BYTES)
            return self._error(400, "seq query parameter is required")
        try:
            data = self._read_body(MAX_UPLOAD_BYTES)
        except ValueError as exc:
            return self._error(413, str(exc))
        if not data or len(data) % 2:
            return self._error(400, "Body must be 16-bit mono PCM")
        with session.lock:
            if session.deleted or session.meta["status"] not in ("recording", "paused"):
                return self._error(409, "Session is no longer recording")
            if seq < session.meta["nextSeq"]:
                return self._json(200, {"nextSeq": session.meta["nextSeq"], "duplicate": True})
            with open(session.pcm_path, "ab") as f:
                f.write(data)
            session.meta["bytes"] += len(data)
            session.meta["nextSeq"] = seq + 1
            session.save()
            result = {"nextSeq": session.meta["nextSeq"], "audioSec": round(session.audio_sec(), 1)}
        self.engine.maybe_schedule_live(session)
        return self._json(200, result)

    def do_DELETE(self):
        _, session, action = self._route()
        if session is None or action is not None:
            return self._error(404, "Session not found")
        with session.lock:
            if session.meta["status"] in ("finishing", "refining"):
                return self._error(409, "Wait for transcription to finish before deleting")
        self.store.delete(session)
        return self._send(204)

    def _static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (PUBLIC / name).resolve()
        if target.parent != PUBLIC.resolve() or not target.is_file():
            return self._error(404, "Not found")
        ctype = STATIC_TYPES.get(target.suffix, "application/octet-stream")
        return self._send(200, target.read_bytes(), ctype)


def recover(store, engine):
    for s in store.all():
        status = s.meta["status"]
        if status == "finishing":
            engine.schedule("final", s)
        elif status == "refining":
            s.meta["status"] = "done"
            s.meta["refineProgress"] = None
            s.meta["error"] = "Refinement was interrupted by a server restart. You can run it again."
            s.save()


def main():
    store = Store()
    transcriber = Transcriber()
    engine = Engine(transcriber)
    recover(store, engine)

    def warm():
        try:
            transcriber.warm(LIVE_MODEL)
        except Exception as exc:
            print(f"[whisper] could not load '{LIVE_MODEL}': {exc}\n"
                  "          Make sure the model is cached, or set HF_HUB_OFFLINE=0 to allow a download.", flush=True)

    threading.Thread(target=warm, daemon=True).start()
    Handler.store = store
    Handler.engine = engine
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    print(f"ListenToMe running at http://localhost:{PORT}  (live: {LIVE_MODEL}, refine: {REFINE_MODEL})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()

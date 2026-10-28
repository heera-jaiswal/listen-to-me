#!/usr/bin/env python3
"""End-to-end smoke test: synthetic speech -> chunk upload -> live -> finish -> exports -> refine.

Run:  .venv/bin/python tests/smoke_test.py [--no-refine]
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("LTM_TEST_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"
CHUNK = 160000  # 5 s of 16 kHz int16
SPEEDUP = float(os.environ.get("LTM_TEST_SPEEDUP", "4"))

SENTENCES = [
    "The quick brown fox jumps over the lazy dog near the river bank.",
    "Yesterday I walked to the market and bought three apples and a loaf of bread.",
    "Our meeting has been moved to Thursday afternoon at half past two.",
    "Please remember to submit the quarterly report before the end of the month.",
    "The weather forecast says it will rain heavily over the weekend.",
    "She opened the window and listened to the birds singing in the garden.",
    "Engineers measured the bridge twice before approving the final design.",
    "A cup of hot coffee is the best way to start a cold morning.",
    "The library closes at nine o'clock on weekdays and at five on Sundays.",
    "He practiced the piano every evening until the melody felt natural.",
    "The train to Boston departs from platform four in ten minutes.",
    "We decided to plant tomatoes, basil, and peppers in the backyard this spring.",
    "The museum exhibits ancient pottery from several different civilizations.",
    "Remember to charge your laptop before the long flight to Seattle.",
    "The chef carefully seasoned the soup with rosemary and a pinch of salt.",
    "Volunteers gathered early on Saturday to clean the beach.",
    "The new software update fixes several security vulnerabilities.",
    "My grandmother taught me how to knit a warm scarf last winter.",
    "The conference keynote focused on renewable energy and battery storage.",
    "A gentle breeze carried the scent of jasmine across the courtyard.",
]
GAP_AFTER = 5  # insert 5 s of silence after this sentence index
KEYWORDS = ["fox", "market", "Thursday", "quarterly", "weather", "window", "bridge",
            "coffee", "library", "piano", "Boston", "tomatoes", "museum", "Seattle", "rosemary",
            "beach", "security", "scarf", "keynote", "jasmine"]

failures = []


def check(cond, label, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail and not cond else ""), flush=True)
    if not cond:
        failures.append(label)


def call(method, path, body=None, raw=False, expect=None):
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None and not isinstance(body, bytes):
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            payload, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        payload, code = e.read(), e.code
    if raw:
        return code, payload
    return code, (json.loads(payload) if payload else None)


def make_audio(tmp):
    pieces = []
    for i, text in enumerate(SENTENCES):
        aiff, raw = tmp / f"s{i}.aiff", tmp / f"s{i}.raw"
        subprocess.run(["say", "-v", "Samantha", "-r", "170", "-o", str(aiff), text], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff), "-f", "s16le",
                        "-ar", "16000", "-ac", "1", str(raw)], check=True)
        pieces.append(raw.read_bytes() + b"\x00\x00" * 16000 * 1)  # 1 s pause between sentences
        if i == GAP_AFTER:
            pieces.append(b"\x00\x00" * 16000 * 5)
    return b"".join(pieces)


def wait_server():
    for _ in range(60):
        try:
            if call("GET", "/api/config")[0] == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise SystemExit("server did not start")


def norm(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower())


def main():
    do_refine = "--no-refine" not in sys.argv
    tmp = Path(tempfile.mkdtemp(prefix="ltm-smoke-"))
    env = dict(os.environ, LTM_PORT=str(PORT), LTM_SESSIONS_DIR=str(tmp / "sessions"))
    log = open(tmp / "server.log", "w")
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_server()
        print("Generating speech…", flush=True)
        pcm = make_audio(tmp)
        dur = len(pcm) / 32000
        print(f"  audio: {dur:.1f}s", flush=True)

        print("Static + config", flush=True)
        check(call("GET", "/", raw=True)[0] == 200, "index.html served")
        for f in ("app.js", "styles.css", "recorder-worklet.js"):
            check(call("GET", f"/{f}", raw=True)[0] == 200, f"{f} served")
        check(call("GET", "/../server.py", raw=True)[0] == 404, "path traversal blocked")
        check(call("GET", "/api/config")[1]["liveModel"] == "base.en", "config")

        print("Create + upload", flush=True)
        code, sess = call("POST", "/api/sessions", {"title": "Smoke Test / ünïcode"})
        check(code == 201, "session created")
        sid = sess["id"]
        chunks = [pcm[i:i + CHUNK] for i in range(0, len(pcm), CHUNK)]
        saw_live = False
        for seq, ch in enumerate(chunks):
            code, r = call("POST", f"/api/sessions/{sid}/audio?seq={seq}", ch)
            if code != 200:
                check(False, f"upload seq {seq}", f"{code} {r}")
            if seq == 3:
                code, r = call("POST", f"/api/sessions/{sid}/audio?seq=3", ch)
                check(r and r.get("duplicate") is True, "duplicate seq ignored")
                check(call("POST", f"/api/sessions/{sid}/audio?seq=99", b"\x00")[0] == 400, "odd-length body rejected")
                check(call("POST", f"/api/sessions/{sid}/audio", ch)[0] == 400, "missing seq rejected")
            if seq == 4:
                call("POST", f"/api/sessions/{sid}/status", {"status": "paused"})
                check(call("GET", f"/api/sessions/{sid}")[1]["status"] == "paused", "pause works")
                call("POST", f"/api/sessions/{sid}/status", {"status": "recording"})
            time.sleep(5 / SPEEDUP)
            if len(call("GET", f"/api/sessions/{sid}")[1]["segments"]) > 0:
                saw_live = True
        t0 = time.time()
        while time.time() - t0 < 120:
            d = call("GET", f"/api/sessions/{sid}")[1]
            if d["segments"]:
                saw_live = True
            if d["backlogSec"] < 25:
                break
            time.sleep(1)
        check(saw_live, "segments appeared before finish (live transcription)")
        check(d["backlogSec"] < 25, "live backlog drained", f"{d['backlogSec']}s after {time.time() - t0:.0f}s")
        d = call("GET", f"/api/sessions/{sid}")[1]
        print(f"  backlog before finish: {d['backlogSec']}s, live segments: {len(d['segments'])}", flush=True)
        check(abs(d["audioSec"] - dur) < 0.2, "audio length matches", f"{d['audioSec']} vs {dur}")

        print("Finish", flush=True)
        code, _ = call("POST", f"/api/sessions/{sid}/finish")
        check(code == 202, "finish accepted")
        check(call("POST", f"/api/sessions/{sid}/audio?seq=999", b"\x00\x00")[0] == 409, "upload after finish -> 409")
        t0 = time.time()
        while time.time() - t0 < 300:
            d = call("GET", f"/api/sessions/{sid}")[1]
            if d["status"] == "done":
                break
            time.sleep(1)
        check(d["status"] == "done", "finalized", d["status"])
        check(not d.get("error"), "no error", str(d.get("error")))
        check("liveSegments" not in d, "liveSegments hidden")
        verify(d, dur, "live")

        print("Exports", flush=True)
        for fmt in ("txt", "srt", "md"):
            code, body = call("GET", f"/api/sessions/{sid}/export.{fmt}", raw=True)
            check(code == 200 and len(body) > 100, f"export.{fmt}")
            if fmt == "srt":
                check(b"-->" in body and body.startswith(b"1\n00:00:"), "srt format")
        code, body = call("GET", f"/api/sessions/{sid}/audio.file", raw=True)
        check(code == 200 and len(body) > 10000, "audio file", f"{code} {len(body)}")
        if shutil.which("ffprobe"):
            (tmp / "out.m4a").write_bytes(body)
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                  str(tmp / "out.m4a")], capture_output=True, text=True).stdout.strip()
            check(out and abs(float(out) - dur) < 1.5, "audio duration", f"{out} vs {dur}")
        check(call("POST", f"/api/sessions/{sid}/rename", {"title": " "})[0] == 400, "empty rename rejected")
        check(call("POST", f"/api/sessions/{sid}/rename", {"title": "Renamed"})[1]["title"] == "Renamed", "rename")
        check(any(s["id"] == sid for s in call("GET", "/api/sessions")[1]), "listed")

        if do_refine:
            print("Refine (medium)", flush=True)
            live_n = len(d["segments"])
            code, _ = call("POST", f"/api/sessions/{sid}/refine")
            check(code == 202, "refine accepted")
            check(call("DELETE", f"/api/sessions/{sid}")[0] == 409, "delete blocked while refining")
            t0 = time.time()
            while time.time() - t0 < 600:
                d = call("GET", f"/api/sessions/{sid}")[1]
                if d["status"] == "done":
                    break
                time.sleep(2)
            check(d["status"] == "done" and d["refinedWith"] == "medium", "refined", f"{d['status']} {d.get('refinedWith')} {d.get('error')}")
            verify(d, dur, "refined")
            print(f"  segments live={live_n} refined={len(d['segments'])}", flush=True)

        check(call("DELETE", f"/api/sessions/{sid}")[0] == 204, "delete")
        check(call("GET", f"/api/sessions/{sid}")[0] == 404, "gone after delete")
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        if failures:
            print("\n--- server log ---\n" + (tmp / "server.log").read_text()[-3000:])
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'ALL CHECKS PASSED'}")
    sys.exit(1 if failures else 0)


def verify(d, dur, tag):
    segs = d["segments"]
    text = " ".join(s["text"] for s in segs)
    hits = [k for k in KEYWORDS if k.lower() in text.lower()]
    check(len(hits) >= len(KEYWORDS) - 1, f"[{tag}] keyword recall {len(hits)}/{len(KEYWORDS)}", f"missing {set(KEYWORDS) - set(hits)}")
    starts = [s["start"] for s in segs]
    check(starts == sorted(starts), f"[{tag}] timestamps non-decreasing")
    check(all(0 <= s["start"] <= s["end"] <= dur + 1 for s in segs), f"[{tag}] timestamps within audio")
    norms = [norm(s["text"]) for s in segs]
    dups = [n for i, n in enumerate(norms) if len(n) > 15 and n in norms[:i]]
    check(not dups, f"[{tag}] no duplicated segments", str(dups[:2]))
    # no repeated sentence from boundary overlap
    for k in KEYWORDS:
        n = text.lower().count(k.lower())
        if n > 1:
            check(False, f"[{tag}] '{k}' appears {n}x (overlap duplication?)")
    # silence gap: sentences 0..5 duration; nothing should be timestamped inside the 5s gap
    print(f"  [{tag}] {len(segs)} segments; first: {segs[0]['text'][:60]!r}", flush=True)
    for s in segs:
        print(f"      {s['start']:7.2f}-{s['end']:7.2f}  {s['text']}")


if __name__ == "__main__":
    main()

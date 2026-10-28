const SAMPLE_RATE = 16000;
const UPLOAD_EVERY_SAMPLES = SAMPLE_RATE * 5;
const POLL_MS = 3000;
const MIC_KEY = 'ltm.mic';

const $ = (id) => document.getElementById(id);
const el = {
  engine: $('engine'), title: $('title'), mic: $('mic'),
  startBtn: $('start-btn'), pauseBtn: $('pause-btn'), stopBtn: $('stop-btn'),
  recDot: $('rec-dot'), timer: $('timer'), meter: $('meter'), meterBar: $('meter-bar'), status: $('status'),
  list: $('session-list'), listEmpty: $('session-empty'), newBtn: $('new-btn'),
  tMeta: $('t-meta'), transcript: $('transcript'), actions: $('actions'),
  progress: $('progress'), progressBar: $('progress-bar'),
  dlTxt: $('dl-txt'), dlSrt: $('dl-srt'), dlMd: $('dl-md'), dlAudio: $('dl-audio'),
  copyBtn: $('copy-btn'), finalizeBtn: $('finalize-btn'), refineBtn: $('refine-btn'), deleteBtn: $('delete-btn'),
};

const rec = {
  active: false, paused: false, session: null,
  ctx: null, stream: null, source: null, node: null,
  chunks: [], chunkSamples: 0, totalSamples: 0, seq: 0,
  queue: [], uploading: false, wakeLock: null,
};
let viewId = null;
let viewData = null;
let pollTimer = null;
let config = { liveModel: 'base.en', refineModel: 'medium' };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const isJson = res.headers.get('content-type')?.includes('application/json');
  const body = isJson ? await res.json() : null;
  if (!res.ok) {
    const err = new Error(body?.error || `${res.status} ${res.statusText}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

const postJson = (path, data) =>
  api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}) });

function fmt(sec) {
  const s = Math.max(0, Math.floor(sec));
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}

function setStatus(text, tone = '') {
  el.status.textContent = text;
  el.status.dataset.tone = tone;
}

async function loadMics() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  const devices = await navigator.mediaDevices.enumerateDevices();
  const inputs = devices.filter((d) => d.kind === 'audioinput' && d.deviceId && d.deviceId !== 'default');
  const current = el.mic.value || localStorage.getItem(MIC_KEY) || '';
  el.mic.replaceChildren(new Option('System default', ''));
  inputs.forEach((d, i) => el.mic.add(new Option(d.label || `Microphone ${i + 1}`, d.deviceId)));
  el.mic.value = inputs.some((d) => d.deviceId === current) ? current : '';
}

function updateButtons() {
  el.startBtn.disabled = rec.active;
  el.pauseBtn.disabled = !rec.active;
  el.stopBtn.disabled = !rec.active;
  el.pauseBtn.textContent = rec.paused ? 'Resume' : 'Pause';
  el.mic.disabled = rec.active;
  el.title.disabled = rec.active;
  el.newBtn.disabled = rec.active;
  el.recDot.dataset.state = rec.active ? (rec.paused ? 'paused' : 'live') : '';
  el.list.querySelectorAll('button').forEach((b) => { b.disabled = rec.active; });
}

function updateMeter(rms) {
  const level = Math.min(100, Math.round(Math.sqrt(rms) * 180));
  el.meterBar.style.width = `${level}%`;
  el.meter.setAttribute('aria-valuenow', String(level));
}

async function createAudioContext() {
  try {
    return new AudioContext({ sampleRate: SAMPLE_RATE });
  } catch {
    return new AudioContext();
  }
}

async function start() {
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
    setStatus('This browser cannot record here. Open http://localhost:8765 in Chrome.', 'error');
    return;
  }
  el.startBtn.disabled = true;
  setStatus('Requesting microphone…');
  try {
    const deviceId = el.mic.value;
    rec.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    await loadMics();

    rec.ctx = await createAudioContext();
    await rec.ctx.audioWorklet.addModule('recorder-worklet.js');
    rec.source = rec.ctx.createMediaStreamSource(rec.stream);
    rec.node = new AudioWorkletNode(rec.ctx, 'recorder', { processorOptions: { targetRate: SAMPLE_RATE } });
    rec.node.port.onmessage = onAudio;

    rec.session = await postJson('/api/sessions', { title: el.title.value.trim() });
    Object.assign(rec, { active: true, paused: false, chunks: [], chunkSamples: 0, totalSamples: 0, seq: 0, queue: [] });

    rec.source.connect(rec.node);
    rec.node.connect(rec.ctx.destination);
    if (rec.ctx.state === 'suspended') await rec.ctx.resume();

    rec.stream.getAudioTracks()[0]?.addEventListener('ended', () => {
      setStatus('Microphone disconnected. Saving what was recorded…', 'warn');
      stop();
    });

    await requestWakeLock();
    el.timer.textContent = fmt(0);
    viewId = rec.session.id;
    render(rec.session);
    updateButtons();
    refreshList();
    startPolling();
    const label = el.mic.selectedOptions[0]?.textContent || 'microphone';
    setStatus(`Listening on ${label}. Keep this tab open.`, 'live');
  } catch (err) {
    cleanupAudio();
    rec.active = false;
    updateButtons();
    const msg = err.name === 'NotAllowedError' ? 'Microphone permission was denied.' : err.message;
    setStatus(`Could not start: ${msg}`, 'error');
  }
}

function onAudio(event) {
  const { pcm, rms } = event.data;
  updateMeter(rec.paused ? 0 : rms);
  if (!rec.active || rec.paused) return;
  const samples = new Int16Array(pcm);
  rec.chunks.push(samples);
  rec.chunkSamples += samples.length;
  rec.totalSamples += samples.length;
  el.timer.textContent = fmt(rec.totalSamples / SAMPLE_RATE);
  if (rec.chunkSamples >= UPLOAD_EVERY_SAMPLES) flushChunk();
}

function flushChunk() {
  if (!rec.chunkSamples || !rec.session) return;
  const merged = new Int16Array(rec.chunkSamples);
  let offset = 0;
  for (const c of rec.chunks) {
    merged.set(c, offset);
    offset += c.length;
  }
  rec.chunks = [];
  rec.chunkSamples = 0;
  rec.queue.push({ seq: rec.seq++, data: merged.buffer, sessionId: rec.session.id });
  pumpUploads();
}

async function pumpUploads() {
  if (rec.uploading) return;
  rec.uploading = true;
  let delay = 1000;
  while (rec.queue.length) {
    const item = rec.queue[0];
    try {
      await api(`/api/sessions/${item.sessionId}/audio?seq=${item.seq}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: item.data,
      });
      rec.queue.shift();
      if (delay > 1000 && rec.active) setStatus('Connection restored. Listening…', 'live');
      delay = 1000;
    } catch (err) {
      if (err.status === 404 || err.status === 409) {
        rec.queue = [];
        setStatus('The server closed this session; audio upload stopped.', 'error');
        break;
      }
      setStatus(`Upload failed, retrying… (${rec.queue.length} chunk(s) waiting; audio is safe in this tab)`, 'warn');
      await sleep(delay);
      delay = Math.min(delay * 2, 15000);
    }
  }
  rec.uploading = false;
}

async function waitForUploads() {
  while (rec.queue.length || rec.uploading) {
    if (!rec.uploading) pumpUploads();
    await sleep(250);
  }
}

async function togglePause() {
  if (!rec.active) return;
  rec.paused = !rec.paused;
  if (rec.paused) flushChunk();
  updateButtons();
  setStatus(rec.paused ? 'Paused. Nothing is being recorded.' : 'Listening…', rec.paused ? 'warn' : 'live');
  try {
    await postJson(`/api/sessions/${rec.session.id}/status`, { status: rec.paused ? 'paused' : 'recording' });
  } catch { /* status is cosmetic; recording continues */ }
}

async function stop() {
  if (!rec.active) return;
  rec.active = false;
  rec.paused = false;
  flushChunk();
  cleanupAudio();
  updateButtons();
  el.stopBtn.disabled = true;
  el.startBtn.disabled = true;
  setStatus('Uploading remaining audio…');
  await waitForUploads();
  try {
    await postJson(`/api/sessions/${rec.session.id}/finish`);
    setStatus('Finishing transcript…');
  } catch (err) {
    setStatus(`Could not finalize: ${err.message}`, 'error');
  }
  releaseWakeLock();
  el.startBtn.disabled = false;
  refreshList();
  poll();
  startPolling();
}

function cleanupAudio() {
  try { rec.source?.disconnect(); } catch { /* already disconnected */ }
  try { rec.node?.disconnect(); } catch { /* already disconnected */ }
  if (rec.node) rec.node.port.onmessage = null;
  rec.stream?.getTracks().forEach((t) => t.stop());
  rec.ctx?.close().catch(() => {});
  Object.assign(rec, { source: null, node: null, stream: null, ctx: null });
  updateMeter(0);
}

async function requestWakeLock() {
  try {
    rec.wakeLock = await navigator.wakeLock?.request('screen');
  } catch { rec.wakeLock = null; }
}

function releaseWakeLock() {
  rec.wakeLock?.release().catch(() => {});
  rec.wakeLock = null;
}

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(poll, POLL_MS);
}

async function poll() {
  if (!viewId) return;
  const id = viewId;
  try {
    const data = await api(`/api/sessions/${id}`);
    if (id !== viewId) return;
    const wasBusy = viewData && ['finishing', 'refining'].includes(viewData.status);
    render(data);
    if (wasBusy && data.status === 'done') {
      refreshList();
      if (!rec.active) setStatus(data.refinedWith ? `Transcript improved with ${data.refinedWith}.` : 'Transcript ready.', 'ok');
    }
    const ownsRecording = rec.active && rec.session?.id === id;
    if (data.status === 'done' && !ownsRecording) clearInterval(pollTimer);
  } catch (err) {
    if (err.status === 404) {
      clearInterval(pollTimer);
      showEmpty();
    }
  }
}

function segmentNode(seg, partial = false) {
  const p = document.createElement('p');
  p.className = partial ? 'seg partial' : 'seg';
  const ts = document.createElement('span');
  ts.className = 'ts';
  ts.textContent = fmt(seg.start);
  const text = document.createElement('span');
  text.textContent = partial ? `${seg.text} …` : seg.text;
  p.append(ts, text);
  return p;
}

function describe(data) {
  const parts = [`${fmt(data.audioSec)} recorded`];
  switch (data.status) {
    case 'recording':
    case 'paused': {
      const lag = data.backlogSec;
      parts.push(lag > 0 ? `transcribed to ${fmt(data.transcribedSec)}` : 'transcript up to date');
      if (data.status === 'paused') parts.push('paused');
      break;
    }
    case 'finishing':
      parts.push('finishing transcript…');
      break;
    case 'refining':
      parts.push(`improving with ${data.refineModel}… ${Math.round((data.refineProgress || 0) * 100)}%`);
      break;
    default:
      parts.push(`${data.segments.length} segments`);
      parts.push(data.refinedWith ? `model: ${data.refinedWith}` : `model: ${data.liveModel}`);
  }
  return parts.join(' · ');
}

function render(data) {
  viewData = data;
  const box = el.transcript;
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const nodes = data.segments.map((s) => segmentNode(s));
  if (data.partial) nodes.push(segmentNode({ start: data.transcribedSec, text: data.partial }, true));
  if (!nodes.length) {
    const p = document.createElement('p');
    p.className = 'empty muted';
    p.textContent = ['recording', 'paused'].includes(data.status)
      ? 'Listening… the first lines appear after about 20–30 seconds of speech.'
      : data.status === 'finishing' ? 'Transcribing the last part…' : 'No speech was detected in this session.';
    nodes.push(p);
  }
  box.replaceChildren(...nodes);
  if (nearBottom) box.scrollTop = box.scrollHeight;

  document.getElementById('t-heading').textContent = data.title;
  el.tMeta.textContent = describe(data) + (data.error ? ` · ⚠ ${data.error}` : '');

  const refining = data.status === 'refining';
  el.progress.hidden = !refining;
  el.progressBar.style.width = `${Math.round((data.refineProgress || 0) * 100)}%`;

  const ownsRecording = rec.active && rec.session?.id === data.id;
  const base = `/api/sessions/${data.id}`;
  el.actions.hidden = false;
  el.dlTxt.href = `${base}/export.txt`;
  el.dlSrt.href = `${base}/export.srt`;
  el.dlMd.href = `${base}/export.md`;
  el.dlAudio.href = `${base}/audio.file`;
  const hasText = data.segments.length > 0;
  [el.dlTxt, el.dlSrt, el.dlMd].forEach((a) => a.toggleAttribute('aria-disabled', !hasText));
  el.dlAudio.toggleAttribute('aria-disabled', data.audioSec === 0);
  el.copyBtn.disabled = !hasText;
  el.finalizeBtn.hidden = ownsRecording || !['recording', 'paused'].includes(data.status);
  el.refineBtn.hidden = data.status !== 'done' || data.audioSec === 0 || data.refinedWith === data.refineModel;
  el.refineBtn.textContent = `Improve accuracy (${data.refineModel})`;
  el.deleteBtn.disabled = ownsRecording || ['finishing', 'refining'].includes(data.status);
}

function showEmpty() {
  viewId = null;
  viewData = null;
  el.actions.hidden = true;
  el.progress.hidden = true;
  document.getElementById('t-heading').textContent = 'Transcript';
  el.tMeta.textContent = '';
  const p = document.createElement('p');
  p.className = 'empty muted';
  p.innerHTML = 'Press <strong>Start</strong> and talk. Text appears every 20–30 seconds and is finalized when you press Stop.';
  el.transcript.replaceChildren(p);
  markSelected();
}

function markSelected() {
  el.list.querySelectorAll('button[data-id]').forEach((b) => {
    if (b.dataset.id === viewId) b.setAttribute('aria-current', 'true');
    else b.removeAttribute('aria-current');
  });
}

async function refreshList() {
  let items = [];
  try { items = await api('/api/sessions'); } catch { return; }
  el.listEmpty.hidden = items.length > 0;
  el.list.replaceChildren(...items.map((s) => {
    const li = document.createElement('li');
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'session-item';
    b.dataset.id = s.id;
    b.disabled = rec.active;
    const title = document.createElement('span');
    title.className = 'session-title';
    title.textContent = s.title;
    const meta = document.createElement('span');
    meta.className = 'session-meta';
    const when = new Date(s.createdAt).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    meta.textContent = `${when} · ${fmt(s.durationSec)}`;
    const badge = document.createElement('span');
    badge.className = `badge ${s.status}`;
    badge.textContent = s.status;
    b.append(title, meta, badge);
    b.addEventListener('click', () => openSession(s.id));
    li.append(b);
    return li;
  }));
  markSelected();
}

async function openSession(id) {
  if (rec.active) return;
  viewId = id;
  markSelected();
  await poll();
  if (viewData && viewData.status !== 'done') startPolling();
}

async function refine() {
  if (!viewData) return;
  const mins = Math.max(1, Math.round((viewData.audioSec / 60) * 0.75));
  if (!confirm(`Re-transcribe this session with the ${viewData.refineModel} model? It may take around ${mins} minute(s). The current transcript is kept until it finishes.`)) return;
  try {
    render(await postJson(`/api/sessions/${viewData.id}/refine`));
    setStatus(`Improving transcript with ${viewData.refineModel}…`);
    startPolling();
    refreshList();
  } catch (err) {
    setStatus(`Could not start: ${err.message}`, 'error');
  }
}

async function finalize() {
  if (!viewData) return;
  try {
    render(await postJson(`/api/sessions/${viewData.id}/finish`));
    startPolling();
    refreshList();
  } catch (err) {
    setStatus(`Could not finalize: ${err.message}`, 'error');
  }
}

async function remove() {
  if (!viewData || !confirm(`Delete “${viewData.title}” and its audio? This cannot be undone.`)) return;
  try {
    await api(`/api/sessions/${viewData.id}`, { method: 'DELETE' });
    showEmpty();
    refreshList();
    setStatus('Session deleted.');
  } catch (err) {
    setStatus(`Could not delete: ${err.message}`, 'error');
  }
}

async function copyTranscript() {
  if (!viewData) return;
  const text = viewData.segments.map((s) => `[${fmt(s.start)}] ${s.text}`).join('\n');
  try {
    await navigator.clipboard.writeText(text);
    setStatus('Transcript copied to clipboard.', 'ok');
  } catch {
    setStatus('Clipboard is not available.', 'error');
  }
}

function blockDisabledLinks(event) {
  if (event.currentTarget.hasAttribute('aria-disabled')) event.preventDefault();
}

async function init() {
  el.startBtn.addEventListener('click', start);
  el.pauseBtn.addEventListener('click', togglePause);
  el.stopBtn.addEventListener('click', stop);
  el.newBtn.addEventListener('click', () => { if (!rec.active) { clearInterval(pollTimer); showEmpty(); el.title.value = ''; el.title.focus(); } });
  el.refineBtn.addEventListener('click', refine);
  el.finalizeBtn.addEventListener('click', finalize);
  el.deleteBtn.addEventListener('click', remove);
  el.copyBtn.addEventListener('click', copyTranscript);
  [el.dlTxt, el.dlSrt, el.dlMd, el.dlAudio].forEach((a) => a.addEventListener('click', blockDisabledLinks));
  el.mic.addEventListener('change', () => localStorage.setItem(MIC_KEY, el.mic.value));
  navigator.mediaDevices?.addEventListener?.('devicechange', loadMics);

  document.addEventListener('visibilitychange', () => {
    if (rec.active && document.visibilityState === 'visible' && (!rec.wakeLock || rec.wakeLock.released)) requestWakeLock();
  });
  window.addEventListener('beforeunload', (e) => {
    if (rec.active || rec.queue.length) {
      e.preventDefault();
      e.returnValue = '';
    }
  });

  try {
    config = await api('/api/config');
    el.engine.textContent = `Runs locally · live: ${config.liveModel} · refine: ${config.refineModel}`;
  } catch {
    el.engine.textContent = 'Server not reachable';
  }
  await loadMics();
  await refreshList();
  updateButtons();
}

init();

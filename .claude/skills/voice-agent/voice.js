// Browser side of the voice agent: mic capture with endpointing, playback, and the transcript panel.
// Globals read by orb_sketch.js:
let outAnalyser = null, outDataArray = null;

const $ = (id) => document.getElementById(id);
const HEADERS = { 'X-Voice-Agent': '1' };
const VAD = { startMs: 220, startMsPlayback: 380, endMs: 850, prerollMs: 400, minVoicedMs: 300, maxMs: 60000 };

let outCtx = null, micCtx = null, micStream = null, micMuted = false;
let nextT = 0, sources = new Set(), minEpoch = 0, serverState = 'listening', ended = false;

// ─── panel ──────────────────────────────────────────────────────────────────
function add(cls, text) {
  const d = document.createElement('div');
  d.className = 'm ' + cls;
  d.textContent = text;
  $('log').appendChild(d);
  $('log').scrollTop = $('log').scrollHeight;
  return d;
}

// Minimal markdown for on-screen blocks, built from DOM nodes only: nothing from the model is ever parsed as HTML.
function inlineMd(parent, text) {
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*|__[^_]+__)|(\*[^*\s][^*]*\*|_[^_\s][^_]*_)|(\[[^\]]+\]\(https?:\/\/[^\s)]+\))|(https?:\/\/[^\s)<>"']+)/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    parent.append(text.slice(last, m.index));
    const s = m[0];
    let el;
    if (m[1]) { el = document.createElement('code'); el.textContent = s.slice(1, -1); }
    else if (m[2]) { el = document.createElement('strong'); inlineMd(el, s.slice(2, -2)); }
    else if (m[3]) { el = document.createElement('em'); inlineMd(el, s.slice(1, -1)); }
    else {
      const lm = m[4] && s.match(/^\[([^\]]+)\]\((.+)\)$/);
      el = document.createElement('a');
      el.href = lm ? lm[2] : s; el.textContent = lm ? lm[1] : s; el.target = '_blank'; el.rel = 'noopener noreferrer';
    }
    parent.append(el);
    last = m.index + s.length;
  }
  parent.append(text.slice(last));
}

function renderMd(container, text) {
  const lines = text.replace(/\r/g, '').split('\n');
  let i = 0, list = null, para = null;
  const endBlocks = () => { list = null; para = null; };
  const cells = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      endBlocks();
      const buf = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) buf.push(lines[i]);
      const pre = document.createElement('pre'); pre.textContent = buf.join('\n'); container.append(pre);
      i++; continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      endBlocks();
      const table = document.createElement('table');
      const tr = table.insertRow();
      for (const c of cells(line)) { const th = document.createElement('th'); inlineMd(th, c); tr.append(th); }
      for (i += 2; i < lines.length && /^\s*\|/.test(lines[i]); i++) {
        const r = table.insertRow();
        for (const c of cells(lines[i])) inlineMd(r.insertCell(), c);
      }
      container.append(table); continue;
    }
    let m;
    if ((m = line.match(/^\s*(#{1,6})\s+(.*)$/))) {
      endBlocks();
      const h = document.createElement('div'); h.className = 'h'; inlineMd(h, m[2]); container.append(h);
    } else if ((m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/))) {
      para = null;
      const tag = /\d/.test(m[2]) ? 'OL' : 'UL';
      if (!list || list.tagName !== tag) { list = document.createElement(tag); container.append(list); }
      const li = document.createElement('li'); inlineMd(li, m[3]);
      if (m[1].length >= 2) li.style.marginLeft = '1.2em';
      list.append(li);
    } else if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      endBlocks(); container.append(document.createElement('hr'));
    } else if (!line.trim()) {
      endBlocks();
    } else if (list && /^\s{2,}\S/.test(line)) {
      list.lastElementChild.append(' '); inlineMd(list.lastElementChild, line.trim());
    } else {
      list = null;
      if (!para) { para = document.createElement('p'); container.append(para); } else para.append(document.createElement('br'));
      inlineMd(para, line);
    }
    i++;
  }
}

function addScreen(text) {
  const d = add('screen', '');
  d.classList.add('md');
  renderMd(d, text);
  $('log').scrollTop = $('log').scrollHeight;
}

function setStateText() {
  const playing = isPlaying();
  $('state').textContent = paused ? 'session ended - press Start again'
    : !micCtx ? 'press start, then talk'
    : vad.inSpeech ? 'listening...'
    : transcribing ? 'transcribing...'
    : playing ? 'speaking - talk over me to interrupt'
    : serverState === 'approval' ? 'waiting for your approval'
    : serverState === 'thinking' ? 'Claude Code is working...'
    : micMuted ? 'mic muted' : 'listening';
}
setInterval(setStateText, 200);

function post(path, body) {
  return fetch(path, { method: 'POST', headers: { ...HEADERS, 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
}

// ─── playback ───────────────────────────────────────────────────────────────
function ensureOutput() {
  if (outCtx) return;
  outCtx = new AudioContext({ sampleRate: 24000 });
  outAnalyser = outCtx.createAnalyser();
  outAnalyser.fftSize = 256;
  outDataArray = new Uint8Array(outAnalyser.frequencyBinCount);
  outAnalyser.connect(outCtx.destination);
}

function isPlaying() { return !!outCtx && sources.size > 0; }

// ─── the talking face ───────────────────────────────────────────────────────
// With the face on and the lip-sync engine running, the server marks a sentence's audio `lip` and streams frames
// of the face saying it. Audio is the clock: a sentence's audio is held only until its first few frames are in,
// then plays exactly as before while the frame for the current moment is drawn. If frames fail or the face is
// off, the held audio simply plays, so speech is never lost and never waits long.
const LEAD_FRAMES = 4, HOLD_MAX_MS = 5000;
const talk = { wanted: false, order: [], held: new Map(), segs: [], canvas: null, expression: null };

const EMBED = window.top !== window.self;   // shown in a frame by Flight Deck (see the end of this file)
function faceOn() { if (EMBED) return true; try { return localStorage.getItem('va_face') !== 'off'; } catch (e) { return true; } }

let lipReady = false;   // the talking-face engine has loaded; until then replies play as plain audio
function askLipsync() {
  talk.wanted = faceOn();
  post('/lipsync', { on: talk.wanted }).then((r) => r.json()).then((d) => { lipReady = !!d.ready; }).catch(() => {});
}

function onAudio(ev) {
  if (!(ev.lip && talk.wanted && faceOn())) return playPcm(ev.pcm, ev.rate);
  let h = talk.held.get(ev.sid);
  if (!h) {
    h = { sid: ev.sid, chunks: [], frames: [], count: 0, ready: false, fps: 20, expression: null, at: performance.now() };
    talk.held.set(ev.sid, h); talk.order.push(ev.sid);
  }
  if (h.seg) h.seg.end = playPcm(ev.pcm, ev.rate).end;      // already speaking: late audio goes straight out
  else h.chunks.push(ev);
}

function onFaceFrame(ev) {
  const h = talk.held.get(ev.sid);
  if (!h) return;
  const im = new Image();
  im.src = 'data:image/jpeg;base64,' + ev.jpg;
  h.frames[ev.i] = im; h.fps = ev.fps; h.expression = ev.expression;
  if (++h.count >= LEAD_FRAMES) { h.ready = true; releaseHeld(); }
}

function onClipEnd(ev) {
  const h = talk.held.get(ev.sid);
  if (h) { h.ready = true; h.complete = true; releaseHeld(); }
}

// Sentences are spoken strictly in order: each waits for its first frames, then the next may follow.
function releaseHeld() {
  while (talk.order.length) {
    const h = talk.held.get(talk.order[0]);
    if (!h) { talk.order.shift(); continue; }
    if (!h.seg) {
      if (!h.ready && performance.now() - h.at < HOLD_MAX_MS) return;
      let t0 = null, end = 0;
      for (const c of h.chunks) { const r = playPcm(c.pcm, c.rate); if (t0 === null) t0 = r.start; end = r.end; }
      h.chunks = [];
      h.seg = { t0, end, h };
      talk.segs.push(h.seg);
    }
    if (!h.complete && performance.now() - h.at < 60000) return;   // its audio or frames may still be arriving
    talk.order.shift();                                             // frames stay reachable through its segment
    talk.held.delete(h.sid);
  }
}
setInterval(() => { if (talk.order.length) releaseHeld(); }, 150);

const BLEND_MS = 450;
function drawTalk() {
  requestAnimationFrame(drawTalk);
  if (!outCtx || !talk.segs.length) { if (talk.canvas) talk.canvas.classList.remove('on'); return; }
  const now = outCtx.currentTime;
  while (talk.segs.length > 1 && now >= talk.segs[1].t0) talk.segs.shift();
  const seg = talk.segs[0];
  if (now > seg.end + 0.12) {
    talk.segs.shift();
    if (!talk.segs.length && talk.canvas) talk.canvas.classList.remove('on');
    return;
  }
  if (now < seg.t0) return;
  if (!talk.canvas) {
    const host = document.getElementById('portrait');
    if (!host) return;
    talk.canvas = document.createElement('canvas'); talk.canvas.id = 'talk'; talk.canvas.width = talk.canvas.height = 512;
    host.appendChild(talk.canvas);
  }
  const frames = seg.h.frames;
  let i = Math.min(frames.length - 1, Math.floor((now - seg.t0) * seg.h.fps));
  while (i >= 0 && !(frames[i] && frames[i].complete && frames[i].naturalWidth)) i--;   // late frame: hold the last one
  if (i < 0) return;
  // A sentence in another expression is another portrait: blend into it over a moment instead of cutting.
  const ctx = talk.canvas.getContext('2d');
  if (talk.drawnExpression && talk.drawnExpression !== seg.h.expression && +getComputedStyle(talk.canvas).opacity > 0.05) {   // still visible, even while fading out
    if (!talk.prev) { talk.prev = document.createElement('canvas'); talk.prev.width = talk.prev.height = 512; }
    talk.prev.getContext('2d').drawImage(talk.canvas, 0, 0);
    talk.blendAt = performance.now();
    if (window.voiceFace) voiceFace.morph();
  }
  talk.drawnExpression = seg.h.expression;
  const p = talk.blendAt ? (performance.now() - talk.blendAt) / (window.voiceFace && voiceFace.fx ? 1800 : BLEND_MS) : 1;
  if (p < 1) {
    ctx.globalAlpha = 1; ctx.drawImage(talk.prev, 0, 0);
    ctx.globalAlpha = p * p * (3 - 2 * p); ctx.drawImage(frames[i], 0, 0, 512, 512); ctx.globalAlpha = 1;
    seg.drawn = -1;                                        // drawn blended: draw it again plainly once the blend is over
  } else if (seg.drawn !== i) { talk.blendAt = 0; ctx.drawImage(frames[i], 0, 0, 512, 512); seg.drawn = i; }
  talk.expression = seg.h.expression;
  talk.canvas.classList.add('on');
}
requestAnimationFrame(drawTalk);

function playPcm(b64, rate) {
  ensureOutput();
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const pcm = new Int16Array(bytes.buffer, 0, bytes.length >> 1);
  if (!pcm.length) return { start: nextT, end: nextT };
  const buf = outCtx.createBuffer(1, pcm.length, rate);
  const ch = buf.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
  const src = outCtx.createBufferSource();
  src.buffer = buf;
  src.connect(outAnalyser);
  nextT = Math.max(nextT, outCtx.currentTime + 0.06);
  const start = nextT;
  src.start(nextT);
  nextT += buf.duration;
  sources.add(src);
  src.onended = () => sources.delete(src);
  return { start, end: nextT };
}

function flushPlayback() {
  for (const s of sources) { try { s.onended = null; s.stop(); } catch (e) {} }
  sources.clear();
  nextT = 0;
  talk.order.length = 0; talk.held.clear(); talk.segs.length = 0;
  if (talk.canvas) talk.canvas.classList.remove('on');
  if (outCtx) outCtx.resume();
}

// ─── events from the server ─────────────────────────────────────────────────
const events = new EventSource('/events');
events.onmessage = (e) => {
  const ev = JSON.parse(e.data);
  switch (ev.type) {
    case 'hello':
      minEpoch = ev.epoch;
      $('voice').replaceChildren(...ev.voices.map((v) => new Option(v, v, false, v === ev.voice)));
      showSession(ev.session); showCost(ev.cost_total); showYolo(ev.yolo);
      askLipsync();
      if (ev.agent) { document.title = ev.agent + ' - Voice Agent'; agentName = ev.agent; showSession(ev.session); }
      break;
    case 'lipsync': lipReady = !!ev.ready; break;
    case 'yolo': showYolo(ev.on); add('notice', ev.on ? 'YOLO on - tools run without asking.' : 'YOLO off - approvals are back.'); break;
    case 'session': showSession(ev.id); break;
    case 'switched':
      $('log').replaceChildren();
      add('notice', ev.id ? (ev.fork ? 'Resumed a copy of session ' : 'Resumed session ') + ev.id.slice(0, 8) + '. Its history is loaded; say something to continue.' : 'Started a new session.');
      showSession(ev.id); serverState = 'listening';
      break;
    case 'state': serverState = ev.value; break;
    case 'user': add('user', ev.text); break;
    case 'spoken': if (ev.epoch >= minEpoch) add(ev.fixed ? 'fixed' : 'said', ev.text); break;
    case 'screen': addScreen(ev.text); break;
    case 'tool': add('tool', ev.status + ' (' + ev.name.replace(/^mcp__/, '') + ')'); break;
    case 'audio': if (ev.epoch >= minEpoch) onAudio(ev); break;
    case 'faceset': if (talk.canvas) talk.canvas.classList.remove('on'); if (window.voiceFace) voiceFace.reload(ev.name); $('faceset').value = ev.name; break;
    case 'frame': if (ev.epoch >= minEpoch) onFaceFrame(ev); break;
    case 'clip_done': case 'clip_fail': if (ev.epoch >= minEpoch) onClipEnd(ev); break;
    case 'flush': minEpoch = ev.epoch; flushPlayback(); break;
    case 'speech_end': case 'turn_end':
      if (ev.cost_total != null) showCost(ev.cost_total);
      if (ev.type === 'turn_end' || !isPlaying()) serverState = 'listening';
      break;
    case 'perm': addPerm(ev); break;
    case 'perm_done': document.querySelector(`[data-perm="${CSS.escape(ev.id)}"]`)?.remove(); add('notice', ev.allow ? 'Approved.' : 'Declined.'); break;
    case 'notice': add('notice', ev.text); break;
    case 'error': add('err', ev.text); break;
    case 'ended': finish(ev.session_id); break;
    case 'paused': pauseSession(ev.session_id); break;
  }
};
events.onerror = () => { if (!ended) $('state').textContent = 'connection to the local server lost'; };

let yolo = false;
function showYolo(on) { yolo = !!on; $('yolo').classList.toggle('on', yolo); $('yolo').textContent = yolo ? 'YOLO on' : 'YOLO off'; }

let agentName = '';
function showSession(id) { $('engine').textContent = (agentName || 'Claude Code') + (id ? ' ' + id.slice(0, 8) : ''); }
function showCost(c) { if (c) $('cost').textContent = 'reported $' + Number(c).toFixed(2); }

function addPerm(ev) {
  if (document.querySelector(`[data-perm="${CSS.escape(ev.id)}"]`)) return;
  const d = document.createElement('div');
  d.className = 'perm'; d.dataset.perm = ev.id;
  const title = document.createElement('div');
  title.textContent = 'Approve: ' + ev.spoken + (ev.mode === 'click' ? ' (click only)' : ' (say yes or no)');
  const pre = document.createElement('pre'); pre.textContent = ev.detail;
  const row = document.createElement('div'); row.className = 'row';
  const ok = document.createElement('button'); ok.className = 'ok'; ok.textContent = 'Approve';
  const no = document.createElement('button'); no.textContent = 'Decline';
  ok.onclick = () => post('/permission', { id: ev.id, allow: true });
  no.onclick = () => post('/permission', { id: ev.id, allow: false });
  row.append(ok, no); d.append(title, pre, row);
  $('log').appendChild(d);
  $('log').scrollTop = $('log').scrollHeight;
}

// End session keeps the page and the server: the mic and Claude Code stop, and Start again picks the conversation up.
let paused = false;
function pauseSession(sessionId) {
  if (paused) return;                                    // told twice: by the reply to End and by the server's event
  paused = true;
  stopMic();
  flushPlayback();
  add('notice', 'Session ended. Press Start again to pick it up' + (sessionId ? ', or continue in the terminal with: claude --resume ' + sessionId : '') + '.');
  $('state').textContent = 'session ended';
  $('start').textContent = 'Start again'; $('start').hidden = false; $('mute').hidden = true; $('end').hidden = true;
}
async function wake() {
  if (!paused) return;
  paused = false;
  $('end').hidden = false; $('start').textContent = 'Start talking';
  await post('/wake');
}

function finish(sessionId) {
  ended = true;
  stopMic();
  flushPlayback();
  events.close();
  add('notice', 'Session ended.' + (sessionId ? ' Continue in the terminal with: claude --resume ' + sessionId : ''));
  $('state').textContent = 'session ended';
}

// ─── mic and endpointing ────────────────────────────────────────────────────
const vad = { inSpeech: false, floor: 0.008, above: 0, below: 0, voiced: 0, frames: [], preroll: [], prerollMs: 0, total: 0, startedDuringPlayback: false };
let transcribing = false;
let inflight = 0;

async function startMic() {
  ensureOutput();
  await outCtx.resume();
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  micCtx = new AudioContext();
  await micCtx.audioWorklet.addModule('/mic_worklet.js');
  const node = new AudioWorkletNode(micCtx, 'mic-capture');
  node.port.onmessage = (e) => onFrame(e.data);
  micCtx.createMediaStreamSource(micStream).connect(node);
  $('start').hidden = true; $('mute').hidden = false;
}

function stopMic() {
  micStream?.getTracks().forEach((t) => t.stop());
  micCtx?.close();
  micCtx = null;
}

function reportSpeaking(active) {
  fetch('/speaking', { method: 'POST', headers: { ...HEADERS, 'Content-Type': 'application/json' }, body: JSON.stringify({ active }) }).catch(() => {});
}

function onFrame(f) {
  if (micMuted || ended || !micCtx) return;             // a frame that arrives after the mic was closed
  const ms = f.length / micCtx.sampleRate * 1000;
  let sum = 0;
  for (let i = 0; i < f.length; i++) sum += f[i] * f[i];
  const rms = Math.sqrt(sum / f.length);
  const playing = isPlaying();
  const threshold = playing ? Math.max(0.045, vad.floor * 6) : Math.max(0.013, vad.floor * 3);

  if (!vad.inSpeech) {
    if (rms < threshold) vad.floor += (rms - vad.floor) * 0.03;
    vad.preroll.push(f); vad.prerollMs += ms;
    while (vad.prerollMs > VAD.prerollMs) vad.prerollMs -= vad.preroll.shift().length / micCtx.sampleRate * 1000;
    vad.above = rms > threshold ? vad.above + ms : 0;
    if (vad.above >= (playing ? VAD.startMsPlayback : VAD.startMs)) {
      vad.inSpeech = true;
      reportSpeaking(true);
      vad.startedDuringPlayback = playing;
      vad.frames = vad.preroll.slice(); vad.total = vad.prerollMs; vad.voiced = vad.above; vad.below = 0;
      vad.preroll = []; vad.prerollMs = 0;
      // Pause rather than stop: if this turns out to be noise or echo, playback carries on.
      if (playing) outCtx.suspend();
    }
    return;
  }
  vad.frames.push(f); vad.total += ms;
  if (rms > threshold * 0.7) { vad.voiced += ms; vad.below = 0; } else vad.below += ms;
  if (vad.below >= VAD.endMs || vad.total >= VAD.maxMs) endUtterance();
}

async function endUtterance() {
  const { frames, voiced, startedDuringPlayback: during } = vad;
  const rate = micCtx.sampleRate;
  vad.inSpeech = false; vad.frames = []; vad.above = 0; vad.total = 0;
  if (voiced < VAD.minVoicedMs) { if (!inflight) reportSpeaking(false); if (during) outCtx.resume(); return; }
  inflight++;
  transcribing = true;
  let result = { action: 'ignored' };
  try {
    const resp = await fetch('/utterance', { method: 'POST', headers: { ...HEADERS, 'Content-Type': 'audio/wav', 'X-During-Playback': during ? '1' : '0' }, body: encodeWav(frames, rate) });
    result = await resp.json();
  } catch (e) { add('err', 'Could not reach the local server.'); }
  transcribing = false;
  inflight--;
  // The server's wait for the rest of a sentence starts only once nothing is being said or transcribed.
  if (!inflight && !vad.inSpeech) reportSpeaking(false);
  if (during && result.action !== 'accepted') outCtx.resume();
}

function encodeWav(frames, fromRate) {
  const toRate = 16000, ratio = fromRate / toRate;
  const total = frames.reduce((n, f) => n + f.length, 0);
  const merged = new Float32Array(total);
  let off = 0;
  for (const f of frames) { merged.set(f, off); off += f.length; }
  const n = Math.floor(total / ratio);
  const buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); str(8, 'WAVEfmt '); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, toRate, true); v.setUint32(28, toRate * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, 'data'); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    const a = Math.floor(i * ratio), b = Math.min(Math.floor((i + 1) * ratio), total);
    let s = 0;
    for (let j = a; j < b; j++) s += merged[j];
    s = Math.max(-1, Math.min(1, s / Math.max(1, b - a)));
    v.setInt16(44 + i * 2, s < 0 ? s * 32768 : s * 32767, true);
  }
  return new Blob([buf], { type: 'audio/wav' });
}

// ─── controls ───────────────────────────────────────────────────────────────
$('start').onclick = () => wake().then(startMic).catch((e) => add('err', 'Microphone unavailable: ' + e.message));
$('mute').onclick = () => { micMuted = !micMuted; $('mute').classList.toggle('on', micMuted); const l = micMuted ? 'Unmute mic' : 'Mute mic'; $('mute').title = l; $('mute').setAttribute('aria-label', l); };
// the page ends on the server's answer, not only on its event: a dropped event stream must not leave the mic open
$('end').onclick = () => post('/pause').then((r) => { if (!r.ok) throw 0; return r.json(); }).then((d) => pauseSession(d.session_id)).catch(() => { $('state').textContent = 'the local server is gone: run /voice-agent again'; });
$('yolo').onclick = () => post('/yolo', { on: !yolo });
$('toterm').onclick = async () => {
  $('toterm').disabled = true;
  try {
    const r = await post('/terminal');
    const d = await r.json().catch(() => ({}));
    if (r.status === 404) add('err', 'This needs the voice server restarted once: run /voice-agent resume.');
    else if (!r.ok || !d.ok) add('err', 'Could not open iTerm. ' + (d.error || ''));
    else add('notice', 'Continuing in iTerm. This voice session is ended.');
  } catch (e) { add('err', 'The local server is gone: run /voice-agent again.'); }
  $('toterm').disabled = false;
};
$('voice').onchange = () => post('/voice', { voice: $('voice').value });

// ─── ambient: time of day, day of the month, weather ───────────────────────
// Colour keys through the day as [hour offset from sunrise or sunset, hue, saturation %, lightness %, strength].
const ambient = { weather: null };
const WMO = [[0, 'clear'], [1, 'mostly clear'], [2, 'partly cloudy'], [3, 'overcast'], [48, 'fog'], [57, 'drizzle'], [67, 'rain'], [77, 'snow'], [82, 'showers'], [86, 'snow showers'], [99, 'thunderstorm']];
const hm = (s, dflt) => { const m = /^(\d\d):(\d\d)$/.exec(s || ''); return m ? +m[1] + m[2] / 60 : dflt; };
function ambientColour(now) {
  const w = ambient.weather, h = now.getHours() + now.getMinutes() / 60;
  const rise = hm(w && w.sunrise, 7), set = hm(w && w.sunset, 18.5), noon = (rise + set) / 2;
  const keys = [[0, 232, 60, 34, .42], [rise - 1, 250, 55, 36, .44], [rise, 18, 85, 54, .52], [rise + 1.5, 200, 72, 54, .46], [noon, 206, 70, 58, .46],
                [set - 1.5, 40, 88, 55, .52], [set, 350, 78, 52, .56], [set + 1, 275, 58, 40, .48], [24, 232, 60, 34, .42]];
  let i = 0;
  while (i < keys.length - 2 && h >= keys[i + 1][0]) i++;
  const a = keys[i], b = keys[i + 1], t = Math.min(1, Math.max(0, (h - a[0]) / Math.max(.01, b[0] - a[0])));
  const dh = ((b[1] - a[1] + 540) % 360) - 180;   // hue takes the short way round
  let hue = (a[1] + dh * t + 360) % 360, sat = a[2] + (b[2] - a[2]) * t, lig = a[3] + (b[3] - a[3]) * t, str = a[4] + (b[4] - a[4]) * t;
  if (w) {
    const cloud = Math.min(100, Math.max(0, w.cloud || 0)) / 100, code = w.code || 0;
    sat *= 1 - .4 * cloud;                                                // cloud drains the colour
    if (code >= 51 && code <= 86) { hue += ((((215 - hue) + 540) % 360) - 180) * .6; lig *= .85; }   // rain and snow pull it to blue-grey
    if (code >= 95) { hue = 268; sat = 45; }                              // thunderstorm: violet
  }
  return { hue, sat, lig, str };
}
function drawAmbient() {
  const now = new Date(), c = ambientColour(now), w = ambient.weather, root = document.documentElement.style;
  root.setProperty('--amb', `hsla(${c.hue.toFixed(0)}, ${c.sat.toFixed(0)}%, ${c.lig.toFixed(0)}%, ${c.str.toFixed(2)})`);
  root.setProperty('--ambtop', `hsla(${c.hue.toFixed(0)}, ${c.sat.toFixed(0)}%, ${c.lig.toFixed(0)}%, ${(c.str * .22).toFixed(2)})`);
  root.setProperty('--ambline', `hsl(${c.hue.toFixed(0)}, ${Math.max(25, c.sat).toFixed(0)}%, 68%)`);
  const days = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
  document.querySelector('#month i').style.width = (100 * now.getDate() / days).toFixed(1) + '%';
  const chip = $('ambchip');
  chip.textContent = '';
  const b = document.createElement('b');
  b.textContent = now.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }) + ' · ' + now.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  chip.appendChild(b);
  let tail = ' · day ' + now.getDate() + ' of ' + days;
  if (w && w.ok) tail += ' · ' + w.place + ' ' + Math.round(w.temp) + '° ' + (WMO.find(([max]) => (w.code || 0) <= max) || [0, ''])[1];
  chip.appendChild(document.createTextNode(tail));
}
async function loadWeather() {
  try {
    const r = await fetch('/ambient');
    const d = r.ok ? await r.json() : null;
    ambient.weather = d && d.ok ? d : null;   // an older server has no /ambient: time and date still show
  } catch (e) { ambient.weather = null; }
  drawAmbient();
}
drawAmbient(); loadWeather();
setInterval(drawAmbient, 30000); setInterval(loadWeather, 15 * 60000);

// ─── session picker ─────────────────────────────────────────────────────────
function ago(t) {
  const m = Math.round((Date.now() / 1000 - t) / 60);
  return m < 60 ? m + ' min ago' : m < 1440 ? Math.round(m / 60) + ' h ago' : Math.round(m / 1440) + ' d ago';
}

function sessionRow(title, detail, onPick, current, tag) {
  const d = document.createElement('div');
  d.className = 's' + (current ? ' cur' : '');
  const t = document.createElement('div'); t.className = 't'; t.textContent = title;
  if (tag) { const g = document.createElement('span'); g.className = 'tag'; g.textContent = tag; t.append(g); }
  const s = document.createElement('div'); s.className = 'd'; s.textContent = detail;
  d.append(t, s);
  d.onclick = onPick;
  return d;
}

async function openSessions() {
  const box = $('sessions');
  if (!box.hidden) { box.hidden = true; return; }
  box.replaceChildren(sessionRow('Loading...', '', () => {}));
  box.hidden = false;
  let data;
  try { data = await (await fetch('/sessions')).json(); } catch (e) { box.replaceChildren(sessionRow('Could not load sessions', '', () => {})); return; }
  const pick = (id) => async () => {
    box.hidden = true;
    flushPlayback();
    const r = await post('/resume', { id });
    if (!r.ok) add('err', 'Could not switch session.');
  };
  const head = document.createElement('div');
  head.className = 'd'; head.style.padding = '6px 10px';
  head.textContent = 'Sessions of agent: ' + (agentName || 'this folder');
  box.replaceChildren(head,
    sessionRow('+ New ' + (agentName || '') + ' session', 'Start fresh, no history', pick(null)),
    ...data.sessions.map((s) => sessionRow(s.title || s.id.slice(0, 8),
      ago(s.mtime) + ' · ' + s.turns + ' msgs' + (s.last && s.last !== s.title ? ' · ' + s.last : ''),
      pick(s.id), s.id === data.current, (agentName ? agentName + ' · ' : '') + (s.voice ? 'voice' : 'terminal'))));
}
fetch('/facesets').then((r) => r.json()).then((d) => {
  $('faceset').replaceChildren(...d.sets.map((s) => new Option(s === 'default' ? 'man' : s, s, false, s === d.current)));
}).catch(() => {});
$('faceset').onchange = () => post('/faceset', { name: $('faceset').value });
$('sessbtn').onclick = openSessions;
document.addEventListener('click', (e) => { if (!$('sessions').hidden && !e.target.closest('#sessions, #sessbtn')) $('sessions').hidden = true; });

// ─── embedded in Flight Deck ────────────────────────────────────────────────
// The server only lets one local origin frame this page (--embed-origin), so the parent is that page.
// It gets a start / stop button and hotkeys: commands come in as messages, and the conversation
// state and any key pressed in here go back out, so its hotkeys work wherever the focus is.
if (EMBED) {
  document.documentElement.classList.add('embed');
  try { localStorage.setItem('va_face', 'on'); } catch (e) {}
  const HOST = (location.ancestorOrigins && location.ancestorOrigins[0]) || '*';
  const tell = (m) => window.parent.postMessage(Object.assign({ fd: true }, m), HOST);
  const talking = () => !!micCtx && !paused;
  let starting = false;
  window.addEventListener('message', (e) => {
    if (e.source !== window.parent || (HOST !== '*' && e.origin !== HOST) || !e.data || !e.data.fd) return;
    // a skin of the host page: its look (CSS under html.skin-<name>), and optionally the character's name and face
    if (typeof e.data.skin === 'string' && /^[a-z]{1,20}$/.test(e.data.skin)) document.documentElement.classList.add('skin-' + e.data.skin);
    if (typeof e.data.name === 'string') $('mood').dataset.name = e.data.name.slice(0, 40);
    if (typeof e.data.face === 'string' && /^[a-z]{1,20}$/.test(e.data.face)) fetch('/facesets').then((r) => r.json())
      .then((d) => { if (d.sets.includes(e.data.face) && d.current !== e.data.face) post('/faceset', { name: e.data.face }); }).catch(() => {});
    if (e.data.cmd === 'start' && !talking() && !starting) {
      starting = true;
      wake().then(startMic).catch((err) => add('err', 'Microphone unavailable: ' + err.message)).finally(() => { starting = false; });
    } else if (e.data.cmd === 'stop' && talking()) $('end').onclick();
  });
  let last = '';
  setInterval(() => {
    const now = JSON.stringify([talking(), $('state').textContent, ended, lipReady]);
    if (now !== last) { last = now; tell({ talking: talking(), state: $('state').textContent, ended, lips: lipReady }); }
  }, 250);
  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest('input, textarea, select')) return;
    tell({ key: e.key });
  });
  tell({ ready: true });
}

// The face: a photoreal portrait per expression (faces/<set>/<name>.jpg, made by make_faces.py), crossfaded.
// Which portrait shows is decided in two layers:
//   - what the session is doing, measured here: listening to him, working, waiting for approval
//   - the expression the model declared for its reply (<face>NAME</face>, parsed by voice_server.py)
// Around it, a trail of what Claude Code is actually doing while it works.
// Reads from voice.js: events, vad, serverState, isPlaying, micCtx. From orb_sketch.js: audioState.
(() => {
  const NAMES = ['neutral', 'attentive', 'curious', 'thinking', 'focused', 'amused', 'delighted', 'warm', 'excited',
    'surprised', 'skeptical', 'puzzled', 'concerned', 'serious', 'confident', 'sheepish', 'playful', 'calm'];
  // Colour of the light around the portrait, per expression (hue in degrees).
  const HUE = { neutral: 212, attentive: 205, curious: 262, thinking: 250, focused: 218, amused: 38, delighted: 44,
    warm: 28, excited: 332, surprised: 188, skeptical: 22, puzzled: 280, concerned: 6, serious: 222, confident: 160,
    sheepish: 318, playful: 300, calm: 195 };
  const STATE_HUE = { reading: 210, scanning: 210, writing: 42, searching: 158, recalled: 158, connecting: 158,
    browsing: 330, delegating: 268, skill: 165, working: 30 };
  const VERB = { reading: 'reading', scanning: 'scanning', writing: 'writing', searching: 'searching memory',
    recalled: 'recalled', connecting: 'tracing connections', browsing: 'on the web', delegating: 'delegating',
    skill: 'running', working: 'working' };

  // ---- elements: the portrait (two stacked images to crossfade), its ring of light, and a canvas for the trail
  const wrap = document.createElement('div'); wrap.id = 'portrait';
  const ring = document.createElement('div'); ring.id = 'ring';
  const imgs = [new Image(), new Image()];
  imgs.forEach((im) => { im.alt = ''; im.draggable = false; wrap.appendChild(im); });
  const cv = document.createElement('canvas'); cv.id = 'face';
  document.body.append(ring, wrap, cv);
  const ctx = cv.getContext('2d');
  const orbCanvas = () => document.querySelector('canvas:not(#face):not(#talk)');

  let loaded = {}, ver = '';
  const src = (n) => '/faces/' + n + '.jpg' + ver;
  function preload() {
    const mine = loaded = {};
    for (const n of NAMES) { const im = new Image(); im.onload = () => { mine[n] = true; }; im.src = src(n); }
  }
  preload();
  // Another avatar was picked: fetch its portraits and let the next frame show them.
  function reload(setName) { ver = '?s=' + encodeURIComponent(setName); preload(); shown = null; setFx(setName); }

  // ---- a face of dust (sets named in FX): the portraits stay stills, and a live layer moves around and over them:
  // stars that drift and twinkle, dust that streams off the edges, slow haze, and a faint waver of the face itself.
  // Calm when idle; it stirs with work and brightens with the voice.
  const FX = { stardust: 1 };
  let fx = false, fxE = 0, morphAt = -1e9;
  const disp = () => warp.querySelector('feDisplacementMap');
  // One expression becoming another: the face loosens into haze, changes inside it, and gathers again.
  function morph() { if (fx) morphAt = performance.now(); }
  const stars = [], dust = [], haze = [];
  const warp = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  warp.setAttribute('width', 0); warp.setAttribute('height', 0); warp.style.position = 'absolute';
  warp.innerHTML = '<filter id="dustwarp" x="-8%" y="-8%" width="116%" height="116%"><feTurbulence type="fractalNoise" baseFrequency="0.011 0.016" numOctaves="2" seed="7" result="n"/>' +
    '<feDisplacementMap in="SourceGraphic" in2="n" scale="7" xChannelSelector="R" yChannelSelector="G"/></filter>';
  document.body.appendChild(warp);
  const turb = warp.querySelector('feTurbulence');
  function setFx(name) {
    fx = !!FX[name];
    document.documentElement.classList.toggle('fx-dust', fx);
    stars.length = dust.length = haze.length = 0;
    if (!fx) return;
    for (let i = 0; i < 110; i++) stars.push({ r: Math.sqrt(Math.random()) * .66, a: Math.random() * 6.283, w: (Math.random() - .5) * .016,
      p: Math.random() * 6.283, tw: .5 + Math.random() * 1.6, s: .5 + Math.random() * 1.1, h: 200 + Math.random() * 70 });
    for (let i = 0; i < 6; i++) haze.push({ a: Math.random() * 6.283, w: (Math.random() < .5 ? -1 : 1) * (.03 + Math.random() * .04), r: .46 + Math.random() * .12,
      s: .16 + Math.random() * .08, h: 205 + Math.random() * 65, p: Math.random() * 6.283 });
  }
  fetch('/facesets').then((r) => r.json()).then((d) => setFx(d.current)).catch(() => {});
  function drawDust(L, dt, nowMs, alphaAll, energy) {
    const t = nowMs / 1000, d = L.d;
    const mp = (nowMs - morphAt) / 2400, mo = mp > 0 && mp < 1 ? Math.sin(mp * Math.PI) ** 1.5 : 0;   // 0 → 1 → 0 across a change
    energy += mo * 1.4;
    fxE += (energy - fxE) * Math.min(1, dt * 1.5);
    disp().setAttribute('scale', (7 + mo * 20).toFixed(1));
    ctx.save(); ctx.globalCompositeOperation = 'lighter';
    for (const z of haze) {                                   // slow clouds passing over the edges of the face
      z.a += z.w * dt * (1 + fxE);
      const x = L.cx + Math.cos(z.a) * z.r * d, y = L.cy + Math.sin(z.a) * z.r * d * .95, rad = z.s * d * (1 + Math.sin(t / 3 + z.p) * .12);
      const g = ctx.createRadialGradient(x, y, 0, x, y, rad), al = (.035 + mo * .05) * alphaAll;
      g.addColorStop(0, `hsla(${z.h},80%,62%,${al})`); g.addColorStop(1, `hsla(${z.h},80%,50%,0)`);
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, rad, 0, 7); ctx.fill();
    }
    for (const s of stars) {                                  // stars: a slow turn, each with its own twinkle
      s.a += s.w * dt * (1 + fxE * 1.5);
      const tw = .5 + .5 * Math.sin(t * s.tw + s.p), x = L.cx + Math.cos(s.a) * s.r * d, y = L.cy + Math.sin(s.a) * s.r * d + Math.sin(t / 4 + s.p) * 3;
      const inside = s.r < .34 ? .45 : 1;                     // quieter over the face itself
      ctx.fillStyle = `hsla(${s.h},85%,86%,${(.15 + tw * .7) * inside * alphaAll})`;
      ctx.beginPath(); ctx.arc(x, y, s.s * (.6 + tw * .6), 0, 7); ctx.fill();
    }
    const want = 70 + fxE * 90;                               // dust leaving the edges, a little upward
    while (dust.length < want) {
      const a = Math.random() * 6.283, r = (.26 + Math.random() * .2) * d;
      dust.push({ x: Math.cos(a) * r, y: Math.sin(a) * r * .95, vx: Math.cos(a) * (3 + Math.random() * 7), vy: Math.sin(a) * (3 + Math.random() * 7) - 3 - Math.random() * 5,
        t: 0, life: 4 + Math.random() * 6, s: .5 + Math.random() * 1.3, h: 200 + Math.random() * 75, p: Math.random() * 6.283 });
    }
    for (let i = dust.length - 1; i >= 0; i--) {
      const m = dust[i]; m.t += dt;
      if (m.t > m.life) { dust.splice(i, 1); continue; }
      const sp = 1 + fxE * 2.2;
      m.x += (m.vx + Math.sin(t * .7 + m.p) * 5) * dt * sp; m.y += (m.vy + Math.cos(t * .6 + m.p) * 4) * dt * sp;
      const al = Math.min(1, m.t / 1.2) * Math.min(1, (m.life - m.t) / 2.2) * (.55 + fxE * .25) * alphaAll;
      ctx.fillStyle = `hsla(${m.h},85%,82%,${al})`;
      ctx.beginPath(); ctx.arc(L.cx + m.x, L.cy + m.y, m.s, 0, 7); ctx.fill();
    }
    ctx.restore();
  }

  let on = true;
  try { on = localStorage.getItem('va_face') !== 'off'; } catch (e) {}
  let declared = null, declaredAt = 0, declaredThisTurn = false, shown = null, top = 0;
  let level = 0, hue = 212, lastHue = null, vis = 0, lean = 0;
  const motes = [];
  let moteCount = 0, turnStart = 0, actions = 0, lastTurn = '', nowText = '';

  function show(name) {
    if (name === shown || !loaded[name]) return;
    if (shown) morph();
    shown = name;
    top = 1 - top;
    const im = imgs[top], other = imgs[1 - top];
    im.src = src(name);
    // Fade the new portrait in over the old one, and only once it is decoded, so nothing flashes or dips to black.
    const go = () => { if (imgs[top] !== im) return; im.style.zIndex = 2; other.style.zIndex = 1; im.style.opacity = 1;
      setTimeout(() => { if (imgs[top] === im) other.style.opacity = 0; }, fx ? 2300 : 950); };
    im.style.opacity = 0;
    (im.decode ? im.decode() : Promise.resolve()).then(go, go);
  }

  function setFace(name) {
    if (!NAMES.includes(name)) return;
    declared = name; declaredAt = performance.now(); declaredThisTurn = true;
  }

  // What the face should be showing right now, and the word under it.
  function current() {
    const now = performance.now();
    if (!micCtx && !declared) return ['calm', ''];
    if (vad.inSpeech) return ['attentive', 'listening'];
    if (serverState === 'approval') return ['curious', 'waiting for you'];
    if (serverState === 'thinking' && !declaredThisTurn && !isPlaying()) return [actions ? 'focused' : 'thinking', 'working'];
    // with the talking face, the expression is the one of the sentence being spoken right now
    if (talk.segs.length && talk.expression) { declared = talk.expression; declaredAt = now; return [declared, declared]; }
    if (declared && (isPlaying() || now - declaredAt < 14000)) return [declared, declared];
    return ['attentive', ''];   // an expression belongs to what was just said; afterwards the face settles
  }

  function addMote(state, text, sub) {
    const n = moteCount++, side = n % 2 ? 1 : -1, slot = (n >> 1) % 4;     // four fixed places a side, so labels never stack
    motes.push({ state, text: text.length > 52 ? text.slice(0, 51) + '…' : text, sub, t: 0, life: 11,
      a: -Math.PI / 2 + side * (0.5 + slot * 0.36) });
    while (motes.length > 8) motes.shift();
  }

  function onActivity(ev) {
    actions++;
    const verb = VERB[ev.state] || 'working';
    if (ev.notes && ev.notes.length) {
      ev.notes.forEach((n, i) => setTimeout(() => addMote('recalled', n, false), i * 220));
      nowText = 'recalled ' + ev.notes.length + ' notes · “' + ev.label + '”';
    } else {
      addMote(ev.state, (ev.sub ? 'subagent · ' : '') + verb + ' · ' + ev.label, ev.sub);
      nowText = (ev.sub ? 'subagent ' : '') + verb + ' · ' + ev.label;
    }
  }

  events.addEventListener('message', (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === 'face') setFace(ev.expression);
    else if (ev.type === 'activity') onActivity(ev);
    else if (ev.type === 'user') { turnStart = performance.now(); actions = 0; nowText = ''; lastTurn = ''; declaredThisTurn = false; }
    else if (ev.type === 'turn_end') {
      if (actions) lastTurn = actions + (actions === 1 ? ' action' : ' actions') + (ev.seconds ? ' · ' + Math.round(ev.seconds) + 's' : '');
      nowText = ''; turnStart = 0;
    } else if (ev.type === 'switched') { motes.length = 0; nowText = ''; lastTurn = ''; declared = null; }
  });

  function layout() {
    // In Flight Deck's frame the chat panel is narrower and sits on the LEFT, so the face area starts after it (x0).
    const w = innerWidth, h = innerHeight, emb = window.top !== window.self, wide = w > (emb ? 560 : 800);
    const pw = !emb ? 440 : w > 800 ? 380 : 300, sw = wide ? w - pw : w, x0 = emb && wide ? pw : 0;
    const d = wide ? Math.max(200, Math.min(sw * .6, h * .54, 520)) : Math.max(110, h * .24);
    return { cx: x0 + sw / 2, cy: wide ? h * .40 : h * .17, d, sw, wide, x0 };
  }

  function drawMotes(L, alphaAll) {
    ctx.save();
    ctx.font = '13px system-ui, sans-serif'; ctx.textBaseline = 'middle';
    for (const m of motes) {
      const fade = Math.min(1, m.t / .5) * Math.min(1, (m.life - m.t) / 2.5) * alphaAll;
      const r = L.d * (.66 + m.t * .004), x = L.cx + Math.cos(m.a) * r, y = L.cy + Math.sin(m.a) * r * .92;
      const h = STATE_HUE[m.state] ?? 210, right = Math.cos(m.a) > 0;
      const col = `hsla(${h},85%,74%,${fade})`;
      ctx.strokeStyle = `hsla(${h},85%,70%,${fade * .25})`; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(L.cx + Math.cos(m.a) * L.d * .5, L.cy + Math.sin(m.a) * L.d * .5); ctx.lineTo(x, y); ctx.stroke();
      ctx.fillStyle = col; ctx.shadowColor = col; ctx.shadowBlur = 10;
      ctx.beginPath(); ctx.arc(x, y, m.sub ? 2.2 : 3.2, 0, 7); ctx.fill();
      ctx.shadowBlur = 0; ctx.textAlign = right ? 'left' : 'right';
      const room = right ? L.x0 + L.sw - x - 18 : x - L.x0 - 18;
      let text = m.text;
      while (text.length > 8 && ctx.measureText(text).width > room) text = text.slice(0, -4) + '…';
      ctx.fillStyle = `hsla(${h},60%,88%,${fade * .9})`;
      ctx.fillText(text, x + (right ? 9 : -9), y);
    }
    ctx.restore();
  }

  let lastT = performance.now();
  function frame() {
    const nowMs = performance.now(), dt = Math.min(.05, (nowMs - lastT) / 1000); lastT = nowMs;
    const dpr = Math.min(2, devicePixelRatio || 1);
    if (cv.width !== Math.round(innerWidth * dpr) || cv.height !== Math.round(innerHeight * dpr)) { cv.width = innerWidth * dpr; cv.height = innerHeight * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    for (let i = motes.length - 1; i >= 0; i--) { motes[i].t += dt; if (motes[i].t > motes[i].life) motes.splice(i, 1); }

    const [name, word] = current();
    show(name);
    const ready = on && shown && loaded[shown];
    vis += ((ready ? 1 : 0) - vis) * Math.min(1, dt * 4);
    const L = layout();

    // light: colour follows the expression, or the kind of work while working
    const last = motes[motes.length - 1];
    const working = serverState === 'thinking' && !declaredThisTurn;
    const targetHue = working && last && last.t < 6 && STATE_HUE[last.state] != null ? STATE_HUE[last.state] : (HUE[shown] ?? 212);
    hue = (hue + (((targetHue - hue + 540) % 360) - 180) * Math.min(1, dt * 2.5) + 360) % 360;
    const amp = isPlaying() ? Math.min(1, (audioState.amp || 0) * 3.2) : 0;
    level += (amp - level) * Math.min(1, dt * (amp > level ? 26 : 9));
    const orb = orbCanvas(), hr = Math.round(hue);
    if (orb) orb.style.opacity = fx && on ? 0 : '';   // the round orb behind would show through a face with no hard edge
    if (orb && hr !== lastHue) { orb.style.filter = on ? `hue-rotate(${hr - 212}deg)` : ''; lastHue = on ? hr : null; }

    // the portrait breathes, leans in a little while listening, and its light follows the voice
    const breathe = Math.sin(nowMs / 2600) * .006;
    lean += ((vad.inSpeech ? .02 : 0) - lean) * Math.min(1, dt * 1.6);   // eased: a sudden size change reads as a jump cut
    const s = 1 + breathe + lean;
    wrap.style.cssText = `left:${L.cx - L.d / 2}px;top:${L.cy - L.d / 2}px;width:${L.d}px;height:${L.d}px;opacity:${vis};transform:scale(${s.toFixed(4)}) translateY(${(Math.sin(nowMs / 3300) * (fx ? 5 : 1.5)).toFixed(2)}px)` + (fx ? ` rotate(${(Math.sin(nowMs / 4700) * .5).toFixed(3)}deg);filter:url(#dustwarp) brightness(${(1 + level * .22).toFixed(3)})` : '');
    const glow = .16 + level * .5 + (vad.inSpeech ? .12 : 0) + (working ? .08 + Math.sin(nowMs / 500) * .05 : 0);
    const rd = L.d * .98;
    ring.style.cssText = `left:${L.cx - rd / 2}px;top:${L.cy - rd / 2}px;width:${rd}px;height:${rd}px;opacity:${fx ? 0 : vis};` +
      `box-shadow:0 0 ${Math.round(L.d * (.22 + level * .2))}px ${Math.round(L.d * (.02 + level * .05))}px hsla(${hr},90%,60%,${glow.toFixed(3)})`;
    if (fx && vis > .01) drawDust(L, dt, nowMs, vis, level * 1.6 + (working ? .7 : 0) + (vad.inSpeech ? .25 : 0));
    if (vis > .01) drawMotes(L, vis);

    const cap = document.getElementById('mood');
    if (cap) {
      const text = on && vis > .5 ? word : '';
      if (cap.textContent !== text) cap.textContent = text;
      cap.style.top = (L.cy + L.d / 2 + 6) + 'px'; cap.style.width = L.sw + 'px'; cap.style.left = L.x0 + 'px';
      cap.style.display = L.wide ? '' : 'none';
    }
    const nowEl = document.getElementById('now');
    if (nowEl) {
      const busy = serverState === 'thinking' && turnStart;
      const text = busy ? (nowText || 'thinking') + ' · ' + Math.round((nowMs - turnStart) / 1000) + 's' + (actions ? ' · ' + actions + (actions === 1 ? ' action' : ' actions') : '')
        : lastTurn ? 'last turn: ' + lastTurn : '';
      if (nowEl.textContent !== text) nowEl.textContent = text;
      nowEl.style.opacity = busy ? 1 : .55;
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  const btn = document.getElementById('facebtn');
  const showBtn = () => { if (btn) { btn.textContent = on ? 'Face on' : 'Face off'; btn.classList.toggle('on', on); } };
  if (btn) btn.onclick = () => { on = !on; lastHue = null; try { localStorage.setItem('va_face', on ? 'on' : 'off'); } catch (e) {} showBtn(); askLipsync(); };
  showBtn();

  // For tests and for trying expressions from the console: voiceFace.set('skeptical'), voiceFace.names
  window.voiceFace = { set: setFace, names: NAMES, activity: onActivity, reload, morph, get fx() { return fx; } };
})();

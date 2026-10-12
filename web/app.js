/* Fosc53B web scope — talks to server.py over WebSocket. */
(() => {
'use strict';
const NCH_MAX = 5;
const HDIV = 16, VDIV = 10, UNITS = 250, UPD = UNITS / VDIV;   // vendor app: 16 time divs across, 10 volt divs (25 units) high
const COLORS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181'];
const RING = 1 << 20;                                            // samples kept per channel (~40 s at the fastest timebases)
const $ = id => document.getElementById(id);

// ------------------------------------------------------------------ state
const S = { settings: null, limits: { nch: 5, tmin: 20, tmax: 38, stall_max: 11 }, tables: null, attrs: null, device: null,
            status: 'connecting', fps: 0, url: '', wsOk: false };
const rings = Array.from({ length: NCH_MAX }, () => new Uint8Array(RING));
let wr = 0;                       // total samples written (per channel)
let last = null;                  // last frame descriptor
let running = true, viewEnd = 0, zoom = 1;
let trigStart = -1;               // window start chosen by the software trigger
let cursorsOn = false, measureOn = true;
const cur = { x: [0.25, 0.75], y: [0.35, 0.65] };
let dirty = true, ws = null, wsTimer = null, backoff = 500, freshSocket = false;   // freshSocket: drop stale history on the first hello of a new connection
const dragSel = { on: false, sel: -1 };
const TOUCH = matchMedia('(pointer: coarse)').matches;
const TAG = TOUCH ? { w: 36, h: 30, x: 50, grab: 26 } : { w: 26, h: 22, x: 44, grab: 18 };   // channel tag box, edge hit zone, snap distance
const GRAB = TOUCH ? 24 : 16;                                                             // cursor grab distance (px)

// ------------------------------------------------------------------ helpers
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const tdivS = tb => S.tables ? S.tables.time_ms[tb] / 1000 : 0.004;
const hwTb = () => S.settings ? clamp(S.settings.timebase, 0, 38) : 26;
const dipNow = () => last ? last.dip : 1600;
const fs = () => dipNow() / (HDIV * tdivS(hwTb()));            // samples per second
const vdiv = ch => { if (!S.tables || !S.settings) return 0.1; const st = S.tables.stall[clamp(S.settings.stall[ch], 0, S.tables.stall.length - 1)]; return st[1] === 'mV' ? st[0] / 1000 : st[0]; };
const toVolts = (ch, v) => (v - S.settings.vref[ch]) / UPD * vdiv(ch);
const enabled = ch => S.settings && (S.settings.ch_en >> ch) & 1;
function fmtV(v) { const a = Math.abs(v); if (a >= 100) return v.toFixed(0) + ' V'; if (a >= 10) return v.toFixed(1) + ' V'; if (a >= 1) return v.toFixed(2) + ' V'; if (a >= 0.01) return (v * 1000).toFixed(0) + ' mV'; return (v * 1000).toFixed(1) + ' mV'; }
function fmtT(s) { const a = Math.abs(s); if (a >= 3600) return (s / 3600).toFixed(2) + ' h'; if (a >= 1) return s.toFixed(a >= 100 ? 0 : 2) + ' s'; if (a >= 1e-3) return (s * 1e3).toFixed(a >= 0.1 ? 0 : 2) + ' ms'; if (a >= 1e-6) return (s * 1e6).toFixed(1) + ' µs'; return (s * 1e9).toFixed(0) + ' ns'; }
function fmtSa(f) { if (!(f > 0)) return '—'; if (f >= 1e6) return (f / 1e6).toFixed(2) + ' MSa/s'; if (f >= 1e3) return (f / 1e3).toFixed(1) + ' kSa/s'; return f.toFixed(0) + ' Sa/s'; }
function fmtHz(f) { if (!(f > 0)) return '—'; if (f >= 1e6) return (f / 1e6).toFixed(3) + ' MHz'; if (f >= 1e3) return (f / 1e3).toFixed(3) + ' kHz'; return f.toFixed(f >= 100 ? 1 : 2) + ' Hz'; }
function stallLabel(ch) { const st = S.tables.stall[clamp(S.settings.stall[ch], 0, S.tables.stall.length - 1)]; return st[0] + ' ' + st[1]; }
let hintT; function hint(t) { const h = $('hint'); h.textContent = t; h.classList.add('show'); clearTimeout(hintT); hintT = setTimeout(() => h.classList.remove('show'), 1600); }

// ------------------------------------------------------------------ websocket
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { S.wsOk = true; backoff = 500; freshSocket = true; renderStatus(); };
  ws.onclose = () => { S.wsOk = false; renderStatus(); clearTimeout(wsTimer); wsTimer = setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 5000); };
  ws.onerror = () => ws.close();
  ws.onmessage = ev => {
    if (typeof ev.data === 'string') onJson(JSON.parse(ev.data)); else onFrame(ev.data);
  };
}
const pending = {}; let sendTimer = null;
function send(settings, immediate = true) {
  Object.assign(pending, settings);
  const flush = () => { if (ws && ws.readyState === 1 && Object.keys(pending).length) ws.send(JSON.stringify({ type: 'set', settings: JSON.parse(JSON.stringify(pending)) })); for (const k in pending) delete pending[k]; sendTimer = null; };
  if (immediate) { clearTimeout(sendTimer); flush(); } else if (!sendTimer) sendTimer = setTimeout(flush, 80);
}
function onJson(m) {
  if (m.type === 'hello') {
    if (freshSocket) { freshSocket = false; rings.forEach(r => r.fill(0)); wr = 0; last = null; trigStart = -1; viewEnd = 0; zoom = 1; }
    S.device = m.device; S.status = m.status; S.fps = m.fps || 0; S.attrs = m.attrs; S.settings = m.settings; S.limits = m.limits; S.tables = m.tables; S.url = m.url;
    buildChannels(); buildTrig(); renderAll();
  } else if (m.type === 'status') { S.status = m.status; S.fps = m.fps || 0; renderStatus(); }
  else if (m.type === 'settings') { S.settings = m.settings; renderSettings(); dirty = true; }
}
function onFrame(buf) {
  const dv = new DataView(buf);
  if (dv.getUint8(0) !== 0xF5) return;
  const mask = dv.getUint8(1), dip = dv.getUint16(2, true), tb = dv.getUint8(4), flags = dv.getUint8(5), n = dv.getUint16(6, true);
  let off = 20;
  const pos = wr % RING;
  for (let ch = 0; ch < NCH_MAX; ch++) {
    const r = rings[ch];
    if ((mask >> ch) & 1) {
      const src = new Uint8Array(buf, off, n); off += n;
      const first = Math.min(n, RING - pos);
      r.set(src.subarray(0, first), pos);
      if (first < n) r.set(src.subarray(first), 0);
    } else {
      for (let i = 0; i < n; i++) r[(pos + i) % RING] = 0;
    }
  }
  wr += n;
  last = { start: wr - n, n, dip, tb, roll: !!(flags & 1), mask };
  // software trigger: choose where the visible window starts (vendor app does the same on the phone)
  const t = S.settings && S.settings.trig;
  if (running && t && t.en && n >= dip && ((mask >> t.ch) & 1) && !last.roll) {
    const r = rings[t.ch], lvl = t.level, pt = Math.min(t.point, dip - 1);
    let prev = -1, found = -1;
    for (let i = last.start + pt; i < wr; i++) {
      const v = r[i % RING];
      if (v === 0) { prev = -1; continue; }
      if (prev >= 0) {
        if (t.rising ? (v > lvl && prev < lvl) : (v < lvl && prev > lvl)) { found = i; break; }
      }
      if (v !== lvl) prev = v;
    }
    if (found >= 0 && found - pt + dip <= wr) trigStart = found - pt;
  } else trigStart = -1;
  dirty = true;
}

// ------------------------------------------------------------------ window selection
function window_() {
  const dip = dipNow();
  const avail = Math.min(wr, RING - 2);
  if (running) {
    let W = dip, s;
    if (trigStart >= 0 && wr - trigStart <= RING - 4) s = trigStart; else s = wr - W;
    s = Math.max(s, wr - avail);
    return { s, W: Math.min(W, avail) };
  }
  let W = Math.max(8, Math.round(dip * zoom));
  W = Math.min(W, avail);
  viewEnd = clamp(viewEnd, Math.max(W, wr - avail + W), wr);
  return { s: viewEnd - W, W };
}

// ------------------------------------------------------------------ drawing
const canvas = $('scope'), ctx = canvas.getContext('2d');
let cw = 0, chh = 0, dpr = 1;
function resize() {
  dpr = Math.min(window.devicePixelRatio || 1, 3);
  const r = canvas.getBoundingClientRect();
  cw = Math.max(1, Math.round(r.width)); chh = Math.max(1, Math.round(r.height));
  canvas.width = Math.round(cw * dpr); canvas.height = Math.round(chh * dpr);
  dirty = true;
}
new ResizeObserver(resize).observe(canvas);
const PADL = 0;   // traces use the full width; tags float over the left edge
function draw() {
  if (!dirty) return; dirty = false;
  const w = cw, h = chh;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() || '#0b1020';
  ctx.fillRect(0, 0, w, h);
  // grid
  ctx.lineWidth = 1;
  for (let i = 0; i <= HDIV; i++) { const x = Math.round(i / HDIV * (w - 1)) + 0.5; ctx.strokeStyle = i === HDIV / 2 ? '#2b3756' : '#1d2740'; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
  for (let i = 0; i <= VDIV; i++) { const y = Math.round(i / VDIV * (h - 1)) + 0.5; ctx.strokeStyle = i === VDIV / 2 ? '#2b3756' : '#1d2740'; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  ctx.strokeStyle = '#2b3756';
  for (let i = 0; i <= HDIV * 5; i++) { const x = i / (HDIV * 5) * (w - 1); ctx.beginPath(); ctx.moveTo(x, h / 2 - 3); ctx.lineTo(x, h / 2 + 3); ctx.stroke(); }
  for (let i = 0; i <= VDIV * 5; i++) { const y = i / (VDIV * 5) * (h - 1); ctx.beginPath(); ctx.moveTo(w / 2 - 3, y); ctx.lineTo(w / 2 + 3, y); ctx.stroke(); }
  if (!S.settings) { drawText('waiting for bridge…', w / 2, h / 2, '#7d879c', 14, 'center'); return; }
  if (!last) { drawText(statusLine(), w / 2, h / 2, '#7d879c', 14, 'center'); drawTags(w, h); return; }

  const { s, W } = window_();
  const yOf = v => (UNITS - v) / UNITS * h;
  ctx.lineWidth = 1.6; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
  for (let ch = 0; ch < S.limits.nch; ch++) {
    if (!enabled(ch)) continue;
    const r = rings[ch];
    ctx.strokeStyle = COLORS[ch]; ctx.beginPath();
    let pen = false;
    if (W <= w * 2) {
      for (let i = 0; i < W; i++) {
        const v = r[(s + i) % RING];
        if (v === 0) { pen = false; continue; }
        const x = i / (W - 1 || 1) * w, y = yOf(clamp(v, 1, 254));
        if (pen) ctx.lineTo(x, y); else { ctx.moveTo(x, y); pen = true; }
      }
    } else {                                  // min/max per pixel column
      const per = W / w;
      for (let px = 0; px < w; px++) {
        const a = s + Math.floor(px * per), b = s + Math.floor((px + 1) * per);
        let mn = 255, mx = 0;
        for (let i = a; i < b; i++) { const v = r[i % RING]; if (v === 0) continue; if (v < mn) mn = v; if (v > mx) mx = v; }
        if (mx === 0) { pen = false; continue; }
        const y1 = yOf(clamp(mx, 1, 254)), y2 = yOf(clamp(mn, 1, 254));
        if (pen) ctx.lineTo(px, y1); else { ctx.moveTo(px, y1); pen = true; }
        ctx.lineTo(px, y2);
      }
    }
    ctx.stroke();
  }
  // trigger markers
  const t = S.settings.trig;
  if (t.en && enabled(t.ch)) {
    ctx.fillStyle = COLORS[t.ch];
    const ly = yOf(t.level); ctx.beginPath(); ctx.moveTo(w, ly - 6); ctx.lineTo(w - 9, ly); ctx.lineTo(w, ly + 6); ctx.fill();
    if (running) { const tx = clamp(t.point, 0, W) / (W - 1 || 1) * w; ctx.beginPath(); ctx.moveTo(tx - 6, 0); ctx.lineTo(tx, 8); ctx.lineTo(tx + 6, 0); ctx.fill(); }
  }
  drawTags(w, h);
  // cursors
  if (cursorsOn) {
    ctx.setLineDash([6, 6]); ctx.lineWidth = 1;
    for (let i = 0; i < 2; i++) {
      const x = Math.round(cur.x[i] * w) + 0.5; ctx.strokeStyle = i ? '#c3c2b7' : '#f2f4f8'; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      const y = Math.round(cur.y[i] * h) + 0.5; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    ctx.setLineDash([]);
  }
  // stopped: history position bar
  if (!running) {
    const avail = Math.min(wr, RING - 2), W2 = W;
    const x0 = (s - (wr - avail)) / avail * w, x1 = (s + W2 - (wr - avail)) / avail * w;
    ctx.fillStyle = 'rgba(255,255,255,.08)'; ctx.fillRect(0, h - 5, w, 5);
    ctx.fillStyle = '#e66767'; ctx.fillRect(x0, h - 5, Math.max(3, x1 - x0), 5);
    drawText('STOP', 48, 20, '#e66767', 13, 'left', true);
  }
  renderReadout(s, W);
}
function tagLayout(h) {                 // [{ch, x, y}] – tags whose positions would overlap step to the right
  const out = [];
  for (let ch = 0; ch < S.limits.nch; ch++) {
    const y = (UNITS - S.settings.vref[ch]) / UNITS * h;
    let x = 2;
    for (const o of out) if (Math.abs(o.y - y) < TAG.h + 2 && o.x >= x) x = o.x + TAG.w + 4;
    out.push({ ch, x, y });
  }
  return out;
}
function drawTags(w, h) {
  if (!S.settings) return;
  for (const { ch, x, y } of tagLayout(h)) {
    const on = enabled(ch), sel = S.settings.ch_sel === ch;
    ctx.globalAlpha = on ? 1 : 0.35;
    const tw = TAG.w, th = TAG.h;
    ctx.fillStyle = COLORS[ch]; roundRect(x, y - th / 2, tw, th, 6); ctx.fill();
    if (sel) { ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; roundRect(x, y - th / 2, tw, th, 6); ctx.stroke(); }
    drawText(String(ch + 1), x + tw / 2, y + (TOUCH ? 5.5 : 4.5), '#fff', TOUCH ? 15 : 12, 'center', true);
    ctx.beginPath(); ctx.moveTo(x + tw, y); ctx.lineTo(x + tw + 12, y); ctx.strokeStyle = COLORS[ch]; ctx.lineWidth = 1; ctx.stroke();
    ctx.globalAlpha = 1;
  }
}
function roundRect(x, y, w, h, r) { ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath(); }
function drawText(txt, x, y, color, size, align, bold) { ctx.fillStyle = color; ctx.font = `${bold ? '700 ' : ''}${size}px system-ui, sans-serif`; ctx.textAlign = align || 'left'; ctx.fillText(txt, x, y); }
function statusLine() { return ({ searching: 'searching for the scope on the LAN…', busy: 'scope is in use by another app (close it)', connecting: 'connecting…', handshake: 'handshaking…', reconnecting: 'reconnecting…', connected: 'waiting for data…' })[S.status] || S.status; }

// ------------------------------------------------------------------ readout & measurements
function renderReadout(s, W) {
  const box = $('readout'); const lines = [];
  if (cursorsOn) {
    const dt = (cur.x[1] - cur.x[0]) * W / fs();
    lines.push(`Δt ${fmtT(dt)}   1/Δt ${fmtHz(1 / Math.abs(dt))}`);
    const ch = S.settings.ch_sel;
    const v1 = UNITS * (1 - cur.y[0]), v2 = UNITS * (1 - cur.y[1]);
    lines.push(`CH${ch + 1}  Y1 ${fmtV(toVolts(ch, v1))}  Y2 ${fmtV(toVolts(ch, v2))}  ΔV ${fmtV(toVolts(ch, v2) - toVolts(ch, v1))}`);
    for (let i = 0; i < 2; i++) {
      const idx = s + Math.round(cur.x[i] * (W - 1));
      const vals = [];
      for (let c = 0; c < S.limits.nch; c++) if (enabled(c)) { const v = rings[c][idx % RING]; vals.push(`CH${c + 1} ${v ? fmtV(toVolts(c, v)) : '—'}`); }
      lines.push(`X${i + 1}: ${vals.join('  ')}`);
    }
  }
  if (!running) lines.push(`history: ${fmtT((s + W - wr) / fs())} … ${fmtT((s - wr) / fs())}   window ${fmtT(W / fs())}`);
  box.textContent = lines.join('\n'); box.classList.toggle('show', lines.length > 0);
}
let measT = 0;
function measure() {
  if (!measureOn || !last || !S.settings) return;
  const now = performance.now(); if (now - measT < 200) return; measT = now;
  const { s, W } = window_();
  const tb = $('measTable').querySelector('tbody'); const rows = [];
  for (let ch = 0; ch < S.limits.nch; ch++) {
    if (!enabled(ch)) continue;
    const r = rings[ch]; let mn = 255, mx = 0, sum = 0, n = 0;
    for (let i = 0; i < W; i++) { const v = r[(s + i) % RING]; if (!v) continue; n++; sum += v; if (v < mn) mn = v; if (v > mx) mx = v; }
    if (!n) { rows.push(`<tr><td><span class="sw" style="background:${COLORS[ch]}"></span>CH${ch + 1}</td><td colspan="6" class="muted">no data</td></tr>`); continue; }
    const mean = sum / n; let sq = 0, cross = [], prev = -1; const hi = mean + 1.5, lo = mean - 1.5; let armed = false;
    for (let i = 0; i < W; i++) { const v = r[(s + i) % RING]; if (!v) continue; sq += (v - mean) * (v - mean); if (v < lo) armed = true; else if (v > hi && armed) { cross.push(i); armed = false; } }
    const rms = Math.sqrt(sq / n);
    let f = 0; if (cross.length >= 2) f = fs() / ((cross[cross.length - 1] - cross[0]) / (cross.length - 1));
    const V = v => toVolts(ch, v), k = vdiv(ch) / UPD;
    rows.push(`<tr><td><span class="sw" style="background:${COLORS[ch]}"></span>CH${ch + 1}</td><td>${fmtV((mx - mn) * k)}</td><td>${fmtV(V(mx))}</td><td>${fmtV(V(mn))}</td><td>${fmtV(V(mean))}</td><td>${fmtV(rms * k)}</td><td>${fmtHz(f)}</td></tr>`);
  }
  tb.innerHTML = rows.join('');
}

// ------------------------------------------------------------------ UI: status / header
function renderStatus() {
  const dot = $('statusDot'), txt = $('statusText');
  dot.className = 'dot ' + (!S.wsOk ? 'bad' : S.status === 'connected' ? 'ok' : 'warn');
  txt.textContent = !S.wsOk ? 'bridge offline' : (S.status === 'connected' ? `${S.fps.toFixed(0)} fps · ${fmtSa(fs())}` : statusLine());
  $('devName').textContent = S.device ? (S.device.name || 'Fosc53B') : 'Fosc53B';
  $('btnRun').textContent = running ? 'Run' : 'Stop'; $('btnRun').classList.toggle('stopped', !running);
  $('btnCursors').classList.toggle('on', cursorsOn); $('btnMeasure').classList.toggle('on', measureOn);
  $('btnAuto').classList.toggle('on', auto.on); $('btnAuto').textContent = auto.on ? 'Auto…' : 'Auto';
  $('measCard').style.display = measureOn ? '' : 'none';
  const tb = hwTb(); const eff = tdivS(tb) * (running ? 1 : zoom);
  $('tbValue').textContent = fmtT(eff) + (running ? '' : ' ⤢');
  if (S.device) $('devInfo').innerHTML = [['Name', S.device.name], ['IP', S.device.ip], ['MAC', S.device.mac], ['Firmware', S.device.ver_soft + ' / hw ' + S.device.ver_hard], ['Channels', S.limits.nch], ['Model key', S.attrs ? S.attrs.key : '—'], ['Screen', last ? `${last.dip} samples` : '—'], ['Open on phone', S.url]].map(([k, v]) => `<b>${k}</b><span>${v ?? '—'}</span>`).join('');
}
function renderAll() { renderStatus(); renderSettings(); dirty = true; }

// ------------------------------------------------------------------ UI: channels
const chanUI = [];
function buildChannels() {
  const list = $('chanList'); list.innerHTML = ''; chanUI.length = 0;
  for (let ch = 0; ch < S.limits.nch; ch++) {
    const el = document.createElement('div'); el.className = 'chan';
    el.innerHTML = `<div class="tag" style="background:${COLORS[ch]}">${ch + 1}</div>
      <div class="line1"><button class="btn sm on-off">On</button><button class="btn sm vm">−</button><span class="vdiv">—</span><button class="btn sm vp">+</button><button class="btn sm coupling">DC</button></div>
      <div class="line2"><span>Pos</span><input type="range" min="50" max="200" value="125"><button class="btn sm au" title="Auto scale this channel">A</button></div>`;
    list.appendChild(el);
    const u = { el, tag: el.querySelector('.tag'), onoff: el.querySelector('.on-off'), vm: el.querySelector('.vm'), vp: el.querySelector('.vp'), vdiv: el.querySelector('.vdiv'), coup: el.querySelector('.coupling'), pos: el.querySelector('input') };
    chanUI.push(u);
    u.tag.onclick = () => send({ ch_sel: ch });
    u.onoff.onclick = () => send({ ch_en: S.settings.ch_en ^ (1 << ch), ch_sel: ch });
    u.vm.onclick = () => { const st = S.settings.stall.slice(); st[ch] = Math.max(1, st[ch] - 1); send({ stall: st, ch_sel: ch }); };
    u.vp.onclick = () => { const st = S.settings.stall.slice(); st[ch] = Math.min(S.limits.stall_max, st[ch] + 1); send({ stall: st, ch_sel: ch }); };
    u.coup.onclick = () => send({ ch_acdc: S.settings.ch_acdc ^ (1 << ch), ch_sel: ch });
    u.pos.oninput = () => { const vr = S.settings.vref.slice(); vr[ch] = +u.pos.value; S.settings.vref[ch] = vr[ch]; dirty = true; send({ vref: vr, ch_sel: ch }, false); };
    el.querySelector('.au').onclick = () => { send({ ch_sel: ch }); autoStart([ch], false); };
  }
}
function renderSettings() {
  if (!S.settings) return;
  chanUI.forEach((u, ch) => {
    const on = enabled(ch), sel = S.settings.ch_sel === ch;
    u.el.classList.toggle('sel', sel); u.tag.classList.toggle('off', !on); u.tag.classList.toggle('sel', sel);
    u.onoff.textContent = on ? 'On' : 'Off'; u.onoff.classList.toggle('on', on);
    u.vdiv.textContent = stallLabel(ch);
    u.coup.textContent = (S.settings.ch_acdc >> ch) & 1 ? 'AC' : 'DC';
    if (document.activeElement !== u.pos) u.pos.value = S.settings.vref[ch];
  });
  const t = S.settings.trig;
  $('trigEn').textContent = t.en ? 'On' : 'Off'; $('trigEn').classList.toggle('on', t.en);
  [...$('trigSrc').children].forEach((b, i) => b.classList.toggle('on', i === t.ch));
  $('trigEdge').textContent = t.rising ? '↗ rising' : '↘ falling';
  if (document.activeElement !== $('trigLevel')) $('trigLevel').value = t.level;
  $('trigLevelV').textContent = fmtV(toVolts(t.ch, t.level));
  if (document.activeElement !== $('trigPoint')) $('trigPoint').value = t.point;
  $('trigPoint').max = dipNow(); $('trigPointV').textContent = fmtT(t.point / fs());
  renderStatus();
}
function buildTrig() {
  const seg = $('trigSrc'); seg.innerHTML = '';
  for (let ch = 0; ch < S.limits.nch; ch++) { const b = document.createElement('button'); b.textContent = ch + 1; b.style.color = COLORS[ch]; b.onclick = () => send({ trig: { ch } }); seg.appendChild(b); }
  $('trigEn').onclick = () => send({ trig: { en: !S.settings.trig.en } });
  $('trigEdge').onclick = () => send({ trig: { rising: !S.settings.trig.rising } });
  $('trigLevel').oninput = () => { S.settings.trig.level = +$('trigLevel').value; $('trigLevelV').textContent = fmtV(toVolts(S.settings.trig.ch, S.settings.trig.level)); dirty = true; send({ trig: { level: +$('trigLevel').value } }, false); };
  $('trigPoint').oninput = () => { S.settings.trig.point = +$('trigPoint').value; $('trigPointV').textContent = fmtT(S.settings.trig.point / fs()); dirty = true; send({ trig: { point: +$('trigPoint').value } }, false); };
}

// ------------------------------------------------------------------ auto-set
// Vertical: V/div and position so each channel spans ~6 of the 10 divisions, centred. Global Auto also picks a time base
// showing ~4 periods of the fastest channel and puts an edge trigger at its mid level. V/div is applied by the scope
// hardware (~1 s), so the result is checked on fresh samples and refined, up to AUTO_MAX passes.
const AUTO_MAX = 5;
const auto = { on: false, chs: [], tb: false, iter: 0, timer: null, wrSent: 0 };
function stallVolts(idx) { const st = S.tables.stall[clamp(idx, 0, S.tables.stall.length - 1)]; return st[1] === 'mV' ? st[0] / 1000 : st[0]; }
function recentStats(ch, N) {                 // min / max / mean (screen units) and frequency over the newest N samples
  const r = rings[ch], s0 = Math.max(0, wr - N); let mn = 255, mx = 0, sum = 0, n = 0;
  for (let i = s0; i < wr; i++) { const v = r[i % RING]; if (!v) continue; n++; sum += v; if (v < mn) mn = v; if (v > mx) mx = v; }
  if (n < 8) return null;
  const mean = sum / n, hy = Math.max(1.5, 0.1 * (mx - mn)), hi = mean + hy, lo = mean - hy;   // hysteresis scales with the signal so noise does not count as cycles
  const cross = []; let armed = false;
  for (let i = s0; i < wr; i++) { const v = r[i % RING]; if (!v) continue; if (v < lo) armed = true; else if (v > hi && armed) { cross.push(i); armed = false; } }
  const f = cross.length >= 3 ? fs() / ((cross[cross.length - 1] - cross[0]) / (cross.length - 1)) : 0;
  return { mn, mx, mean, n, f };
}
function autoStart(chs, withTimebase) {
  if (auto.on) { autoDone('auto cancelled'); return; }
  if (!S.settings || !last) { hint('no data yet'); return; }
  Object.assign(auto, { on: true, chs: chs.filter(enabled), tb: withTimebase, iter: 0, wrSent: 0 });
  if (!auto.chs.length) { autoDone('no channel on'); return; }
  renderStatus(); autoStep();
}
function autoDone(msg) { auto.on = false; clearTimeout(auto.timer); auto.timer = null; if (msg) hint(msg); renderStatus(); }
function autoStep() {
  auto.timer = null;
  if (!auto.on || !S.settings || !last) { autoDone(); return; }
  auto.iter++;
  // Judge on the newest ~0.25 s (at least one screen), and never on samples from the ~1.3 s the scope takes to apply a change.
  const fresh = auto.wrSent ? wr - auto.wrSent - Math.round(fs() * 1.3) : wr;
  const N = clamp(Math.min(fresh, Math.max(dipNow(), Math.round(fs() * 0.25))), 64, RING - 4);
  const stall = S.settings.stall.slice(), vref = S.settings.vref.slice();
  const mid = {}, freq = {}, span = {};                                     // per channel, in volts / Hz
  let changed = false, settled = true;
  for (const ch of auto.chs) {
    const st = recentStats(ch, N); if (!st) continue;
    const cur = S.settings.stall[ch];
    if (st.mn <= 3 || st.mx >= 252) {                                      // clipped: zoom well out, re-centre, look again
      stall[ch] = Math.min(S.limits.stall_max, cur + 3); vref[ch] = 125;
      if (stall[ch] !== cur) { changed = true; settled = false; }
      continue;
    }
    const V = u => (u - S.settings.vref[ch]) / UPD * stallVolts(cur);      // screen units -> volts (absolute, scale independent)
    const top = V(st.mx), bot = V(st.mn); span[ch] = Math.max(top - bot, 0.02); mid[ch] = (top + bot) / 2; freq[ch] = st.f;
    let idx = 1, vr = 125;
    for (; idx < S.limits.stall_max; idx++) {                               // finest V/div with the trace in <= 6 div and fully on screen
      const vd = stallVolts(idx); vr = clamp(Math.round(125 - mid[ch] / vd * UPD), 50, 200);
      if (span[ch] / vd <= 6 && vr + top / vd * UPD <= 240 && vr + bot / vd * UPD >= 10) break;
    }
    if (idx >= S.limits.stall_max) { idx = S.limits.stall_max; vr = clamp(Math.round(125 - mid[ch] / stallVolts(idx) * UPD), 50, 200); }
    if (idx !== cur) settled = false;
    if (idx !== cur || Math.abs(vr - S.settings.vref[ch]) > 2) { stall[ch] = idx; vref[ch] = vr; changed = true; }
  }
  const upd = { stall, vref };
  if (auto.tb) {
    // time base from the selected channel if it is periodic, else the fastest periodic one; ~4 periods across the 16 divisions
    const periodic = auto.chs.filter(ch => freq[ch] > 0);
    const ref = periodic.includes(S.settings.ch_sel) ? S.settings.ch_sel : periodic.sort((a, b) => freq[b] - freq[a])[0];
    if (ref !== undefined) {
      const want = 1000 / (4 * freq[ref]);
      let best = S.limits.tmin;
      for (let i = S.limits.tmin; i <= S.limits.tmax; i++) if (S.tables.time_ms[i] <= want) best = i;
      if (best !== S.settings.timebase) { upd.timebase = best; changed = true; settled = false; }
      const vd = stallVolts(stall[ref]);
      upd.trig = { en: true, ch: ref, level: clamp(Math.round(vref[ref] + mid[ref] / vd * UPD), 1, 254), rising: true };
      upd.ch_sel = ref;
    } else if (auto.chs.some(ch => span[ch] > Math.max(0.02, 3 / UPD * stallVolts(S.settings.stall[ch]))) && S.settings.timebase < 32) {
      // something is moving (more than 3 LSB) but no full cycle fits the window: slow down and look again, not past 100 ms/div
      const tb = Math.min(32, S.settings.timebase + 3);
      if (tb !== S.settings.timebase) { upd.timebase = tb; changed = true; settled = false; }
    }
  }
  if (changed) { send(upd); auto.wrSent = wr; }
  if (settled || auto.iter >= AUTO_MAX) { autoDone(settled ? 'auto set' : 'auto: not stable, stopped'); return; }
  const tdiv = tdivS(upd.timebase ?? hwTb());
  const wait = clamp(1600 + 8 * tdiv * 1000, 1600, 6000);                 // hardware applies the change in ~1.3 s, then collect fresh data
  hint(`auto… (${auto.iter}/${AUTO_MAX})`);
  auto.timer = setTimeout(autoStep, wait);
}

// ------------------------------------------------------------------ UI: header buttons & keys
function setRunning(r) { running = r; if (!r) { viewEnd = wr; zoom = 1; } trigStart = -1; renderStatus(); dirty = true; }
function stepTimebase(d) {
  if (!S.settings) return;
  if (running) { send({ timebase: clamp(S.settings.timebase + d, S.limits.tmin, S.limits.tmax) }); }
  else { zoom = clamp(zoom * (d > 0 ? 2 : 0.5), 1 / 64, Math.max(1, Math.min(wr, RING) / dipNow())); renderStatus(); dirty = true; }
}
$('btnRun').onclick = () => setRunning(!running);
$('btnAuto').onclick = () => autoStart([...Array(S.limits.nch).keys()], true);
$('tbMinus').onclick = () => stepTimebase(-1); $('tbPlus').onclick = () => stepTimebase(1);
$('btnCursors').onclick = () => { cursorsOn = !cursorsOn; renderStatus(); dirty = true; };
$('btnMeasure').onclick = () => { measureOn = !measureOn; renderStatus(); };
$('btnPanel').onclick = () => { document.body.classList.toggle('hidepanel'); setTimeout(resize, 50); };
$('btnFull').onclick = () => { const el = document.documentElement; if (document.fullscreenElement) document.exitFullscreen(); else if (el.requestFullscreen) el.requestFullscreen(); };
$('btnPng').onclick = () => { draw(); canvas.toBlob(b => download(b, `fosc53b-${stamp()}.png`)); };
$('btnExport').onclick = () => {
  if (!last) return; const { s, W } = window_(); const chs = []; for (let c = 0; c < S.limits.nch; c++) if (enabled(c)) chs.push(c);
  const out = ['time_s,' + chs.map(c => `ch${c + 1}_V`).join(',')];
  for (let i = 0; i < W; i++) out.push((i / fs()).toExponential(6) + ',' + chs.map(c => { const v = rings[c][(s + i) % RING]; return v ? toVolts(c, v).toFixed(5) : ''; }).join(','));
  download(new Blob([out.join('\n')], { type: 'text/csv' }), `fosc53b-${stamp()}.csv`);
};
function stamp() { return new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19); }
function download(blob, name) { const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 5000); }
window.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  if (e.code === 'Space') { e.preventDefault(); setRunning(!running); }
  else if (e.key === 'ArrowLeft') stepTimebase(-1); else if (e.key === 'ArrowRight') stepTimebase(1);
  else if (e.key >= '1' && e.key <= '5' && S.settings) { const ch = +e.key - 1; if (ch < S.limits.nch) send(e.shiftKey ? { ch_en: S.settings.ch_en ^ (1 << ch), ch_sel: ch } : { ch_sel: ch }); }
  else if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && S.settings) { e.preventDefault(); const vr = S.settings.vref.slice(); vr[S.settings.ch_sel] = clamp(vr[S.settings.ch_sel] + (e.key === 'ArrowUp' ? 5 : -5), 50, 200); send({ vref: vr }); }
  else if (e.key === 'c' || e.key === 'C') $('btnCursors').click(); else if (e.key === 'm' || e.key === 'M') $('btnMeasure').click();
  else if (e.key === 'a' || e.key === 'A') $('btnAuto').click();
});

// ------------------------------------------------------------------ pointer interaction on the canvas
const ptrs = new Map(); let drag = null;
canvas.addEventListener('pointerdown', e => {
  canvas.setPointerCapture(e.pointerId); ptrs.set(e.pointerId, { x: e.offsetX, y: e.offsetY });
  if (ptrs.size === 2) { const [a, b] = [...ptrs.values()]; drag = { kind: 'pinch', d0: Math.hypot(a.x - b.x, a.y - b.y), z0: zoom }; return; }
  if (!S.settings) return;
  const x = e.offsetX, y = e.offsetY;
  // channel tags on the left edge
  { let best = -1, bd = TAG.grab; for (const t of tagLayout(chh)) { if (x < t.x || x > t.x + TAG.w + 12) continue; const d = Math.abs(t.y - y); if (d < bd) { bd = d; best = t.ch; } } if (best >= 0) { drag = { kind: 'vref', ch: best, y0: y, v0: S.settings.vref[best] }; send({ ch_sel: best }); return; } }
  if (cursorsOn) {
    let best = null, bd = GRAB;
    for (let i = 0; i < 2; i++) { const d = Math.abs(cur.x[i] * cw - x); if (d < bd) { bd = d; best = { kind: 'cx', i }; } }
    for (let i = 0; i < 2; i++) { const d = Math.abs(cur.y[i] * chh - y); if (d < bd) { bd = d; best = { kind: 'cy', i }; } }
    if (best) { drag = best; return; }
  }
  if (!running) { drag = { kind: 'pan', x0: x, e0: viewEnd }; return; }
  drag = { kind: 'none' };
});
canvas.addEventListener('pointermove', e => {
  if (!ptrs.has(e.pointerId)) return; ptrs.set(e.pointerId, { x: e.offsetX, y: e.offsetY });
  if (!drag) return;
  const x = e.offsetX, y = e.offsetY;
  if (drag.kind === 'pinch' && ptrs.size === 2 && !running) { const [a, b] = [...ptrs.values()]; const d = Math.hypot(a.x - b.x, a.y - b.y) || 1; zoom = clamp(drag.z0 * drag.d0 / d, 1 / 64, Math.max(1, Math.min(wr, RING) / dipNow())); renderStatus(); dirty = true; }
  else if (drag.kind === 'vref') { const nv = clamp(Math.round(drag.v0 - (y - drag.y0) / chh * UNITS), 50, 200); if (nv !== S.settings.vref[drag.ch]) { S.settings.vref[drag.ch] = nv; chanUI[drag.ch].pos.value = nv; const vr = S.settings.vref.slice(); send({ vref: vr }, false); dirty = true; } }
  else if (drag.kind === 'cx') { cur.x[drag.i] = clamp(x / cw, 0, 1); dirty = true; }
  else if (drag.kind === 'cy') { cur.y[drag.i] = clamp(y / chh, 0, 1); dirty = true; }
  else if (drag.kind === 'pan') { const { W } = window_(); viewEnd = Math.round(drag.e0 - (x - drag.x0) / cw * W); dirty = true; }
});
const endPtr = e => { ptrs.delete(e.pointerId); if (ptrs.size === 0) drag = null; else if (drag && drag.kind === 'pinch') drag = null; };
canvas.addEventListener('pointerup', endPtr); canvas.addEventListener('pointercancel', endPtr);
canvas.addEventListener('wheel', e => { if (running || !last) return; e.preventDefault(); const { W } = window_(); if (e.ctrlKey || Math.abs(e.deltaY) > Math.abs(e.deltaX)) { zoom = clamp(zoom * (e.deltaY > 0 ? 1.25 : 0.8), 1 / 64, Math.max(1, Math.min(wr, RING) / dipNow())); renderStatus(); } else viewEnd = Math.round(viewEnd + e.deltaX / cw * W); dirty = true; }, { passive: false });
canvas.addEventListener('dblclick', () => { if (!running) { viewEnd = wr; zoom = 1; renderStatus(); dirty = true; } });

// ------------------------------------------------------------------ main loop
function loop() { draw(); measure(); requestAnimationFrame(loop); }
resize(); connect(); requestAnimationFrame(loop);
setInterval(renderStatus, 1000);
})();

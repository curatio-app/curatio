// End-to-end check of the REAL player (src/player/Player.tsx) in the REAL desktop
// app, with the local engine, against a mock of the playback API. No account.
//
//   node scripts/desktop-player-e2e.mjs            # development app
//   APP=/path/to/Curatio.app node scripts/desktop-player-e2e.mjs
//
// It builds the harness page (tests/desktop), generates a test film with the
// bundled FFmpeg (three audio tracks at different pitches, two subtitle tracks
// whose cue text encodes the time), then drives the player like a viewer and
// checks what the viewer would see and hear: the timeline, seeking, the audio
// and subtitle menus, speed. Exit code 1 when a check fails.
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const work = process.env.WORK || fs.mkdtempSync(path.join(os.tmpdir(), 'curatio-player-e2e-'));
const engineBin = path.join(root, 'resources/engine/mac/bin');
const PORT = 5396;
const DEBUG = 9337;
const DURATION = 180;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const only = (process.env.ONLY || '').split(',').filter(Boolean);

// ---- test film -------------------------------------------------------------
const film = path.join(work, 'film.mkv');
if (!fs.existsSync(film)) {
  const srt = (word) => Array.from({ length: DURATION / 5 }, (_, n) => {
    const at = (s) => `${String(Math.floor(s / 3600)).padStart(2, '0')}:${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')},000`;
    return `${n + 1}\n${at(n * 5)} --> ${at(n * 5 + 4)}\n${word} cue ${n}\n`;
  }).join('\n');
  fs.writeFileSync(path.join(work, 'en.srt'), srt('EN'));
  fs.writeFileSync(path.join(work, 'fr.srt'), srt('FR'));
  const tone = (hz, layout) => `aevalsrc='0.5*sin(2*PI*${hz}*t)':s=48000:c=${layout}`;
  execFileSync(path.join(engineBin, 'ffmpeg'), [
    '-hide_banner', '-loglevel', 'error', '-y',
    '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=24',
    '-f', 'lavfi', '-i', tone(440, '5.1(side)'),
    '-f', 'lavfi', '-i', tone(880, 'stereo'),
    '-f', 'lavfi', '-i', tone(220, '5.1(side)'),
    '-i', path.join(work, 'en.srt'), '-i', path.join(work, 'fr.srt'),
    '-t', String(DURATION), '-map', '0', '-map', '1', '-map', '2', '-map', '3', '-map', '4', '-map', '5',
    '-c:v', 'h264_videotoolbox', '-bf', '3', '-profile:v', 'high', '-b:v', '3M', '-g', '240',
    '-c:a:0', 'truehd', '-strict', '-2', '-c:a:1', 'ac3', '-c:a:2', 'ac3', '-c:s', 'srt',
    '-metadata:s:a:0', 'language=eng', '-metadata:s:a:0', 'title=TrueHD 5.1', '-disposition:a:0', 'default',
    '-metadata:s:a:1', 'language=eng', '-metadata:s:a:1', 'title=Commentary', '-disposition:a:1', '0',
    '-metadata:s:a:2', 'language=fra', '-metadata:s:a:2', 'title=AC-3 5.1', '-disposition:a:2', '0',
    '-metadata:s:s:0', 'language=eng', '-metadata:s:s:1', 'language=fra',
    film,
  ]);
}

// ---- harness page ----------------------------------------------------------
const pageDir = path.join(work, 'page');
execFileSync('npx', ['vite', 'build', '-c', 'tests/desktop/vite.config.ts'], { cwd: root, env: { ...process.env, HARNESS_OUT: pageDir }, stdio: 'ignore' });

// ---- mock playback API (what Player.tsx calls) -------------------------------
/** The server's MediaFacts shape (app/media_probe.py), from a real ffprobe of the film. */
function mediaFacts() {
  const probe = JSON.parse(execFileSync(path.join(engineBin, 'ffprobe'), ['-v', 'error', '-show_format', '-show_streams', '-of', 'json', film]).toString());
  const tag = (s, name) => s.tags?.[name] ?? s.tags?.[name.toUpperCase()] ?? '';
  const audioString = { truehd: 'mlpa', ac3: 'ac-3', eac3: 'ec-3', aac: 'mp4a.40.2', dts: 'dtsc', flac: 'flac', opus: 'opus' };
  const video = probe.streams.find((s) => s.codec_type === 'video');
  return {
    container: 'matroska',
    duration: Number(probe.format.duration),
    size: Number(probe.format.size),
    video: { index: video.index, codec: video.codec_name, profile: video.profile, pix_fmt: video.pix_fmt, bit_depth: 8, width: video.width, height: video.height, frame_rate: 24, dynamic_range: 'sdr', dv_profile: null, codec_string: 'avc1.640028' },
    audio: probe.streams.filter((s) => s.codec_type === 'audio').map((s) => ({ index: s.index, codec: s.codec_name, profile: s.profile ?? null, channels: s.channels, language: tag(s, 'language'), title: tag(s, 'title'), default: Boolean(s.disposition?.default), codec_string: audioString[s.codec_name] ?? null })),
    subtitles: probe.streams.filter((s) => s.codec_type === 'subtitle').map((s) => ({ index: s.index, codec: s.codec_name, language: tag(s, 'language'), title: tag(s, 'title'), forced: Boolean(s.disposition?.forced), default: Boolean(s.disposition?.default), text: true })),
  };
}
const facts = mediaFacts();
const origin = `http://127.0.0.1:${PORT}`;
const mediaUrl = `${origin}/media/Harness.Movie.2024.1080p.BluRay.x264.TrueHD.5.1.mkv`;
const apiCalls = [];
const mime = { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml' };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, origin);
  const json = (body, status = 200) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
  if (url.pathname.startsWith('/harness/')) {
    const file = path.join(pageDir, url.pathname.slice('/harness/'.length));
    if (!fs.existsSync(file)) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { 'content-type': mime[path.extname(file)] || 'application/octet-stream', 'cache-control': 'no-store' });
    return fs.createReadStream(file).pipe(res);
  }
  if (url.pathname.startsWith('/media/')) {
    const size = fs.statSync(film).size;
    const m = /bytes=(\d*)-(\d*)/.exec(req.headers.range || '');
    const a = m && m[1] ? Math.min(+m[1], size - 1) : 0;
    const b = m && m[2] ? Math.min(+m[2], size - 1) : size - 1;
    res.writeHead(m ? 206 : 200, { 'accept-ranges': 'bytes', 'content-type': 'video/x-matroska', 'content-length': b - a + 1, ...(m ? { 'content-range': `bytes ${a}-${b}/${size}` } : {}) });
    if (req.method === 'HEAD') return res.end();
    return fs.createReadStream(film, { start: a, end: b }).pipe(res);
  }
  if (!url.pathname.startsWith('/account/api/')) { res.writeHead(404); return res.end(); }
  let body = '';
  req.on('data', (chunk) => (body += chunk));
  req.on('end', () => {
    apiCalls.push({ method: req.method, path: url.pathname, body: body.slice(0, 2000) });
    const route = url.pathname.replace('/account/api/', '');
    // docs/playback-api.md: one library source (a signed /media/ file).
    if (route === 'playback/sources') return json({
      quality: 'original',
      availability: { playable: true, in_library: true, sources: [{ provider_id: 'plex', label: 'Plex', kind: 'library' }] },
      sources: [{
        id: 'src_harness', provider_id: 'plex', provider_label: 'Plex', kind: 'library', variant: 'original', delivery: 'file',
        url: mediaUrl, headers: {}, label: '1080p · H.264 · TrueHD · 5.1', detail: 'Harness.Movie.2024.1080p.BluRay.x264.TrueHD.5.1.mkv', badges: ['Original'],
        container: 'mkv', video_codec: 'h264', audio_codec: 'truehd', hdr: null, resolution: '1080p', width: facts.video.width, height: facts.video.height,
        audio_channels: 6, languages: [], size_bytes: facts.size, bitrate: facts.bitrate ?? null, duration_seconds: facts.duration ?? null, subtitles: [],
        route_id: 'route_harness', file_id: 'file_harness', startup_timeout_seconds: null,
      }],
    });
    if (route === 'playback/probe') return json({ results: { [mediaUrl]: { state: 'passed', reason: null, status: 206, content_type: 'video/x-matroska', size: facts.size, range: true } }, originLooksUnreachable: false });
    if (route === 'playback/inspect') return json({ results: { [mediaUrl]: { state: 'ok', media: facts } } });
    // The engine prepares everything locally: the server remux must never be asked.
    if (route.startsWith('playback/remux/')) return json({ detail: 'the harness has no server remux' }, 500);
    return json({ ok: true });
  });
}).listen(PORT);

// ---- the app -----------------------------------------------------------------
const electron = process.env.APP ? path.join(process.env.APP, 'Contents/MacOS/Curatio') : path.join(root, 'node_modules/electron/dist/Electron.app/Contents/MacOS/Electron');
const env = { ...process.env, CURATIO_SERVER_URL: origin, CURATIO_DASHBOARD_PATH: `/harness/tests/desktop/harness.html?position=${process.env.POSITION || 0}&duration=${DURATION}` };
delete env.ELECTRON_RUN_AS_NODE;
// A fresh profile each run: no saved resume position or settings from an earlier run.
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'curatio-player-profile-'));
const app = spawn(electron, [...(process.env.APP ? [] : ['.']), `--remote-debugging-port=${DEBUG}`, `--user-data-dir=${profile}`], { cwd: root, env, stdio: 'ignore' });

async function connect() {
  for (let i = 0; i < 80; i++) {
    await sleep(300);
    try {
      const target = (await (await fetch(`http://127.0.0.1:${DEBUG}/json`)).json()).find((t) => t.type === 'page' && t.url.includes('harness'));
      if (!target) continue;
      const ws = new WebSocket(target.webSocketDebuggerUrl);
      await new Promise((r) => (ws.onopen = r));
      let id = 0;
      const pending = new Map();
      const consoleLines = [];
      ws.onmessage = (m) => {
        const d = JSON.parse(m.data);
        if (d.id) pending.get(d.id)?.(d);
        else if (d.method === 'Runtime.consoleAPICalled') consoleLines.push(d.params.args.map((a) => (a.value !== undefined ? (typeof a.value === 'string' ? a.value : JSON.stringify(a.value)) : a.preview ? JSON.stringify(Object.fromEntries(a.preview.properties.map((p) => [p.name, p.value]))) : a.type)).join(' '));
      };
      const send = (method, params = {}) => new Promise((r) => { pending.set(++id, r); ws.send(JSON.stringify({ id, method, params })); });
      await send('Runtime.enable');
      const evaluate = async (expression) => {
        const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
        if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || 'evaluate failed');
        return r.result?.result?.value;
      };
      const key = async (k, code = `Digit${k}`) => {
        for (const type of ['keyDown', 'keyUp']) await send('Input.dispatchKeyEvent', { type, key: k, code, text: type === 'keyDown' && k.length === 1 ? k : undefined, windowsVirtualKeyCode: k.length === 1 ? k.toUpperCase().charCodeAt(0) : undefined });
      };
      return { evaluate, key, send, consoleLines, close: () => ws.close() };
    } catch { /* not up yet */ }
  }
  throw new Error('the app never loaded the harness page');
}

// Helpers that run in the page.
const PAGE = `
window.__h = {
  secs(text) { const p = text.trim().split(':').map(Number); return p.reduce((a, b) => a * 60 + b, 0); },
  label() { const el = document.querySelector('[class*="timeLabel"]'); if (!el) return null; const [c, d] = el.textContent.split('/'); return { current: this.secs(c), duration: /\\d/.test(d) ? this.secs(d) : null, text: el.textContent.trim() }; },
  video() { return document.querySelector('video'); },
  state() { const v = this.video(); if (!v) return null; return { t: +v.currentTime.toFixed(2), paused: v.paused, readyState: v.readyState, rate: v.playbackRate, audioBytes: v.webkitAudioDecodedByteCount, frames: v.getVideoPlaybackQuality().totalVideoFrames, label: this.label() }; },
  cue() { const v = this.video(); for (const t of v.textTracks) if (t.mode === 'showing' && t.activeCues && t.activeCues.length) return t.activeCues[0].text; return null; },
  showing() { return [...this.video().textTracks].filter((t) => t.mode === 'showing').map((t) => t.label || t.language); },
  wake() { const p = document.querySelector('video')?.parentElement; for (const el of [p, document.body]) el?.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: 300, clientY: 300 })); },
  click(pattern, scope) { const re = new RegExp(pattern, 'i'); const all = [...(scope ? document.querySelectorAll(scope) : document.querySelectorAll('button, [role=tab], [role=option], [role=menuitemradio]'))]; const el = all.find((b) => re.test(b.getAttribute('aria-label') || '') || re.test(b.textContent || '')); if (!el) return false; el.click(); return true; },
  panel() { const p = document.querySelector('aside, [role=dialog]'); if (!p) return null; return { tabs: [...p.querySelectorAll('[role=tab]')].map((t) => ({ text: t.textContent.trim(), selected: t.getAttribute('aria-selected') === 'true', visible: (() => { const r = t.getBoundingClientRect(), pr = p.getBoundingClientRect(); return r.left >= pr.left - 1 && r.right <= pr.right + 1; })() })), options: [...p.querySelectorAll('button:not([role=tab])')].map((b) => ({ text: b.textContent.trim().replace(/\\s+/g, ' '), on: b.getAttribute('aria-pressed') === 'true' || b.getAttribute('aria-checked') === 'true' || /active|selected/i.test(b.className) })), text: p.textContent.replace(/\\s+/g, ' ').slice(0, 400) }; },
  // Dominant pitch of what is actually being decoded (the film's audio tracks differ in pitch).
  async pitch() {
    const v = this.video();
    // The window is a real one: a stray click or key press on it can mute the player.
    if (v.muted) { document.querySelector('[aria-label=Unmute]')?.click(); await new Promise((r) => setTimeout(r, 300)); }
    if (!this.ctx) { this.ctx = new AudioContext(); const src = this.ctx.createMediaElementSource(v); this.an = this.ctx.createAnalyser(); this.an.fftSize = 8192; this.an.smoothingTimeConstant = 0; /* no blending with the previous reading */ const g = this.ctx.createGain(); g.gain.value = 0.0001; src.connect(this.an); this.an.connect(g); g.connect(this.ctx.destination); }
    await this.ctx.resume();
    await new Promise((r) => setTimeout(r, 700));
    const data = new Float32Array(this.an.frequencyBinCount); this.an.getFloatFrequencyData(data);
    let best = 0; for (let i = 1; i < data.length; i++) if (data[i] > data[best]) best = i;
    return data[best] < -90 ? 0 : Math.round(best * this.ctx.sampleRate / this.an.fftSize);
  },
  sampler: null, samples: [],
  startSampling() { this.samples = []; clearInterval(this.sampler); this.sampler = setInterval(() => { const l = this.label(); if (l) this.samples.push(l.current); }, 50); },
  stopSampling() { clearInterval(this.sampler); return this.samples; },
};
1`;

const results = [];
const check = (name, pass, detail) => { results.push({ name, pass: Boolean(pass), detail }); console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail !== undefined ? `  ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); };
const wanted = (group) => !only.length || only.includes(group);

let page;
try {
  page = await connect();
  await page.evaluate(PAGE);
  const state = () => page.evaluate('window.__h.state()');
  const until = async (test, ms = 30000) => { const end = Date.now() + ms; let last; while (Date.now() < end) { last = await state(); if (last && test(last)) return last; await sleep(150); } return last; };
  const playing = (near) => (s) => !s.paused && s.readyState >= 3 && s.label && (near == null || Math.abs(s.label.current - near) <= 3);
  const digit = async (n) => { await page.evaluate('window.__h.wake()'); await page.key(String(n)); };

  // -- start -------------------------------------------------------------------
  const started = await until((s) => playing()(s) && s.label.current >= 2, 40000);
  check('plays through the local engine with picture and sound', started && started.frames > 10 && started.audioBytes > 0, started && { frames: started.frames, audioBytes: started.audioBytes });
  check('never asks the server to prepare it', !apiCalls.some((c) => c.path.includes('playback/remux/')), apiCalls.filter((c) => c.path.includes('remux')).map((c) => c.path));

  if (wanted('timeline')) {
    check('duration shows the length of the film from the first second', started?.label?.duration != null && Math.abs(started.label.duration - DURATION) <= 1, started?.label?.text);
    const a = await state(); await sleep(3000); const b = await state();
    check('the clock advances one second per second', b.label.current - a.label.current >= 2 && b.label.current - a.label.current <= 4, `${a.label.text} -> ${b.label.text}`);
    check('duration does not creep while playing', Math.abs(b.label.duration - DURATION) <= 1, b.label.text);

    // 50% is beyond what has been prepared from the start: the player restarts the session there.
    await digit(5);
    const half = await until(playing(DURATION * 0.5), 30000);
    check('seeking to 50% lands at 50% of the film', half && Math.abs(half.label.current - DURATION * 0.5) <= 3, half?.label?.text);
    check('duration is unchanged after the seek', half && Math.abs(half.label.duration - DURATION) <= 1, half?.label?.text);

    await sleep(1500);
    await page.evaluate('window.__h.startSampling()');
    await digit(8);
    const far = await until(playing(DURATION * 0.8), 30000);
    await sleep(500);
    const samples = await page.evaluate('window.__h.stopSampling()');
    const lowest = Math.min(...samples);
    check('seeking to 80% lands at 80%', far && Math.abs(far.label.current - DURATION * 0.8) <= 3, far?.label?.text);
    check('the clock never jumps back to 0:00 or the keyframe during a seek', lowest >= DURATION * 0.5 - 2, `lowest shown ${lowest}s of ${samples.length} samples`);

    // Truth check: the film's subtitle cues carry their own time ("EN cue N" spans 5N..5N+4 s).
    await page.evaluate(`for (const t of window.__h.video().textTracks) t.mode = /en/i.test(t.language + t.label) ? 'showing' : 'disabled'; 1`);
    let truth = null;
    for (let i = 0; i < 60 && !truth; i++) { await sleep(250); const cue = await page.evaluate('window.__h.cue()'); const s = await state(); const m = /cue (\d+)/.exec(cue || ''); if (m) truth = { cue, shown: s.label.current, from: m[1] * 5, to: m[1] * 5 + 4 }; }
    check('the time shown matches the film (subtitle cue as the reference)', truth && truth.shown >= truth.from - 1 && truth.shown <= truth.to + 1, truth ?? 'no subtitle cue became active');
  }

  if (wanted('audio')) {
    await digit(1); await until(playing(DURATION * 0.1), 30000);
    check('default audio is the 5.1 track (440 Hz)', Math.abs((await page.evaluate('window.__h.pitch()')) - 440) < 15, await page.evaluate('({ context: window.__h.ctx.state, clock: window.__h.ctx.currentTime, muted: window.__h.video().muted, volume: window.__h.video().volume })'));
    await page.evaluate('window.__h.wake()');
    await page.evaluate(`window.__h.click('^Audio and subtitles$')`); await sleep(400);
    await page.evaluate(`window.__h.click('^Audio$', '[role=tab]')`); await sleep(300);
    const panel = await page.evaluate('window.__h.panel()');
    const options = (panel?.options || []).map((o) => o.text);
    check('the Audio menu lists the three tracks of the file', ['TrueHD', 'Commentary', 'French|AC-3'].every((p) => options.some((o) => new RegExp(p, 'i').test(o))), options);
    const clicked = await page.evaluate(`window.__h.click('Commentary', 'aside button, [role=dialog] button')`);
    const before = await state();
    const after = clicked ? await until((s) => playing()(s) && s.t > 0.5, 30000) : null;
    await sleep(1500);
    const pitch = await page.evaluate('window.__h.pitch()');
    check('choosing Commentary plays that track (880 Hz)', clicked && Math.abs(pitch - 880) < 20, { clicked, pitch });
    const now = await state();
    check('switching audio keeps the position', clicked && now && Math.abs(now.label.current - before.label.current) <= 6, `${before?.label?.text} -> ${now?.label?.text}`);
    await digit(3); await until(playing(DURATION * 0.3), 30000); await sleep(1200);
    check('the chosen audio survives a seek', Math.abs((await page.evaluate('window.__h.pitch()')) - 880) < 20);
    await page.key('Escape', 'Escape');
  }

  if (wanted('subtitles')) {
    await page.evaluate('window.__h.wake()');
    await page.evaluate(`window.__h.click('^Audio and subtitles$')`); await sleep(300);
    await page.evaluate(`window.__h.click('^Subtitles$', '[role=tab]')`); await sleep(300);
    const panel = await page.evaluate('window.__h.panel()');
    const options = (panel?.options || []).map((o) => o.text);
    check('the Subtitles menu lists Off, English and French once each', ['^Off', 'English', 'French'].every((p) => options.filter((o) => new RegExp(p, 'i').test(o)).length === 1), options);
    await page.evaluate(`window.__h.click('French', 'aside button, [role=dialog] button')`);
    let cue = null; for (let i = 0; i < 40 && !cue; i++) { await sleep(250); cue = await page.evaluate('window.__h.cue()'); }
    check('French subtitles appear within 10 s', /^FR cue/.test(cue || ''), cue || await page.evaluate(`(() => { const v = document.querySelector('video'); const vtt = performance.getEntriesByType('resource').filter((e) => /\\.vtt/.test(e.name)).slice(-4).map((e) => e.name.slice(-40) + ' ' + Math.round(e.startTime) + ' ' + e.transferSize + '/' + e.decodedBodySize); return { t: v.currentTime, now: Math.round(performance.now()), vtt, tracks: [...v.textTracks].map((t) => ({ label: t.label, mode: t.mode, cues: t.cues ? t.cues.length : null, first: t.cues && t.cues[0] ? t.cues[0].startTime : null, last: t.cues && t.cues.length ? t.cues[t.cues.length - 1].endTime : null })) }; })()`));
    await page.key('Escape', 'Escape');
    await digit(6); await until(playing(DURATION * 0.6), 30000);
    cue = null; for (let i = 0; i < 40 && !cue; i++) { await sleep(250); cue = await page.evaluate('window.__h.cue()'); }
    const s = await state(); const n = Number(/cue (\d+)/.exec(cue || '')?.[1]);
    check('the subtitle choice survives a seek, in time with the film', /^FR cue/.test(cue || '') && s.label.current >= n * 5 - 1 && s.label.current <= n * 5 + 5, { cue, shown: s?.label?.text });
  }

  if (wanted('speed')) {
    await page.evaluate('window.__h.wake()');
    await page.evaluate(`window.__h.click('^Playback speed')`); await sleep(300);
    await page.evaluate(`window.__h.click('^1\\\\.5', 'aside button, [role=dialog] button')`); await sleep(400);
    check('speed 1.5x applies', (await state()).rate === 1.5);
    await page.key('Escape', 'Escape');
    await digit(4); await until(playing(DURATION * 0.4), 30000); await sleep(800);
    check('speed is kept after a seek', (await state()).rate === 1.5, (await state()).rate);
  }

  if (wanted('panel')) {
    await page.evaluate('window.__h.wake()');
    await page.evaluate(`window.__h.click('^Playback settings$|^More$|^Settings$')`) || await page.evaluate(`window.__h.click('^Audio and subtitles$')`);
    await sleep(400);
    const panel = await page.evaluate('window.__h.panel()');
    check('every settings tab is fully visible', panel && panel.tabs.length >= 3 && panel.tabs.every((t) => t.visible), panel?.tabs);
    check('the desktop app does not say the server prepared it', panel && !/server/i.test(panel.text), panel?.text?.slice(0, 160));
    await page.key('Escape', 'Escape');
  }

  if (wanted('resume')) {
    // A title with a saved position asks first, as on tvOS/iOS.
    const reopen = async () => {
      await page.evaluate(`location.href = location.pathname + '?position=60&duration=${DURATION}'`);
      for (let i = 0; i < 60; i++) { await sleep(250); try { if (await page.evaluate('Boolean(document.querySelector("video"))')) break; } catch { /* navigating */ } }
      await page.evaluate(PAGE);
      let text = ''; for (let i = 0; i < 40 && !/Resume Playing/.test(text); i++) { await sleep(250); text = await page.evaluate('document.querySelector("[role=dialog]")?.textContent || ""'); }
      return text;
    };
    const asked = await reopen();
    // This device's latest position wins (an earlier group may have played further); else the server's 1:00.
    const at = /Resume Playing \((\d+):(\d+)\)/.exec(asked); const savedAt = at ? Number(at[1]) * 60 + Number(at[2]) : -100;
    check('a saved position asks: resume or start from the beginning', savedAt >= 60 && /Start from Beginning/.test(asked), asked);
    await sleep(1500);
    check('nothing plays until the viewer answers', (await state())?.paused !== false, (await state())?.label?.text);
    await page.evaluate(`window.__h.click('^Resume Playing', '[role=dialog] button')`);
    const resumed = await until(playing(), 30000);
    check('Resume Playing continues from the saved position', resumed && Math.abs(resumed.label.current - savedAt) <= 6, resumed?.label?.text);
    await reopen();
    await page.evaluate(`window.__h.click('^Start from Beginning', '[role=dialog] button')`);
    const fresh = await until(playing(), 30000);
    check('Start from Beginning plays from 0:00', fresh && fresh.label.current <= 8, fresh?.label?.text);
  }

  if (process.env.VERBOSE) {
    console.log(page.consoleLines.filter((l) => l.includes('[curatio]')).join('\n'));
    try { console.log(fs.readFileSync(path.join(profile, 'engine.log'), 'utf8')); } catch { /* none */ }
    console.log(apiCalls.filter((c) => /remux|streams|inspect/.test(c.path)).map((c) => `${c.method} ${c.path}`).join('\n'));
  }
} catch (err) {
  check('the run completed', false, String(err?.stack || err));
}

const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
page?.close();
app.kill();
server.close();
setTimeout(() => { fs.rmSync(profile, { recursive: true, force: true }); process.exit(failed.length ? 1 : 0); }, 600);

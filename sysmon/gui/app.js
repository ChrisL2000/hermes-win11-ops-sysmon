/* sysmon dashboard -- no framework, no build step.
   Reads /api/stream (SSE) and falls back to polling /api/state. */

const $ = (id) => document.getElementById(id);
const state = {
  samples: [],          // {t, cpu, mem, gpu}
  trans: [],            // {t, progress} for the current transition
  runstates: [],
  lastState: null,
  es: null,
};

const MAX_SAMPLES = 300;

/* ---------------------------------------------------------------- fetch */
async function jget(path) {
  const r = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

async function jpost(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `${path} -> ${r.status}`);
  return data;
}

/* ----------------------------------------------------------------- init */
async function init() {
  try {
    const rs = await jget('/api/runstates');
    state.runstates = rs.runstates || [];
    const sel = $('target');
    sel.innerHTML = '';
    for (const s of state.runstates) {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = `${s.label}  (~${Math.round(s.est_seconds)}s)`;
      sel.appendChild(opt);
    }
    if (rs.current) sel.value = rs.current;
    if (rs.errors && rs.errors.length) appendLog(`manifest errors: ${rs.errors.join(' | ')}`);

    const head = await jget('/api/head');
    const name = head.agent.name;
    $('agent').textContent = name;
    $('host').textContent = `${head.host.hostname} · ${head.host.os.caption || ''} ${head.host.os.build || ''} · ${head.host.tailnet_ip || 'no tailnet'}`;
    const tok = (head.api.token_fingerprint || '').slice(0, 8);
    $('tokenfp').textContent = tok ? `token ${tok}…` : '';
    const links = head.links || {};
    if (links.grafana) $('l-grafana').href = links.grafana;
    if (links.kibana) $('l-kibana').href = links.kibana;
    document.title = `sysmon · ${head.host.hostname}`;
  } catch (e) {
    appendLog(`init failed: ${e.message}`);
  }
  await refresh();
  connect();
  setInterval(drawChart, 1000);
}

/* ---------------------------------------------------------------- live */
function connect() {
  if (state.es) state.es.close();
  const es = new EventSource('/api/stream');
  state.es = es;
  es.onmessage = (ev) => {
    try {
      const d = JSON.parse(ev.data);
      applyState(d.state, d.latest, d.health);
    } catch (e) { /* ignore malformed frame */ }
  };
  es.onerror = () => {
    es.close();
    state.es = null;
    setTimeout(async () => { await refresh(); connect(); }, 3000);
  };
}

async function refresh() {
  try {
    const d = await jget('/api/state');
    applyState(d, d.latest, { runstate: d.current.runstate, busy: d.busy });
    const l = await jget('/api/logs?n=60');
    renderLogs(l.logs || []);
  } catch (e) {
    appendLog(`refresh failed: ${e.message}`);
  }
}

/* --------------------------------------------------------------- render */
function applyState(st, latest, health) {
  if (!st) return;
  state.lastState = st;
  const cur = st.current || {};
  const manifest = state.runstates.find((r) => r.id === cur.runstate);
  $('curstate').textContent = manifest ? manifest.label : (cur.runstate || 'unknown');
  $('cursince').textContent = cur.since ? `since ${clock(cur.since)}` : '';
  $('curdesc').textContent = manifest ? manifest.description : '';

  if (health && health.uptime_s != null) $('uptime').textContent = `up ${dur(health.uptime_s)}`;

  const tr = st.transition;
  const busy = !!st.busy;
  const fill = $('meterfill');
  const dot = $('statusdot');
  const badge = $('tstate');

  $('go').disabled = busy;
  $('stop').disabled = !busy;

  if (tr) {
    const tgt = state.runstates.find((r) => r.id === tr.to);
    $('tfrom').textContent = tr.from || 'unknown';
    $('tto').textContent = tgt ? tgt.label : tr.to;
    $('tpct').textContent = `${Math.round(tr.progress || 0)}%`;
    fill.style.width = `${Math.min(100, tr.progress || 0)}%`;
    fill.className = 'meterfill' + (tr.state === 'failed' ? ' failed' : tr.state === 'aborted' ? ' aborted' : '');
    const elapsed = tr.started_at ? (Date.now() / 1000 - tr.started_at) : 0;
    const eta = tr.eta_s != null ? Math.round(tr.eta_s) : null;
    $('teta').textContent = tr.state === 'running'
      ? `${dur(elapsed)} elapsed · ~${eta}s left`
      : `${tr.state} · ${elapsed ? dur(elapsed) : ''}`;
    badge.textContent = tr.state;
    badge.className = 'badge ' + (tr.state === 'running' ? 'running'
      : tr.state === 'done' ? 'ok' : tr.state === 'failed' || tr.state === 'aborted' ? 'error' : '');
    dot.className = 'dot ' + (tr.state === 'running' ? 'running'
      : tr.state === 'failed' || tr.state === 'aborted' ? 'error' : 'ok');
    renderSteps(tr.steps || []);
    const running = (tr.steps || []).find((s) => s.status === 'running');
    $('phase').textContent = tr.error ? tr.error : (running ? `phase: ${running.phase}` : '');

    if (tr.state === 'running') {
      const last = state.trans[state.trans.length - 1];
      if (!last || Math.abs(last.progress - tr.progress) > 0.09 || Date.now() / 1000 - last.t > 2) {
        state.trans.push({ t: Date.now() / 1000, progress: tr.progress || 0 });
      }
    }
  } else {
    $('tfrom').textContent = '—';
    $('tto').textContent = '—';
    $('tpct').textContent = '0%';
    fill.style.width = '0%';
    fill.className = 'meterfill';
    $('teta').textContent = 'no transition';
    badge.textContent = 'idle';
    badge.className = 'badge';
    dot.className = 'dot ok';
    $('steps').innerHTML = '';
    $('phase').textContent = '';
  }

  if (latest) {
    const t = latest.t;
    state.samples.push({ t, cpu: latest.cpu || 0, mem: latest.mem || 0, gpu: latest.gpu });
    while (state.samples.length > MAX_SAMPLES) state.samples.shift();
    renderProcs(latest.top || []);
  }
}

function renderSteps(steps) {
  const ul = $('steps');
  const keep = ul.scrollTop;
  ul.innerHTML = '';
  for (const s of steps) {
    const li = document.createElement('li');
    li.className = s.status;
    const icon = { ok: '✓', failed: '×', running: '▶', skipped: '–', aborted: '■', pending: '·' }[s.status] || '·';
    li.innerHTML = `<span class="st">${icon}</span><span class="lbl">${esc(s.label)}</span>` +
      `<span class="det">${esc(s.verify_detail || s.detail || '')}</span>`;
    ul.appendChild(li);
  }
  ul.scrollTop = keep;
}

function renderProcs(rows) {
  const t = $('procs');
  t.innerHTML = rows.slice(0, 8).map((p) =>
    `<tr><td class="n">${esc(p.name)}</td><td class="v">${p.mem_mb} MB</td><td class="v">${p.cpu}%</td></tr>`
  ).join('');
}

function renderLogs(rows) {
  $('log').textContent = rows.slice(-40).map((r) => {
    const ts = (r['@timestamp'] || '').slice(11, 19);
    return `${ts} ${(r.log && r.log.level || 'info').padEnd(5)} ${r.event && r.event.kind || ''} ${r.message || ''}`;
  }).join('\n');
  $('log').scrollTop = $('log').scrollHeight;
}

function appendLog(line) {
  const el = $('log');
  el.textContent += (el.textContent ? '\n' : '') + line;
  el.scrollTop = el.scrollHeight;
}

/* ---------------------------------------------------------------- chart */
function drawChart() {
  const c = $('chart');
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight || 150;
  if (c.width !== Math.floor(w * dpr) || c.height !== Math.floor(h * dpr)) {
    c.width = Math.floor(w * dpr);
    c.height = Math.floor(h * dpr);
  }
  const g = c.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const s = state.samples;
  if (s.length < 2) {
    g.fillStyle = '#7c8a99';
    g.font = '11px system-ui';
    g.fillText('collecting…', 8, h / 2);
    return;
  }
  const t1 = s[s.length - 1].t;
  const t0 = t1 - 300;
  const x = (t) => Math.max(0, Math.min(w, ((t - t0) / 300) * w));
  const y = (v) => h - 4 - (Math.max(0, Math.min(100, v)) / 100) * (h - 10);

  // grid
  g.strokeStyle = '#1c232b';
  g.lineWidth = 1;
  for (const lvl of [25, 50, 75]) {
    g.beginPath(); g.moveTo(0, y(lvl)); g.lineTo(w, y(lvl)); g.stroke();
  }

  // progress overlay (transition) as a filled band
  if (state.trans.length > 1) {
    g.beginPath();
    g.moveTo(x(state.trans[0].t), h);
    for (const p of state.trans) g.lineTo(x(p.t), h - (p.progress / 100) * (h - 10) - 4);
    g.lineTo(x(state.trans[state.trans.length - 1].t), h);
    g.closePath();
    g.fillStyle = 'rgba(76,194,255,0.07)';
    g.fill();
    g.beginPath();
    state.trans.forEach((p, i) => {
      const px = x(p.t), py = h - (p.progress / 100) * (h - 10) - 4;
      i ? g.lineTo(px, py) : g.moveTo(px, py);
    });
    g.strokeStyle = 'rgba(76,194,255,0.55)';
    g.setLineDash([4, 3]);
    g.lineWidth = 1.4;
    g.stroke();
    g.setLineDash([]);
  }

  const line = (key, color, width) => {
    g.beginPath();
    let started = false;
    for (const p of s) {
      const v = p[key];
      if (v == null) { started = false; continue; }
      const px = x(p.t), py = y(v);
      started ? g.lineTo(px, py) : g.moveTo(px, py);
      started = true;
    }
    g.strokeStyle = color; g.lineWidth = width; g.lineJoin = 'round';
    g.stroke();
  };
  line('mem', 'rgba(67,209,124,0.75)', 1);
  line('gpu', 'rgba(167,139,250,0.8)', 1);
  line('cpu', 'rgba(76,194,255,0.95)', 1.4);

  // last values
  const last = s[s.length - 1];
  g.font = '10px ui-monospace, monospace';
  g.fillStyle = '#4cc2ff'; g.fillText(`cpu ${last.cpu.toFixed(0)}%`, 6, 11);
  g.fillStyle = '#a78bfa';
  if (last.gpu != null) g.fillText(`gpu ${last.gpu.toFixed(0)}%`, 66, 11);
  g.fillStyle = '#43d17c'; g.fillText(`ram ${last.mem.toFixed(0)}%`, 126, 11);
}

/* --------------------------------------------------------------- actions */
$('go').addEventListener('click', async () => {
  const target = $('target').value;
  if (!target) return;
  $('go').disabled = true;
  try {
    state.trans = [];
    await jpost('/api/transition', { target, dry_run: $('dryrun').checked });
    appendLog(`requested ${target}`);
    await refresh();
  } catch (e) {
    appendLog(`request failed: ${e.message}`);
    $('go').disabled = false;
  }
});

$('stop').addEventListener('click', async () => {
  try {
    const r = await jpost('/api/abort');
    appendLog(r.detail || 'abort requested');
  } catch (e) {
    appendLog(`abort failed: ${e.message}`);
  }
});

/* ----------------------------------------------------------------- util */
const esc = (v) => String(v ?? '').replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
const dur = (sec) => {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m${String(sec % 60).padStart(2, '0')}s`;
  return `${Math.floor(sec / 3600)}h${String(Math.floor((sec % 3600) / 60)).padStart(2, '0')}m`;
};
const clock = (epochS) => new Date(epochS * 1000).toLocaleTimeString();

init();

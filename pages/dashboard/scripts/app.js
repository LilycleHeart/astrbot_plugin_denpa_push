/**
 * 电波推送 · Signal Observatory — Dashboard 前端逻辑
 * 通过 window.AstrBotPluginPage bridge 与后端通信
 */

const bridge = window.AstrBotPluginPage;

// ─── State ───
const state = {
  ctx: null,
  status: null,
  subscriptions: {},
  logs: [],
  sessions: [],
  activeSession: '__all__',
};

// ─── Waveform Engine ───
class SignalWaveform {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.points = [];
    this.maxPoints = 300;
    this.phase = 0;
    this.spikes = [];
    this.active = false;
    this._resize();
    this._bindResize();
    this._loop();
  }

  _resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;
    if (w === 0 || h === 0) return;
    this.canvas.width = w * dpr;
    this.canvas.height = h * dpr;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = w;
    this.h = h;
    this.mid = h / 2;
  }

  _bindResize() {
    let timer;
    window.addEventListener('resize', () => {
      clearTimeout(timer);
      timer = setTimeout(() => this._resize(), 150);
    });
  }

  setActive(v) { this.active = v; }

  triggerSpike() {
    this.spikes.push({ x: this.w || 300, amp: 0.7 + Math.random() * 0.3, decay: 0 });
  }

  _loop() {
    this._draw();
    requestAnimationFrame(() => this._loop());
  }

  _draw() {
    const { ctx, w, h, mid } = this;
    if (!w || !h) { this._resize(); return; }
    ctx.clearRect(0, 0, w, h);

    // background grid lines
    ctx.strokeStyle = 'rgba(139, 154, 181, 0.06)';
    ctx.lineWidth = 1;
    for (let y = 0; y < h; y += 24) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    this.phase += this.active ? 0.03 : 0.012;

    // generate new point
    const baseAmp = this.active ? 6 : 2.5;
    let y = Math.sin(this.phase * 2.1) * baseAmp
          + Math.sin(this.phase * 5.7) * (baseAmp * 0.4)
          + (Math.random() - 0.5) * (this.active ? 3 : 1.2);

    // apply spikes
    for (let i = this.spikes.length - 1; i >= 0; i--) {
      const s = this.spikes[i];
      s.decay += 0.02;
      s.x -= 1.5;
      if (s.decay > 1 || s.x < -50) { this.spikes.splice(i, 1); continue; }
      const dist = Math.abs(w - s.x);
      if (dist < 60) {
        const envelope = Math.exp(-dist * dist / 800) * (1 - s.decay);
        y += Math.sin(dist * 0.3) * s.amp * h * 0.35 * envelope;
      }
    }

    this.points.push(y);
    if (this.points.length > this.maxPoints) this.points.shift();

    // draw waveform
    const step = w / this.maxPoints;
    const grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, 'rgba(0, 229, 200, 0.05)');
    grad.addColorStop(0.7, 'rgba(0, 229, 200, 0.6)');
    grad.addColorStop(1, 'rgba(0, 229, 200, 1)');

    ctx.beginPath();
    ctx.strokeStyle = grad;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = 'round';

    const len = this.points.length;
    const offsetX = w - len * step;
    for (let i = 0; i < len; i++) {
      const px = offsetX + i * step;
      const py = mid + this.points[i];
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    }
    ctx.stroke();

    // glow fill under curve
    ctx.lineTo(offsetX + (len - 1) * step, mid);
    ctx.lineTo(offsetX, mid);
    ctx.closePath();
    const fillGrad = ctx.createLinearGradient(0, mid - 40, 0, mid + 40);
    fillGrad.addColorStop(0, 'rgba(0, 229, 200, 0.08)');
    fillGrad.addColorStop(0.5, 'rgba(0, 229, 200, 0.03)');
    fillGrad.addColorStop(1, 'rgba(0, 229, 200, 0)');
    ctx.fillStyle = fillGrad;
    ctx.fill();

    // baseline
    ctx.beginPath();
    ctx.strokeStyle = 'rgba(139, 154, 181, 0.15)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 6]);
    ctx.moveTo(0, mid);
    ctx.lineTo(w, mid);
    ctx.stroke();
    ctx.setLineDash([]);
  }
}

// ─── Toast ───
function toast(msg, type = 'info') {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => { el.remove(); }, 3000);
}

// ─── Render: Status ───
function renderStatus(data) {
  if (!data) return;
  const el = document.getElementById('monitor-status');
  const textEl = document.getElementById('status-text');
  const running = data.monitor_running;

  el.className = `header-status ${running ? 'active' : 'inactive'}`;
  textEl.textContent = running ? '监听中' : '离线';

  document.getElementById('stat-subs').textContent = data.total_tracked || 0;
  document.getElementById('stat-pushes').textContent = data.total_pushes || 0;
  document.getElementById('stat-interval').textContent = `${data.poll_interval || 5}m`;

  // runtime pod
  document.getElementById('rt-loop').textContent = running ? '运行中' : '已停止';
  document.getElementById('rt-sessions').textContent = data.session_count || 0;
  document.getElementById('rt-auth').textContent = data.auth_configured ? '已配置' : '未配置';
  document.getElementById('rt-pw').textContent = data.playwright_ready ? '就绪' : '未启动';

  // config pod
  document.getElementById('cfg-interval').textContent = `${data.poll_interval || 5} 分钟`;
  document.getElementById('cfg-lang').textContent = data.translation_language || '中文';
  document.getElementById('cfg-color').textContent = data.color_source === 'first_image' ? '首图' : '头像';
  document.getElementById('cfg-gif').textContent = data.gif_encoder || 'auto';
  document.getElementById('cfg-proxy').textContent = data.proxy || '直连';

  waveform.setActive(running);
}

// ─── Render: Subscriptions ───
function renderSubs(data) {
  if (!data) return;
  const list = document.getElementById('sub-list');
  const empty = document.getElementById('sub-empty');
  const select = document.getElementById('session-select');

  // sessions dropdown
  const sessions = Object.keys(data);
  state.sessions = sessions;
  const currentVal = select.value;
  select.innerHTML = '<option value="__all__">全部会话</option>';
  sessions.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s;
    opt.textContent = s.length > 24 ? s.slice(0, 24) + '…' : s;
    select.appendChild(opt);
  });
  if (sessions.includes(currentVal) || currentVal === '__all__') select.value = currentVal;

  // flatten subs
  let items = [];
  const filterSession = state.activeSession;
  for (const [session, users] of Object.entries(data)) {
    if (filterSession !== '__all__' && session !== filterSession) continue;
    for (const [name, info] of Object.entries(users)) {
      items.push({ name, info, session });
    }
  }

  // clear old items (keep empty state element)
  list.querySelectorAll('.sub-item').forEach(el => el.remove());

  if (items.length === 0) {
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';

  items.forEach(({ name, info }) => {
    const li = document.createElement('li');
    li.className = 'sub-item';
    const initial = name.charAt(0).toUpperCase();
    const lastCheck = info.last_checked_at
      ? new Date(info.last_checked_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
      : '—';
    li.innerHTML = `
      <div class="sub-avatar">${initial}</div>
      <div class="sub-info">
        <div class="sub-name">@${name}</div>
        <div class="sub-meta">最后检查 ${lastCheck}</div>
      </div>
      <button class="sub-remove" data-name="${name}" title="取消追踪">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
      </button>
    `;
    list.appendChild(li);
  });

  // bind remove buttons
  list.querySelectorAll('.sub-remove').forEach(btn => {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.name;
      try {
        await bridge.apiPost('dashboard/unsubscribe', { username: name });
        toast(`已取消追踪 @${name}`, 'success');
        refresh();
      } catch (e) {
        toast(e?.message || '操作失败', 'error');
      }
    });
  });
}

// ─── Render: Logs ───
function renderLogs(data) {
  if (!data) return;
  const list = document.getElementById('log-list');
  const empty = document.getElementById('log-empty');

  list.querySelectorAll('.log-item').forEach(el => el.remove());

  const logs = data.logs || data;
  if (!Array.isArray(logs) || logs.length === 0) {
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';

  logs.slice(0, 30).forEach(log => {
    const el = document.createElement('div');
    el.className = 'log-item';
    const time = new Date(log.time).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    el.innerHTML = `
      <span class="log-time">${time}</span>
      <span class="log-badge ${log.type}">${log.type === 'push' ? '推送' : log.type === 'error' ? '异常' : '信息'}</span>
      <span class="log-text">${escapeHtml(log.message)}</span>
    `;
    list.appendChild(el);
  });
}

function escapeHtml(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

// ─── Refresh ───
async function refresh() {
  try {
    const [status, subs, logs] = await Promise.all([
      bridge.apiGet('dashboard/status'),
      bridge.apiGet('dashboard/subscriptions'),
      bridge.apiGet('dashboard/logs'),
    ]);
    if (status) { state.status = status; renderStatus(status); }
    if (subs) { state.subscriptions = subs; renderSubs(subs); }
    if (logs) { state.logs = logs; renderLogs(logs); }
  } catch (e) {
    console.warn('[DenpaPush] refresh error:', e);
  }
}

// ─── Init ───
let waveform;

async function init() {
  state.ctx = await bridge.ready();

  waveform = new SignalWaveform(document.getElementById('waveform'));

  // add subscription
  document.getElementById('add-btn').addEventListener('click', async () => {
    const input = document.getElementById('add-input');
    const name = input.value.trim().replace(/^@/, '');
    if (!name) return;
    try {
      await bridge.apiPost('dashboard/subscribe', { username: name });
      toast(`已开始追踪 @${name}`, 'success');
      input.value = '';
      waveform.triggerSpike();
      refresh();
    } catch (e) {
      toast(e?.message || '添加失败', 'error');
    }
  });

  document.getElementById('add-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') document.getElementById('add-btn').click();
  });

  // session filter
  document.getElementById('session-select').addEventListener('change', (e) => {
    state.activeSession = e.target.value;
    renderSubs(state.subscriptions);
  });

  // initial load + polling
  refresh();
  setInterval(refresh, 30000);

  // spike on new push (poll logs diff)
  let lastLogCount = 0;
  setInterval(async () => {
    try {
      const logs = await bridge.apiGet('dashboard/logs');
      const arr = logs?.logs || logs;
      if (Array.isArray(arr)) {
        if (lastLogCount > 0 && arr.length > lastLogCount) {
          waveform.triggerSpike();
        }
        lastLogCount = arr.length;
      }
    } catch (_) {}
  }, 15000);
}

init();

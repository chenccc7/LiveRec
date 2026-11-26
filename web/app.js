/* LiveRec 控制台 — 前端逻辑（真实 API + WebSocket 实时推送） */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* ── 平台 / 图标 / 常量 ─────────────────────────────── */
const PLAT = {
  douyin:   { name: '抖音', cls: 'douyin',   color: '#fe2c55' },
  bilibili: { name: 'B站',  cls: 'bilibili', color: '#00a1d6' },
  huya:     { name: '虎牙', cls: 'huya',     color: '#ff9600' },
  douyu:    { name: '斗鱼', cls: 'douyu',    color: '#ff6d00' },
  custom:   { name: '自定义', cls: 'douyu',  color: '#6b7280' },
  unknown:  { name: '未知', cls: 'douyu',    color: '#6b7280' }
};

const ICON = {
  play:  '<path d="M8 5.2v13.6c0 .8.9 1.3 1.6.9l10.4-6.8c.6-.4.6-1.4 0-1.8L9.6 4.3C8.9 3.9 8 4.4 8 5.2z"/>',
  stop:  '<rect x="6.5" y="6.5" width="11" height="11" rx="1.8"/>',
  pause: '<rect x="7" y="5.5" width="3.6" height="13" rx="1.2"/><rect x="13.4" y="5.5" width="3.6" height="13" rx="1.2"/>',
  edit:  '<path d="M4 20h4.2L19.1 9.1a2.1 2.1 0 0 0 0-3L17.9 4.9a2.1 2.1 0 0 0-3 0L4 15.8V20z" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"/>',
  folder:'<path d="M3.5 7.2A1.7 1.7 0 0 1 5.2 5.5h4l2 2.3h7.6a1.7 1.7 0 0 1 1.7 1.7v7.8a1.7 1.7 0 0 1-1.7 1.7H5.2a1.7 1.7 0 0 1-1.7-1.7V7.2z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
  eye:   '<path d="M2.5 12s3.6-6.2 9.5-6.2S21.5 12 21.5 12 17.9 18.2 12 18.2 2.5 12 2.5 12z" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="12" r="2.6" fill="none" stroke="currentColor" stroke-width="1.8"/>',
  trash: '<path d="M4.5 6.8h15M9.3 6.8V4.9h5.4v1.9M6.6 6.8l.9 12.1a1.7 1.7 0 0 0 1.7 1.6h5.6a1.7 1.7 0 0 0 1.7-1.6l.9-12.1" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
  doc:   '<path d="M6.5 3.8h6.1l4.9 4.9v11.5a1.6 1.6 0 0 1-1.6 1.6H6.5a1.6 1.6 0 0 1-1.6-1.6V5.4a1.6 1.6 0 0 1 1.6-1.6z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M12.4 4v4.9h5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>',
  chev:  '<path d="M6 9.5l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  warn:  '<path d="M12 3.4 22 20.2H2L12 3.4z" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"/><path d="M12 9.4v4.6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="17.2" r="1.05" fill="currentColor"/>',
  clock: '<circle cx="12" cy="12" r="8.6" fill="none" stroke="currentColor" stroke-width="1.9"/><path d="M12 7.4V12l3.2 2" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>',
  retry: '<path d="M20 12a8 8 0 1 1-2.6-5.9M20 4v4.2h-4.2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
};
const svg = (path, size) =>
  `<svg viewBox="0 0 24 24" width="${size || 14}" height="${size || 14}" fill="currentColor" stroke="none">${path}</svg>`;

const STATUS_TEXT = {
  recording: '录制中', waiting: '等待开播', stopped: '已停止',
  error: '异常', idle: '待机', starting: '启动中'
};
const QUALITIES = ['原画', '蓝光', '超清', '高清', '标清', '流畅'];
const FORMATS = [
  ['ts', 'TS（边录边写，最稳）'],
  ['mkv', 'MKV（多轨，兼容好）'],
  ['flv', 'FLV（原始封装）'],
  ['mp4', 'MP4（分片写入）'],
  ['mp3音频', 'MP3 音频'],
  ['m4a音频', 'M4A 音频']
];
const DM_TEXT = {
  on: '同步中', off: '未开启',
  unsupported: '适配中', error: '连接异常', connecting: '连接中'
};

/* ── 全局状态 ───────────────────────────────────────── */
const S = {
  version: '',
  rooms: [],
  stats: {},
  settings: {},
  cookies: {},
  logs: [],
  logSeq: 0,
  logPath: '',
  ffmpeg: '',
  dm: {},              // rid -> [{type,nickname,content,giftName,giftCount,offsetMs}]
  dmOpen: new Set(),   // 展开弹幕抽屉的 rid
  editing: null,       // 正在编辑的房间
  pendingDanmaku: true, // 新增房间时是否同步采集弹幕
  unattended: true,    // 无人值守：启动即自动监测
  checkInterval: 60,   // 开播检测间隔（秒）
  cookieState: {}      // platform -> 最近一次 Cookie 检测结果
};

/* ── 工具函数 ──────────────────────────────────────── */
const pad2 = n => String(n).padStart(2, '0');

function fmtDur(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  if (!s) return '00:00:00';
  return `${pad2(Math.floor(s / 3600))}:${pad2(Math.floor((s % 3600) / 60))}:${pad2(s % 60)}`;
}

function fmtMeter(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  // 分段时长可能 ≥ 1 小时，必须把小时位带上，否则 3600 会被截成 00:00
  return h > 0 ? `${h}:${pad2(m)}:${pad2(ss)}` : `${pad2(m)}:${pad2(ss)}`;
}

function tsMs(v) {
  if (!v) return 0;
  const n = Number(v);
  if (Number.isFinite(n) && n > 0) return n > 1e12 ? n : n * 1000;
  const d = Date.parse(String(v));
  return Number.isFinite(d) ? d : 0;
}

function fmtClock(ts) {
  const n = Number(ts) || 0;
  if (!n) return '—';
  const d = new Date(n > 1e12 ? n : n * 1000);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

function fmtBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(2)} ${units[i]}`;
}

function fmtOffset(ms) {
  const s = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  return `${pad2(Math.floor(s / 60))}:${pad2(s % 60)}`;
}

function fmtNum(n) {
  return Number(n || 0).toLocaleString('zh-CN');
}

function initials(name) {
  const ch = Array.from(String(name || '').trim())[0] || '?';
  return /[a-zA-Z]/.test(ch) ? ch.toUpperCase() : ch;
}

function avatarStyle(plat) {
  const p = (PLAT[plat] || PLAT.unknown).color;
  return `background:linear-gradient(145deg, ${p}, rgba(0,0,0,.55))`;
}

function maskWord(v) {
  const s = String(v == null ? '' : v);
  return s.length > 60 ? `${s.slice(0, 58)}…` : s;
}

/* ── HTTP / WebSocket ──────────────────────────────── */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* 空响应 */ }
  if (!res.ok) {
    const msg = (data && (data.detail || data.error)) || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

let ws = null;
let wsTimer = null;
// 删除确认态放在这里而不是按钮 DOM 上：卡片每秒刷新会重绘按钮，DOM 上的状态会被冲掉。
let armedRid = '';
let armedTimer = 0;

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    setOnline(true);
    if (wsTimer) { clearTimeout(wsTimer); wsTimer = null; }
  };

  ws.onmessage = ev => {
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    handleEvent(msg);
  };

  ws.onclose = () => {
    setOnline(false);
    wsTimer = setTimeout(connectWS, 2000);
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}

function setOnline(ok) {
  const el = $('.logs-head .live, .logs-head span:last-child');
  document.body.dataset.online = ok ? '1' : '0';
  const dot = $('.logs-head .dot-live');
  if (dot) dot.style.opacity = ok ? '1' : '.25';
}

function handleEvent(msg) {
  switch (msg.type) {
    case 'hello':
      S.version = msg.version || '';
      S.rooms = msg.rooms || [];
      S.stats = msg.stats || {};
      S.settings = msg.settings || {};
      S.unattended = msg.unattended !== false;
      S.checkInterval = msg.checkInterval || 60;
      S.cookies = msg.cookies || {};
      S.logPath = msg.logPath || '';
      applyLogs(msg.logs || []);
      renderAll();
      loadAllDanmaku();
      break;
    case 'unattended':
      S.unattended = msg.unattended !== false;
      if (msg.checkInterval) S.checkInterval = msg.checkInterval;
      break;
    case 'rooms':
      refreshState();
      break;
    case 'room':
      if (msg.room) {
        mergeRooms([msg.room]);
        renderStats();
        patchRoom(msg.room);
      }
      break;
    case 'tick':
      if (msg.stats) S.stats = msg.stats;
      if (msg.rooms) mergeRooms(msg.rooms);
      renderStats();
      (msg.rooms || []).forEach(patchRoom);
      break;
    case 'danmaku':
      onDanmaku(msg.event || {});
      break;
    default:
      break;
  }
}

async function refreshState() {
  try {
    const st = await api('/api/state');
    S.version = st.version || S.version;
    S.rooms = st.rooms || [];
    S.stats = st.stats || S.stats;
    S.settings = st.settings || S.settings;
    S.unattended = st.unattended !== false;
    S.checkInterval = st.checkInterval || S.checkInterval || 60;
    S.cookies = st.cookies || S.cookies;
    S.ffmpeg = st.ffmpeg || S.ffmpeg;
    S.logPath = st.logPath || S.logPath;
    renderAll();
    loadAllDanmaku();
  } catch (err) {
    pushLocalLog('ERROR', `刷新状态失败：${err.message}`);
  }
}

/* 打开程序/刷新状态时，把各房间已有的弹幕历史拉回来填进抽屉 */
async function loadAllDanmaku() {
  for (const r of S.rooms) {
    const rid = String(r.id);
    try {
      const res = await api(`/api/danmaku/${rid}?limit=40`);
      const items = (res && (res.items || res.events)) || (Array.isArray(res) ? res : []);
      if (!items.length) continue;
      S.dm[rid] = items.slice(-200).map(it => ({
        type: it.type, nickname: it.nickname, content: it.content,
        giftName: it.gift_name || it.giftName, giftCount: it.gift_count || it.giftCount,
        offsetMs: it.offset_ms || it.offsetMs
      }));
    } catch (err) {
      /* 单个房间拉历史失败不影响其它房间 */
    }
  }
  renderAll();
}

function mergeRooms(list) {
  list.forEach(snap => {
    const idx = S.rooms.findIndex(r => String(r.id) === String(snap.id));
    if (idx === -1) { S.rooms.push(snap); return; }
    S.rooms[idx] = { ...S.rooms[idx], ...snap };
  });
}

/* ── 页面渲染：概览 / 页脚 / 日志 ──────────────────── */

async function toggleRoomDanmaku(rid, next) {
  try {
    await api(`/api/rooms/${rid}`, { method: 'PATCH', body: { danmaku: next } });
    const room = S.rooms.find(r => String(r.id) === String(rid));
    if (room) room.danmaku = next;
    pushLocalLog('INFO', next ? '该直播间已开启弹幕录制（会重启录制任务）' : '该直播间已关闭弹幕录制（会重启录制任务）');
    renderAll();
  } catch (err) {
    pushLocalLog('ERROR', `切换弹幕录制失败：${err.message}`);
  }
}

function renderStats() {
  const st = S.stats || {};
  const vals = [
    { v: `${st.total || 0}`, u: '' },
    { v: `${st.recording || 0}`, u: '个' },
    { v: `${st.waiting || 0}`, u: '个' },
    { v: fmtBytes(st.bytes || 0), u: '' },
    { v: fmtNum(st.danmaku || 0), u: '条' }
  ];
  $$('.overview .ov .v').forEach((el, i) => {
    const it = vals[i];
    if (!it) return;
    el.innerHTML = it.u ? `${it.v}<small>${it.u}</small>` : it.v;
  });
}

function renderFooter() {
  const foot = $$('.foot span');
  if (foot[0]) foot[0].textContent = S.ffmpeg ? `ffmpeg ${shortPath(S.ffmpeg)}` : 'ffmpeg 未找到';
  if (foot[1]) foot[1].textContent = `保存目录 ${S.settings.saveRoot || 'downloads/'}`;
  if (foot[2]) foot[2].textContent = `监听 ${location.host}`;
  if (foot[3]) foot[3].textContent = `LiveRec v${S.version || '0.1.0'} · core v4.0.7`;

  const head = $$('.logs-head span').find(el => /logs[\\/]|\.log/.test(el.textContent));
  if (head) head.textContent = S.logPath ? shortPath(S.logPath) : 'logs/';
}

function shortPath(p) {
  const s = String(p || '');
  return s.length > 46 ? `…${s.slice(-44)}` : s;
}

function isOn(v) {
  if (v === true) return true;
  if (v === false || v == null) return false;
  const s = String(v).trim().toLowerCase();
  return s === '是' || s === 'true' || s === '1' || s === 'yes' || s === 'on';
}

function lvClass(level) {
  const l = String(level || '').toUpperCase();
  if (l.startsWith('SUCC')) return 's';
  if (l.startsWith('WARN')) return 'w';
  if (l.startsWith('ERR')) return 'e';
  return 'i';
}

function logRow(entry) {
  const lv = String(entry.level || 'INFO').toUpperCase();
  const label = ({ S: 'SUCCESS', W: 'WARN', E: 'ERROR' })[lv[0]] || 'INFO';
  const time = entry.time || new Date(Number(entry.ts || 0) * 1000).toTimeString().slice(0, 8);
  const div = document.createElement('div');
  div.className = 'lrow';
  div.innerHTML = `<span class="ts">${time}</span>` +
    `<span class="lv ${lvClass(lv)}">${label}</span>` +
    `<span class="msg">${escapeHtml(entry.message || '')}</span>`;
  return div;
}

function applyLogs(list) {
  const box = $('#logs');
  if (!box) return;
  if (!list.length && !box.children.length) {
    box.innerHTML = '<div class="lrow"><span class="ts">--:--:--</span><span class="lv i">INFO</span>' +
      '<span class="msg">等待事件…</span></div>';
    return;
  }
  list.forEach(entry => {
    if (entry.id && entry.id <= S.logSeq) return;
    if (entry.id) S.logSeq = Math.max(S.logSeq, entry.id);
    box.appendChild(logRow(entry));
  });
  while (box.children.length > 200) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

function pushLocalLog(level, message) {
  applyLogs([{ id: undefined, level, message, time: new Date().toTimeString().slice(0, 8) }]);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ── 卡片渲染 ──────────────────────────────────────── */
function roomSplit(r) {
  const mode = String(r.splitMode || 'default').toLowerCase();
  if (mode === 'off') return { on: false, secs: 0 };
  if (mode === 'custom') {
    const secs = Number(r.splitSeconds) || Number(S.settings.splitSeconds) || 1800;
    return { on: true, secs };
  }
  return { on: !!S.settings.splitEnabled, secs: Number(S.settings.splitSeconds) || 1800 };
}

function roomView(r) {
  const sp = roomSplit(r);
  const splitOn = sp.on;
  const splitSecs = sp.secs || 1800;
  const elapsed = Number(r.elapsed) || 0;
  const segElapsed = splitOn ? elapsed % splitSecs : elapsed;
  const segTotal = splitOn ? splitSecs : 0;
  return {
    ...r,
    status: r.status === 'starting' ? 'waiting' : (r.status || 'idle'),
    elapsedText: fmtDur(elapsed),
    startText: fmtClock(r.startedAt),
    // 卡片上的「录制时长」按开始录制的时间戳算，和文件时长/体积无关
    timerText: r.startedAt ? fmtDur(Math.max(0, Math.floor(
      (Date.now() - tsMs(r.startedAt)) / 1000))) : '—',
    sizeText: r.sizeText || fmtBytes(r.size || 0),
    bitrateText: r.bitrate || '—',
    resText: r.resolution || '—',
    segText: r.currentFile ? `${Math.max(1, r.segments || 1)} 段` : '—',
    meterNow: fmtMeter(segElapsed),
    meterTotal: splitOn ? fmtMeter(segTotal) : '不分段',
    meterPct: splitOn ? Math.min(100, (segElapsed / segTotal) * 100) : 100,
    fileName: r.currentFile ? String(r.currentFile).split(/[\\/]/).pop() : '',
    proxy: isOn(S.settings.proxyEnabled),
    unattended: r.unattended !== false,
    danmaku: r.danmaku !== false,
    qualityNote: r.qualityNote || '',
    qualityActual: r.qualityActual || '',
    note: r.note || '',
    checkSecs: Number(r.checkInterval) || Number(S.checkInterval) || 60,
    nextCheck: Number(r.nextCheck) || 0
  };
}

function checkText(r) {
  if (!r.nextCheck) return `每 ${r.checkSecs}s 检测一次`;
  const remain = Math.max(0, Math.round((r.nextCheck - Date.now()) / 1000));
  if (remain <= 0) return '正在检测开播状态…';
  return `${remain}s 后再次检测`;
}

function renderCard(r0) {
  const r = roomView(r0);
  const p = PLAT[r.platform] || PLAT.unknown;
  const rec = r.status === 'recording';
  const wait = r.status === 'waiting';
  const err = r.status === 'error';
  const idle = r.status === 'idle';

  const statusHtml = rec
    ? `<span class="status recording" title="开始录制 ${r.startText}"><i class="dot"></i>录制中 <b data-f="start">${r.startText}</b></span>`
    : `<span class="status ${r.status}"><i class="dot"></i>${STATUS_TEXT[r.status] || r.status}</span>`;

  const tags = [
    `<span class="tag plat ${p.cls}">${p.name}</span>`,
    r.qualityNote
      ? `<span class="tag warn" title="${escapeHtml(r.qualityNote)}">${r.qualityActual || r.quality || '原画'}·降级</span>`
      : `<span class="tag">${r.quality || '原画'}</span>`,
    r.proxy ? '<span class="tag proxy">代理</span>' : '',
    // 每张卡片各自的弹幕开关：关掉就是「只录视频、不录弹幕」
    `<button class="tag dm${r.danmaku ? ' on' : ''}" data-act="dm"
       title="${r.danmaku
        ? '弹幕录制：已开启（点击关闭，只录视频）'
        : '弹幕录制：已关闭（点击开启，切换时会重启录制任务）'}">
       <i class="autodot"></i>${r.danmaku ? '弹幕' : '无弹幕'}</button>`,
    // 每张卡片各自的无人值守开关：开着 = 下次启动会自动进入开播监测
    ``
  ].join('');

  let body = '';

  if (rec) {
    body = `
      <div class="meter">
        <div class="row"><span>当前分段 ${r.meterNow} / ${r.meterTotal}</span><b>第 ${Math.max(1, r.segments || 1)} 段</b></div>
        <div class="bar"><i style="width:${r.meterPct.toFixed(1)}%"></i></div>
      </div>
      <div class="stats">
        <div class="stat"><div class="k">文件大小</div><div class="v" data-f="size">${r.sizeText}</div></div>
        <div class="stat"><div class="k">码率</div><div class="v" data-f="bitrate">${r.bitrateText}</div></div>
        <div class="stat"><div class="k">分辨率</div><div class="v" data-f="res">${r.resText}</div></div>
      </div>
      <div class="file">${svg(ICON.doc, 12)}<span data-f="file">${escapeHtml(r.fileName || '等待第一段落盘…')}</span></div>`;
  } else if (wait) {
    body = `
      <div class="notice waiting">
        ${svg(ICON.clock, 14)}
        <div>
          ${r.title ? escapeHtml(r.title) : '—— 暂未开播 ——'}
          <span class="fix" style="color:#b99c62"><b data-f="check">${checkText(r)}</b>；开播后自动开始录制</span>
          ${r.note ? `<span class="fix" style="color:#9aa6b8" data-f="note">${escapeHtml(r.note)}</span>` : ''}
        </div>
      </div>`;
  } else if (err) {
    const raw = String(r.error || '未知错误');
    const cut = raw.search(/[：:]/);
    const code = cut > 0 ? raw.slice(0, cut) : raw.slice(0, 40);
    const fix = cut > 0 ? raw.slice(cut + 1).trim() : '查看日志了解失败原因，或点击左下角重试';
    body = `
      <div class="notice error">
        ${svg(ICON.warn, 14)}
        <div>
          <code>${escapeHtml(code)}</code>
          <span class="fix">${escapeHtml(fix)}</span>
        </div>
      </div>`;
  } else {
    body = `
      <div class="stats">
        <div class="stat"><div class="k">录制时长</div><div class="v" data-f="timer">${r.timerText}</div></div>
        <div class="stat"><div class="k">文件大小</div><div class="v" data-f="size">${r.sizeText}</div></div>
        <div class="stat"><div class="k">分辨率</div><div class="v" data-f="res">${r.resText}</div></div>
      </div>
      ${r.fileName ? `<div class="file">${svg(ICON.doc, 12)}<span data-f="file">${escapeHtml(r.fileName)}</span></div>` : ''}`;
  }

  const dmCount = Number(r.danmakuCount) || 0;
  const dmStatus = r.danmakuStatus || 'off';
  const dmOn = dmStatus === 'on';
  const dmHtml = (r.danmaku && (rec || dmCount > 0)) ? `
    <div class="dm ${S.dmOpen.has(String(r.id)) ? 'open' : ''}">
      <div class="dm-head">
        <span>弹幕</span>
        ${dmOn ? '<span class="live"><i></i>LIVE</span>' : ''}
        <span class="chev">${svg(ICON.chev, 13)}</span>
      </div>
      <div class="dm-list" data-f="dmlist">${dmNodes(r.id)}</div>
    </div>` : '';

  const folderBtn = `<button class="ibtn" data-act="folder" title="打开录制目录">${svg(ICON.folder)}</button>`;
  const liveBtn = `<button class="ibtn" data-act="live" title="打开直播页">${svg(ICON.eye)}</button>`;
  const editBtn = `<button class="ibtn" data-act="edit" title="编辑">${svg(ICON.edit)}</button>`;
  const delBtn = `<button class="ibtn danger${armedRid === String(r.id) ? ' armed' : ''}" data-act="trash"
    title="${armedRid === String(r.id) ? '再点一次确认删除' : '删除'}">${svg(ICON.trash)}</button>`;

  const actions = rec
    ? `<button class="ibtn stop" data-act="stop" title="停止录制">${svg(ICON.stop)}</button>${liveBtn}${folderBtn}<div class="spacer"></div>${editBtn}${delBtn}`
    : wait
      ? `<button class="ibtn" data-act="retry" title="立即检测一次">${svg(ICON.retry)}</button><button class="ibtn" data-act="stop" title="暂停监测">${svg(ICON.pause)}</button>${folderBtn}<div class="spacer"></div>${editBtn}${delBtn}`
      : err
        ? `<button class="ibtn" data-act="retry" title="重试">${svg(ICON.retry)}</button>${folderBtn}<div class="spacer"></div>${editBtn}${delBtn}`
        : `<button class="ibtn" data-act="start" title="开始录制">${svg(ICON.play)}</button>${folderBtn}<div class="spacer"></div>${editBtn}${delBtn}`;

  return `
  <article class="card ${r.status}" data-id="${r.id}" data-status="${r.status}" data-qnote="${escapeHtml(r.qualityNote)}" data-note="${escapeHtml(r.note || '')}" data-auto="${r.unattended ? '1' : '0'}" data-dm="${r.danmaku ? '1' : '0'}">
    <div class="chead">
      ${statusHtml}
      <div class="spacer"></div>
      <div class="tags">${tags}</div>
    </div>
    <div class="who">
      <div class="avatar" style="${avatarStyle(r.platform)}">${initials(r.anchor || '?')}</div>
      <div class="info">
        <div class="name">${escapeHtml(r.anchor || '未命名直播间')}</div>
        <div class="title">${escapeHtml(r.title || '——')}</div>
      </div>
    </div>
    ${body}
    ${dmHtml}
    <div class="cact">${actions}</div>
  </article>`;
}

function dmNodes(rid) {
  const list = S.dm[String(rid)] || [];
  if (!list.length) {
    return '<div class="dmi"><span class="t">--:--</span><span class="nick">系统</span>' +
      '<span class="txt" style="color:#4a5468">暂无弹幕数据…</span></div>';
  }
  return list.slice(-40).map(d => dmNode(d)).join('');
}

function dmNode(d) {
  const type = d.type === 'gift' ? 'gift' : d.type === 'enter' ? 'enter' : d.type === 'like' ? 'like' : 'chat';
  const nick = escapeHtml(d.nickname || '匿名');
  let text = escapeHtml(d.content || '');
  if (type === 'gift') {
    text = `送出 ${escapeHtml(d.giftName || '礼物')}${d.giftCount > 1 ? ` ×${d.giftCount}` : ''}`;
  } else if (type === 'enter') {
    text = '进入直播间';
  } else if (type === 'like') {
    text = d.content ? escapeHtml(d.content) : '点赞';
  }
  return `<div class="dmi ${type}"><span class="t">${fmtOffset(d.offsetMs)}</span>` +
    `<span class="nick">${nick}</span><span class="txt">${text}</span></div>`;
}

function renderAll() {
  renderStats();
  renderFooter();
  const grid = $('#grid');
  if (!S.rooms.length) {
    grid.innerHTML = `<div class="empty" style="grid-column:1/-1;text-align:center;padding:64px 0;color:#5b6579;line-height:2">
      <div style="font-size:15px;color:#8b95a8">还没有添加直播间</div>
      <div>点右上角「添加直播间」粘贴地址即可，支持抖音 / B站 / 斗鱼 / 虎牙</div>
    </div>`;
    return;
  }
  grid.innerHTML = S.rooms.map(renderCard).join('');
}

function patchRoom(snap) {
  const card = $(`.card[data-id="${snap.id}"]`);
  if (!card) { renderAll(); return; }
  const room = S.rooms.find(r => String(r.id) === String(snap.id)) || {};
  const view = roomView({ ...room, ...snap });

  const needRerender =
    card.dataset.status !== view.status ||
    (card.dataset.qnote || '') !== view.qualityNote ||
    (card.dataset.note || '') !== view.note ||
    (card.dataset.auto || '1') !== (view.unattended ? '1' : '0') ||
    (card.dataset.dm || '1') !== (view.danmaku ? '1' : '0') ||
    (card.querySelector('.dm') !== null) !== (view.status === 'recording' || Number(view.danmakuCount) > 0);

  if (needRerender) {
    const html = renderCard({ ...room, ...snap });
    const tmp = document.createElement('div');
    tmp.innerHTML = html.trim();
    card.replaceWith(tmp.firstElementChild);
    return;
  }

  const set = (sel, val) => {
    const el = card.querySelector(sel);
    if (el && el.textContent !== String(val)) el.textContent = val;
  };
  set('[data-f="start"]', view.startText);
  set('[data-f="elapsed"]', view.elapsedText);
  set('[data-f="timer"]', view.timerText);
  set('[data-f="size"]', view.sizeText);
  set('[data-f="bitrate"]', view.bitrateText);
  set('[data-f="res"]', view.resText);
  const segEl = card.querySelector('[data-f="seg"]');
  if (segEl) segEl.textContent = view.status === 'recording'
    ? `${Math.max(1, view.segments || 1)} 个`
    : view.segText;
  if (view.fileName) set('[data-f="file"]', view.fileName);
  const bar = card.querySelector('.meter .bar > i');
  if (bar) bar.style.width = `${view.meterPct.toFixed(1)}%`;
  const rowTxt = card.querySelector('.meter .row > span');
  if (rowTxt) rowTxt.textContent = `当前分段 ${view.meterNow} / ${view.meterTotal}`;
  const rowSeg = card.querySelector('.meter .row > b');
  if (rowSeg) rowSeg.textContent = `第 ${Math.max(1, view.segments || 1)} 段`;
  const nameEl = card.querySelector('.who .name');
  if (nameEl && view.anchor) nameEl.textContent = view.anchor;
  const titleEl = card.querySelector('.who .title');
  if (titleEl && view.title) titleEl.textContent = view.title;
  set('[data-f="check"]', checkText(view));
}

/* ── 弹幕 ──────────────────────────────────────────── */
function onDanmaku(evt) {
  const rid = String(evt.roomId || '');
  if (!rid) return;
  (S.dm[rid] = S.dm[rid] || []).push({
    type: evt.type, nickname: evt.nickname, content: evt.content,
    giftName: evt.gift_name, giftCount: evt.gift_count, offsetMs: evt.offset_ms
  });
  if (S.dm[rid].length > 200) S.dm[rid] = S.dm[rid].slice(-200);

  const card = $(`.card[data-id="${rid}"]`);
  if (!card) return;
  const list = card.querySelector('[data-f="dmlist"]');
  if (list) {
    const empty = list.querySelector('.dmi .txt') && /暂无弹幕/.test(list.textContent || '');
    if (empty) list.innerHTML = '';
    list.insertAdjacentHTML('beforeend', dmNode(S.dm[rid][S.dm[rid].length - 1]));
    while (list.children.length > 40) list.removeChild(list.firstChild);
    list.scrollTop = list.scrollHeight;
  }
}

/* ── 卡片操作（事件委托） ──────────────────────────── */
$('#grid').addEventListener('click', async ev => {
  const head = ev.target.closest('.dm-head');
  if (head) {
    const card = head.closest('.card');
    const rid = String(card.dataset.id);
    const box = head.parentElement;
    const open = box.classList.toggle('open');
    if (open) { S.dmOpen.add(rid); box.querySelector('.dm-list').scrollTop = 1e6; }
    else { S.dmOpen.delete(rid); }
    return;
  }

  // 卡片标签里的「弹幕 / 无弹幕」：每张卡独立决定要不要录弹幕
  const dmBtn = ev.target.closest('[data-act="dm"]');
  if (dmBtn) {
    const card = dmBtn.closest('.card');
    const rid = String(card.dataset.id);
    const room = S.rooms.find(r => String(r.id) === rid) || {};
    dmBtn.disabled = true;
    await toggleRoomDanmaku(rid, room.danmaku === false);
    return;
  }

  // 卡片标签里的「自动 / 手动」：每张卡独立的无人值守开关

  const btn = ev.target.closest('.ibtn');
  if (!btn) return;
  const card = btn.closest('.card');
  const rid = String(card.dataset.id);
  const room = S.rooms.find(r => String(r.id) === rid) || {};
  const act = btn.dataset.act;

  if (act === 'trash') {
    // 确认态记在全局变量里，别挂在按钮 DOM 上：
    // 录制中的卡片每秒都会刷新（rebuild/局部 patch），按钮被重绘后挂在上面的状态就丢了，
    // 于是就出现「录制中的卡片怎么点都删不掉」。
    if (armedRid !== rid) {
      armedRid = rid;
      btn.dataset.armed = '1';
      btn.classList.add('armed');
      btn.title = '再点一次确认删除';
      clearTimeout(armedTimer);
      armedTimer = setTimeout(() => {
        armedRid = '';
        document.querySelectorAll('.ibtn[data-act="trash"].armed').forEach(el => {
          el.classList.remove('armed');
          delete el.dataset.armed;
          el.title = '删除';
        });
      }, 6000);
      return;
    }
    armedRid = '';
    clearTimeout(armedTimer);
    btn.disabled = true;
    try {
      await api(`/api/rooms/${rid}`, { method: 'DELETE' });
    } catch (err) {
      pushLocalLog('ERROR', `删除失败：${err.message}`);
    }
    return;
  }

  const guard = async (label, fn) => {
    btn.disabled = true;
    try { await fn(); } catch (err) { pushLocalLog('ERROR', `${label}失败：${err.message}`); }
    finally { btn.disabled = false; }
  };

  if (act === 'start') return guard('开始录制', () => api(`/api/rooms/${rid}/start`, { method: 'POST' }));
  if (act === 'stop') return guard('停止录制', () => api(`/api/rooms/${rid}/stop`, { method: 'POST' }));
  if (act === 'retry') return guard('重试', () => api(`/api/rooms/${rid}/retry`, { method: 'POST' }));
  if (act === 'folder') {
    const target = room.outDir || S.settings.saveRoot || '';
    return guard('打开目录', () => api('/api/open-folder', { method: 'POST', body: { path: target } }));
  }
  if (act === 'live') {
    if (room.url) window.open(room.url, '_blank', 'noopener');
    return;
  }
  if (act === 'edit') openAdd(room);
});

/* ── 顶栏：开始 / 停止 / 添加 / 设置 ───────────────── */
function bindHeaderButtons() {
  $$('header .btn, header .icon-btn').forEach(btn => {
    const text = btn.textContent.trim();
    if (text.includes('全部开始')) {
      btn.onclick = async () => {
        btn.disabled = true;
        try { await api('/api/all/start', { method: 'POST' }); } catch (e) { pushLocalLog('ERROR', e.message); }
        btn.disabled = false;
      };
    } else if (text.includes('全部停止')) {
      btn.onclick = async () => {
        btn.disabled = true;
        try { await api('/api/all/stop', { method: 'POST' }); } catch (e) { pushLocalLog('ERROR', e.message); }
        btn.disabled = false;
      };
    } else if (text.includes('添加直播间')) {
      btn.onclick = () => openAdd(null);
    }
  });
  // 设置按钮：优先用 id（原型里它只有一个齿轮 svg，没有文字，靠 text/title 匹配不可靠）
  const gear = $('#btn-settings') || $$('header .btn.ghost').pop();
  if (gear) {
    gear.onclick = () => openSettings();
    gear.title = '设置';
    gear.setAttribute('aria-label', '设置');
  }
}
bindHeaderButtons();


/* ── 添加 / 编辑直播间弹窗 ─────────────────────────── */
const mask = $('#mask');
const modal = $('#mask .modal');

function fillSelects() {
  const selects = $$('#mask .modal select');
  if (selects[0]) {
    selects[0].innerHTML = QUALITIES.map(q => `<option value="${q}">${q}</option>`).join('');
  }
  if (selects[1]) {
    selects[1].innerHTML = FORMATS.map(([v, label]) => `<option value="${v}">${label}</option>`).join('');
  }
}

/* 画质列表：识别到直播间后换成该房间真实可选的档位 */
let qualitySeq = 0;

function setQualityNote(text) {
  const detectBox = $('#mask .modal .detect');
  let note = $('#mask .modal .qnote');
  if (!note) {
    note = document.createElement('div');
    note.className = 'qnote';
    if (detectBox && detectBox.parentNode) detectBox.after(note);
    else modal.insertBefore(note, modal.firstChild);
  }
  note.textContent = text || '';
  note.style.display = text ? '' : 'none';
}

function setQualityOptions(items, note) {
  const sel = $$('#mask .modal select')[0];
  if (!sel) return;
  const prev = sel.value || (S.editing && S.editing.quality) || S.settings.quality || '原画';
  if (!Array.isArray(items) || !items.length) {
    sel.innerHTML = QUALITIES.map(q => `<option value="${q}">${q}</option>`).join('');
    sel.value = QUALITIES.includes(prev) ? prev : (S.settings.quality || '原画');
    setQualityNote(note || '');
    return;
  }
  sel.innerHTML = items.map(it => {
    const native = it.native ? ` data-native="${escapeHtml(it.native)}"` : '';
    return `<option value="${escapeHtml(it.value)}"${native}>${escapeHtml(it.label)}</option>`;
  }).join('');
  const values = items.map(it => it.value);
  const fallback = (items.find(it => it.default) || items[0]).value;
  sel.value = values.includes(prev) ? prev : fallback;
  setQualityNote(note || '');
}

function optRows() {
  return $$('#mask .optrow');
}

function setSwitch(sw, on) {
  sw.classList.toggle('on', !!on);
}

function syncModalToggles() {
  const rows = optRows();
  if (rows[0]) setSwitch($('.sw', rows[0]), !!S.settings.proxyEnabled);
  // 分段录制那一行已经换成选择框（没有 .sw），弹幕开关是最后一个带开关的行
  const switchRows = rows.filter(row => !!$('.sw', row));
  const dmRow = switchRows[switchRows.length - 1];
  if (dmRow && dmRow !== rows[0]) setSwitch($('.sw', dmRow), !!S.pendingDanmaku);
}

function segSelect() {
  return $('#mask .modal select[data-opt="split"]');
}

function segInputBox() {
  return $('#mask .modal .seginp');
}

function syncSegInput(room) {
  const sel = segSelect();
  const box = segInputBox();
  if (!sel || !box) return;
  const custom = sel.value === 'custom';
  box.style.display = custom ? '' : 'none';
  const num = $('input', box);
  if (!num) return;
  const fallback = Number(S.settings.splitSeconds) || 1800;
  const own = Number(room && room.splitSeconds) || 0;
  num.value = custom ? (own || fallback) : '';
}

function openAdd(room) {
  S.editing = room || null;
  fillSelects();
  qualitySeq++;
  setQualityOptions(null, '');
  const input = $('#mask .modal input');
  const selects = $$('#mask .modal select');
  const h3 = $('#mask .mhead h3');
  const hint = $('#mask .mhead .hint');
  const footBtn = $$('#mask .mfoot button').pop();

  if (room) {
    input.value = room.url || '';
    if (selects[0]) selects[0].value = room.quality || S.settings.quality || '原画';
    S.pendingDanmaku = room.danmaku !== false;
    if (h3) h3.textContent = '编辑直播间';
    if (hint) hint.textContent = '改完点保存立即生效，正在录制的房间会自动用新配置重连';
    if (footBtn) footBtn.textContent = '保存修改';
  } else {
    input.value = '';
    if (selects[0]) selects[0].value = S.settings.quality || '原画';
    S.pendingDanmaku = true;
    if (h3) h3.textContent = '添加直播间';
    if (hint) hint.textContent = '粘贴分享链接即可，平台与主播自动识别';
    if (footBtn) footBtn.textContent = '保存并开始';
  }
  if (selects[1] && S.settings.saveFormat) selects[1].value = S.settings.saveFormat;
  const segSel = segSelect();
  if (segSel) segSel.value = (room && room.splitMode) || 'default';
  syncSegInput(room);

  syncModalToggles();
  setDetect(null);
  mask.classList.add('show');
  setTimeout(() => input.focus(), 60);
  if (room) detect(room.url, true);
}

function closeMask() {
  mask.classList.remove('show');
  S.editing = null;
}

function setDetect(info) {
  const box = $('#mask .modal .detect');
  if (!box) return;
  if (!info) {
    box.style.display = 'none';
    box.innerHTML = '';
    return;
  }
  box.style.display = '';
  if (info.error) {
    box.innerHTML = `<span class="tag plat douyu">${escapeHtml(info.label || '未识别')}</span>` +
      `<span style="color:#ff6b7a">${escapeHtml(info.error)}</span>`;
    return;
  }
  const p = PLAT[info.platform] || PLAT.unknown;
  box.innerHTML = `<span class="tag plat ${p.cls}">${p.name}</span>` +
    `<span>主播 <b>${escapeHtml(info.anchor || '未知')}</b></span>` +
    `<span>${info.isLive ? '开播中' : '未开播'}</span>` +
    `<span class="ok">${info.isLive ? '● 已取到直播流' : '○ 已识别房间'}</span>`;
}

let detectTimer = null;
let detectSeq = 0;

function detect(url, force) {
  const box = $('#mask .modal .detect');
  const u = String(url || '').trim();
  if (!u) { setDetect(null); return; }
  if (detectTimer) clearTimeout(detectTimer);
  if (box) {
    box.style.display = '';
    box.innerHTML = '<span style="color:#5b6579">正在识别平台与直播间信息…</span>';
  }
  detectTimer = setTimeout(async () => {
    const seq = ++detectSeq;
    const qseq = ++qualitySeq;
    try {
      const info = await api('/api/preview', { method: 'POST', body: { url: u } });
      if (seq === detectSeq && !S.editing) setDetect(info);
      else if (seq === detectSeq) setDetect(info);
      if (info.platform && info.platform !== 'unknown') {
        api(`/api/qualities?url=${encodeURIComponent(u)}`).then(q => {
          if (qseq !== qualitySeq) return;
          if (q && q.ok) setQualityOptions(q.qualities, q.note || '');
          // 识别那一步已经报过错了（比如平台解析失败），这里就不再重复同一句话
          else setQualityOptions(null, info.error ? '' : ((q && q.error) || '未取到该直播间的可选画质，先按默认六档选择'));
        }).catch(() => {
          if (qseq === qualitySeq) setQualityOptions(null, info.error ? '' : '画质列表获取失败，先按默认六档选择');
        });
      }
    } catch (err) {
      if (seq === detectSeq) setDetect({ error: err.message, label: '识别失败' });
    }
  }, force ? 0 : 600);
}

$('#mask .modal input').addEventListener('input', ev => detect(ev.target.value));

$('#btn-close').onclick = closeMask;
// 只允许右上角的 ✕ 关闭：点遮罩不再关闭（免得编辑到一半误触就把内容丢了），
// 底部的「取消」按钮也随 index.html 一起去掉了。

(() => {
  const sel = segSelect();
  if (sel) sel.addEventListener('change', () => syncSegInput(S.editing));
})();

optRows().forEach((row, idx) => {
  const sw = $('.sw', row);
  if (!sw) return;
  sw.onclick = async () => {
    const on = !sw.classList.contains('on');
    setSwitch(sw, on);
    if (idx === 0) {
      S.settings.proxyEnabled = on;
      try { await api('/api/settings', { method: 'POST', body: { proxyEnabled: on } }); }
      catch (e) { pushLocalLog('ERROR', e.message); }
    } else if (idx === 1) {
      S.settings.splitEnabled = on;
      try { await api('/api/settings', { method: 'POST', body: { splitEnabled: on } }); }
      catch (e) { pushLocalLog('ERROR', e.message); }
    } else {
      S.pendingDanmaku = on;
    }
  };
});

const saveBtn = (() => {
  const btns = $$('#mask .mfoot .btn, #mask .mfoot button');
  return btns[btns.length - 1];
})();

if (saveBtn) {
  saveBtn.onclick = async () => {
    const input = $('#mask .modal input');
    const selects = $$('#mask .modal select');
    const url = String(input.value || '').trim();
    if (!url) { pushLocalLog('WARN', '请先粘贴直播间地址'); input.focus(); return; }
    const quality = selects[0] ? selects[0].value : '原画';
    const saveFormat = selects[1] ? selects[1].value : null;
    const segSel = segSelect();
    const splitMode = segSel ? segSel.value : 'default';
    let splitSeconds = 0;
    if (splitMode === 'custom') {
      const box = segInputBox();
      const num = box ? $('input', box) : null;
      splitSeconds = Math.max(60, Math.round(Number(num && num.value)
        || Number(S.settings.splitSeconds) || 1800));
    }

    saveBtn.disabled = true;
    const oldText = saveBtn.textContent;
    saveBtn.textContent = '保存中…';
    try {
      if (saveFormat && saveFormat !== S.settings.saveFormat) {
        await api('/api/settings', { method: 'POST', body: { saveFormat } });
        S.settings.saveFormat = saveFormat;
      }
      if (S.editing) {
        await api(`/api/rooms/${S.editing.id}`, {
          method: 'PATCH',
          body: { url, quality, danmaku: S.pendingDanmaku, splitMode, splitSeconds }
        });
      } else {
        await api('/api/rooms', {
          method: 'POST',
          body: { url, quality, danmaku: S.pendingDanmaku, start: true, anchor: '',
                  splitMode, splitSeconds }
        });
      }
      closeMask();
      await refreshState();
    } catch (err) {
      pushLocalLog('ERROR', `保存失败：${err.message}`);
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = oldText;
    }
  };
}

/* ── Cookie 有效性检测 ─────────────────────────────── */
const CK_LABEL = { douyin: '抖音', bilibili: 'B站', huya: '虎牙', douyu: '斗鱼' };

function paintCookieChip(platform, result) {
  const chip = document.querySelector(`[data-ckout="${platform}"]`);
  if (!chip) return;
  chip.className = `ckout ${result.state || ''}`;
  chip.textContent = result.message || '';
  chip.title = [result.message, result.detail].filter(Boolean).join('\n');
}

async function runCookieCheck(platform, wrap) {
  const box = wrap.querySelector(`[data-ck="${platform}"]`);
  const btn = wrap.querySelector(`[data-ckbtn="${platform}"]`);
  const chip = wrap.querySelector(`[data-ckout="${platform}"]`);
  const value = box ? box.value.trim() : '';
  const label = CK_LABEL[platform] || platform;

  if (chip) {
    chip.className = 'ckout checking';
    chip.textContent = value ? '检测中…（用输入框里的 Cookie）' : '检测中…（用已保存的 Cookie）';
    chip.title = '';
  }
  if (btn) { btn.disabled = true; btn.textContent = '检测中'; }

  try {
    const body = value ? { platform, cookie: value } : { platform };
    const res = await api('/api/cookie-check', { method: 'POST', body });
    S.cookieState[platform] = res;
    if (wrap.isConnected) {
      paintCookieChip(platform, res);
    }
    // 运行日志由后端统一记一条（server.py 的 /api/cookie-check），这里不再重复写。
  } catch (err) {
    const fail = { state: 'network', message: `检测请求失败：${err.message}` };
    S.cookieState[platform] = fail;
    if (wrap.isConnected) paintCookieChip(platform, fail);
    pushLocalLog('ERROR', `${label} Cookie 检测失败：${err.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '检测'; }
  }
}

/* ── 设置弹窗（Cookie / 路径 / 格式 / 分段） ───────── */
function openSettings() {
  const old = $('#settings-mask');
  if (old) old.remove();

  const st = S.settings;
  const cookieKeys = [
    ['douyin', '抖音 cookie'], ['bilibili', 'B站 cookie'],
    ['huya', '虎牙 cookie'], ['douyu', '斗鱼 cookie']
  ];
  const cookieFields = cookieKeys.map(([k, label]) => {
    const c = S.cookies[k] || {};
    const ph = c.set ? `已保存：${c.masked}（留空则不修改）` : '未设置';
    const known = S.cookieState[k];
    const chip = known
      ? `<em class="ckout ${known.state}" data-ckout="${k}">${escapeHtml(known.message)}</em>`
      : `<em class="ckout" data-ckout="${k}">${c.set ? '已保存，可点右侧「检测」验证' : '未设置'}</em>`;
    return `<label class="fld ckfld"><span>${label}
        <button class="btn mini ckbtn" type="button" data-ckbtn="${k}">检测</button>
        ${chip}</span>
      <textarea data-ck="${k}" rows="2" placeholder="${ph}" spellcheck="false"></textarea></label>`;
  }).join('');

  const wrap = document.createElement('div');
  wrap.id = 'settings-mask';
  wrap.className = 'mask show';
  wrap.innerHTML = `
  <div class="modal" style="width:640px;max-height:86vh;display:flex;flex-direction:column">
    <div class="mhead">
      <div><b>全局设置</b> <span style="color:#5b6579;font-size:12px">写入 config/config.ini，与命令行模式共用</span></div>
      <button class="x" data-close="1">✕</button>
    </div>
    <div class="mbody" style="overflow:auto;padding:4px 22px 8px">
      <div class="sgrid">
        <label class="fld"><span>默认录制画质</span>
          <select data-st="quality">${QUALITIES.map(q => `<option value="${q}" ${st.quality === q ? 'selected' : ''}>${q}</option>`).join('')}</select></label>
        <label class="fld"><span>保存格式</span>
          <select data-st="saveFormat">${FORMATS.map(([v, l]) => `<option value="${v}" ${st.saveFormat === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
        <label class="fld" style="grid-column:1/-1"><span>录制保存路径（留空 = 程序目录下 downloads）</span>
          <input data-st="saveRoot" value="${escapeHtml(st.saveRoot || '')}" placeholder="downloads" spellcheck="false"></label>
        <label class="fld" style="grid-column:1/-1"><span>弹幕保存路径（留空 = 跟随每场录像目录下的 danmaku/）</span>
          <input data-st="danmakuRoot" value="${escapeHtml(st.danmakuRoot || '')}" placeholder="留空则跟随视频目录" spellcheck="false"></label>
          <label class="fld ckfld" style="grid-column:1/-1"><span>ffmpeg 路径（留空 = 用内置的）</span>
          <input data-st="ffmpegPath" value="${escapeHtml(st.ffmpegPath || '')}" placeholder="${escapeHtml(st.ffmpeg || 'bin\\ffmpeg.exe')}" spellcheck="false">
          <em class="ckout" style="color:#7b8698" title="${escapeHtml(st.ffmpeg || '')}">当前生效：${escapeHtml(st.ffmpeg || '未找到')}</em></label>
        <label class="fld"><span>循环检测间隔（秒）</span>
          <input data-st="loopInterval" type="number" min="10" value="${st.loopInterval || 300}"></label>
        <label class="fld"><span>开播检测间隔（无人值守，秒）</span>
          <input data-st="checkInterval" type="number" min="20" max="3600" value="${st.checkInterval || 60}"></label>
        <label class="fld"><span>同时解析线程数</span>
          <input data-st="concurrent" type="number" min="1" max="32" value="${st.concurrent || 6}"></label>
        <label class="fld"><span>分段录制</span>
          <select data-st="splitEnabled">
            <option value="1" ${st.splitEnabled ? 'selected' : ''}>开启</option>
            <option value="0" ${st.splitEnabled ? '' : 'selected'}>关闭</option>
          </select></label>
        <label class="fld"><span>单段时长（秒）</span>
          <input data-st="splitSeconds" type="number" min="60" step="60" value="${st.splitSeconds || 1800}"></label>
        <label class="fld"><span>代理录制</span>
          <select data-st="proxyEnabled">
            <option value="1" ${st.proxyEnabled ? 'selected' : ''}>开启</option>
            <option value="0" ${st.proxyEnabled ? '' : 'selected'}>关闭</option>
          </select></label>
        <label class="fld"><span>代理地址</span>
          <input data-st="proxyAddr" value="${escapeHtml(st.proxyAddr || '')}" placeholder="http://127.0.0.1:7890" spellcheck="false"></label>
        ${cookieFields}
      </div>
      <div class="hint-line">四个平台的取流与弹幕都已内置（虎牙录制在本版重新实现：多 CDN × 双格式取线路 + WUP 兜底 token + 本地 HLS 中转续签）。「检测」只做验证、不会保存，保存请点右下角。</div>
    </div>
    <div class="mfoot">
      <span style="color:#5b6579;font-size:12px">保存后立即生效，正在录制的房间不受影响</span>
      <div style="display:flex;gap:10px">
        <button class="btn accent" data-save="1">保存设置</button>
      </div>
    </div>
  </div>`;
  document.body.appendChild(wrap);

  const close = () => wrap.remove();
  wrap.addEventListener('click', ev => {
    // 只认右上角的 ✕：点遮罩、按 Esc 都不关闭，避免填了一半的 Cookie 丢失。
    if (ev.target.closest('.mhead .x')) close();
  });

  $$('[data-ckbtn]', wrap).forEach(btn => {
    btn.onclick = () => runCookieCheck(btn.dataset.ckbtn, wrap);
  });

  wrap.querySelector('[data-save]').onclick = async () => {
    const body = {};
    $$('[data-st]', wrap).forEach(el => {
      const key = el.dataset.st;
      let val = el.value;
      if (key === 'loopInterval' || key === 'concurrent' || key === 'splitSeconds' || key === 'checkInterval') val = Number(val);
      else if (key === 'splitEnabled' || key === 'proxyEnabled') val = val === '1';
      body[key] = val;
    });
    const cookies = {};
    $$('[data-ck]', wrap).forEach(el => { if (el.value.trim()) cookies[el.dataset.ck] = el.value.trim(); });
    if (Object.keys(cookies).length) body.cookies = cookies;
    try {
      const res = await api('/api/settings', { method: 'POST', body });
      S.settings = res.settings || S.settings;
      if (S.settings.checkInterval) S.checkInterval = Number(S.settings.checkInterval) || S.checkInterval;

      // 重新拉一次设置：拿到最新的打码 cookie，否则再次打开弹窗会显示「未设置」
      try {
        const fresh = await api('/api/settings');
        S.cookies = fresh.cookies || S.cookies || {};
      } catch (_) { /* 忽略：下一次 /api/state 还会刷新 */ }

      close();
      renderFooter();
      renderAll();

      // 刚保存过 cookie 的平台，顺手检测一次（用已保存的这份）。
      // 日志统一由后端记（保存一条 + 每个平台检测一条），这里不再重复写。
      const savedKeys = Object.keys(cookies);
      if (savedKeys.length) {
        savedKeys.forEach(async platform => {
          delete S.cookieState[platform];
          try {
            const chk = await api('/api/cookie-check', { method: 'POST', body: { platform } });
            S.cookieState[platform] = chk;
          } catch (err) {
            S.cookieState[platform] = { state: 'network', message: `检测失败：${err.message}` };
          }
        });
      }
    } catch (err) {
      pushLocalLog('ERROR', `保存设置失败：${err.message}`);
    }
  };
}

/* ── 日志轮询（WebSocket 未推送日志，补充拉取） ────── */
async function pollLogs() {
  try {
    const res = await api(`/api/logs?after=${S.logSeq}&limit=80`);
    if (res.path) S.logPath = res.path;
    applyLogs(res.logs || []);
  } catch (_) { /* 忽略 */ }
}

/* ── 启动 ──────────────────────────────────────────── */
function injectStyle() {
  const css = `
  #settings-mask .modal .mbody{padding:4px 22px 8px}
  #settings-mask .sgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px 14px;padding:6px 0}
  #settings-mask .fld{display:flex;flex-direction:column;gap:6px}
  #settings-mask .fld > span{font-size:12px;color:#8b95a8}
  #settings-mask .fld input,#settings-mask .fld select,#settings-mask .fld textarea{
    background:#0d1119;border:1px solid #232b39;border-radius:8px;color:#e6ebf3;
    padding:8px 10px;font-size:13px;font-family:inherit;outline:none;width:100%;box-sizing:border-box}
  #settings-mask .fld textarea{resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:12px}
  #settings-mask .fld input:focus,#settings-mask .fld select:focus,#settings-mask .fld textarea:focus{border-color:#3a4a66}
  #settings-mask .hint-line{margin-top:10px;font-size:12px;color:#5b6579;line-height:1.7}
  #settings-mask .mfoot{display:flex;align-items:center;justify-content:space-between;gap:12px}
  .ibtn.armed{color:#ff4d5e;border-color:#5c2029;background:#2a1116}
  .ibtn:disabled{opacity:.5;cursor:default}
  /* 卡片上的无人值守开关：默认灰=手动，开启为绿色=自动 */
  .tag.auto,
  .tag.dm{display:inline-flex;align-items:center;gap:5px;cursor:pointer;
    background:transparent;border:1px solid #242b3a;color:#8b95a8;font-family:inherit;
    transition:color .15s,border-color .15s,background .15s}
  .tag.auto:hover,
  .tag.dm:hover{border-color:#33405a;color:#c6d0e2}
  .tag.auto .autodot,
  .tag.dm .autodot{width:6px;height:6px;border-radius:50%;background:#5a6376;transition:.15s}
  .tag.auto.on{color:#5fd3b6;border-color:#1f4a45;background:rgba(49,208,170,.08)}
  .tag.auto.on .autodot{background:#31d0aa;box-shadow:0 0 0 3px rgba(49,208,170,.14)}
  .tag.dm.on{color:#7fb2ff;border-color:#26405f;background:rgba(91,157,255,.08)}
  .tag.dm.on .autodot{background:#5b9dff;box-shadow:0 0 0 3px rgba(91,157,255,.14)}
  .tag.auto:disabled,
  .tag.dm:disabled{opacity:.5;cursor:default}
  #settings-mask .ckfld{grid-column:1 / -1}
  #settings-mask .ckfld > span{display:flex;align-items:center;gap:8px}
  #settings-mask .ckfld .ckbtn{flex:0 0 auto}
  #settings-mask .btn.mini{height:22px;padding:0 9px;font-size:11.5px;border-radius:6px;line-height:1}
  #settings-mask .ckout{
    margin-left:auto;font-style:normal;font-size:11.5px;line-height:1.4;color:#7b8698;
    max-width:62%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-align:right}
  #settings-mask .ckout.login{color:#31d0aa}
  #settings-mask .ckout.likely{color:#5b9dff}
  #settings-mask .ckout.expired{color:#ff6b7a}
  #settings-mask .ckout.format{color:#ffb020}
  #settings-mask .ckout.network,#settings-mask .ckout.empty{color:#8b95a8}
  #settings-mask .ckout.checking{color:#5b9dff}
  #mask .qnote{font-size:11.5px;line-height:1.6;color:#7b8698;margin:2px 0 10px;padding-left:1px}
  #mask .modal select[data-room]{border-color:#2a3a52}
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);
}

async function boot() {
  // 标题带上端口：程序换端口重启后，旧窗口的标题对不上，就不会被误当成当前窗口，
  // 也不会出现「看着是控制台、其实那一页早就 404」的情况。
  document.title = `LiveRec 控制台 · ${location.port}`;
  injectStyle();
  fillSelects();
  syncModalToggles();
  connectWS();
  try {
    await refreshState();
  } catch (err) {
    pushLocalLog('ERROR', `无法连接后端：${err.message}`);
  }
  setInterval(pollLogs, 2500);
  if (location.hash === '#add') openAdd(null);
  if (location.hash === '#settings') openSettings();
}

boot();

/* Entropaxis 团队协作工作台 · M1-4 前端逻辑（P1 首页）
 *
 * 硬约束（Spec 1.2 / 4.2 / AC-18）：
 * - 前端只读：数据一律经本机服务 `/api/*`（已带启动令牌），不直接读写任何文件。
 * - 写操作只走 M1-2 的五条写路由（/api/segments/open|end、/api/worklog、/api/capsules、
 *   /api/files/open）；本文件不实现任何文件写入路径。
 * - 零外链：无 CDN / 远程字体 / 远程图片；离线可开。
 * - 令牌：服务启动时打印 `token <值>` 到 stderr；本页经同源页面参数 `?token=` 注入
 *   （同源页面参数只在本机浏览器地址栏可见，不落盘、不进日志）。
 *
 * P1 数据真源：`/api/capsules`（capsule_index）、`/api/todos`（todo_index）、
 * `/api/worklog`（aggregate）、`/api/gates`（pending_gates 服务端聚合）。
 */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

// ---------- 启动令牌与 API 客户端 ----------
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const STATE = {
  capsules: [],        // capsule_index 行（"我的"过滤由服务端做）
  todos: [],           // todo_index 分组
  worklog: null,       // aggregate 输出（本周窗口）
  gates: [],           // pending_gates 六类卡片
  runs: [],            // 本机运行态
  projects: [],        // registry 项目
  me: '',              // 本机人员 id
  error: '',
};

async function api(path, options = {}) {
  const headers = { 'X-Entropaxis-Token': TOKEN, ...(options.headers || {}) };
  const resp = await fetch(path, { ...options, headers });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`${resp.status} ${path}: ${detail.slice(0, 200)}`);
  }
  return resp.json();
}

// ---------- 工具 ----------
const LIFE_PILL = { draft: '', active: 'brand', delivered: 'green', archived: 'ghost' };
const GATE_META = {
  prototype_review: { icon: '▣', tone: 'violet', label: '原型' },
  preview_review: { icon: '▶', tone: 'brand', label: '预览' },
  proposal_review: { icon: '◆', tone: 'amber', label: '方案' },
  audit_fuse: { icon: '⚑', tone: 'rose', label: '熔断' },
  worktime_review: { icon: '⏱', tone: 'blue', label: '工时' },
  dispatch_failed: { icon: '✕', tone: 'rose', label: '失败' },
};
const SEGMENTS = [
  ['01_调研', '调研'], ['02_方案', '方案'], ['03_设计', '设计'], ['04_Spec', 'Spec'],
  ['05_审计', '审计'], ['07_验收', '验收'], ['08_汇报', '汇报'],
];
const STATE_LABEL = {
  running: '运行中', residual: '残留待处置', parked: '停驻',
  done: '完成', todo: '待开始', uninstantiated: '未实例化',
};
const STATE_TONE = { running: 'brand', residual: 'res', parked: '', done: '', todo: '', uninstantiated: '' };
const TODO_SOURCE_LABEL = {
  tasks_md: 'Tasks.md · Active',
  open_segment: '我持有的未结任务段',
  assignment_pending: '被指派的待办',
  capsule_due: '到期提醒',
};
const fmtInt = n => (n || 0).toLocaleString('zh-CN');
const fmtHours = seconds => (seconds / 3600).toFixed(1);
const fmtToken = n => (n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : Math.round(n / 1e3) + 'K');
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const usageTotal = cap => {
  const t = (cap.usage || {}).total || {};
  return (t.in || 0) + (t.out || 0) + (t.cache_read || 0) + (t.cache_write || 0);
};
const lastActivity = cap => {
  const segs = cap.open_segments || [];
  const times = segs.map(s => s.lastActivityAt || s.startAt).filter(Boolean).sort();
  return times.length ? times[times.length - 1].slice(5, 16).replace('T', ' ') : '—';
};

// 系统胶囊（project == "_root"，Q-12）：只在本机，不同步、无租约（NG-7）
// 「系统」标签与「不同步」徽标由服务端在行上给 project_label / sync_state（AC-20）。
const isSystem = cap => cap.project === '_root';

// ---------- 阶段条 ----------
function stageBarHtml(cap) {
  const slots = {};
  const bar = workbenchStageBar(cap);
  for (const entry of bar) slots[entry.stage] = entry;
  return SEGMENTS.map(([key, label], i) => {
    const entry = slots[key] || { state: 'uninstantiated', sub: null };
    const done = entry.state === 'done';
    const cur = ['running', 'residual', 'parked'].includes(entry.state);
    const cls = cur ? `cur ${STATE_TONE[entry.state]}` : done ? 'done'
      : entry.state === 'todo' ? '' : 'off';
    const num = String(i + 1).padStart(2, '0');
    const text = STATE_LABEL[entry.state] || entry.state;
    return `<div class="stg ${cls}" title="${key} · ${text}">
      <div class="n">${done ? '✓' : num}</div><span class="lbl">${label}</span>
      ${entry.sub ? `<span class="sub">${esc(entry.sub)}</span>` : ''}
      <span class="s">${text}</span></div>`;
  }).join('');
}

// 阶段判据在服务端（workbench_view.stage_bar 同口径）算好并随 /api/capsules 下发，
// 前端只渲染；这里只做「缺字段时的保守兜底」，避免旧行把整页打挂。
function workbenchStageBar(cap) {
  if (Array.isArray(cap.stage_bar)) return cap.stage_bar;
  return SEGMENTS.map(([key]) => ({ stage: key, state: 'uninstantiated', sub: null }));
}

// ---------- P1 ----------
async function loadAll() {
  const [capsules, todos, worklog, gates, runs, projects] = await Promise.all([
    api('/api/capsules'), api('/api/todos'), api('/api/worklog'),
    api('/api/gates'), api('/api/runs'), api('/api/projects'),
  ]);
  STATE.capsules = capsules;
  STATE.todos = todos;
  STATE.worklog = worklog;
  STATE.gates = gates;
  STATE.runs = runs;
  STATE.projects = projects;
}

function renderMe() {
  $('#meName').textContent = STATE.me ? STATE.me : '未绑定';
  $('#idBtn').innerHTML = STATE.me
    ? `<span class="avatar" style="background:#0d9488">${esc(STATE.me.slice(0, 1))}</span>${esc(STATE.me)}`
    : '<span class="avatar" style="background:#64748b">?</span>未绑定';
  $('#wsRoot').value = location.origin;
  const opts = (STATE.projects.length ? STATE.projects : [])
    .map(p => `<option value="${esc(p.id)}">${esc(p.id)}</option>`).join('');
  $('#meSelect').innerHTML = opts || '<option>（无项目）</option>';
  $('#p1Date').textContent = `${STATE.worklog.since} ~ ${STATE.worklog.until} · 一屏看清今天要处理什么`;
}

function renderGates() {
  const box = $('#gates');
  box.innerHTML = STATE.gates.map(g => {
    const meta = GATE_META[g.kind] || { icon: '?', tone: '', label: g.kind };
    return `<div class="gate" data-cap="${esc(g.capsule_id)}" data-tab="assign">
      <div class="ic" style="background:var(--${meta.tone}-bg);color:var(--${meta.tone})">${meta.icon}</div>
      <div><b>${esc(meta.label)}待确认 · ${esc(g.capsule_id)}</b>
        <div class="small muted">${esc(g.detail || '')}</div></div></div>`;
  }).join('');
  $('#gateCnt').textContent = String(STATE.gates.length);
  $('#gatesEmpty').style.display = STATE.gates.length ? 'none' : '';
  $$('#gates .gate').forEach(el => el.onclick = () => toast(`${el.dataset.cap}：${el.dataset.tab}`));
}

function renderMyCaps() {
  $('#myCaps').innerHTML = STATE.capsules.map(c => `
    <div class="ccard" data-cap="${esc(c.id)}">
      <div class="row"><span class="mono small muted">${esc(c.id)}</span>
        <span class="pill ${LIFE_PILL[c.lifecycle] || ''}">${esc(c.lifecycle || '—')}</span>
        ${isSystem(c) ? '<span class="pill ghost">系统</span><span class="pill ghost">不同步</span>'
          : `<span class="small faint" style="margin-left:auto">${esc(c.project_label || c.project || '')}</span>`}</div>
      <div class="t">${esc(c.topic)}</div>
      <div class="stages compact">${stageBarHtml(c)}</div>
      <div class="row small"><span class="muted">最后活动</span><b>${esc(lastActivity(c))}</b></div>
    </div>`).join('');
  $('#navCapCnt').textContent = String(STATE.capsules.length);
  $$('#myCaps .ccard').forEach(el => el.onclick = () => toast(`胶囊 ${el.dataset.cap}（P4 详情 M1-6 落地）`));
}

function renderWeek() {
  const w = STATE.worklog;
  $('#wkHuman').textContent = fmtHours(w.effective_seconds) + ' h';
  $('#wkMachine').textContent = fmtHours(w.machine_seconds) + ' h';
  const token = STATE.capsules.reduce((s, c) => s + usageTotal(c), 0);
  $('#wkToken').textContent = fmtToken(token);
}

function renderTodos() {
  $('#todoBox').innerHTML = STATE.todos.length ? STATE.todos.map(group => {
    const label = TODO_SOURCE_LABEL[group.source] || group.source;
    const items = (group.items || []).map(it => {
      const text = it.text || it.name
        || `${it.role || ''} ${it.status || ''}`.trim()
        || (it.review_due_at || it.closure_deadline ? '到期提醒' : '待办');
      const due = [it.review_due_at ? `复核 ${it.review_due_at}` : '',
                   it.closure_deadline ? `归档 ${it.closure_deadline}` : '']
        .filter(Boolean).join(' · ');
      return `<label class="todo"><input type="checkbox" disabled><span class="tx">
        <span class="mono">${esc(it.capsule_id || '')}</span> ${esc(text)}
        ${due ? `<span class="pill amber">${esc(due)}</span>` : ''}</span></label>`;
    }).join('');
    return `<div class="small faint">${esc(label)}</div>${items}`;
  }).join('') : '<div class="empty" style="padding:14px">暂无 TODO</div>';
}

function renderP6() {
  const w = STATE.worklog;
  const byActor = w.by_actor || {};
  $('#kpi6').innerHTML = [
    ['人工有效工时', fmtHours(w.effective_seconds) + ' h', 'worklogs.spent'],
    ['Agent 机时', fmtHours(w.machine_seconds) + ' h', 'attempts.duration_s'],
    ['token 合计', fmtToken(STATE.capsules.reduce((s, c) => s + usageTotal(c), 0)), 'attempts.usage'],
    ['条目数', String((w.items || []).length), 'aggregate items'],
  ].map(k => `<div class="kpi"><div class="v" style="font-size:18px">${k[1]}</div><div class="l">${k[0]} · <span class="faint">${k[2]}</span></div></div>`).join('');
  const rows = Object.entries(byActor).map(([k, v]) =>
    `<tr><td>${esc(k)}</td><td class="num">${fmtHours(v)}</td></tr>`).join('');
  $('#pivot').innerHTML = `<table><tr><th>actor</th><th class="num">秒 / 折算小时</th></tr>${rows}</table>`;
}

function renderRuns() {
  $('#runTable').innerHTML = STATE.runs.map(r => `<tr>
    <td class="mono">${esc(r.dispatch_id || '')}</td><td>${esc(r.role || '')}</td>
    <td>${esc(r.capsule ? String(r.capsule).split('/').pop() : '未绑定胶囊')}</td>
    <td>${r.alive ? '<span class="pill violet">running</span>' : '<span class="pill ghost">已退出</span>'}</td>
    <td class="num">${esc(r.preference_number ?? '—')}</td></tr>`).join('');
  $('#agentBtn').innerHTML = `<span class="dot ${STATE.runs.some(r => r.alive) ? 'pulse' : ''}"
    style="background:${STATE.runs.some(r => r.alive) ? 'var(--violet)' : 'var(--faint)'}"></span>运行中 ${STATE.runs.filter(r => r.alive).length}`;
}

// ---------- 演示态与导航（保留原型的场景开关，仅影响显示，不影响数据） ----------
let scenario = 'normal';
function setScenario(s) {
  scenario = s;
  document.body.className = 'sc-' + s;
  const conn = { normal: ['在线', 'var(--emerald)'], offline: ['离线 · 只写本地', 'var(--amber)'],
    conflict: ['冲突待裁决', 'var(--rose)'], fail: ['在线', 'var(--emerald)'],
    empty: ['单机模式', 'var(--faint)'] }[s] || ['在线', 'var(--emerald)'];
  $('#connBtn').innerHTML = `<span class="dot ${s === 'offline' ? '' : 'pulse'}" style="background:${conn[1]}"></span>${conn[0]}`;
  $('#idBtn').innerHTML = STATE.me
    ? `<span class="avatar" style="background:#0d9488">${esc(STATE.me.slice(0, 1))}</span>${esc(STATE.me)}`
    : '<span class="avatar" style="background:#64748b">?</span>未绑定';
  $$('.not-off').forEach(e => e.style.display = s === 'offline' ? 'none' : '');
  $('#noConflict').style.display = s === 'conflict' ? 'none' : '';
  renderP6();
}
$('#scenario').onchange = e => setScenario(e.target.value);

function go(p) {
  $$('.page').forEach(e => e.classList.toggle('active', e.id === p));
  $$('.nav').forEach(e => e.classList.toggle('active', e.dataset.p === p));
  closePops();
  $('#main').scrollTop = 0;
  if (location.hash !== '#' + p) history.replaceState(null, '', '#' + p);
}
$$('.nav').forEach(b => b.onclick = () => go(b.dataset.p));

// ---------- 弹层 / 提示 ----------
function closePops() { $$('.pop').forEach(p => p.classList.remove('open')); }
$$('[data-pop]').forEach(b => b.onclick = e => {
  e.stopPropagation();
  const p = $('#' + b.dataset.pop);
  const open = p.classList.contains('open');
  closePops();
  if (!open) p.classList.add('open');
});
document.addEventListener('click', e => { if (!e.target.closest('.pop')) closePops(); });
function modal(title, body, acts) {
  $('#mTitle').textContent = title;
  $('#mBody').textContent = body;
  $('#mActs').innerHTML = '';
  acts.forEach(([label, cls, fn]) => {
    const b = document.createElement('button');
    b.className = 'btn ' + (cls || '');
    b.textContent = label;
    b.onclick = () => { $('#modalBg').classList.remove('open'); fn && fn(); };
    $('#mActs').appendChild(b);
  });
  $('#modalBg').classList.add('open');
}
let toastTimer;
function toast(m) {
  const t = $('#toast');
  t.textContent = m;
  t.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.style.display = 'none'; }, 2600);
}

// ---------- 主题 ----------
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  $('#themeLbl').textContent = t === 'dark' ? '暗' : '亮';
  $('#themeSel').value = t;
}
$('#themeBtn').onclick = () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
$('#themeSel').onchange = e => setTheme(e.target.value);
$('#offSw').onchange = e => setScenario(e.target.checked ? 'offline' : 'normal');

// ---------- P2 会话（M1-5 起接真实数据） ----------
function renderP2() {
  $('#bindCap').innerHTML = STATE.capsules.length
    ? STATE.capsules.map(c => `<option>${esc(c.id)} ${esc(c.topic)}</option>`).join('') + '<option>不绑定</option>'
    : '<option>不绑定</option>';
  $('#sessList').innerHTML = STATE.capsules.length
    ? STATE.capsules.slice(0, 8).map(c => `<div class="sess"><div class="t">${esc(c.id)} ${esc(c.topic)}</div>
        <div class="row small muted"><span class="mono">${esc(c.id)}</span><span class="pill">本机</span></div></div>`).join('')
    : '<div class="empty" style="padding:20px 8px">无胶囊</div>';
  $('#msgs').innerHTML = '<div class="empty" style="margin:auto"><h4>从一句话开始</h4><div>P2 会话流在 M1-5 接入真实数据。</div></div>';
}
$('#input').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } };
function send() {
  const v = $('#input').value.trim();
  if (!v) return;
  $('#msgs').insertAdjacentHTML('beforeend',
    `<div class="msg me"><div class="who">我 · 刚刚</div></div>`);
  $('#msgs').lastElementChild.append(v);
  $('#input').value = '';
  $('#msgs').scrollTop = 1e6;
}

// ---------- P7 存储与同步（M2 落地，当前单机模式只读提示） ----------
function renderP7() {
  $('#remoteList').innerHTML = '<div class="faint small">未配置远端（workspace-config.yaml::remotes 为空）· 单机模式，全部功能可用。</div>';
  $('#conflictBox').innerHTML = '';
  $('#noConflict').style.display = scenario === 'conflict' ? 'none' : '';
}

// ---------- P9 成员 ----------
function renderP9() {
  $('#memberTable').innerHTML = '<div class="faint small">stakeholders.yaml 人员主库（resolve_stakeholder）在 M1-8 接入。</div>';
}

// ---------- 启动 ----------
async function boot() {
  const qs = new URLSearchParams(location.search);
  if (qs.get('os') === 'win') document.documentElement.dataset.os = 'win';
  if (qs.get('theme')) setTheme(qs.get('theme'));
  try {
    await loadAll();
    STATE.me = (STATE.capsules[0] && STATE.capsules[0].owner) || '';
  } catch (error) {
    STATE.error = String(error && error.message || error);
    $('#gates').innerHTML = `<div class="banner rose flex">加载失败：${esc(STATE.error)}</div>`;
  }
  renderMe();
  renderGates();
  renderMyCaps();
  renderWeek();
  renderTodos();
  renderP2();
  renderP7();
  renderP9();
  renderRuns();
  setScenario(qs.get('sc') || 'normal');
  $('#scenario').value = qs.get('sc') || 'normal';
  go(/^#p\d+$/.test(location.hash) ? location.hash.slice(1) : 'p1');
}
boot();

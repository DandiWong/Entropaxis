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
// P4 头部状态机（方案 4.2 P4：draft → active → delivered → archived）
const LIFECYCLES = ['draft', 'active', 'delivered', 'archived'];
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
  $$('#myCaps .ccard').forEach(el => el.onclick = () => openCapsule(el.dataset.cap));
}

function renderMyProjects() {
  // P1「我的项目」芯片：registry 项目 + 该系统/项目下的在办胶囊数（M1-6）
  const rows = STATE.projects.filter(p => !p.is_system).map(p => {
    const caps = STATE.capsules.filter(c => c.project === p.id && !c.bad);
    return `<span class="chip" data-proj="${esc(p.id)}">${esc(p.name)}
      <span class="small faint">在办 ${caps.length}</span>
      ${p.dir_missing ? '<span class="pill amber">目录不存在</span>' : ''}</span>`;
  }).join('');
  $('#myProjects').innerHTML = rows || '<div class="faint small">注册表为空</div>';
  $$('#myProjects .chip').forEach(el => el.onclick = () => { curProj = el.dataset.proj; renderP5(); go('p5'); });
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
  if (p === 'p4' && curCap) renderP4(curCap);
  if (location.hash !== '#' + p) history.replaceState(null, '', '#' + p);
}
$$('.nav').forEach(b => b.onclick = () => { if (b.dataset.p === 'p4' && curCap) renderP4(curCap); go(b.dataset.p); });

// M1-6：P3/P4/P5 打开即用当前数据渲染（数据在 boot 时已由 /api/* 取全）
function openCapsule(id, tab) {
  const c = STATE.capsules.find(x => x.id === id);
  if (!c) return toast(`未找到胶囊 ${id}`);
  curCap = id;
  renderP4(id, tab);
  go('p4');
}

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

// ---------- P2 会话（M1-5：接真实只读数据） ----------
// 会话列表 = capsule_index 的「我的」行（会话正文由 Agent CLI 自存，GUI 不代理模型）；
// 右栏阶段条 = 行上的 stage_bar；角色卡与徽标 = /api/runs（Q-4：胶囊 ID 读 capsule.yaml::id）。
let curCap = '';
function p2Caps() { return STATE.capsules.filter(c => !c.bad); }
function runOf(cap) {
  if (!cap) return null;
  return STATE.runs.find(r => r.capsule_id === cap.id) || null;
}
function execEndLabel(v) {
  return v === 'default' ? '→ 本机（P8 默认）' : '显式选择，覆盖 P8';
}
function roleCardHtml(cap) {
  const run = runOf(cap);
  if (!run) return '<div class="empty" style="padding:14px">当前无运行中的角色调度</div>';
  const secs = run.timeout_s ? Math.max(0, Math.round(run.timeout_s)) : null;
  return `<div class="row"><span class="dot ${run.alive ? 'pulse' : ''}" style="background:${run.alive ? 'var(--brand-2)' : 'var(--faint)'}"></span>
      <span class="rn">${esc(run.role || '—')}</span>
      <span class="pill ${run.alive ? 'brand' : 'ghost'}" style="margin-left:auto">${run.alive ? 'running' : '已退出'}</span></div>
    <dl class="kv" style="margin:10px 0 8px">
      <dt>承载 profile</dt><dd><b class="mono" style="color:var(--brand-2)">${esc(run.profile || '—')}</b></dd>
      <dt>agent / model</dt><dd class="mono">${esc(run.agent || '—')} / ${esc(run.model || '—')}</dd>
      <dt>执行端</dt><dd>本机</dd>
      <dt>胶囊</dt><dd class="mono">${esc(run.capsule_id || '未绑定胶囊')}</dd>
      <dt>偏好序号</dt><dd class="mono">${run.preference_number == null ? '—' : '#' + esc(run.preference_number)}</dd>
      <dt>超时上限</dt><dd class="mono">${secs == null ? '—' : secs + 's'}</dd>
    </dl>`;
}
function usageHtml(cap) {
  const u = (cap && cap.usage) || {};
  const role = u.role || {}, manager = u.manager || {}, total = u.total || {};
  const sum = t => (t.in || 0) + (t.out || 0) + (t.cache_read || 0) + (t.cache_write || 0);
  return `<dl class="kv">
      <dt>角色调度</dt><dd class="mono">${fmtInt(sum(role))}</dd>
      <dt>Manager 会话</dt><dd class="mono">${fmtInt(sum(manager))}</dd>
      <dt>合计</dt><dd class="mono">${fmtInt(sum(total))}</dd>
      <dt>cache 读 / 写</dt><dd class="mono">${fmtInt(total.cache_read)} / ${fmtInt(total.cache_write)}</dd>
      <dt>缺用量</dt><dd class="mono">${fmtInt(u.unknown_count)} 次</dd>
    </dl>`;
}
function renderP2() {
  const caps = p2Caps();
  if (!curCap || !caps.some(c => c.id === curCap)) curCap = caps.length ? caps[0].id : '';
  const cap = caps.find(c => c.id === curCap) || null;
  $('#bindCap').innerHTML = caps.map(c =>
    `<option value="${esc(c.id)}"${c.id === curCap ? ' selected' : ''}>${esc(c.id)} ${esc(c.topic)}</option>`).join('')
    + '<option value="">不绑定</option>';
  $('#sessList').innerHTML = caps.length ? caps.map(c => `
    <div class="sess${c.id === curCap ? ' active' : ''}" data-cap="${esc(c.id)}">
      <div class="t">${esc(c.topic)}</div>
      <div class="row small muted"><span class="mono">${esc(c.id)}</span>
        <span class="pill">本机</span><span style="margin-left:auto">${esc(lastActivity(c))}</span></div></div>`).join('')
    : '<div class="empty" style="padding:20px 8px">无胶囊</div>';
  $('#ctxCapId').textContent = cap ? cap.id : '—';
  $('#p2stages').innerHTML = cap ? stageBarHtml(cap) : '';
  $('#roleCard').innerHTML = roleCardHtml(cap);
  $('#sessUsage').innerHTML = usageHtml(cap);
  $('#msgs').innerHTML = cap
    ? `<div class="empty" style="margin:auto"><h4>${esc(cap.topic)}</h4>
       <div>阶段与角色事实来自本机只读工具；正文由 Agent CLI 自存，GUI 不代理模型。</div></div>`
    : '<div class="empty" style="margin:auto"><h4>从一句话开始</h4><div>先在 P1 或 P3 选一个胶囊。</div></div>';
  $$('#sessList .sess').forEach(el => el.onclick = () => { curCap = el.dataset.cap; renderP2(); });
}
$('#bindCap').onchange = e => { curCap = e.target.value; renderP2(); };
$('#execSeg').onclick = e => {
  const b = e.target.closest('button');
  if (!b || b.disabled) return;
  $$('#execSeg button').forEach(x => x.classList.toggle('on', x === b));
  $('#execHint').textContent = execEndLabel(b.dataset.v);
};
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

// ---------- P3 胶囊列表（M1-6：数据来自 /api/capsules） ----------
const CUR_STATES = ['running', 'residual', 'parked'];
let sortK = 'id', sortDir = 1;
function capStage(c) {
  const cur = (c.stage_bar || []).find(s => CUR_STATES.includes(s.state));
  return cur ? cur : null;
}
function fillFilters() {
  const projSel = $('[data-f="proj"]');
  if (projSel.dataset.filled) return;
  projSel.dataset.filled = '1';
  const opts = STATE.projects.map(p =>
    `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  projSel.insertAdjacentHTML('beforeend', opts);
  const owners = [...new Set(STATE.capsules.map(c => c.owner).filter(Boolean))].sort();
  $('[data-f="owner"]').insertAdjacentHTML('beforeend',
    owners.map(o => `<option value="${esc(o)}">${esc(o)}</option>`).join(''));
  const members = [...new Set(STATE.capsules.flatMap(c => c.members || []))].sort();
  $('[data-f="member"]').insertAdjacentHTML('beforeend',
    members.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join(''));
}
function renderP3() {
  fillFilters();
  const f = {};
  $$('#filters [data-f]').forEach(e => f[e.dataset.f] = e.type === 'checkbox' ? e.checked : e.value);
  const q = (f.q || '').trim().toLowerCase();
  const rows = STATE.capsules.filter(c => {
    const st = capStage(c);
    return (!f.proj || c.project === f.proj)
      && (!f.life || c.lifecycle === f.life)
      && (!f.stage || (st ? st.state === f.stage : f.stage === 'uninstantiated'))
      && (!f.owner || c.owner === f.owner)
      && (!f.member || (c.members || []).includes(f.member))
      && (!f.from || lastActivity(c).slice(0, 10) >= f.from)
      && (!f.to || lastActivity(c).slice(0, 10) <= f.to)
      && (!q || (c.id + ' ' + c.topic).toLowerCase().includes(q));
  }).sort((a, b) => {
    const key = k => sortK === 'projName' ? (k.project_label || k.project || '')
      : sortK === 'life' ? (k.lifecycle || '')
        : sortK === 'stage' ? ((capStage(k) || {}).state || '')
          : sortK === 'hours' ? (k.worklog_seconds || 0)
            : sortK === 'tokens' ? usageTotal(k) : String(k[sortK] || '');
    const av = key(a), bv = key(b);
    return (av > bv ? 1 : av < bv ? -1 : 0) * sortDir;
  });
  $('#capTable tbody').innerHTML = rows.map(c => {
    const st = capStage(c);
    const stageText = st ? `${st.label} · ${STATE_LABEL[st.state] || st.state}` : '—';
    const sync = isSystem(c)
      ? '<span class="pill ghost">系统</span><span class="pill ghost">不同步</span>'
      : `<span class="pill ghost">${esc(c.sync_state === 'local' ? '仅本地' : c.sync_state)}</span>`;
    return `<tr class="click" data-cap="${esc(c.id)}"><td onclick="event.stopPropagation()">
        <input type="checkbox" class="sel" value="${esc(c.id)}"></td>
      <td class="mono">${esc(c.id)}</td><td>${esc(c.topic)}</td>
      <td>${esc(c.project_label || c.project || '')}</td>
      <td><span class="pill ${LIFE_PILL[c.lifecycle] || ''}">${esc(c.lifecycle || '—')}</span></td>
      <td>${esc(stageText)}</td><td class="mono small">${esc(c.owner || '—')}</td>
      <td class="num">${(c.worklog_seconds / 3600).toFixed(2)}</td>
      <td class="num">${fmtToken(usageTotal(c))}</td><td>${sync}</td></tr>`;
  }).join('');
  $('#capCount').textContent = `共 ${rows.length} 行`;
  $('#pullBtn').disabled = !selected().length;
  $$('#capTable tbody tr.click').forEach(tr => tr.onclick = () => openCapsule(tr.dataset.cap));
}
const selected = () => $$('#capTable .sel:checked').map(e => e.value);
$('#filters').addEventListener('input', renderP3);
$('#filters').addEventListener('change', renderP3);
$('#capTable').addEventListener('change', e => {
  if (e.target.id === 'selAll') $$('#capTable .sel').forEach(x => x.checked = e.target.checked);
  $('#pullBtn').disabled = !selected().length;
});
$$('#capTable th.sort').forEach(th => th.onclick = () => {
  sortDir = sortK === th.dataset.k ? -sortDir : 1;
  sortK = th.dataset.k;
  renderP3();
});

// ---------- P4 胶囊详情（M1-6：数据来自 /api/capsules/{id} 同一行） ----------
const P4_TABS = [['assign', '角色指派'], ['conf', '角色配置'], ['files', '交付物'],
  ['time', '工时'], ['tok', 'Token'], ['audit', '审计']];
function renderP4(id, tab = 'assign') {
  const c = STATE.capsules.find(x => x.id === id);
  if (!c) { $('#p4body').innerHTML = '<div class="empty">未找到该胶囊</div>'; return; }
  curCap = id;
  const run = runOf(c);
  const sys = isSystem(c);
  const T = (k, l) => `<button class="tab ${k === tab ? 'on' : ''}" data-t="${k}">${l}</button>`;
  const P = (k, h) => `<div class="tabpane ${k === tab ? 'on' : ''}" data-t="${k}">${h}</div>`;
  const lease = sys ? '<span class="pill ghost">无需租约（系统胶囊不同步）</span>'
    : '<span class="pill">无人持有</span>';
  $('#p4sub').textContent = `${c.id} · ${c.project_label || c.project || '—'}`;
  $('#p4acts').innerHTML = sys ? '' : `
    <button class="btn sm" onclick="toast('update_capsule.py --start-task（写路由 M1-2）')">开任务段</button>
    <button class="btn sm" onclick="toast('update_capsule.py --log-time（写路由 M1-2）')">记工时</button>
    <button class="btn sm pri" onclick="go('p2');$('#bindCap').value='${esc(c.id)}';toast('已在 P2 绑定 ${esc(c.id)}')">派角色 → 会话</button>`;
  const assignRows = (c.assignments || []).map(a => `<tr>
      <td><b>${esc(a.role || '—')}</b></td><td class="mono small">${esc(a.assignment_id || '—')}</td>
      <td><span class="pill ${a.status === 'succeeded' ? 'green' : a.status === 'failed' ? 'rose' : a.status === 'running' ? 'violet' : ''}">${esc(a.status || '—')}</span></td>
      <td class="mono small">${esc(a.deliverable || '—')}</td>
      <td class="num">${a.attempts == null ? '—' : esc(a.attempts)}</td></tr>`).join('');
  const segRows = (c.open_segments || []).map(s => `<tr>
      <td class="mono small">${esc(s.seg_id || '—')}</td><td>${esc(s.name || '—')}</td>
      <td>${esc(s.actor || '—')}</td><td class="mono small">${esc(s.dispatch_id || '—')}</td>
      <td class="small">${esc((s.startAt || '').slice(5, 16).replace('T', ' '))}</td></tr>`).join('');
  const usageRows = ['role', 'manager', 'total'].map(k => {
    const u = (c.usage || {})[k] || {};
    return `<tr><td>${k === 'role' ? '角色调度' : k === 'manager' ? 'Manager 会话' : '合计'}</td>
      <td class="num">${fmtInt(u.in)}</td><td class="num">${fmtInt(u.out)}</td>
      <td class="num">${fmtInt(u.cache_read)}</td><td class="num">${fmtInt(u.cache_write)}</td></tr>`;
  }).join('');
  $('#p4body').innerHTML = `
    <div class="card">
      <div class="row wrap"><span class="mono muted">${esc(c.id)}</span>
        <span class="pill ${LIFE_PILL[c.lifecycle] || ''}">${esc(c.lifecycle || '—')}</span>
        <span class="pill">${esc(c.project_label || c.project || '')}</span>
        ${sys ? '<span class="pill ghost">不同步</span>' : ''}${lease}
        <span style="margin-left:auto" class="row"><span class="small muted">owner</span>
          <b class="mono small">${esc(c.owner || '—')}</b>
          <span class="small muted" style="margin-left:8px">成员</span>
          <span class="small mono">${esc((c.members || []).join('、') || '—')}</span></span></div>
      <h1 style="margin:8px 0 12px;font-size:20px">${esc(c.topic)}</h1>
      <div class="stages">${stageBarHtml(c)}</div>
      <div class="row wrap" style="margin-top:10px">
        <div class="lifecycle">${LIFECYCLES.map(s =>
          `<span class="${s === c.lifecycle ? 'on' : ''}">${s}</span>`).join('<i>→</i>')}</div>
        <span class="small muted" style="margin-left:12px">review_due_at <b>${esc(c.review_due_at || '—')}</b>
          · closure_deadline <b>${esc(c.closure_deadline || '—')}</b></span>
        <span class="small muted" style="margin-left:auto">open 段 ${esc((c.open_segments || []).length)}
          · 缺用量 ${fmtInt((c.usage || {}).unknown_count)} 次</span></div>
      ${run ? `<div class="banner blue flex" style="margin:12px 0 0">⇢ ${esc(run.role)} 运行中 ·
        ${esc(run.profile)} · ${esc(run.agent)}/${esc(run.model)} · 偏好 #${esc(run.preference_number ?? '—')} ·
        胶囊 ${esc(run.capsule_id || '未绑定胶囊')}</div>` : ''}
    </div>
    <div class="tabs">${P4_TABS.map(([k, l]) => T(k, l)).join('')}</div>
    ${P('assign', `<div class="card" style="padding:0"><table>
      <tr><th>角色</th><th>assignment_id</th><th>状态</th><th>交付物</th><th class="num">attempts</th></tr>
      ${assignRows || '<tr><td colspan="5" class="faint">暂无指派</td></tr>'}</table></div>
      <div class="small muted" style="margin-top:6px">状态五态：pending · running · succeeded · failed · blocked。</div>`)}
    ${P('conf', `<div class="card"><h3>生效链<span class="hint">dispatch_role.py resolve · 只读</span></h3>
      <table><tr><th>层级</th><th>来源</th><th>值</th></tr>
        <tr><td>第 1 级</td><td>胶囊档案 roles_manifest.assignments[].command_profile</td>
          <td class="mono">${esc((run || {}).profile || '—')}</td></tr>
        <tr><td>第 3 级</td><td>roles.yaml 偏好与超时</td>
          <td class="mono">超时 ${esc((run || {}).timeout_s ?? '—')}s</td></tr></table></div>`)}
    ${P('files', `<div class="card"><div class="empty">交付物清单由胶囊目录与 Front Matter 汇总，M1-7 随 P6 一并接入。</div></div>`)}
    ${P('time', `<div class="card" style="padding:0"><table>
      <tr><th>seg_id</th><th>名称</th><th>actor</th><th>dispatch_id</th><th>开始</th></tr>
      ${segRows || '<tr><td colspan="5" class="faint">无未结角色段</td></tr>'}</table></div>
      <div class="small muted" style="margin-top:6px">工时合计 ${(c.worklog_seconds / 3600).toFixed(2)} h（窗口内）· 机时 ${((c.machine_seconds || 0) / 3600).toFixed(2)} h</div>`)}
    ${P('tok', `<div class="card" style="padding:0"><table>
      <tr><th>小计</th><th class="num">in</th><th class="num">out</th><th class="num">cache 读</th><th class="num">cache 写</th></tr>
      ${usageRows}</table></div>
      <div class="small muted" style="margin-top:6px">缺用量 ${fmtInt((c.usage || {}).unknown_count)} 次（单列，不按 0 计，6.5.1）</div>`)}
    ${P('audit', `<div class="card"><div class="empty">审计问题表读 03_方案审计报告.md / 05_实施审计报告.md 的 audit-state 围栏；「待我拍板」出卡见首页。</div></div>`)}`;
  $$('#p4 .tab').forEach(b => b.onclick = () => {
    $$('#p4 .tab, #p4 .tabpane').forEach(x => x.classList.toggle('on', x.dataset.t === b.dataset.t));
  });
}

// ---------- P5 项目（M1-6：数据来自 /api/projects + /api/capsules） ----------
let curProj = '';
function projById(id) { return STATE.projects.find(p => p.id === id); }
function projCaps(pid) { return STATE.capsules.filter(c => c.project === pid && !c.bad); }
function renderP5() {
  const projs = STATE.projects;
  if (!curProj || !projById(curProj)) curProj = projs.length ? projs[0].id : '';
  const tree = [...projs.filter(p => !p.parent), ...projs.filter(p => p.parent)];
  $('#ptree').innerHTML = '<div class="small faint" style="padding:0 8px 6px">registry.yaml · 按 parent 层级</div>'
    + tree.map(p => `<div class="node ${p.parent ? 'child' : ''} ${p.id === curProj ? 'on' : ''}"
        data-id="${esc(p.id)}"${p.is_system ? ' style="margin-top:8px;border-top:1px dashed var(--border);border-radius:0"' : ''}>
        ${p.is_system ? '⚙' : p.parent ? '└' : '▾'} ${esc(p.name)}
        ${p.is_system ? '<span class="pill ghost" style="margin-left:auto">只读</span>' : ''}
        ${p.dir_missing ? '<span class="pill amber" title="注册表断链：主目录不存在，不自动修">目录不存在</span>' : ''}
        ${p.remote ? '<span class="small faint" style="margin-left:auto">☁</span>' : ''}</div>`).join('');
  $$('#ptree .node').forEach(n => n.onclick = () => { curProj = n.dataset.id; renderP5(); });
  const p = projById(curProj);
  if (!p) { $('#pdetail').innerHTML = '<div class="empty">注册表为空</div>'; return; }
  const caps = projCaps(p.id);
  const hours = caps.reduce((s, c) => s + (c.worklog_seconds || 0), 0) / 3600;
  const tokens = caps.reduce((s, c) => s + usageTotal(c), 0);
  $('#pullProj').disabled = $('#newCapProj').disabled = !!p.is_system;
  const capRows = caps.map(c => `<div class="row" style="padding:5px 0;cursor:pointer" data-cap="${esc(c.id)}">
      <span class="mono small">${esc(c.id)}</span>${esc(c.topic)}
      <span class="pill ${LIFE_PILL[c.lifecycle] || ''}" style="margin-left:auto">${esc(c.lifecycle || '')}</span></div>`).join('');
  $('#pdetail').innerHTML = p.is_system ? `
    <div class="banner blue flex">ⓘ 虚拟节点，不在 registry.yaml。系统胶囊只在本机，不同步、无租约（NG-7）；团队视角不含。</div>
    <div class="card" style="margin-bottom:12px"><div class="row">
      <h1 style="margin:0;font-size:18px">${esc(p.name)}</h1>
      <span class="pill ghost">只读</span><span class="pill ghost">不同步</span></div>
      <dl class="kv" style="margin-top:12px"><dt>主目录</dt><dd class="mono">${esc(p.path)}</dd>
        <dt>本月投入</dt><dd>${hours.toFixed(2)} h · ${fmtInt(tokens)} token</dd></dl></div>
    <div class="card"><h3>在办系统胶囊</h3>${capRows || '<div class="faint small">无在办系统胶囊</div>'}</div>`
    : `${p.dir_missing ? `<div class="banner amber flex">注册表断链：目录 ${esc(p.path)} 不存在。不自动修，请确认目录迁移或注销登记。</div>` : ''}
    <div class="card" style="margin-bottom:12px"><div class="row">
      <h1 style="margin:0;font-size:18px">${esc(p.name)}</h1>
      ${(p.aliases || []).map(a => `<span class="pill">${esc(a)}</span>`).join('')}
      <span style="margin-left:auto" class="row">${p.remote
        ? `<span class="pill brand">${esc(p.remote.split(':')[0])}</span> <span class="mono small">${esc(p.remote)}</span>`
        : '<span class="faint small">未绑定远端 · 单机</span>'}</span></div>
      <dl class="kv" style="margin-top:12px"><dt>id</dt><dd class="mono">${esc(p.id)}</dd>
        <dt>主目录</dt><dd class="mono">${esc(p.path)}</dd>
        <dt>代码目录</dt><dd class="mono">${esc((p.code || []).join('、') || '—')}</dd>
        <dt>本月投入</dt><dd>${hours.toFixed(2)} h · ${fmtInt(tokens)} token</dd></dl></div>
    <div class="card"><h3>在办胶囊<span class="hint">lifecycle ∈ {draft, active}</span></h3>
      ${capRows || '<div class="faint small">无在办胶囊</div>'}</div>`;
  $$('#pdetail [data-cap]').forEach(el => el.onclick = () => openCapsule(el.dataset.cap));
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
  renderMyProjects();
  renderWeek();
  renderTodos();
  renderP2();
  renderP3();
  renderP5();
  renderP7();
  renderP9();
  renderRuns();
  setScenario(qs.get('sc') || 'normal');
  $('#scenario').value = qs.get('sc') || 'normal';
  go(/^#p\d+$/.test(location.hash) ? location.hash.slice(1) : 'p1');
}
boot();

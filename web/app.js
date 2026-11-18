'use strict';

/* ============================================================
 * 社工管理界面 · 原生 JS 单页应用
 * 路由：#/dashboard #/project/<名> #/project/<名>/activities
 *       #/project/<名>/activity/<目录> #/templates #/sync
 *       #/search #/docs #/doc/<token> #/admin/users #/admin/audit
 * ============================================================ */

const API = '/sw-api/api';
const CATS = ['发票票据', '项目计划', '项目执行', '日常工作佐证', '政策文件', '其他'];
const STATUS_TEXT = { imported: '已入库', generated: '已生成' };
// 15 项核对 → 文件前缀（仅用于点清单项时过滤右侧文件，判定仍以后端 check 为准）
const CHECK_PREFIX = {
  1: ['01-'], 2: ['02-'], 3: ['03-'], 4: ['05-'], 5: ['06-'],
  6: ['07-'], 7: ['08-'], 8: ['09-'], 9: ['09-', '21-'], 10: ['10-'],
  11: ['11-'], 12: ['12-'], 13: ['13-'], 14: ['04-'], 15: [],
};
const TASKS = [
  { v: 'brief', label: '简报（brief）' },
  { v: 'plan', label: '计划书（plan）' },
  { v: 'summary', label: '总结报告（summary）' },
];
const STYLES = ['务实', '平实', '党建'];

let token = localStorage.getItem('sw_token') || '';
let me = null;
let projectsCache = null;
let projectsPromise = null;
let tplCache = null;
let routeSeq = 0;
let actCtx = null;          // 当前活动页上下文（文件操作 / 弹窗用）

/* ---------------- 基础工具 ---------------- */
const $ = id => document.getElementById(id);
const el = (sel, root = document) => root.querySelector(sel);
const els = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const enc = encodeURIComponent;

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

const fmtBytes = n => {
  if (n == null || isNaN(n)) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
};

const fmtTime = iso => {
  if (!iso) return '—';
  const s = String(iso);
  return s.slice(0, 16).replace('T', ' ');
};

const todayCn = () => {
  const d = new Date();
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
};

const phKey = p => String(p || '').replace(/^\{\{/, '').replace(/\}\}$/, '').trim();

const loading = () => '<div class="loading">加载中…</div>';
const empty = text => `<div class="empty">${esc(text || '暂无数据')}</div>`;

function toast(msg, kind = 'info') {
  const box = document.createElement('div');
  box.className = 'toast ' + kind;
  box.textContent = String(msg ?? '');
  $('toasts').appendChild(box);
  setTimeout(() => {
    box.classList.add('out');
    setTimeout(() => box.remove(), 320);
  }, 3600);
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (token) headers['x-token'] = token;
  let body = opts.body;
  if (body && typeof body === 'object' && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  const r = await fetch(API + path, { method: opts.method || 'GET', headers, body });
  const text = await r.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) {
    let msg = r.status + '';
    if (data && typeof data === 'object') {
      const d = data.detail ?? data.message ?? data.error;
      msg = typeof d === 'string' ? d : JSON.stringify(d ?? data);
    } else if (typeof data === 'string' && data.trim()) {
      msg = data.slice(0, 300);
    }
    const err = new Error(msg);
    err.status = r.status;
    if (r.status === 401 && path !== '/login') logoutLocal();
    throw err;
  }
  return data;
}

function canWrite() { return me && (me.role === 'admin' || me.role === 'editor'); }
function isAdmin() { return me && me.role === 'admin'; }

/* ---------------- 登录 / 框架 ---------------- */
function showLogin() {
  $('loginCard').classList.remove('hidden');
  $('appShell').classList.add('hidden');
  closeModal();
}

function showApp() {
  $('loginCard').classList.add('hidden');
  $('appShell').classList.remove('hidden');
  $('me').textContent = `${me.username} · ${me.role}`;
  $('me').title = `${me.username} · ${me.role} · ${(me.projects || []).join(' / ')}`;
  els('[data-admin="1"]').forEach(x => x.classList.toggle('hidden', !isAdmin()));
}

function logoutLocal() {
  localStorage.removeItem('sw_token');
  token = '';
  me = null;
  projectsCache = null;
  tplCache = null;
  showLogin();
}

async function ensureProjects(force = false) {
  if (force) { projectsCache = null; projectsPromise = null; }
  if (projectsCache) return projectsCache;
  if (!projectsPromise) {
    projectsPromise = api('/projects')
      .then(d => { projectsCache = (d && d.items) || []; return projectsCache; })
      .finally(() => { projectsPromise = null; });
  }
  return projectsPromise;
}

async function ensureTemplates(force = false) {
  if (force || !tplCache) tplCache = await api('/templates');
  return tplCache || [];
}

function projectShort(p) {
  const d = (p && p.data) || {};
  return (d.project && d.project.short) || p.name || '';
}

/* ---------------- 侧边栏 ---------------- */
function icon(name) {
  const paths = {
    dashboard: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    project: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    template: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/><path d="M8 13h8M8 17h6"/>',
    sync: '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/>',
    audit: '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/>',
    docs: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
  };
  return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths[name] || ''}</svg>`;
}

async function renderSidebar() {
  let projects = [];
  try { projects = await ensureProjects(); } catch (e) { toast('项目列表加载失败：' + e.message, 'err'); }
  const projectNav = projects.length ? projects.map(p => {
    const name = p.name;
    const short = projectShort(p);
    return `<div class="nav-project">
      <a class="nav-project-head" data-project="${esc(name)}" href="#/project/${enc(name)}" title="${esc(short)}">
        <span class="dot"></span><span>${esc(short)}</span>
      </a>
      <div class="nav-sub">
        <a class="nav-link" data-nav="project:${esc(name)}" href="#/project/${enc(name)}"><span>概览</span></a>
        <a class="nav-link" data-nav="activities:${esc(name)}" href="#/project/${enc(name)}/activities"><span>活动列表</span></a>
      </div>
    </div>`;
  }).join('') : '<div class="nav-link"><span class="muted">暂无项目</span></div>';

  $('sidebarNav').innerHTML = `
    <a class="nav-link" data-nav="dashboard" href="#/dashboard">${icon('dashboard')}<span>仪表盘</span></a>
    <div class="nav-group">
      <div class="nav-group-title">${icon('project')}<span>项目</span></div>
      ${projectNav}
    </div>
    <div class="nav-group">
      <a class="nav-link" data-nav="templates" href="#/templates">${icon('template')}<span>模板</span></a>
      <a class="nav-link" data-nav="sync" href="#/sync">${icon('sync')}<span>同步</span></a>
    </div>
    <div class="nav-group">
      <a class="nav-group-title" href="#/search">${icon('search')}<span>检索</span></a>
      <div class="nav-sub">
        <a class="nav-link" data-nav="search" href="#/search"><span>知识检索</span></a>
        <a class="nav-link" data-nav="docs" href="#/docs"><span>文档库</span></a>
      </div>
    </div>
    <div class="nav-group" data-admin="1">
      <a class="nav-group-title" href="#/admin/users">${icon('users')}<span>管理</span></a>
      <div class="nav-sub">
        <a class="nav-link" data-nav="users" href="#/admin/users"><span>用户</span></a>
        <a class="nav-link" data-nav="audit" href="#/admin/audit"><span>审计</span></a>
      </div>
    </div>`;
  showApp();
  updateActiveNav(parseHash().parts);
}

function updateActiveNav(parts) {
  let key = 'dashboard';
  if (parts[0] === 'project' && parts[1]) {
    const name = parts[1];
    key = parts[2] === 'activities' ? `activities:${name}` : `project:${name}`;
  } else if (parts[0] === 'templates') key = 'templates';
  else if (parts[0] === 'sync') key = 'sync';
  else if (parts[0] === 'search') key = 'search';
  else if (parts[0] === 'docs' || parts[0] === 'doc') key = 'docs';
  else if (parts[0] === 'admin' && parts[1] === 'users') key = 'users';
  else if (parts[0] === 'admin' && parts[1] === 'audit') key = 'audit';
  els('#sidebarNav [data-nav]').forEach(a => {
    a.classList.toggle('active', a.dataset.nav === key);
  });
  const currentProject = parts[0] === 'project' ? parts[1] : '';
  els('#sidebarNav [data-project]').forEach(a => {
    a.classList.toggle('current', a.dataset.project === currentProject);
  });
}

/* ---------------- 弹窗 ---------------- */
function openModal(title, body, foot, wide = false) {
  $('modalTitle').textContent = title;
  $('modalBody').innerHTML = body;
  $('modalFoot').innerHTML = foot || '';
  $('modal').classList.toggle('wide', !!wide);
  $('modal').classList.remove('hidden');
}

function closeModal() {
  $('modal').classList.add('hidden');
  $('modalBody').innerHTML = '';
  $('modalFoot').innerHTML = '';
}

/* ---------------- 通用片段 ---------------- */
function statusBadge(status) {
  const text = STATUS_TEXT[status] || '';
  if (!text) return '<span class="badge muted">新目录</span>';
  const cls = status === 'generated' ? 'ok' : 'info';
  return `<span class="badge ${cls}">${esc(text)}</span>`;
}

function checkBadge(check) {
  if (!check) return '<span class="badge muted">—</span>';
  if (check.complete) return '<span class="badge ok">完整</span>';
  return `<span class="badge warn">缺 ${Number(check.missing_count || 0)} 项</span>`;
}

function setTitle(text) {
  $('pageTitle').textContent = text;
  document.title = text + ' · 社工管理';
}

function pageHead(title, sub, actions) {
  return `<div class="page-head">
    <div><h2>${esc(title)}</h2>${sub ? `<div class="sub">${sub}</div>` : ''}</div>
    ${actions ? `<div class="actions">${actions}</div>` : ''}
  </div>`;
}

function errorState(e) {
  return `<div class="error-box">加载失败：${esc(e && e.message ? e.message : e)}
    <div style="margin-top:12px"><button class="btn sm" data-act="reload">重试</button></div>
  </div>`;
}

/* ---------------- 路由 ---------------- */
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, qs] = raw.split('?');
  const parts = path.split('/').filter(Boolean).map(p => {
    try { return decodeURIComponent(p); } catch { return p; }
  });
  return { parts, params: new URLSearchParams(qs || '') };
}

async function route() {
  if (!me) return;
  const seq = ++routeSeq;
  const { parts, params } = parseHash();
  actCtx = null;
  updateActiveNav(parts);
  closeModal();
  try {
    if (!parts.length) {
      history.replaceState(null, '', '#/dashboard');
      await pageDashboard();
      return;
    }
    if (parts[0] === 'dashboard') { await pageDashboard(); return; }
    if (parts[0] === 'project' && parts[1] && parts[2] === 'activities') {
      await pageActivities(parts[1], params); return;
    }
    if (parts[0] === 'project' && parts[1] && parts[2] === 'activity' && parts[3]) {
      await pageActivity(parts[1], parts[3], params); return;
    }
    if (parts[0] === 'project' && parts[1]) { await pageProject(parts[1]); return; }
    if (parts[0] === 'templates') { await pageTemplates(params); return; }
    if (parts[0] === 'sync') { await pageSync(); return; }
    if (parts[0] === 'search') { await pageSearch(params); return; }
    if (parts[0] === 'docs') { await pageSearch(new URLSearchParams('tab=docs')); return; }
    if (parts[0] === 'doc' && parts[1]) { await pageDoc(parts[1]); return; }
    if (parts[0] === 'admin' && parts[1] === 'users') { await pageUsers(); return; }
    if (parts[0] === 'admin' && parts[1] === 'audit') { await pageAudit(); return; }
    history.replaceState(null, '', '#/dashboard');
    await pageDashboard();
  } catch (e) {
    if (seq !== routeSeq) return;
    $('view').innerHTML = errorState(e);
    toast(e.message || '加载失败', 'err');
  }
}

/* ============================================================
 * 1. 仪表盘
 * ============================================================ */
async function pageDashboard() {
  setTitle('仪表盘');
  $('view').innerHTML = loading();
  const d = await api('/dashboard');
  const ps = d.projects || [];
  const total = ps.reduce((s, p) => s + Number(p.activity_total || 0), 0);
  const done = ps.reduce((s, p) => s + Number(p.activity_complete || 0), 0);

  const cards = ps.map(p => {
    const t = Number(p.activity_total || 0);
    const c = Number(p.activity_complete || 0);
    const pct = t ? Math.round(c / t * 100) : 0;
    return `<a class="proj-card" href="#/project/${enc(p.name)}/activities">
      <div class="name">${esc(projectShort(p))}</div>
      <div class="meta">${esc(p.name)}</div>
      <div class="bar"><i style="width:${pct}%"></i></div>
      <div class="foot"><span>活动 ${c} / ${t}</span><span>${pct}%</span></div>
    </a>`;
  }).join('');

  const recent = (d.recent || []).map(r => `<a class="recent-item" href="#/project/${enc(r.project)}/activity/${enc(r.activity)}">
      <div class="t"><b>${esc(r.activity)}</b><span>${esc(projectShort(ps.find(p => p.name === r.project) || { name: r.project }))}</span></div>
      ${statusBadge(r.status)}
      <div class="when">${esc(fmtTime(r.mtime))}</div>
    </a>`).join('');

  $('view').innerHTML = `
    <div class="stats">
      <div class="stat"><div class="k">项目数</div><div class="v">${ps.length}</div></div>
      <div class="stat"><div class="k">活动总数</div><div class="v">${total}</div></div>
      <div class="stat"><div class="k">已完整活动</div><div class="v">${done}<small>/ ${total}</small></div></div>
    </div>
    <div class="card">
      <div class="card-head"><h3>项目进度</h3><span class="muted">点卡片进入活动列表</span></div>
      ${cards ? `<div class="proj-grid">${cards}</div>` : empty('还没有项目')}
    </div>
    <div class="card">
      <div class="card-head"><h3>最近改动</h3><span class="muted">按活动目录最近修改时间</span></div>
      ${recent ? `<div class="recent-list">${recent}</div>` : empty('暂无改动记录')}
    </div>`;
}

/* ============================================================
 * 2. 项目概览
 * ============================================================ */
async function pageProject(name) {
  setTitle('项目概览');
  $('view').innerHTML = loading();
  const [proj, acts] = await Promise.all([
    api('/projects/' + enc(name)),
    api(`/projects/${enc(name)}/activities`),
  ]);
  const d = proj.data || {};
  const p = d.project || {};
  const parties = d.parties || {};
  const staff = d.staff || {};
  const budget = d.budget || {};
  const period = d.period || {};
  const planned = acts.planned || [];
  const existing = acts.existing || [];
  const missing = acts.missing || [];
  const extra = acts.extra || [];

  const indicators = (d.indicators || []).map((ind, i) => `<tr>
      <td class="nowrap">${esc(ind.code || String(i + 1))}</td>
      <td>${esc(ind.strategy || '')}</td>
      <td>${esc(ind.name || '')}</td>
      <td>${esc(ind.target || '')}</td>
      <td class="num">${esc(ind.count || 1)}</td>
      <td class="num">${esc(ind.budget ?? '')}</td>
    </tr>`).join('');

  const kv = (k, v) => `<div class="kv-row"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`;

  $('view').innerHTML = `
    <div class="crumb"><a href="#/dashboard">仪表盘</a> / 项目</div>
    ${pageHead(projectShort(proj), esc(p.name || name), `
      <a class="btn" href="#/project/${enc(name)}/activities">活动列表</a>
      <a class="btn primary" href="#/project/${enc(name)}/activities?view=plan">按指标建目录</a>`)}
    <div class="stats">
      <div class="stat"><div class="k">计划活动</div><div class="v">${planned.length}</div></div>
      <div class="stat"><div class="k">磁盘已有</div><div class="v">${existing.length}</div></div>
      <div class="stat"><div class="k">缺目录</div><div class="v">${missing.length}</div></div>
      <div class="stat"><div class="k">多出目录</div><div class="v">${extra.length}</div></div>
    </div>
    <div class="card">
      <div class="card-head"><h3>项目主数据</h3></div>
      <div class="kv">
        ${kv('目录名', `<span class="mono">${esc(name)}</span>`)}
        ${kv('项目简称', esc(p.short || name))}
        ${kv('项目类别', esc(p.category || '—'))}
        ${kv('项目级别', esc(p.level || '—'))}
        ${kv('项目周期', esc([period.start, period.end].filter(Boolean).join(' ～ ') || '—'))}
        ${kv('立项社区', esc((parties.funder || {}).name || '—'))}
        ${kv('执行机构', esc((parties.executer || {}).name || '—'))}
        ${kv('项目负责人', esc((staff.leader || {}).name || '—'))}
        ${kv('预算总额', esc(budget.total != null ? budget.total : '—'))}
        ${kv('项目编号', `<span class="mono">${esc(p.code || '未生成')}</span>`)}
      </div>
    </div>
    <div class="card pad0">
      <div class="card-head" style="padding:16px 16px 0"><h3>量化指标</h3><span class="muted">指标决定活动目录拆分</span></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>编号</th><th>策略</th><th>指标</th><th>目标</th><th class="num">期数</th><th class="num">预算</th></tr></thead>
          <tbody>${indicators || `<tr><td colspan="6">${empty('项目 yaml 里没有 indicators')}</td></tr>`}</tbody>
        </table>
      </div>
    </div>`;
}

/* ============================================================
 * 3. 活动列表
 * ============================================================ */
async function pageActivities(name, params) {
  setTitle('活动列表');
  const view = ['plan', 'disk', 'diff'].includes(params.get('view')) ? params.get('view') : 'plan';
  $('view').innerHTML = loading();
  const data = await api(`/projects/${enc(name)}/activities`);
  const planned = data.planned || [];
  const existing = data.existing || [];
  const missing = data.missing || [];
  const extra = data.extra || [];

  // 每个已有目录取一次详情（编号 / 状态 / 核对）
  const details = {};
  await Promise.all(existing.map(async dir => {
    try { details[dir] = await api(`/projects/${enc(name)}/activities/${enc(dir)}`); }
    catch (e) { details[dir] = null; }
  }));

  const tabs = [['plan', '计划列表'], ['disk', '磁盘实际'], ['diff', '差异']].map(([k, label]) =>
    `<button data-act="act-view" data-view="${k}" class="${view === k ? 'active' : ''}">${label}</button>`).join('');

  let body = '';
  if (view === 'plan') {
    const rows = planned.map(item => {
      const dir = item.dirname;
      const det = details[dir];
      const exists = existing.includes(dir);
      return `<tr>
        <td class="num">${String(item.seq).padStart(2, '0')}</td>
        <td>${exists ? `<a href="#/project/${enc(name)}/activity/${enc(dir)}">${esc(dir)}</a>` : esc(dir)}</td>
        <td class="mono">${det && det.code ? esc(det.code) : '—'}</td>
        <td>${det ? statusBadge(det.status) : '<span class="badge muted">未建目录</span>'}</td>
        <td>${det ? checkBadge(det.check) : '<span class="badge muted">—</span>'}</td>
        <td class="nowrap">${exists ? `<a class="btn sm" href="#/project/${enc(name)}/activity/${enc(dir)}">打开</a>` : '<span class="muted">未建目录</span>'}</td>
      </tr>`;
    }).join('');
    const extraRows = extra.map(dir => {
      const det = details[dir];
      return `<tr>
        <td class="num">—</td>
        <td><a href="#/project/${enc(name)}/activity/${enc(dir)}">${esc(dir)}</a></td>
        <td class="mono">${det && det.code ? esc(det.code) : '—'}</td>
        <td>${det ? statusBadge(det.status) : '<span class="badge muted">—</span>'}</td>
        <td>${det ? checkBadge(det.check) : '<span class="badge muted">—</span>'}</td>
        <td class="nowrap"><a class="btn sm" href="#/project/${enc(name)}/activity/${enc(dir)}">打开</a></td>
      </tr>`;
    }).join('');
    body = `<div class="table-wrap"><table>
      <thead><tr><th class="num">序号</th><th>目录名</th><th>编号</th><th>状态</th><th>核对状态</th><th>操作</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6">${empty('项目 yaml 没有 indicators，无法生成计划')}</td></tr>`}${extraRows}</tbody>
    </table></div>
    ${extraRows ? '<div class="hint" style="padding:10px 12px">末尾灰色序号为磁盘上多出的目录（计划里没有）。</div>' : ''}`;
  } else if (view === 'disk') {
    const rows = existing.map((dir, i) => {
      const det = details[dir];
      const fileCount = det ? (det.files || []).length + (det.dirs || []).length : '—';
      return `<tr>
        <td class="num">${i + 1}</td>
        <td><a href="#/project/${enc(name)}/activity/${enc(dir)}">${esc(dir)}</a></td>
        <td class="mono">${det && det.code ? esc(det.code) : '—'}</td>
        <td>${det ? statusBadge(det.status) : '<span class="badge muted">—</span>'}</td>
        <td>${det ? checkBadge(det.check) : '<span class="badge muted">—</span>'}</td>
        <td class="num">${esc(fileCount)}</td>
        <td class="nowrap"><a class="btn sm" href="#/project/${enc(name)}/activity/${enc(dir)}">打开</a></td>
      </tr>`;
    }).join('');
    body = `<div class="table-wrap"><table>
      <thead><tr><th class="num">#</th><th>目录名</th><th>编号</th><th>状态</th><th>核对状态</th><th class="num">顶层条目</th><th>操作</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7">${empty('活动目录下还没有子目录')}</td></tr>`}</tbody>
    </table></div>`;
  } else {
    const list = (arr, cls, label) => arr.length
      ? `<div class="chips">${arr.map(d => `<a class="chip" href="#/project/${enc(name)}/activity/${enc(d)}">${esc(d)}</a>`).join('')}</div>`
      : `<div class="muted">无</div>`;
    body = `<div class="card">
        <div class="card-head"><h3>计划有、磁盘没有</h3><span class="badge warn">${missing.length} 项</span></div>
        ${list(missing)}
        <div class="hint" style="margin-top:8px">可点右上角「按指标建目录」补齐。</div>
      </div>
      <div class="card">
        <div class="card-head"><h3>磁盘有、计划没有</h3><span class="badge muted">${extra.length} 项</span></div>
        ${list(extra)}
        <div class="hint" style="margin-top:8px">这些目录不在 indicators 拆分结果里，请人工确认是否保留。</div>
      </div>`;
  }

  $('view').innerHTML = `
    <div class="crumb"><a href="#/dashboard">仪表盘</a> / <a href="#/project/${enc(name)}">${esc(name)}</a> / 活动</div>
    ${pageHead('活动列表', `项目：${esc(name)} · 计划 ${planned.length} 项 / 磁盘 ${existing.length} 项`,
      canWrite() ? `<button class="btn primary" data-act="open-split">按指标建目录</button>` : '')}
    <div class="card">
      <div class="tabs">${tabs}</div>
      ${body}
    </div>`;
}

async function openSplitModal() {
  const name = (actCtx && actCtx.projectName) || parseHash().parts[1];
  if (!name) return;
  const pre = await api(`/projects/${enc(name)}/activities/split`, {
    method: 'POST', body: { dry_run: true },
  });
  const create = pre.would_create || [];
  const skip = pre.would_skip || [];
  const listHtml = (arr, cls) => arr.length
    ? `<div class="chips">${arr.map(d => `<span class="chip">${esc(d)}</span>`).join('')}</div>`
    : '<div class="muted">无</div>';
  openModal('按指标建目录 · 预演', `
    <div class="notice blue">预演不会写磁盘。确认后只创建缺失的目录，已存在的目录会跳过。</div>
    <div class="section-title">将新建（${create.length}）</div>${listHtml(create)}
    <div class="section-title">已存在，跳过（${skip.length}）</div>${listHtml(skip)}
  `, `<button class="btn" data-close-modal>取消</button>
      <button class="btn primary" data-act="split-create" data-project="${esc(name)}" ${create.length ? '' : 'disabled'}>
        ${create.length ? `确认创建 ${create.length} 个目录` : '无需创建'}
      </button>`);
}

/* ============================================================
 * 4. 活动详情（核心页）
 * ============================================================ */
async function pageActivity(name, dirname, params) {
  setTitle('活动详情');
  const sub = params.get('sub') || '';
  const base = `/projects/${enc(name)}/activities/${enc(dirname)}`;
  $('view').innerHTML = loading();
  const [detail, entries, proj] = await Promise.all([
    api(base),
    api(base + '/files' + (sub ? '?sub=' + enc(sub) : '')),
    api('/projects/' + enc(name)),
  ]);
  actCtx = { projectName: name, dirname, sub, detail, entries, project: proj, selected: 0 };

  const check = detail.check || { items: [], ok_count: 0, missing_count: 0, na_count: 0, complete: false };
  const paths = sub.split('/').filter(Boolean);
  const crumb = ['<a href="#" data-act="go-root">根目录</a>']
    .concat(paths.map((p, i) => `<a href="#" data-act="go-sub" data-sub="${esc(paths.slice(0, i + 1).join('/'))}">${esc(p)}</a>`))
    .join(' / ');

  $('view').innerHTML = `
    <div class="crumb"><a href="#/dashboard">仪表盘</a> /
      <a href="#/project/${enc(name)}">${esc(projectShort(proj))}</a> /
      <a href="#/project/${enc(name)}/activities">活动</a> / ${esc(dirname)}</div>
    <div class="page-head">
      <div>
        <h2>${esc(dirname)}</h2>
        <div class="sub">
          ${detail.code ? `编号 <span class="mono">${esc(detail.code)}</span> · ` : ''}
          ${statusBadge(detail.status)} ${checkBadge(check)}
        </div>
      </div>
      <div class="actions">
        <button class="btn primary" data-act="open-fill" ${canWrite() ? '' : 'disabled title="需 editor/admin"'}>生成材料</button>
        <button class="btn" data-act="act-upload" ${canWrite() ? '' : 'disabled title="需 editor/admin"'}>上传文件</button>
        <button class="btn" data-act="open-llm" ${canWrite() ? '' : 'disabled title="需 editor/admin"'}>LLM 生成</button>
        <button class="btn" data-act="do-check">核对</button>
        <button class="btn" data-act="sync-feishu" ${isAdmin() ? '' : 'disabled title="需 admin"'}>同步飞书</button>
      </div>
    </div>
    <div class="act-layout">
      <div class="card checklist pad0" id="checkCard">
        <div class="card-head" style="padding:14px 14px 0"><h3>材料清单</h3>
          <span class="muted">15 项 · ✓ 已齐 ✗ 缺 — 不适用</span></div>
        <div id="checkList">${renderChecklistHtml(check)}</div>
        <div class="check-sum" id="checkSum">${checkSummary(check)}</div>
      </div>
      <div class="card">
        <div class="files-head">
          <div class="path">${crumb}</div>
          <div style="margin-left:auto" class="row">
            ${sub ? '<button class="btn sm" data-act="up-dir">返回上级</button>' : ''}
            <a class="btn sm" href="#/docs?project=${enc(name)}">上传归档</a>
            <button class="btn sm" data-act="refresh-act">刷新</button>
          </div>
        </div>
        <div id="fileList">${renderFileRows(entries, sub, check)}</div>
        <div class="hint" style="margin-top:10px">「上传文件」直接传到当前活动目录；「上传归档」进入文档库归档流程。</div>
      </div>
    </div>`;

  // 点清单项后按前缀过滤文件
  els('#checkList .check-item').forEach(node => {
    node.addEventListener('click', () => {
      const no = Number(node.dataset.no);
      actCtx.selected = actCtx.selected === no ? 0 : no;
      els('#checkList .check-item').forEach(x => x.classList.toggle('active', Number(x.dataset.no) === actCtx.selected));
      renderFilesPanel();
    });
  });
}

function renderChecklistHtml(check) {
  const marks = { ok: '✓', missing: '✗', na: '—' };
  const labels = { ok: '已齐', missing: '缺', na: '不适用' };
  return (check.items || []).map(it => `<div class="check-item" data-no="${it.no}">
    <span class="no">${it.no}</span>
    <span class="mark ${esc(it.status)}" title="${esc(labels[it.status] || '')}">${marks[it.status] || '—'}</span>
    <span class="body">
      <span class="name">${esc(it.name)}</span>
      <span class="detail" title="${esc(it.detail || '')}">${esc(it.detail || '')}</span>
    </span>
  </div>`).join('');
}

function checkSummary(check) {
  const verdict = check.complete ? '<span class="badge ok">完整</span>' : '<span class="badge warn">未完整</span>';
  return `齐 ${check.ok_count} 项 · 缺 ${check.missing_count} 项 · 不适用 ${check.na_count} 项 ${verdict}`;
}

function renderFileRows(entries, sub, check) {
  const selected = actCtx ? actCtx.selected : 0;
  let list = entries || [];
  if (selected && !sub) {
    const prefixes = CHECK_PREFIX[selected] || [];
    if (prefixes.length) {
      list = list.filter(e => prefixes.some(p => String(e.name).startsWith(p)));
    } else {
      list = [];
    }
  }
  if (!list.length) {
    return empty(selected && !sub ? '该项没有对应文件' : '目录为空');
  }
  return list.map(item => {
    const path = sub ? `${sub}/${item.name}` : item.name;
    const isDir = !!item.is_dir;
    const nameHtml = isDir
      ? `<a href="#" data-act="enter-dir" data-path="${esc(path)}">${esc(item.name)}/</a>`
      : `<a href="#" data-act="download" data-path="${esc(path)}">${esc(item.name)}</a>`;
    return `<div class="file-row ${isDir ? 'dir' : ''}">
      <span class="ico">${isDir ? '▸' : '·'}</span>
      <span class="nm">${nameHtml}</span>
      <span class="sz">${isDir ? '目录' : esc(fmtBytes(item.size))}</span>
      <span class="tm">${esc(fmtTime(item.mtime))}</span>
      <span class="op">
        ${isDir ? '' : `<button class="btn sm" data-act="download" data-path="${esc(path)}">下载</button>`}
        ${canWrite() ? `<button class="btn sm danger" data-act="del-file" data-path="${esc(path)}">删除</button>` : ''}
      </span>
    </div>`;
  }).join('');
}

function renderFilesPanel() {
  if (!actCtx) return;
  const box = el('#fileList');
  if (!box) return;
  box.innerHTML = renderFileRows(actCtx.entries, actCtx.sub, actCtx.detail.check);
}

async function refreshActivity() {
  if (!actCtx) return;
  const { projectName, dirname, sub } = actCtx;
  const hash = `#/project/${enc(projectName)}/activity/${enc(dirname)}` + (sub ? `?sub=${enc(sub)}` : '');
  if (location.hash === hash) await route(); else location.hash = hash;
}

async function gotoSub(path) {
  if (!actCtx) return;
  const { projectName, dirname } = actCtx;
  location.hash = `#/project/${enc(projectName)}/activity/${enc(dirname)}?sub=${enc(path)}`;
}

async function downloadActivityFile(path) {
  if (!actCtx) return;
  const { projectName, dirname } = actCtx;
  const url = `${API}/projects/${enc(projectName)}/activities/${enc(dirname)}/download?path=${enc(path)}`;
  const r = await fetch(url, { headers: token ? { 'x-token': token } : {} });
  if (!r.ok) {
    let msg = r.status + '';
    try { const d = await r.json(); msg = d.detail || msg; } catch { /* ignore */ }
    toast('下载失败：' + msg, 'err');
    return;
  }
  const blob = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = path.split('/').pop() || 'file';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 800);
}

async function deleteActivityFile(path) {
  if (!actCtx) return;
  const { projectName, dirname } = actCtx;
  if (!confirm(`删除「${path}」？\n会移到活动目录的 .trash，可在磁盘恢复。`)) return;
  await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/files`, {
    method: 'DELETE', body: { path },
  });
  toast('已移入回收站', 'ok');
  await refreshActivity();
}

async function recheckActivity() {
  if (!actCtx) return;
  const { projectName, dirname } = actCtx;
  const check = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/check`);
  actCtx.detail.check = check;
  const list = el('#checkList');
  if (list) {
    list.innerHTML = renderChecklistHtml(check);
    els('#checkList .check-item').forEach(node => {
      node.addEventListener('click', () => {
        const no = Number(node.dataset.no);
        actCtx.selected = actCtx.selected === no ? 0 : no;
        els('#checkList .check-item').forEach(x => x.classList.toggle('active', Number(x.dataset.no) === actCtx.selected));
        renderFilesPanel();
      });
    });
  }
  const sum = el('#checkSum');
  if (sum) sum.innerHTML = checkSummary(check);
  toast(check.complete ? '核对完成：材料齐全' : `核对完成：还缺 ${check.missing_count} 项`,
    check.complete ? 'ok' : 'warn');
}

/* ---------------- 活动目录上传 ---------------- */
async function collectActivityDirs(base, rel = '', depth = 0, acc = []) {
  const entries = await api(base + '/files' + (rel ? '?sub=' + enc(rel) : ''));
  for (const e of entries) {
    if (!e.is_dir) continue;
    const path = rel ? `${rel}/${e.name}` : e.name;
    acc.push(path);
    if (depth < 1) await collectActivityDirs(base, path, depth + 1, acc);
  }
  return acc;
}

async function openActivityUploadModal() {
  if (!actCtx) return;
  if (!canWrite()) { toast('需 editor/admin 权限', 'warn'); return; }
  const { projectName, dirname } = actCtx;
  const base = `/projects/${enc(projectName)}/activities/${enc(dirname)}`;
  openModal('上传文件到活动目录', `
    <div class="form-grid">
      <label class="field"><span>目标子目录</span>
        <select id="actUpSub"><option value="">活动根目录</option></select></label>
      <label class="field"><span>文件（≤50MB）</span><input type="file" id="actUpFile"></label>
    </div>
    <div class="hint" style="margin-top:8px">
      同名文件不会覆盖；扩展名限 docx / xlsx / doc / xls / pdf / 图片 / txt / md / zip / 7z。
    </div>
    <div id="actUpMsg" style="margin-top:10px"></div>
  `, `<button class="btn" data-close-modal>取消</button>
      <button class="btn primary" data-act="act-upload-run">上传</button>`);

  const sel = el('#actUpSub');
  try {
    const dirs = await collectActivityDirs(base);
    sel.innerHTML += dirs.map(d => `<option value="${esc(d)}">${esc(d)}</option>`).join('');
    if (actCtx.sub) sel.value = actCtx.sub;
  } catch (e) {
    // 子目录列表拿不到时仍可传到活动根目录
    toast('子目录列表加载失败：' + e.message, 'warn');
  }
}

async function uploadActivityFile() {
  if (!actCtx) return;
  const input = el('#actUpFile');
  const f = input && input.files[0];
  if (!f) { toast('请选择文件', 'warn'); return; }
  const { projectName, dirname } = actCtx;
  const sub = el('#actUpSub').value;
  const fd = new FormData();
  fd.append('file', f);
  fd.append('sub', sub);
  el('#actUpMsg').innerHTML = '<span class="muted">上传中…</span>';
  try {
    const d = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/upload`, {
      method: 'POST', body: fd,
    });
    closeModal();
    if (d.warning) toast('已上传；' + d.warning, 'warn');
    else toast(`已上传：${d.path}`, 'ok');
    await refreshActivity();          // 刷新文件列表与核对结果
  } catch (e) {
    el('#actUpMsg').innerHTML = `<div class="notice">上传失败：${esc(e.message)}</div>`;
    toast('上传失败：' + e.message, 'err');   // 409/400/413 显示后端 detail
  }
}

/* ---------------- 生成材料弹窗 ---------------- */
function activityNameFromDir(dirname) {
  const raw = String(dirname || '').replace(/^\d+[-_]/, '');
  const m = raw.match(/^(.*?)[-_]第(.+期)$/);
  return m ? `${m[1]}（第${m[2]}）` : raw;
}

function prefillData(proj, detail, dirname) {
  const d = (proj && proj.data) || {};
  const p = d.project || {};
  const parties = d.parties || {};
  const leader = (d.staff || {}).leader || {};
  const executer = parties.executer || {};
  return {
    '项目名称': p.name || (proj && proj.name) || '',
    '活动名称': activityNameFromDir(dirname),
    '项目编号': (detail && detail.code) || '',
    '项目负责人': leader.name || '',
    '项目社工': leader.name || '',
    '执行社工': leader.name || '',
    '年月日': todayCn(),
    '填表日期': todayCn(),
    '收款单位': executer.name || '',
    '帐号': executer.account || '',
    '开户行': executer.bank || '',
  };
}

async function openFillModal() {
  if (!actCtx) return;
  const { projectName, dirname, project, detail } = actCtx;
  const tpls = await ensureTemplates();
  if (!tpls.length) { toast('模板库为空，请检查 templates/library', 'warn'); return; }
  const defaults = prefillData(project, detail, dirname);
  const options = tpls.map(t => `<option value="${esc(t.name)}">${esc(t.name)}（${esc(t.kind)}）</option>`).join('');
  openModal('生成材料 · 模板填充', `
    <div class="form-grid">
      <label class="field"><span>模板</span><select id="fillTpl">${options}</select></label>
      <label class="field"><span>输出文件名（留空 = 模板名）</span><input id="fillOut" placeholder="如 01-活动计划.docx"></label>
    </div>
    <div class="section-title">模板占位符</div>
    <div id="fillPh" class="chips"></div>
    <div class="section-title">填充数据 <span class="hint">留空不改；改动过的值会覆盖系统数据</span></div>
    <div id="fillForm" class="form-grid"></div>
    <div id="fillMsg" style="margin-top:12px"></div>
  `, `<button class="btn" data-close-modal>取消</button>
      <button class="btn primary" data-act="fill-submit">提交生成</button>`, true);

  const tplSel = el('#fillTpl');
  const renderForm = () => {
    const t = tpls.find(x => x.name === tplSel.value) || tpls[0];
    const phs = t.placeholders || [];
    el('#fillPh').innerHTML = phs.length
      ? phs.map(p => `<span class="chip">${esc(p)}</span>`).join('')
      : '<span class="muted">该模板没有占位符</span>';
    el('#fillForm').innerHTML = phs.map(p => {
      const key = phKey(p);
      const val = defaults[key] != null ? defaults[key] : '';
      return `<label class="field"><span>${esc(key)}</span>
        <input data-fill-key="${esc(key)}" data-def="${esc(val)}" value="${esc(val)}" placeholder="留空则用系统数据">
      </label>`;
    }).join('') || '<div class="muted">无占位符，直接提交即可复制模板。</div>';
  };
  tplSel.addEventListener('change', renderForm);
  renderForm();
}

async function submitFill() {
  if (!actCtx) return;
  const { projectName, dirname } = actCtx;
  const tpl = el('#fillTpl').value;
  const out = el('#fillOut').value.trim();
  const data = {};
  els('[data-fill-key]').forEach(inp => {
    const key = inp.dataset.fillKey;
    const def = inp.dataset.def || '';
    const val = inp.value.trim();
    if (val !== def) data[key] = val;       // 包含「清空」：显式传空串覆盖
  });
  const body = { template: tpl, data };
  if (out) body.output = out;
  el('#fillMsg').innerHTML = '<span class="muted">生成中…</span>';
  try {
    const r = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/fill`, {
      method: 'POST', body,
    });
    const missing = r.missing || [];
    el('#fillMsg').innerHTML = `<div class="notice green">
      已生成 <b>${esc(r.path)}</b>，填充 ${esc(r.filled)} 处${missing.length ? `；缺 ${missing.length} 个占位符：${esc(missing.join('、'))}` : ''}。
    </div>`;
    $('modalFoot').innerHTML = '<button class="btn" data-close-modal>关闭</button>' +
      '<button class="btn primary" data-act="close-refresh">关闭并刷新</button>';
    toast('材料已生成：' + r.path, 'ok');
  } catch (e) {
    el('#fillMsg').innerHTML = `<div class="notice">生成失败：${esc(e.message)}</div>`;
    toast('生成失败：' + e.message, 'err');
  }
}

/* ---------------- LLM 生成弹窗 ---------------- */
function pickTemplate(tpls, task) {
  const want = task === 'brief' ? ['简报'] : task === 'plan' ? ['计划书'] : ['总结报告'];
  return tpls.find(t => want.some(w => t.name.includes(w))) || tpls[0] || null;
}

async function openLlmModal() {
  if (!actCtx) return;
  const { projectName, dirname } = actCtx;
  const tpls = await ensureTemplates();
  const tplOptions = tpls.map(t => `<option value="${esc(t.name)}">${esc(t.name)}（${esc(t.kind)}）</option>`).join('');
  openModal('LLM 生成', `
    <div class="form-grid">
      <label class="field"><span>任务</span>
        <select id="llmTask">${TASKS.map(t => `<option value="${t.v}">${esc(t.label)}</option>`).join('')}</select>
      </label>
      <label class="field"><span>文风</span>
        <select id="llmStyle">${STYLES.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('')}</select>
      </label>
      <label class="field"><span>落盘模板</span>
        <select id="llmTpl">${tplOptions || '<option value="">（模板库为空）</option>'}</select>
      </label>
      <label class="field"><span>输出文件名（留空 = 模板名）</span><input id="llmOut" placeholder="如 07-简报.docx"></label>
    </div>
    <div class="notice blue">先生成 JSON，每个字段可编辑；「应用」后按模板落盘。未修改时走 llm-fill，修改过则用编辑后的数据走模板填充。</div>
    <div class="row" style="margin-bottom:10px">
      <button class="btn" data-act="llm-run">生成</button>
      <span class="muted" id="llmMsg"></span>
    </div>
    <div id="llmResult"></div>
  `, `<button class="btn" data-close-modal>取消</button>
      <button class="btn primary" data-act="llm-apply">应用并落盘</button>`, true);

  const taskSel = el('#llmTask');
  const syncTpl = () => {
    const t = pickTemplate(tpls, taskSel.value);
    if (t) el('#llmTpl').value = t.name;
  };
  taskSel.addEventListener('change', syncTpl);
  syncTpl();
  actCtx.llm = { result: null };
}

async function runLlm() {
  if (!actCtx) return;
  const { projectName, dirname } = actCtx;
  const task = el('#llmTask').value;
  const style = el('#llmStyle').value;
  el('#llmMsg').textContent = '生成中，可能要 1–3 分钟…';
  el('#llmResult').innerHTML = '';
  try {
    const r = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/llm`, {
      method: 'POST', body: { task, style },
    });
    actCtx.llm = { result: r.result || {} };
    el('#llmMsg').textContent = '生成完成，可逐字段修改。';
    el('#llmResult').innerHTML = `<div class="section-title">生成结果</div>` +
      Object.entries(r.result || {}).map(([k, v]) => {
        const isArr = Array.isArray(v);
        const text = isArr ? JSON.stringify(v, null, 2) : String(v ?? '');
        return `<label class="field"><span>${esc(k)}${isArr ? '（数组，JSON）' : ''}</span>
          <textarea data-llm-key="${esc(k)}" data-array="${isArr ? '1' : '0'}">${esc(text)}</textarea>
        </label>`;
      }).join('');
  } catch (e) {
    el('#llmMsg').textContent = '生成失败：' + e.message;
    toast('LLM 生成失败：' + e.message, 'err');
  }
}

async function applyLlm() {
  if (!actCtx || !actCtx.llm || !actCtx.llm.result) {
    toast('请先生成内容', 'warn');
    return;
  }
  const { projectName, dirname } = actCtx;
  const task = el('#llmTask').value;
  const style = el('#llmStyle').value;
  const tpl = el('#llmTpl').value;
  const out = el('#llmOut').value.trim();
  if (!tpl) { toast('请选择落盘模板', 'warn'); return; }

  const data = {};
  for (const ta of els('[data-llm-key]')) {
    const key = ta.dataset.llmKey;
    if (ta.dataset.array === '1') {
      try { data[key] = JSON.parse(ta.value); }
      catch { toast(`字段「${key}」不是合法 JSON`, 'err'); return; }
    } else {
      data[key] = ta.value;
    }
  }
  const dirty = JSON.stringify(data) !== JSON.stringify(actCtx.llm.result);
  const body = { task, style, template: tpl };
  if (out) body.output = out;
  el('#llmMsg').textContent = '落盘中…';
  try {
    let r;
    if (dirty) {
      r = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/fill`, {
        method: 'POST', body: { template: tpl, data, ...(out ? { output: out } : {}) },
      });
    } else {
      r = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/llm-fill`, {
        method: 'POST', body,
      });
    }
    const missing = r.missing || [];
    el('#llmMsg').textContent = `已落盘：${r.path}（填充 ${r.filled} 处${missing.length ? `，缺 ${missing.length} 项` : ''}）`;
    $('modalFoot').innerHTML = '<button class="btn" data-close-modal>关闭</button>' +
      '<button class="btn primary" data-act="close-refresh">关闭并刷新</button>';
    toast('已落盘：' + r.path, 'ok');
  } catch (e) {
    el('#llmMsg').textContent = '落盘失败：' + e.message;
    toast('落盘失败：' + e.message, 'err');
  }
}

/* ============================================================
 * 5. 模板
 * ============================================================ */
let tplExpanded = new Set();

async function pageTemplates(params) {
  setTitle('模板');
  const tab = params.get('tab') === 'generate' ? 'generate' : 'library';
  $('view').innerHTML = loading();
  const tpls = await ensureTemplates();
  const projects = await ensureProjects().catch(() => []);

  const tabs = `<div class="tabs">
    <button data-act="tpl-tab" data-tab="library" class="${tab === 'library' ? 'active' : ''}">模板库</button>
    <button data-act="tpl-tab" data-tab="generate" class="${tab === 'generate' ? 'active' : ''}">文书生成</button>
  </div>`;

  if (tab === 'generate') {
    $('view').innerHTML = `
      ${pageHead('模板', '模板库与文书生成')}
      <div class="card">
        ${tabs}
        <div class="form-grid">
          <label class="field"><span>文书类型</span>
            <select id="genType">
              ${['个案记录', '家访提纲', '服务计划', '活动方案', '项目申报', '评估报告']
                .map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join('')}
            </select>
          </label>
          <label class="field"><span>项目（素材范围）</span>
            <select id="genProject"><option value="">全部授权项目</option>
              ${projects.map(p => `<option value="${esc(p.name)}">${esc(projectShort(p))}</option>`).join('')}
            </select>
          </label>
          <label class="field full"><span>主题 / 关键词</span>
            <textarea id="genContext" placeholder="例：惠民社区为老服务活动，参与者 30 人"></textarea>
          </label>
        </div>
        <div class="row" style="margin-top:12px">
          <button class="btn primary" data-act="gen-run">生成</button>
          <span class="muted">生成 2–6 分钟，请勿重复点击</span>
        </div>
        <div id="genOut"></div>
      </div>`;
    return;
  }

  const rows = tpls.map(t => {
    const open = tplExpanded.has(t.name);
    const phs = t.placeholders || [];
    return `<tr class="tpl-row ${open ? 'open' : ''}" data-act="tpl-toggle" data-name="${esc(t.name)}">
        <td>${esc(t.name)}</td>
        <td><span class="badge ${t.kind === 'xlsx' ? 'info' : 'muted'}">${esc(t.kind)}</span></td>
        <td class="num">${phs.length}</td>
        <td class="nowrap">${open ? '收起' : '查看占位符'}</td>
      </tr>
      ${open ? `<tr class="tpl-detail"><td colspan="4">
        <div class="label">占位符（${phs.length}）</div>
        <div class="chips">${phs.length ? phs.map(p => `<span class="chip">${esc(p)}</span>`).join('') : '<span class="muted">无</span>'}</div>
      </td></tr>` : ''}`;
  }).join('');

  $('view').innerHTML = `
    ${pageHead('模板', `模板库共 ${tpls.length} 个（docx / xlsx）`)}
    <div class="card">
      ${tabs}
      <div class="table-wrap"><table>
        <thead><tr><th>模板名</th><th>类型</th><th class="num">占位符数</th><th>操作</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="4">${empty('模板库为空')}</td></tr>`}</tbody>
      </table></div>
    </div>`;
}

async function runGenerate() {
  const type = el('#genType').value;
  const project = el('#genProject').value;
  const context = el('#genContext').value.trim();
  if (!context) { toast('请输入主题 / 关键词', 'warn'); return; }
  const out = el('#genOut');
  out.innerHTML = '<div class="loading">生成中，2–6 分钟…</div>';
  try {
    const body = { type, context };
    if (project) body.project = project;
    const d = await api('/generate', { method: 'POST', body });
    const text = (d && (d.content || d.output || d.result || d.text)) || JSON.stringify(d, null, 2);
    out.innerHTML = `${d.truncated ? '<div class="notice">正文被模型输出上限截断，建议缩短主题或拆分文书。</div>' : ''}
      <pre class="genout">${esc(text)}</pre>`;
  } catch (e) {
    out.innerHTML = `<div class="error-box">生成失败：${esc(e.message)}</div>`;
    toast('生成失败：' + e.message, 'err');
  }
}

/* ============================================================
 * 6. 同步
 * ============================================================ */
async function pageSync() {
  setTitle('同步');
  $('view').innerHTML = loading();
  const projects = await ensureProjects().catch(() => []);
  let datasets = null;
  let dsError = '';
  try { datasets = await api('/cognee/datasets'); }
  catch (e) { dsError = e.message; }

  const projOptions = projects.map(p => `<option value="${esc(p.name)}">${esc(projectShort(p))}</option>`).join('');
  const dsRows = datasets && datasets.items && datasets.items.length
    ? datasets.items.map(d => `<tr>
        <td>${esc(d.name || d.id)}</td>
        <td class="num">${d.count == null ? '—' : esc(d.count)}</td>
        <td class="num">${d.nodes == null ? '—' : esc(d.nodes)}</td>
        <td class="nowrap">${esc(fmtTime(d.created_at))}</td>
      </tr>`).join('')
    : `<tr><td colspan="4">${empty(dsError ? '读取失败：' + dsError : '暂无数据集')}</td></tr>`;

  $('view').innerHTML = `
    ${pageHead('同步', '飞书镜像与 Cognee 入库都是后台任务，提交后立即返回')}
    <div class="card">
      <div class="card-head"><h3>飞书同步</h3>${isAdmin() ? '' : '<span class="badge muted">需 admin</span>'}</div>
      <div class="row">
        <select id="syncProject" class="grow">
          <option value="">全部项目</option>${projOptions}
        </select>
        <button class="btn primary" data-act="sync-run" ${isAdmin() ? '' : 'disabled'}>开始同步</button>
      </div>
      <div class="notice blue" style="margin:12px 0 0">同步在后台跑，页面不会实时更新；进度与结果请看服务日志和飞书目录。</div>
      <div id="syncMsg"></div>
    </div>
    <div class="card">
      <div class="card-head"><h3>Cognee 入库</h3>${isAdmin() ? '' : '<span class="badge muted">需 admin</span>'}</div>
      <div class="row">
        <select id="indexProject" class="grow">
          <option value="">全部项目</option>${projOptions}
        </select>
        <button class="btn primary" data-act="index-run" ${isAdmin() ? '' : 'disabled'}>开始入库</button>
      </div>
      <div id="indexMsg"></div>
    </div>
    <div class="card">
      <div class="card-head"><h3>已有数据集</h3><span class="muted">条数 / 图节点数</span>
        <div class="actions"><button class="btn sm" data-act="ds-refresh">刷新</button></div>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>数据集</th><th class="num">条数</th><th class="num">节点数</th><th>创建时间</th></tr></thead>
        <tbody>${dsRows}</tbody>
      </table></div>
    </div>`;
}

async function startFeishuSync() {
  const project = el('#syncProject').value;
  el('#syncMsg').innerHTML = '<div class="muted" style="margin-top:10px">提交中…</div>';
  try {
    const r = await api('/sync/feishu', { method: 'POST', body: { project } });
    const names = (r.projects || []).map(p => p.name).join('、');
    el('#syncMsg').innerHTML = `<div class="notice green" style="margin:12px 0 0">
      已开始：${esc(r.task || '飞书同步')}；范围：${esc(names || '全部项目')}。</div>`;
    toast('飞书同步已在后台开始', 'ok');
  } catch (e) {
    el('#syncMsg').innerHTML = `<div class="notice" style="margin:12px 0 0">提交失败：${esc(e.message)}</div>`;
    toast('同步失败：' + e.message, 'err');
  }
}

async function startCogneeIndex() {
  const project = el('#indexProject').value;
  el('#indexMsg').innerHTML = '<div class="muted" style="margin-top:10px">提交中…</div>';
  try {
    const r = await api('/cognee/index', { method: 'POST', body: { project } });
    el('#indexMsg').innerHTML = `<div class="notice green" style="margin:12px 0 0">
      已开始：${esc(r.task || 'Cognee 入库')}；范围：${esc((r.projects || []).join('、') || '全部项目')}。</div>`;
    toast('Cognee 入库已在后台开始', 'ok');
  } catch (e) {
    el('#indexMsg').innerHTML = `<div class="notice" style="margin:12px 0 0">提交失败：${esc(e.message)}</div>`;
    toast('入库失败：' + e.message, 'err');
  }
}

/* ============================================================
 * 7. 检索 + 文档库
 * ============================================================ */
function parseSearchOutput(output) {
  const raw = String(output ?? '');
  const blocks = [];
  const head = [];
  let cur = null;
  for (const line of raw.split('\n')) {
    const m = line.match(/^###\s+(.*)$/);
    if (m) {
      if (cur) blocks.push(cur);
      let rest = m[1].trim();
      let score = null;
      const sm = rest.match(/[（(]\s*score\s*[:：]?\s*([\d.]+)\s*[)）]\s*$/i);
      if (sm) { score = sm[1]; rest = rest.slice(0, sm.index).trim(); }
      cur = { title: rest || '(无标题)', score, lines: [] };
    } else if (cur) {
      cur.lines.push(line);
    } else {
      head.push(line);
    }
  }
  if (cur) blocks.push(cur);
  blocks.forEach(b => {
    while (b.lines.length && !b.lines[0].trim()) b.lines.shift();
    while (b.lines.length && !b.lines[b.lines.length - 1].trim()) b.lines.pop();
    b.content = b.lines.join('\n');
  });
  return { head: head.join('\n').trim(), blocks, raw };
}

function renderSearchResults(output) {
  const { head, blocks, raw } = parseSearchOutput(output);
  if (!blocks.length) {
    return head || raw ? `<pre class="rawbox">${esc(head || raw)}</pre>` : empty('无结果');
  }
  const cards = blocks.map(b => `<div class="result">
    <h4>${esc(b.title)}${b.score != null ? `<span class="score">score ${esc(b.score)}</span>` : ''}</h4>
    <p class="snippet">${esc(b.content.slice(0, 240))}${b.content.length > 240 ? '…' : ''}</p>
    ${b.content.length > 240 ? `<details><summary>展开全文</summary><pre class="rawbox">${esc(b.content)}</pre></details>` : ''}
  </div>`).join('');
  const meta = `<div class="muted" style="margin-bottom:8px">共 ${blocks.length} 条结果${head ? ' · ' + esc(head.split('\n')[0]) : ''}</div>`;
  const rawToggle = `<details style="margin-top:10px"><summary style="cursor:pointer;color:#2563eb;font-size:12px">查看原始输出</summary>
    <pre class="rawbox">${esc(raw)}</pre></details>`;
  return meta + cards + rawToggle;
}

async function pageSearch(params) {
  const tab = params.get('tab') === 'docs' ? 'docs' : 'search';
  setTitle(tab === 'docs' ? '文档库' : '检索');
  const projects = await ensureProjects().catch(() => []);
  const wantProject = params.get('project') || '';
  const projOptions = projects.map(p => `<option value="${esc(p.name)}"${p.name === wantProject ? ' selected' : ''}>${esc(projectShort(p))}</option>`).join('');
  const tabs = `<div class="tabs">
    <button data-act="search-tab" data-tab="search" class="${tab === 'search' ? 'active' : ''}">知识检索</button>
    <button data-act="search-tab" data-tab="docs" class="${tab === 'docs' ? 'active' : ''}">文档库</button>
  </div>`;

  if (tab === 'search') {
    $('view').innerHTML = `
      ${pageHead('检索', '知识库全文检索；单项目 10–90 秒')}
      <div class="card">
        ${tabs}
        <div class="row">
          <input id="searchQ" class="grow" placeholder="检索词，例：民乐北巷 治理公约 协商议事">
          <select id="searchProject"><option value="">全部授权项目</option>${projOptions}</select>
          <button class="btn primary" data-act="search-run">检索</button>
        </div>
        <div class="hint" style="margin-top:8px">不带项目时按项目串行检索，多项目耗时叠加。</div>
        <div id="searchOut" style="margin-top:14px"></div>
      </div>`;
    return;
  }

  $('view').innerHTML = `
    ${pageHead('文档库', '归档文档的浏览、上传、下载与编辑')}
    <div class="card">
      ${tabs}
      <div class="row">
        <select id="dProject"><option value="">全部项目</option>${projOptions}</select>
        <select id="dCategory"><option value="">全部类别</option>
          ${CATS.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('')}
        </select>
        <select id="dHealth">
          <option value="">全部状态</option>
          <option value="source_missing">源文件缺失</option>
          <option value="index_stale">索引过期</option>
          <option value="index_missing">未索引</option>
        </select>
        <input id="dQ" placeholder="文件名关键词" style="width:180px">
        <button class="btn" data-act="docs-filter">筛选</button>
        <span class="muted" id="docCount"></span>
      </div>
      <div class="hint" id="docHealth" style="margin-top:6px"></div>
      <div class="table-wrap" style="margin-top:10px"><table>
        <thead><tr><th>文件名</th><th>项目</th><th>类别</th><th>状态</th><th>归档时间</th><th></th></tr></thead>
        <tbody id="docRows"><tr><td colspan="6">${loading()}</td></tr></tbody>
      </table></div>
    </div>
    <div class="card">
      <div class="card-head"><h3>上传归档</h3><span class="muted">归档 3–30 秒；索引由定时器异步补</span></div>
      <div class="upload-box">
        <input type="file" id="upFile">
        <select id="upProject">${projects.map(p => `<option value="${esc(p.name)}"${p.name === wantProject ? ' selected' : ''}>${esc(projectShort(p))}</option>`).join('')}</select>
        <select id="upCategory"><option value="">自动分类</option>
          ${CATS.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('')}
        </select>
        <button class="btn primary" data-act="upload-run">上传归档</button>
      </div>
      <div class="hint" style="margin-top:8px">图片 / PDF 必须按「日期-项目名称-描述」命名，否则服务端拒收。</div>
      <div id="upMsg" style="margin-top:8px"></div>
    </div>`;
  await loadDocsPane();
}

async function loadDocsPane() {
  const p = el('#dProject').value;
  const c = el('#dCategory').value;
  const q = el('#dQ').value.trim();
  const hf = el('#dHealth').value;
  const qs = new URLSearchParams();
  if (p) qs.set('project', p);
  if (c) qs.set('category', c);
  if (q) qs.set('q', q);
  const [data, health] = await Promise.all([
    api('/docs?' + qs.toString()),
    api('/docs/health' + (p ? '?project=' + enc(p) : '')).catch(() => ({ items: {} })),
  ]);
  const hmap = health.items || {};
  const rows = (data.items || []).filter(d => {
    const h = hmap[d.token] || {};
    if (!hf) return true;
    if (hf === 'source_missing') return h.source === 'missing';
    if (hf === 'index_stale') return h.index === 'stale';
    if (hf === 'index_missing') return h.index === 'missing';
    return true;
  });
  el('#docCount').textContent = `共 ${rows.length} / ${data.total || 0} 条`;
  const counts = { stale: 0, missing: 0, source: 0 };
  Object.values(hmap).forEach(h => {
    if (h.index === 'stale') counts.stale++;
    if (h.index === 'missing') counts.missing++;
    if (h.source === 'missing') counts.source++;
  });
  el('#docHealth').textContent =
    `索引过期 ${counts.stale} · 未索引 ${counts.missing} · 原件缺失 ${counts.source}`;

  el('#docRows').innerHTML = rows.map(d => {
    const h = hmap[d.token] || {};
    const badges = [];
    if (h.source === 'missing') badges.push('<span class="badge err">原件缺失</span>');
    if (h.index === 'stale') badges.push('<span class="badge warn">索引过期</span>');
    if (h.index === 'missing') badges.push('<span class="badge warn">未索引</span>');
    if (!badges.length) badges.push(h.source === 'ok' ? '<span class="badge ok">正常</span>' : '<span class="muted">—</span>');
    return `<tr>
      <td><a href="#/doc/${enc(d.token)}">${esc(d.name)}</a></td>
      <td>${esc(d.project)}</td>
      <td>${esc(d.category || '')}</td>
      <td>${badges.join(' ')}</td>
      <td class="nowrap">${esc(fmtTime(d.archived_at))}</td>
      <td class="nowrap">${isAdmin() ? `<button class="btn sm danger" data-act="doc-del" data-token="${esc(d.token)}" data-name="${esc(d.name)}">删除</button>` : ''}</td>
    </tr>`;
  }).join('') || `<tr><td colspan="6">${empty('没有匹配的文档')}</td></tr>`;
}

async function runSearch() {
  const q = el('#searchQ').value.trim();
  if (!q) { toast('请输入检索词', 'warn'); return; }
  const project = el('#searchProject').value;
  el('#searchOut').innerHTML = '<div class="loading">检索中，可能要 10–90 秒…</div>';
  try {
    const d = await api(`/search?q=${enc(q)}&project=${enc(project)}`);
    el('#searchOut').innerHTML = d.note
      ? `<div class="notice">${esc(d.note)}</div>` + renderSearchResults(d.output)
      : renderSearchResults(d.output);
  } catch (e) {
    el('#searchOut').innerHTML = `<div class="error-box">检索失败：${esc(e.message)}</div>`;
    toast('检索失败：' + e.message, 'err');
  }
}

async function uploadDoc() {
  const f = el('#upFile').files[0];
  if (!f) { toast('请选择文件', 'warn'); return; }
  const fd = new FormData();
  fd.append('file', f);
  fd.append('project', el('#upProject').value);
  const cat = el('#upCategory').value;
  if (cat) fd.append('category', cat);
  el('#upMsg').innerHTML = '<span class="muted">上传归档中，3–30 秒…</span>';
  try {
    const d = await api('/docs', { method: 'POST', body: fd });
    if (d && d.accepted) {
      el('#upMsg').innerHTML = `<div class="notice">归档超时（已受理）。用 token 查状态：<span class="mono">${esc(d.token)}</span></div>`;
      toast('归档超时，已受理', 'warn');
    } else if (d && d.duplicate) {
      el('#upMsg').innerHTML = `<div class="notice green">内容重复，已存在：${esc(d.name || d.token)}</div>`;
      toast('内容重复，未重复建档', 'ok');
    } else {
      el('#upMsg').innerHTML = `<div class="notice green">已归档：${esc(d.name || f.name)} → ${esc(d.project || '')}</div>`;
      toast('上传归档成功', 'ok');
    }
    el('#upFile').value = '';
    await loadDocsPane();
  } catch (e) {
    el('#upMsg').innerHTML = `<div class="notice">上传失败：${esc(e.message)}</div>`;
    toast('上传失败：' + e.message, 'err');
  }
}

async function deleteDoc(tok, name) {
  if (!isAdmin()) { toast('仅 admin 可删除', 'warn'); return; }
  if (!confirm(`删除「${name}」？默认移入回收站，可恢复。`)) return;
  try {
    await api('/docs/' + enc(tok), { method: 'DELETE' });
    toast('已移入回收站', 'ok');
    await loadDocsPane();
  } catch (e) {
    toast('删除失败：' + e.message, 'err');
  }
}

/* ---------------- 文档详情 ---------------- */
async function pageDoc(tok) {
  setTitle('文档详情');
  $('view').innerHTML = loading();
  const d = await api('/docs/' + enc(tok));
  $('view').innerHTML = `
    <div class="crumb"><a href="#/search?tab=docs">文档库</a> / ${esc(d.name)}</div>
    <div class="page-head">
      <div>
        <h2>${esc(d.name)}</h2>
        <div class="sub">${esc(d.project)} / ${esc(d.category || '未分类')}</div>
      </div>
      <div class="actions">
        <button class="btn" data-act="doc-source">下载原件</button>
        ${canWrite() ? '<button class="btn" data-act="doc-edit">编辑 md</button>' : ''}
        ${isAdmin() ? '<button class="btn danger" data-act="doc-delete">删除</button>' : ''}
        <a class="btn" href="#/search?tab=docs">返回</a>
      </div>
    </div>
    <div class="card">
      <div id="docView" class="docbody">${esc(d.content || '（无 md 内容）')}</div>
      <textarea id="docEditor" class="hidden" style="min-height:50vh"></textarea>
      <div class="row hidden" id="docEditBar" style="margin-top:12px">
        <button class="btn primary" data-act="doc-save">保存</button>
        <button class="btn" data-act="doc-cancel">取消</button>
        <span class="hint">保存后触发索引重建，可能 1–3 分钟</span>
      </div>
    </div>`;
  actCtx = { docToken: tok, docName: d.name, docContent: d.content || '' };
}

/* ============================================================
 * 8. 管理：用户 / 审计
 * ============================================================ */
async function pageUsers() {
  setTitle('用户管理');
  if (!isAdmin()) { $('view').innerHTML = empty('需要 admin 权限'); return; }
  $('view').innerHTML = loading();
  const d = await api('/admin/users');
  const users = d.items || [];
  const projChecks = (me.projects || []).map(p =>
    `<label style="display:inline-flex;align-items:center;gap:6px;margin-right:12px">
      <input type="checkbox" value="${esc(p)}" data-newproj style="width:auto"> ${esc(p)}
    </label>`).join('');

  const rows = users.map(u => `<tr>
      <td class="num">${u.id}</td>
      <td>${esc(u.username)}</td>
      <td><span class="badge ${u.role === 'admin' ? 'info' : u.role === 'editor' ? 'ok' : 'muted'}">${esc(u.role)}</span></td>
      <td>${esc((u.projects || []).join('、') || '—')}</td>
      <td>${u.enabled ? '<span class="badge ok">启用</span>' : '<span class="badge err">禁用</span>'}</td>
      <td class="nowrap">
        <button class="btn sm" data-act="user-toggle" data-uid="${u.id}" data-enabled="${u.enabled ? '1' : '0'}">${u.enabled ? '禁用' : '启用'}</button>
        <button class="btn sm" data-act="user-pass" data-uid="${u.id}" data-name="${esc(u.username)}">重置密码</button>
        <button class="btn sm" data-act="user-kick" data-uid="${u.id}" data-name="${esc(u.username)}">踢下线</button>
      </td>
    </tr>`).join('');

  $('view').innerHTML = `
    ${pageHead('用户管理', '角色与项目授权')}
    <div class="card">
      <div class="card-head"><h3>新建用户</h3></div>
      <div class="row">
        <input id="nuName" placeholder="用户名" style="width:150px">
        <input id="nuPass" type="password" placeholder="密码" style="width:150px">
        <select id="nuRole">
          <option value="viewer">viewer（只读）</option>
          <option value="editor">editor（读写）</option>
          <option value="admin">admin（全部）</option>
        </select>
      </div>
      <div class="row" style="margin-top:10px">
        <span class="muted">项目：</span>${projChecks || '<span class="muted">无授权项目</span>'}
        <button class="btn primary" data-act="user-create">建号</button>
      </div>
      <div id="nuMsg" style="margin-top:8px"></div>
    </div>
    <div class="card pad0">
      <div class="card-head" style="padding:16px 16px 0"><h3>用户列表</h3><span class="muted">${users.length} 个</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th class="num">ID</th><th>用户名</th><th>角色</th><th>项目</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="6">${empty('暂无用户')}</td></tr>`}</tbody>
      </table></div>
    </div>`;
}

async function createUser() {
  const projects = els('[data-newproj]:checked').map(x => x.value);
  try {
    await api('/admin/users', {
      method: 'POST',
      body: {
        username: el('#nuName').value.trim(),
        password: el('#nuPass').value,
        role: el('#nuRole').value,
        projects,
      },
    });
    el('#nuMsg').innerHTML = '<span class="ok">已创建</span>';
    toast('用户已创建', 'ok');
    await pageUsers();
  } catch (e) {
    el('#nuMsg').innerHTML = `<span class="err">失败：${esc(e.message)}</span>`;
    toast('创建失败：' + e.message, 'err');
  }
}

async function pageAudit() {
  setTitle('审计');
  if (!isAdmin()) { $('view').innerHTML = empty('需要 admin 权限'); return; }
  $('view').innerHTML = loading();
  const d = await api('/admin/audit?limit=100');
  const rows = (d.items || []).map(r => `<tr>
      <td class="nowrap">${esc(fmtTime(r.ts))}</td>
      <td>${esc(r.username || '—')}</td>
      <td><span class="badge muted">${esc(r.action || '')}</span></td>
      <td>${esc(r.project || '—')}</td>
      <td>${esc(r.target || '—')}</td>
      <td class="mono">${esc(r.ip || '—')}</td>
      <td class="muted" title="${esc(r.detail || '')}">${esc(String(r.detail || '').slice(0, 80))}</td>
    </tr>`).join('');

  $('view').innerHTML = `
    ${pageHead('审计', '最近 100 条写操作记录')}
    <div class="card pad0">
      <div class="table-wrap"><table>
        <thead><tr><th>时间</th><th>用户</th><th>动作</th><th>项目</th><th>对象</th><th>IP</th><th>详情</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="7">${empty('暂无审计记录')}</td></tr>`}</tbody>
      </table></div>
    </div>`;
}

/* ============================================================
 * 9. 全局事件
 * ============================================================ */
document.addEventListener('click', async e => {
  const closer = e.target.closest('[data-close-modal]');
  if (closer) { closeModal(); return; }
  const node = e.target.closest('[data-act]');
  if (!node) return;
  const act = node.dataset.act;
  try {
    if (act === 'reload') { await route(); return; }
    if (act === 'act-view') {
      const name = parseHash().parts[1];
      location.hash = `#/project/${enc(name)}/activities?view=${node.dataset.view}`;
      return;
    }
    if (act === 'open-split') { await openSplitModal(); return; }
    if (act === 'split-create') {
      const name = node.dataset.project;
      node.disabled = true;
      const r = await api(`/projects/${enc(name)}/activities/split`, {
        method: 'POST', body: { dry_run: false },
      });
      closeModal();
      toast(`已创建 ${(r.created || []).length} 个目录`, 'ok');
      await route();
      return;
    }
    if (act === 'enter-dir') {
      e.preventDefault();
      await gotoSub(node.dataset.path);
      return;
    }
    if (act === 'go-root') {
      e.preventDefault();
      if (actCtx) location.hash = `#/project/${enc(actCtx.projectName)}/activity/${enc(actCtx.dirname)}`;
      return;
    }
    if (act === 'go-sub') {
      e.preventDefault();
      await gotoSub(node.dataset.sub);
      return;
    }
    if (act === 'up-dir') {
      e.preventDefault();
      if (!actCtx) return;
      const parts = actCtx.sub.split('/').filter(Boolean);
      parts.pop();
      if (parts.length) await gotoSub(parts.join('/'));
      else location.hash = `#/project/${enc(actCtx.projectName)}/activity/${enc(actCtx.dirname)}`;
      return;
    }
    if (act === 'download') { e.preventDefault(); await downloadActivityFile(node.dataset.path); return; }
    if (act === 'del-file') { await deleteActivityFile(node.dataset.path); return; }
    if (act === 'do-check') { await recheckActivity(); return; }
    if (act === 'refresh-act') { await refreshActivity(); return; }
    if (act === 'sync-feishu') {
      if (!actCtx) return;
      if (!isAdmin()) { toast('需 admin 权限', 'warn'); return; }
      const r = await api('/sync/feishu', { method: 'POST', body: { project: actCtx.projectName } });
      toast(r.task || '飞书同步已在后台开始', 'ok');
      return;
    }
    if (act === 'open-fill') { await openFillModal(); return; }
    if (act === 'act-upload') { await openActivityUploadModal(); return; }
    if (act === 'act-upload-run') { await uploadActivityFile(); return; }
    if (act === 'fill-submit') { await submitFill(); return; }
    if (act === 'close-refresh') { closeModal(); await refreshActivity(); return; }
    if (act === 'open-llm') { await openLlmModal(); return; }
    if (act === 'llm-run') { await runLlm(); return; }
    if (act === 'llm-apply') { await applyLlm(); return; }
    if (act === 'tpl-tab') {
      location.hash = node.dataset.tab === 'generate' ? '#/templates?tab=generate' : '#/templates';
      return;
    }
    if (act === 'tpl-toggle') {
      const name = node.dataset.name;
      if (tplExpanded.has(name)) tplExpanded.delete(name); else tplExpanded.add(name);
      await pageTemplates(parseHash().params);
      return;
    }
    if (act === 'gen-run') { await runGenerate(); return; }
    if (act === 'sync-run') { await startFeishuSync(); return; }
    if (act === 'index-run') { await startCogneeIndex(); return; }
    if (act === 'ds-refresh') { await pageSync(); return; }
    if (act === 'search-tab') {
      location.hash = node.dataset.tab === 'docs' ? '#/search?tab=docs' : '#/search';
      return;
    }
    if (act === 'search-run') { await runSearch(); return; }
    if (act === 'docs-filter') { await loadDocsPane(); return; }
    if (act === 'upload-run') { await uploadDoc(); return; }
    if (act === 'doc-del') { await deleteDoc(node.dataset.token, node.dataset.name); return; }
    if (act === 'doc-source') { await downloadDocSource(); return; }
    if (act === 'doc-edit') {
      el('#docEditor').value = actCtx.docContent;
      el('#docView').classList.add('hidden');
      el('#docEditor').classList.remove('hidden');
      el('#docEditBar').classList.remove('hidden');
      return;
    }
    if (act === 'doc-cancel') {
      el('#docEditor').classList.add('hidden');
      el('#docView').classList.remove('hidden');
      el('#docEditBar').classList.add('hidden');
      return;
    }
    if (act === 'doc-save') {
      const r = await api('/docs/' + enc(actCtx.docToken), {
        method: 'PATCH', body: { content: el('#docEditor').value },
      });
      toast(r.reconcile && r.reconcile.warning ? '已保存；索引重建：' + r.reconcile.warning : '已保存', 'ok');
      await pageDoc(actCtx.docToken);
      return;
    }
    if (act === 'doc-delete') {
      if (!confirm('确认删除（移入回收站）？')) return;
      await api('/docs/' + enc(actCtx.docToken), { method: 'DELETE' });
      toast('已删除', 'ok');
      location.hash = '#/search?tab=docs';
      return;
    }
    if (act === 'user-create') { await createUser(); return; }
    if (act === 'user-toggle') {
      const uid = node.dataset.uid;
      await api('/admin/users/' + uid, {
        method: 'PATCH', body: { enabled: node.dataset.enabled !== '1' },
      });
      toast('状态已更新', 'ok');
      await pageUsers();
      return;
    }
    if (act === 'user-pass') {
      const pw = prompt(`给「${node.dataset.name}」设置新密码：`);
      if (!pw) return;
      await api('/admin/users/' + node.dataset.uid, { method: 'PATCH', body: { password: pw } });
      toast('密码已重置，该账号旧会话已失效', 'ok');
      return;
    }
    if (act === 'user-kick') {
      if (!confirm(`踢掉「${node.dataset.name}」的全部会话？`)) return;
      const r = await api(`/admin/users/${node.dataset.uid}/sessions`, { method: 'DELETE' });
      toast(`已踢下线（${r.removed || 0} 个会话）`, 'ok');
      return;
    }
  } catch (err) {
    toast(err.message || '操作失败', 'err');
  }
});

async function downloadDocSource() {
  if (!actCtx || !actCtx.docToken) return;
  const r = await fetch(`${API}/docs/${enc(actCtx.docToken)}/source`, {
    headers: token ? { 'x-token': token } : {},
  });
  if (!r.ok) {
    let msg = r.status + '';
    try { const d = await r.json(); msg = d.detail || msg; } catch { /* ignore */ }
    toast('下载失败：' + msg, 'err');
    return;
  }
  const blob = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = actCtx.docName || 'source';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 800);
}

/* 回车 / 覆盖层关闭 */
$('modal').addEventListener('click', e => {
  if (e.target === $('modal')) closeModal();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !$('modal').classList.contains('hidden')) closeModal();
});

/* 登录 */
$('loginForm').addEventListener('submit', async e => {
  e.preventDefault();
  $('loginErr').textContent = '';
  const btn = $('btnLogin');
  btn.disabled = true;
  try {
    const d = await api('/login', {
      method: 'POST',
      body: { username: $('loginUser').value.trim(), password: $('loginPass').value },
    });
    token = d.token;
    localStorage.setItem('sw_token', token);
    me = await api('/me');
    showApp();
    await renderSidebar();
    if (!location.hash || location.hash === '#/') location.hash = '#/dashboard';
    await route();
  } catch (err) {
    $('loginErr').textContent = err.message || '登录失败';
  } finally {
    btn.disabled = false;
  }
});

$('btnLogout').addEventListener('click', async () => {
  try { await api('/logout', { method: 'POST' }); } catch { /* 会话可能已失效 */ }
  logoutLocal();
  history.replaceState(null, '', location.pathname + location.search);
});

$('btnToggleSidebar').addEventListener('click', () => {
  $('appShell').classList.toggle('collapsed');
});

window.addEventListener('hashchange', () => { route(); });

/* 启动 */
(async function boot() {
  if (!token) { showLogin(); return; }
  try {
    me = await api('/me');
    showApp();
    await renderSidebar();
    if (!location.hash) history.replaceState(null, '', '#/dashboard');
    await route();
  } catch {
    logoutLocal();
  }
})();

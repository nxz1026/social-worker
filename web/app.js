'use strict';

/* ============================================================
 * 社工管理界面 · 原生 JS 单页应用
 * 路由：#/dashboard #/project/<名> #/project/<名>/activities
 *       #/project/<名>/activity/<目录> #/templates #/sync
 *       #/search #/docs #/doc/<token> #/admin/users #/admin/audit
 * ============================================================ */

const API = '/sw-api/api';
const CATS = ['发票票据', '项目计划', '项目执行', '日常工作佐证', '政策文件', '其他'];
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
let usersCache = null;      // 用户管理页当前列表（编辑弹窗用）
let keysCache = {};         // API Key 明文缓存：{keyId: secret}，只在当前页面内存里
let routeSeq = 0;
let actCtx = null;          // 当前活动页上下文（文件操作 / 弹窗用）
let previewUrl = '';        // 预览弹窗当前占用的 blob URL（关闭时 revoke）

/* ---------------- 基础工具 ---------------- */
const $ = id => document.getElementById(id);
const el = (sel, root = document) => root.querySelector(sel);
const els = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const enc = encodeURIComponent;

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
  const myProjects = Object.keys(me.projects || {});
  $('me').title = `${me.username} · ${me.role} · ${myProjects.join(' / ')}`;
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

/* 侧边栏项目折叠状态：默认收起，用户点开的记在 localStorage。
 * 当前所在项目由 renderSidebar 强制展开（不进这个集合也能看到子项）。*/
const NAV_FOLD_KEY = 'sw_nav_fold';
function navFoldOpen(name) {
  try {
    const raw = JSON.parse(localStorage.getItem(NAV_FOLD_KEY) || '{}');
    return !!raw[name];
  } catch { return false; }
}
function navFoldToggle(name) {
  try {
    const raw = JSON.parse(localStorage.getItem(NAV_FOLD_KEY) || '{}');
    raw[name] = !raw[name];
    localStorage.setItem(NAV_FOLD_KEY, JSON.stringify(raw));
  } catch { /* 存不了就只在本次会话生效 */ }
}

async function renderSidebar() {
  let projects = [];
  try { projects = await ensureProjects(); } catch (e) { toast('项目列表加载失败：' + e.message, 'err'); }
  // 侧边栏两级折叠（2026-11-23 用户要求）：
  //   一级：仪表盘 / 项目 / 模板 / 检索 / 管理 都可折叠（项目按社区再分一层）
  //   二级：项目 → 社区 → 具体项目 → 概览/活动列表
  // 以前只有 3 个项目还看得过来，以后项目多了必须分层。
  const cur = parseHash().parts;
  const curProject = cur[0] === 'project' ? cur[1] : '';
  const curCommunity = curProject
    ? (projects.find(p => p.name === curProject) || {}).community || ''
    : '';

  // 按社区分组
  const byCommunity = new Map();
  for (const p of projects) {
    const c = String(p.community || '未分组');
    if (!byCommunity.has(c)) byCommunity.set(c, []);
    byCommunity.get(c).push(p);
  }

  const projItem = (p) => {
    const name = p.name;
    const open = name === curProject || navFoldOpen(name);
    return `<div class="nav-project${open ? ' open' : ''}" data-project-wrap="${esc(name)}">
      <a class="nav-project-head" data-project="${esc(name)}" href="#/project/${enc(name)}" title="${esc(projectShort(p))}">
        <span class="nav-fold" data-act="nav-fold" data-fold="${esc(name)}" title="${open ? '收起' : '展开'}">${open ? '▾' : '▸'}</span>
        <span class="dot"></span><span class="nav-name">${esc(projectShort(p))}</span>
      </a>
      <div class="nav-sub">
        <a class="nav-link" data-nav="project:${esc(name)}" href="#/project/${enc(name)}"><span>概览</span></a>
        <a class="nav-link" data-nav="activities:${esc(name)}" href="#/project/${enc(name)}/activities"><span>活动列表</span></a>
      </div>
    </div>`;
  };

  const communityBlock = ([cname, list]) => {
    const open = cname === curCommunity || navFoldOpen('社区:' + cname);
    return `<div class="nav-community${open ? ' open' : ''}">
      <div class="nav-community-head" data-act="nav-fold" data-fold="社区:${esc(cname)}"
           title="${open ? '收起' : '展开'}">
        <span class="nav-fold-ico">${open ? '▾' : '▸'}</span>
        <span>${esc(cname)}</span>
        <span class="nav-count">${list.length}</span>
      </div>
      <div class="nav-community-body">${list.map(projItem).join('')}</div>
    </div>`;
  };

  const projectNav = projects.length
    ? Array.from(byCommunity.entries()).map(communityBlock).join('')
    : '<div class="nav-link"><span class="muted">暂无项目</span></div>';

  // 一级分组：可折叠的「项目 / 检索 / 管理」
  // 当前所在路径对应的组强制展开（进了项目页就展开「项目」组），
  // 折叠记录只管用户手动收起的其他组。
  const curGroup = cur[0] === 'project' ? 'projects'
    : cur[0] === 'search' || cur[0] === 'doc' || cur[0] === 'docs' ? 'search'
    : cur[0] === 'admin' ? 'admin' : '';
  const group = (key, iconName, label, inner, href) => {
    const open = key === curGroup || navFoldOpen('组:' + key);
    const head = href
      ? `<a class="nav-group-title nav-group-toggle" href="${href}">
           <span class="nav-fold" data-act="nav-fold" data-fold="组:${key}">${open ? '▾' : '▸'}</span>
           ${icon(iconName)}<span>${esc(label)}</span></a>`
      : `<div class="nav-group-title nav-group-toggle" data-act="nav-fold" data-fold="组:${key}">
           <span class="nav-fold">${open ? '▾' : '▸'}</span>${icon(iconName)}<span>${esc(label)}</span>
         </div>`;
    return `<div class="nav-group${open ? ' open' : ''}" data-group-wrap="${key}">
      ${head}<div class="nav-group-body">${inner}</div>
    </div>`;
  };

  $('sidebarNav').innerHTML = `
    <a class="nav-link" data-nav="dashboard" href="#/dashboard">${icon('dashboard')}<span>仪表盘</span></a>
    ${group('projects', 'project', '项目', projectNav)}
    <div class="nav-group">
      <a class="nav-link" data-nav="templates" href="#/templates">${icon('template')}<span>模板</span></a>
      <a class="nav-link" data-nav="sync" href="#/sync">${icon('sync')}<span>同步</span></a>
    </div>
    ${group('search', 'search', '检索',
      `<a class="nav-link" data-nav="search" href="#/search"><span>知识检索</span></a>
       <a class="nav-link" data-nav="docs" href="#/docs"><span>文档库</span></a>`,
      '#/search')}
    ${group('admin', 'users', '管理',
      `<a class="nav-link" data-nav="users" href="#/admin/users"><span>用户</span></a>
       <a class="nav-link" data-nav="audit" href="#/admin/audit"><span>审计</span></a>`,
      '#/admin/users')}`;
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
  // 当前项目自动展开：renderSidebar 在 route() 之前调用，那时 hash 还没变成
  // 项目路径，展开判断拿不到当前项目。route() 每次会调 updateActiveNav，
  // 在这里补一次（2026-11-23 实测：进项目页后侧边栏仍是收起的）。
  if (currentProject) {
    els('#sidebarNav .nav-project').forEach(w => {
      const name = w.dataset.projectWrap;
      const open = name === currentProject || navFoldOpen(name);
      w.classList.toggle('open', open);
      const fold = w.querySelector('.nav-fold');
      if (fold) { fold.textContent = open ? '▾' : '▸'; fold.title = open ? '收起' : '展开'; }
    });
  }
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
  // 预览用的 blob URL 必须释放，否则每预览一次就在内存里漏一份文件
  if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = ''; }
}

/* ---------------- 通用片段 ---------------- */
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
  const [proj, acts, projCheck] = await Promise.all([
    api('/projects/' + enc(name)),
    api(`/projects/${enc(name)}/activities`),
    // 项目级核对失败不该拖垮整个项目页
    api(`/projects/${enc(name)}/check`).catch(() => null),
  ]);
  const d = proj.data || {};
  const p = d.project || {};
  const parties = d.parties || {};
  const staff = d.staff || {};
  const budget = d.budget || {};
  // period 在 project 下（项目 yaml 的 project.period.start/end），
  // 不是顶层 d.period —— 原来读错层级，项目周期恒显示「—」（E2E-3 发现）
  const period = p.period || d.period || {};
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
      <button class="btn" data-act="open-projfile" data-project="${esc(name)}"
        ${canWrite() ? '' : 'disabled title="需 editor/admin"'}>上传项目资料</button>
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
        ${kv('项目编号规则', `<span class="mono">${esc(projectCodeHint(p))}</span>`)}
      </div>
    </div>
    <div class="card checklist pad0">
      <div class="card-head" style="padding:14px 14px 0"><h3>项目级核对</h3>
        <span class="muted">19 项 · 依据项目管理目录</span></div>
      <div>${projCheck ? renderChecklistHtml(projCheck) : empty('项目级核对不可用')}</div>
      <div class="check-sum">${projCheck ? checkSummary(projCheck) : ''}</div>
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
  // 保留同一活动内已生成的 LLM 结果：整对象重建会把 actCtx.llm 冲掉，
  // 于是「生成」完再点「应用并落盘」会提示「请先生成内容」（2026-11-23 实测）。
  const keepLlm = (actCtx && actCtx.projectName === name
                   && actCtx.dirname === dirname) ? actCtx.llm : null;
  const keepSel = (actCtx && actCtx.projectName === name
                   && actCtx.dirname === dirname) ? actCtx.selected : 0;
  actCtx = { projectName: name, dirname, sub, detail, entries, project: proj,
             selected: keepSel || 0 };
  if (keepLlm) actCtx.llm = keepLlm;

  const check = detail.check || { items: [], ok_count: 0, partial_count: 0, missing_count: 0, na_count: 0, complete: false };
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
        <button class="btn" data-act="open-invoice" ${canWrite() ? '' : 'disabled title="需 editor/admin"'}>报销校验</button>
        <button class="btn" data-act="open-caption" ${canWrite() ? '' : 'disabled title="需 editor/admin"'}>生成图注</button>
        <button class="btn" data-act="open-sat" ${canWrite() ? '' : 'disabled title="需 editor/admin"'}>满意度统计</button>
        <button class="btn primary" data-act="open-fill" ${canWrite() ? '' : 'disabled title="需 editor/admin"'}>生成材料</button>
        <button class="btn" data-act="open-photo-upload" ${canWrite() ? '' : 'disabled title="需 editor/admin"'}>上传照片</button>
        <button class="btn" data-act="act-upload" ${canWrite() ? '' : 'disabled title="需 editor/admin"'}>上传文件</button>
        <button class="btn" data-act="open-tidy" ${canWrite() ? '' : 'disabled title="需 editor/admin"'}>自动整理</button>
        <button class="btn" data-act="do-check">核对</button>
        <button class="btn" data-act="open-trash">回收站</button>
        <button class="btn" data-act="sync-feishu" ${isAdmin() ? '' : 'disabled title="需 admin"'}>同步飞书</button>
      </div>
    </div>
    <div class="act-layout">
      <div class="card checklist pad0" id="checkCard">
        <div class="card-head" style="padding:14px 14px 0"><h3>材料清单</h3>
          <span class="muted">15 项 · ✓ 已齐 ◐ 待补 ✗ 缺 — 不适用</span></div>
        <div id="checkList">${renderChecklistHtml(check)}</div>
        <div class="check-sum" id="checkSum">${checkSummary(check)}</div>
        <div class="check-sum" id="reviewSum">${reviewSummary(check)}</div>
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

  // 点清单项后按前缀过滤文件（按钮点击不触发，避免误切换过滤）
  els('#checkList .check-item').forEach(node => {
    node.addEventListener('click', ev => {
      if (ev.target.closest('button')) return;
      const no = Number(node.dataset.no);
      actCtx.selected = actCtx.selected === no ? 0 : no;
      els('#checkList .check-item').forEach(x => x.classList.toggle('active', Number(x.dataset.no) === actCtx.selected));
      renderFilesPanel();
    });
  });
}

function renderChecklistHtml(check) {
  const marks = { ok: '✓', partial: '◐', missing: '✗', na: '—' };
  const labels = { ok: '已齐', partial: '待补（有未签字文档，缺签字扫描件）', missing: '缺', na: '不适用' };
  const write = canWrite();
  return (check.items || []).map(it => {
    const reviewed = !!it.review;
    const review = reviewed
      ? `<span class="badge ${it.signed ? 'ok' : 'muted'}">${reviewText(it)}</span>`
      : '<span class="badge muted">未核对</span>';
    // 已签字确认但文件还没上传：状态已归位为齐备，这里提醒补扫描件
    const pend = it.pending_upload
      ? ' <span class="badge warn" title="已人工签字确认，文件待上传">待补文件</span>'
      : '';
    const ops = write ? `<span class="ops">
        <button class="btn sm" data-act="rev-review" data-no="${it.no}">${reviewed ? '改核对' : '核对'}</button>
        <button class="btn sm" data-act="rev-sign" data-no="${it.no}" ${it.signed ? 'disabled' : ''}>签字</button>
        ${reviewed ? `<button class="btn sm danger" data-act="rev-clear" data-no="${it.no}">撤销</button>` : ''}
      </span>` : '';
    return `<div class="check-item ${reviewed ? 'reviewed' : ''}" data-no="${it.no}">
    <span class="no">${it.no}</span>
    <span class="mark ${esc(it.status)}" title="${esc(labels[it.status] || '')}">${marks[it.status] || '—'}</span>
    <span class="body">
      <span class="name">${esc(it.name)}</span>
      <span class="detail" title="${esc(it.detail || '')}">${esc(it.detail || '')}</span>
      <span class="review">${review}${pend}${it.review && it.review.reviewed_note ? ` <span class="muted">${esc(it.review.reviewed_note)}</span>` : ''}</span>
    </span>
    ${ops}
  </div>`;
  }).join('');
}

/* 活动目录内的图片/PDF，给「签字扫描件」下拉用；拿不到就留空 */
async function collectActivityScans(base, rel = '', depth = 0, acc = []) {
  const entries = await api(base + '/files' + (rel ? '?sub=' + enc(rel) : ''));
  for (const e of entries) {
    const path = rel ? `${rel}/${e.name}` : e.name;
    if (e.is_dir) {
      if (depth < 2) await collectActivityScans(base, path, depth + 1, acc);
    } else if (/\.(jpe?g|png|gif|bmp|webp|pdf)$/i.test(e.name)) {
      acc.push(path);
    }
  }
  return acc;
}

/* item_no → 材料名，弹窗标题用 */
function checkItemName(check, no) {
  const it = (check.items || []).find(x => Number(x.no) === Number(no));
  return it ? `${it.no} · ${it.name}` : `第 ${no} 项`;
}

async function openReviewModal(no, mode) {
  if (!actCtx) return;
  if (!canWrite()) { toast('需 editor/admin 权限', 'warn'); return; }
  const { projectName, dirname } = actCtx;
  const base = `/projects/${enc(projectName)}/activities/${enc(dirname)}`;
  const name = checkItemName(actCtx.detail.check, no);
  const it = (actCtx.detail.check.items || []).find(x => Number(x.no) === Number(no)) || {};
  let scans = [];
  try { scans = await collectActivityScans(base); } catch (e) { /* ignore */ }
  const scanOpts = ['<option value="">不上传扫描件（人工确认，扫描件后补）</option>']
    .concat(scans.map(p => `<option value="${esc(p)}"${it.scan === p ? ' selected' : ''}>${esc(p)}</option>`))
    .join('');
  openModal(`${mode === 'sign' ? '签字确认' : '核对确认'}：${esc(name)}`, `
    <div class="form-grid">
      <label class="field full"><span>备注（可选）</span>
        <textarea id="revNote" placeholder="例如：金额与人数已核对">${esc(it.review && it.review.reviewed_note || '')}</textarea></label>
      <label class="field full"><span>扫描件（可选，仅图片 / PDF）</span>
        <select id="revScan">${scanOpts}</select></label>
    </div>
    <div class="hint" style="margin-top:8px">
      签字不强制扫描件：现场点击确认即记为已签字，扫描件可后补。
    </div>
    <div id="revMsg" style="margin-top:10px"></div>
  `, `<button class="btn" data-close-modal>取消</button>
     <button class="btn primary" data-act="rev-submit"
             data-no="${no}" data-mode="${mode}">确认</button>`);
}

async function submitReview(node) {
  if (!actCtx) return;
  const no = node.dataset.no;
  const mode = node.dataset.mode;
  const body = {
    item: Number(no),
    note: el('#revNote').value.trim(),
    signed: mode === 'sign',
    scan: el('#revScan').value,
  };
  node.disabled = true;
  el('#revMsg').innerHTML = '<span class="muted">提交中…</span>';
  try {
    await api(`/projects/${enc(actCtx.projectName)}/activities/${enc(actCtx.dirname)}/review`, {
      method: 'POST', body,
    });
    closeModal();
    toast(mode === 'sign' ? `已标记第 ${no} 项为已签字` : `已核对第 ${no} 项`, 'ok');
    await recheckActivity();
  } catch (e) {
    el('#revMsg').innerHTML = `<div class="notice">保存失败：${esc(e.message)}</div>`;
    node.disabled = false;
  }
}

async function clearReview(no) {
  if (!actCtx) return;
  if (!confirm(`撤销第 ${no} 项的核对记录？`)) return;
  try {
    await api(`/projects/${enc(actCtx.projectName)}/activities/${enc(actCtx.dirname)}/review`, {
      method: 'POST', body: { item: Number(no), clear: true },
    });
    toast(`已撤销第 ${no} 项`, 'ok');
    await recheckActivity();
  } catch (e) {
    toast('撤销失败：' + e.message, 'err');
  }
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
  // md 是给 Obsidian 客户端与关系图谱用的骨架，和 office 文件摆在一起太乱。
  // 后端在每个 office 文件上带 mirror=<该文件对应的 md 名>，据此把
  // 被引用的 md 隐藏（2026-11-23 用户要求：文档重复就忽略 md，不显示）。
  // 只隐藏有对应 office 文件的 md；纯 md（无同名 docx/xlsx）照常显示。
  const mirrored = new Set();
  for (const e of list) {
    if (e && !e.is_dir && e.mirror) mirrored.add(String(e.mirror));
  }
  // 活动元数据与索引文件不是给用户看的：activity.yaml 是内部配置，
  // _X-资料索引.md 是生成的目录清单，列出来只会让人困惑。
  const HIDDEN_NAMES = new Set(['activity.yaml', '_photos.json', 'scores.json']);
  const visible = list.filter(e => {
    if (!e || e.is_dir) return true;
    const name = String(e.name);
    if (mirrored.has(name)) return false;              // 被 office 文件引用的 md 骨架
    if (HIDDEN_NAMES.has(name)) return false;          // 内部元数据
    if (name.startsWith('_') && name.endsWith('.md')) return false;  // 生成的索引
    return true;
  });

  if (!visible.length) {
    return empty(selected && !sub ? '该项没有对应文件' : '目录为空');
  }
  return visible.map(item => {
    const path = sub ? `${sub}/${item.name}` : item.name;
    const isDir = !!item.is_dir;
    const nameHtml = isDir
      ? `<a href="#" data-act="enter-dir" data-path="${esc(path)}">${esc(item.name)}/</a>`
      : `<a href="#" data-act="preview" data-path="${esc(path)}">${esc(item.name)}</a>`;
    // Obsidian 骨架只做提示：Web 上不跳转，文件仍走「下载」原件
    const mirrorHtml = item.mirror
      ? ` <span class="badge muted" title="Obsidian 骨架：${esc(item.mirror)}">↗ md</span>`
      : '';
    return `<div class="file-row ${isDir ? 'dir' : ''}">
      <span class="ico">${isDir ? '▸' : '·'}</span>
      <span class="nm">${nameHtml}${mirrorHtml}</span>
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

/* ---------------- 文件预览 ---------------- */
// 按扩展名分流：图片/PDF 走 blob URL，md 轻量渲染，Office 只提示下载，压缩包直接下载
const PREVIEW_IMG = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp'];
const PREVIEW_ARCHIVE = ['zip', 'rar', '7z'];
// Office 类：服务端 LibreOffice 转 PDF 后预览（版式 100% 保真，前端零依赖）
const PREVIEW_OFFICE = ['docx', 'xlsx', 'doc', 'xls', 'ppt', 'pptx', 'odt', 'ods'];

function fileExt(path) {
  const base = String(path).split('/').pop() || '';
  const i = base.lastIndexOf('.');
  return i > 0 ? base.slice(i + 1).toLowerCase() : '';
}

/* 极简 markdown：标题 / 列表 / 代码块 / 段落。
 * 只求「能看」，不追完整语法；先转义再加标签，避免 XSS。 */
function renderSimpleMd(text) {
  const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let inCode = false;
  let inList = false;
  const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
  for (const raw of lines) {
    if (/^```/.test(raw)) {
      if (inCode) { out.push('</code></pre>'); inCode = false; }
      else { closeList(); out.push('<pre class="preview-code"><code>'); inCode = true; }
      continue;
    }
    if (inCode) { out.push(esc(raw)); continue; }
    if (!raw.trim()) { closeList(); out.push(''); continue; }
    const h = raw.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      closeList();
      out.push(`<h${h[1].length}>${inlineMd(h[2])}</h${h[1].length}>`);
      continue;
    }
    const li = raw.match(/^\s*(?:[-*+]|\d+\.)\s+(.*)$/);
    if (li) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push(`<li>${inlineMd(li[1])}</li>`);
      continue;
    }
    closeList();
    out.push(`<p>${inlineMd(raw)}</p>`);
  }
  if (inCode) out.push('</code></pre>');
  closeList();
  return `<div class="preview-md">${out.join('\n')}</div>`;
}

function inlineMd(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

// 弹窗底部按钮：下载 + 关闭（下载复用原有 downloadActivityFile）
function previewFoot(path) {
  return `<button class="btn primary" data-act="preview-download" data-path="${esc(path)}">下载</button>
    <button class="btn" data-close-modal>关闭</button>`;
}

// 找不到目录项时仍能显示文件信息（大小/时间留空）
function previewMeta(path) {
  const sub = actCtx ? actCtx.sub : '';
  const full = sub ? `${sub}/${path.split('/').pop()}` : path;
  const item = (actCtx && actCtx.entries || []).find(e => (sub ? `${sub}/${e.name}` : e.name) === full)
    || (actCtx && actCtx.entries || []).find(e => e.name === path.split('/').pop());
  return item
    ? `大小 ${esc(fmtBytes(item.size || 0))} · 修改于 ${esc(fmtTime(item.mtime))}`
    : '';
}

async function openPreview(path) {
  if (!actCtx) return;
  const name = String(path).split('/').pop() || path;
  const ext = fileExt(path);

  // 压缩包不预览：直接走原有下载逻辑
  if (PREVIEW_ARCHIVE.includes(ext)) {
    await downloadActivityFile(path);
    return;
  }

  // 切换文件时先释放上一个 blob URL
  if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = ''; }
  openModal('预览 · ' + name, loading(), previewFoot(path), true);
  const foot = previewFoot(path);

  const { projectName, dirname } = actCtx;
  const base = `/projects/${enc(projectName)}/activities/${enc(dirname)}`;
  // Office 走 preview（服务端转 PDF），其余走 download 拿原文件
  const isOffice = PREVIEW_OFFICE.includes(ext);
  const url = isOffice
    ? `${API}${base}/preview?path=${enc(path)}`
    : `${API}${base}/download?path=${enc(path)}`;
  if (isOffice) {
    $('modalBody').innerHTML = `<div class="preview-tip">
      <div class="preview-tip-title">${esc(name)}</div>
      <p class="muted">正在转换为 PDF 预览…</p>
      <p class="muted">${previewMeta(path)}</p>
    </div>`;
  }

  // md / txt 只需要文本，其他类型拿 blob 做对象 URL
  const wantText = ext === 'md' || ext === 'markdown' || ext === 'txt';
  try {
    const r = await fetch(url, { headers: token ? { 'x-token': token } : {} });
    if (!r.ok) {
      let msg = String(r.status);
      try { const d = await r.json(); msg = d.detail || msg; } catch { /* ignore */ }
      throw new Error(msg);
    }
    // 用户可能在 fetch 期间已关掉弹窗
    if ($('modal').classList.contains('hidden')) return;

    if (PREVIEW_IMG.includes(ext)) {
      const blob = await r.blob();
      previewUrl = URL.createObjectURL(blob);
      $('modalBody').innerHTML = `<div class="preview-img-wrap">
        <img src="${previewUrl}" alt="${esc(name)}"></div>`;
    } else if (ext === 'pdf' || isOffice) {
      // 服务端已把 docx/xlsx 转成 PDF，这里和原生 pdf 同一条渲染路径
      const blob = await r.blob();
      previewUrl = URL.createObjectURL(blob);
      const note = isOffice
        ? '<div class="preview-note">由 LibreOffice 转换预览，版式与原件一致；下载可拿原始文件</div>'
        : '';
      $('modalBody').innerHTML = note +
        `<iframe class="preview-frame" src="${previewUrl}" title="${esc(name)}"></iframe>`;
    } else if (wantText) {
      const text = await r.text();
      $('modalBody').innerHTML = ext === 'txt'
        ? `<pre class="preview-pre">${esc(text)}</pre>`
        : renderSimpleMd(text);
    } else {
      // 其他格式（doc/xls/ppt 等旧格式）一律提示下载
      $('modalBody').innerHTML = `<div class="preview-tip">
        <div class="preview-tip-title">${esc(name)}</div>
        <p>该格式暂不支持预览，请下载后打开。</p>
        <p class="muted">${previewMeta(path)}</p>
      </div>`;
    }
  } catch (e) {
    if ($('modal').classList.contains('hidden')) return;
    $('modalBody').innerHTML = `<div class="notice">预览失败：${esc(e.message)}</div>`;
  }
  $('modalFoot').innerHTML = foot;
}

async function deleteActivityFile(path) {
  if (!actCtx) return;
  const { projectName, dirname } = actCtx;
  if (!confirm(`删除「${path}」？\n会移到回收站，可在活动页「回收站」里恢复。`)) return;
  await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/files`, {
    method: 'DELETE', body: { path },
  });
  toast('已移入回收站', 'ok');
  await refreshActivity();
}

/* 回收站：列出与恢复。
 * 后端 /trash 与 /trash/restore 早已实现，但界面一直没入口 ——
 * 删除提示写着「可在磁盘恢复」，用户实际只能上服务器找（审计 PR-06 / E2E-3）。
 */
async function openTrash() {
  if (!actCtx) return;
  const { projectName, dirname } = actCtx;
  const base = `/projects/${enc(projectName)}/activities/${enc(dirname)}`;
  openModal('回收站', loading());
  let items = [];
  try {
    const res = await api(base + '/trash');
    items = res.items || [];
  } catch (e) {
    $('modalBody').innerHTML = errorState(e.message || '读取回收站失败');
    return;
  }
  const rows = items.map(it => `<tr>
      <td>${esc(it.deleted_at || '—')}</td>
      <td class="mono">${esc(it.original || '')}</td>
      <td>${fmtBytes(it.size || 0)}</td>
      <td>${canWrite() ? `<button class="btn sm" data-act="restore-file" data-trash="${esc(it.trash)}">恢复</button>` : '—'}</td>
    </tr>`).join('');
  $('modalBody').innerHTML = items.length
    ? `<table class="tbl"><thead><tr><th>删除时间</th><th>原路径</th><th>大小</th><th></th></tr></thead>
       <tbody>${rows}</tbody></table>
       <p class="muted" style="margin-top:8px">恢复后回到原位置；原位置已有同名文件会提示冲突。</p>`
    : empty('回收站是空的');
  $('modalFoot').innerHTML = '<button class="btn" data-close-modal>关闭</button>';
}

async function restoreFromTrash(trashPath) {
  if (!actCtx) return;
  const { projectName, dirname } = actCtx;
  const base = `/projects/${enc(projectName)}/activities/${enc(dirname)}`;
  try {
    await api(base + '/trash/restore', { method: 'POST', body: { trash: trashPath } });
    toast('已恢复', 'ok');
    await openTrash();          // 刷新列表
    await refreshActivity();
  } catch (e) {
    toast(e.message || '恢复失败', 'err');
  }
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
      node.addEventListener('click', ev => {
        if (ev.target.closest('button')) return;
        const no = Number(node.dataset.no);
        actCtx.selected = actCtx.selected === no ? 0 : no;
        els('#checkList .check-item').forEach(x => x.classList.toggle('active', Number(x.dataset.no) === actCtx.selected));
        renderFilesPanel();
      });
    });
  }
  const sum = el('#checkSum');
  if (sum) sum.innerHTML = checkSummary(check);
  const rsum = el('#reviewSum');
  if (rsum) rsum.innerHTML = reviewSummary(check);
  const partial = Number(check.partial_count || 0);
  const missing = Number(check.missing_count || 0);
  if (check.complete) {
    toast('核对完成：材料齐全', 'ok');
  } else if (!missing && partial) {
    toast(`核对完成：${partial} 项待补签字件`, 'warn');
  } else {
    toast(`核对完成：还缺 ${missing} 项`, 'warn');
  }
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

/* 项目级资料上传：不属于任何活动的文档（中期评估、管理制度、合同等）。
   服务端二元判断：命中 19 项清单就归到对应项，不命中落 其他/ 并给改名建议。 */
async function openProjectFileModal(project) {
  if (!canWrite()) { toast('需 editor/admin 权限', 'warn'); return; }
  openModal('上传项目资料', `
    <div class="form-grid">
      <label class="field"><span>项目</span>
        <input type="text" id="pfProject" value="${esc(project)}" readonly></label>
      <label class="field"><span>文件</span>
        <input type="file" id="pfFile" accept=".docx,.xlsx,.doc,.xls,.pdf,.zip,.7z">
      </label>
    </div>
    <div class="hint" style="margin-top:10px">
      命中文档清单（19 项）的文件自动归到对应项，落在 <code>报告/</code>；
      认不出的落在 <code>其他/</code>，返回规范的命名建议。
    </div>
    <div id="pfMsg" class="hint" style="margin-top:8px"></div>
    <div id="pfResult"></div>
  `, `<button class="btn" data-close-modal>取消</button>
      <button class="btn primary" data-act="pf-upload" id="pfUploadBtn">上传</button>`);
}

async function doProjectFileUpload() {
  const project = el('#pfProject')?.value || '';
  const input = el('#pfFile');
  const file = input?.files && input.files[0];
  const msg = el('#pfMsg');
  const btn = el('#pfUploadBtn');
  if (!file) { msg.textContent = '请先选择文件'; return; }
  btn.disabled = true;
  msg.textContent = '上传中…';
  try {
    const fd = new FormData();
    fd.append('project', project);
    fd.append('file', file);
    const r = await api('/sw/project-files/upload', { method: 'POST', body: fd });
    const m = r.matched;
    const sug = r.suggestion;
    el('#pfResult').innerHTML = `
      <div class="card" style="margin-top:10px">
        <div class="kv-row"><span class="k">已保存</span>
          <span class="v mono">${esc(r.stored)}</span></div>
        <div class="kv-row"><span class="k">大小</span>
          <span class="v">${(r.size / 1024 / 1024).toFixed(2)} MB</span></div>
        <div class="kv-row"><span class="k">归属</span>
          <span class="v">${m ? `清单第 ${m.item} 项：${esc(m.name)}` : '未命中清单，已放入 其他/'}</span></div>
        ${sug && sug.rename_to ? `
        <div class="kv-row"><span class="k">建议命名</span>
          <span class="v mono">${esc(sug.rename_to)}</span></div>
        <div class="hint">${esc(sug.hint || ('改名为上面这个，就能自动归到第 ' + (sug.item || '?') + ' 项'))}</div>` : ''}
      </div>`;
    msg.textContent = '';
  } catch (e) {
    msg.textContent = '上传失败：' + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function openPhotoUploadModal() {
  if (!actCtx) return;
  if (!canWrite()) { toast('需 editor/admin 权限', 'warn'); return; }
  const { projectName, dirname } = actCtx;
  actCtx.photo = {
    files: [], hashes: [], precheck: null, busy: false,
    project: projectName, activity: dirname,
  };
  openModal('上传活动照片', `
    <div class="form-grid">
      <label class="field"><span>项目 *</span><select id="photoProject"></select></label>
      <label class="field"><span>活动 *</span><select id="photoActivity"></select></label>
      <label class="field"><span>拍摄日期 *</span>
        <input type="date" id="photoDate" max="${todayIso()}" value="${todayIso()}"></label>
      <label class="field"><span>场景（可选）</span>
        <input type="text" id="photoScene" placeholder="主会场 / 分组走访"></label>
      <label class="field full"><span>照片（最多 200 张，单张 ≤50MB）</span>
        <div id="photoDrop" class="photo-drop">
          <input type="file" id="photoFiles" accept="image/*" multiple hidden>
          <div>拖拽照片到这里，或 <a href="#" data-act="photo-pick">选择文件</a></div>
          <div id="photoSelInfo" class="hint">已选 0 张 / 0 MB</div>
        </div>
      </label>
    </div>
    <label class="row" style="margin-top:10px;gap:6px">
      <input type="checkbox" id="photoAllowDup">
      <span class="hint">允许重复上传（跳过 24 小时同日期同场景批次检查）</span>
    </label>
    <div id="photoMsg" class="hint" style="margin-top:8px"></div>
    <div id="photoResult"></div>
  `, `<button class="btn" data-close-modal>取消</button>
      <button class="btn" data-act="photo-precheck">重新预检</button>
      <button class="btn primary" data-act="photo-upload" id="photoUploadBtn" disabled>上传</button>`);

  const selP = el('#photoProject');
  const projects = await ensureProjects().catch(() => []);
  selP.innerHTML = (projects || [])
    .map(p => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join('');
  selP.value = projectName;
  selP.addEventListener('change', () => loadPhotoActivities(selP.value, ''));

  const drop = el('#photoDrop');
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', e => {
    e.preventDefault();
    drop.classList.remove('over');
    setPhotoFiles(Array.from(e.dataTransfer.files || []));
  });
  el('#photoFiles').addEventListener('change', e => setPhotoFiles(Array.from(e.target.files || [])));

  await loadPhotoActivities(projectName, dirname);
}

async function loadPhotoActivities(project, preferred) {
  const sel = el('#photoActivity');
  if (!sel) return;
  sel.innerHTML = '<option value="">加载中…</option>';
  try {
    const d = await api('/projects/' + enc(project) + '/activities');
    const names = d.existing || [];
    sel.innerHTML = names.length
      ? names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('')
      : '<option value="">（该项目还没有活动目录）</option>';
    if (preferred && names.includes(preferred)) sel.value = preferred;
  } catch (e) {
    sel.innerHTML = '<option value="">加载失败</option>';
    toast('活动列表加载失败：' + e.message, 'warn');
  }
}

function setPhotoFiles(files) {
  const st = actCtx && actCtx.photo;
  if (!st) return;
  const images = files.filter(f => /\.(jpe?g|png|gif|bmp|webp)$/i.test(f.name));
  const ignored = files.length - images.length;
  st.files = images.slice(0, 200);
  st.hashes = [];
  st.precheck = null;
  const bytes = st.files.reduce((n, f) => n + f.size, 0);
  el('#photoSelInfo').textContent = `已选 ${st.files.length} 张 / ${fmtBytes(bytes)}`
    + (images.length > 200 ? '（超出 200 张，已截断）' : '')
    + (ignored ? `（忽略 ${ignored} 个非图片文件）` : '');
  el('#photoResult').innerHTML = '';
  el('#photoMsg').textContent = '';
  const btn = el('#photoUploadBtn');
  if (btn) btn.disabled = !st.files.length;
  if (st.files.length) runPhotoPrecheck();
}

// 浏览器端 SHA-256：Web Crypto 只收 ArrayBuffer，逐张算避免大文件卡死
async function fileSha256(file) {
  const buf = await file.arrayBuffer();
  const digest = await crypto.subtle.digest('SHA-256', buf);
  return Array.from(new Uint8Array(digest))
    .map(b => b.toString(16).padStart(2, '0')).join('');
}

async function runPhotoPrecheck() {
  const st = actCtx && actCtx.photo;
  if (!st || !st.files.length) { toast('先选择照片', 'warn'); return; }
  const project = el('#photoProject').value;
  const activity = el('#photoActivity').value;
  const date = el('#photoDate').value;
  const scene = el('#photoScene').value.trim();
  if (!project || !activity) { toast('项目和活动必填', 'warn'); return; }
  if (!date) { toast('拍摄日期必填', 'warn'); return; }
  if (date > todayIso()) { toast('拍摄日期不能晚于今天', 'warn'); return; }

  st.project = project;
  st.activity = activity;
  const msg = el('#photoMsg');
  const btn = el('#photoUploadBtn');
  st.busy = true;
  if (btn) btn.disabled = true;

  try {
    if (!(window.crypto && crypto.subtle)) {
      st.precheck = { new: st.files.length, duplicated: [], duplicate_batch: null };
      msg.textContent = '当前环境不支持浏览器哈希，跳过预检；上传时由服务端按 sha256 去重。';
      if (btn) btn.disabled = false;
      return;
    }
    st.hashes = [];
    for (let i = 0; i < st.files.length; i++) {
      st.hashes.push(await fileSha256(st.files[i]));
      // 每 3 张让出一次主线程，进度条才动得起来
      if (i % 3 === 0 || i === st.files.length - 1) {
        msg.textContent = `计算哈希 ${i + 1}/${st.files.length}…`;
        await new Promise(r => setTimeout(r, 0));
      }
    }
    msg.textContent = '预检中…';
    const r = await api('/sw/photos/precheck', { method: 'POST', body: {
      project, activity, hashes: st.hashes, shot_date: date, scene,
    } });
    st.precheck = r;
    const dup = new Set(r.duplicated || []);
    const dupNames = st.files.filter((f, i) => dup.has(st.hashes[i])).map(f => f.name);
    let text = `新增 ${r.new} 张 / 重复 ${(r.duplicated || []).length} 张`;
    if (dupNames.length) {
      text += `（${dupNames.slice(0, 3).join('、')}${dupNames.length > 3 ? ' 等' : ''}）`;
    }
    if (r.duplicate_batch) {
      text += `；24 小时内已传过同日期同场景批次 ${r.duplicate_batch}，需勾选「允许重复上传」`;
    }
    msg.textContent = text;
    if (btn) btn.disabled = false;
  } catch (e) {
    msg.innerHTML = `<span class="err">预检失败：${esc(e.message)}</span>`;
    toast('预检失败：' + e.message, 'err');
    if (btn) btn.disabled = !st.files.length;
  } finally {
    st.busy = false;
  }
}

async function uploadPhotos() {
  const st = actCtx && actCtx.photo;
  if (!st || !st.files.length) { toast('先选择照片', 'warn'); return; }
  if (st.busy) { toast('正在预检，请稍候', 'warn'); return; }
  const project = el('#photoProject').value;
  const activity = el('#photoActivity').value;
  const date = el('#photoDate').value;
  const scene = el('#photoScene').value.trim();
  const allowDup = el('#photoAllowDup').checked;
  if (!date) { toast('拍摄日期必填', 'warn'); return; }
  if (date > todayIso()) { toast('拍摄日期不能晚于今天', 'warn'); return; }
  if (st.precheck && st.precheck.duplicate_batch && !allowDup) {
    toast('该批次 24 小时内已上传过；确认重复请勾选「允许重复上传」', 'warn');
    return;
  }

  const fd = new FormData();
  fd.append('project', project);
  fd.append('activity', activity);
  fd.append('shot_date', date);
  fd.append('scene', scene);
  fd.append('note', '');
  fd.append('allow_duplicate', allowDup ? 'true' : 'false');
  fd.append('compress', 'true');
  st.files.forEach(f => fd.append('files', f, f.name));

  el('#photoMsg').textContent = `上传中（${st.files.length} 张）…`;
  el('#photoUploadBtn').disabled = true;
  try {
    const r = await api('/sw/photos/upload', { method: 'POST', body: fd });
    renderPhotoReceipt(r);
    toast(`已上传 ${r.saved} 张，跳过 ${(r.skipped || []).length} 张`,
      r.saved ? 'ok' : 'warn');
  } catch (e) {
    el('#photoMsg').innerHTML = `<span class="err">上传失败：${esc(e.message)}</span>`;
    toast('上传失败：' + e.message, 'err');
    el('#photoUploadBtn').disabled = false;
  }
}

function renderPhotoReceipt(r) {
  const reasons = { duplicate: '内容重复', exists: '同名已存在', invalid: '无法识别' };
  const skipped = (r.skipped || []).map(s =>
    `<li>${esc(s.original)} — ${esc(reasons[s.reason] || s.reason)}`
    + `${s.existing ? `（已有 ${esc(s.existing)}）` : ''}</li>`).join('');
  el('#photoResult').innerHTML = `
    <div class="notice green" style="margin-top:10px">
      批次 ${esc(r.batch_id)}：新增 ${r.saved} 张，跳过 ${(r.skipped || []).length} 张
      ${r.total_bytes ? `，共 ${fmtBytes(r.total_bytes)}` : ''}
    </div>
    ${skipped ? `<div class="section-title">跳过明细</div><ul class="hint">${skipped}</ul>` : ''}`;
  el('#photoMsg').textContent = '上传完成，点「完成」关闭并刷新。';
  $('modalFoot').innerHTML = '<button class="btn primary" data-act="photo-done">完成</button>';
}

/* ---------------- 生成材料弹窗 ---------------- */
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
    <label class="field" style="flex-direction:row;align-items:center;gap:8px;margin:4px 0 10px">
      <input type="checkbox" id="fillUseLlm" style="width:auto;margin:0">
      <span style="margin:0" id="fillLlmLabel">使用 LLM 生成内容</span>
    </label>
    <div id="fillLlmHint" class="hint" style="margin:-6px 0 10px"></div>
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
    // 这些模板的内容段只能由 LLM 产出，不勾就是一片空白 → 强制勾选并置灰。
    // 必须与 LLM_TASK_BY_TPL 的 key 完全一致：少了哪个，那个模板就会被静默降级成普通 fill。
    const REQUIRED_LLM = ['计划书', '简报', '总结报告', '监督记录'];
    const box = el('#fillUseLlm');
    if (box) {
      const need = REQUIRED_LLM.includes(t.name);
      // 切模板时按模板重置，避免把上一个模板的勾选状态带过来
      box.checked = need;
      box.disabled = need;
      el('#fillLlmLabel').textContent = need
        ? '使用 LLM 生成内容（该模板必须用 LLM，不可取消）'
        : '使用 LLM 生成内容（可选：先用知识库+项目资料冷化，再填模板）';
      el('#fillLlmHint').textContent = need
        ? '该模板的背景、目的、目标、总结等段落没有现成数据，只能由 LLM 依据项目书与知识库生成。'
        : '不勾选则只用系统已有数据直接填充。';
    }
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

/* 模板名 → LLM 任务名。只有这四类模板有 LLM 内容段。
   漏映射的模板会被下面的 `|| 'plan'` 兜底成 plan，生成出 13 个计划书字段，
   一个都填不进模板 —— 总结报告曾因此整篇留空（2026-11-24 查出）。
   与 engine/llm.py 的 TASKS 及 api/routes/agent.py 的 _SW_TASK_TEMPLATE 保持一致。*/
const LLM_TASK_BY_TPL = {
  '计划书': 'plan',
  '简报': 'brief',
  '总结报告': 'summary',
  '监督记录': 'supervision',
};

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
  const useLlm = !!(el('#fillUseLlm') && el('#fillUseLlm').checked);
  // 勾了 LLM 就走 LLM（不管有没有改字段）：
  // 原实现在「勾选 + 改过字段」时静默走普通 fill，LLM 根本不执行，
  // 用户以为用了 LLM 其实没用（2026-11-24 查出的 bug）。
  // 复用的是 _llm 留档：有留档直接取，没有才调 LLM，避免重复等 2–4 分钟。
  const untouched = Object.keys(data).length === 0;
  if (useLlm) {
    el('#fillMsg').innerHTML = '<span class="muted">正在用 LLM 冷化生成（约 2–4 分钟，'
      + '依据项目书与知识库）…</span>';
  } else {
    el('#fillMsg').innerHTML = '<span class="muted">生成中…</span>';
  }
  try {
    let r;
    if (useLlm) {
      const task = LLM_TASK_BY_TPL[tpl.replace(/^\d+-/, '').replace(/\.(docx|xlsx)$/i, '')]
        || 'plan';
      // 先看有没有留档：有就只补 LLM 字段（秒出），没有才真调 LLM
      let cached = null;
      try {
        const c = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}`
          + `/llm-cache?task=${enc(task)}`);
        if (c && c.exists && c.result) cached = c.result;
      } catch { /* 读不到就当没有 */ }
      if (cached && !untouched) {
        // 用户改过的字段优先，LLM 字段从留档补
        const merged = { ...Object.fromEntries(
          Object.entries(cached).filter(([k]) => !k.startsWith('_'))) , ...data };
        r = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/fill`, {
          method: 'POST', body: { template: tpl, data: merged, ...(out ? { output: out } : {}) },
        });
        r.grounding_used = '复用留档';
      } else {
        r = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/llm-fill`, {
          method: 'POST', body: { task, template: tpl, ...(out ? { output: out } : {}) },
        });
      }
    } else {
      r = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/fill`, {
        method: 'POST', body,
      });
    }
    const missing = r.missing || [];
    const g = r.grounding ? `；冷化依据：${esc(r.grounding)}` : '';
    el('#fillMsg').innerHTML = `<div class="notice green">
      已生成 <b>${esc(r.path)}</b>，填充 ${esc(r.filled)} 处${missing.length ? `；缺 ${missing.length} 个占位符：${esc(missing.join('、'))}` : ''}${g}。
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
/* 自动整理：把放错位置/命名不规范的材料归位改名。先出方案给用户看。*/
let tidyPlan = null;
async function openTidyModal() {
  if (!actCtx) return;
  const { projectName, dirname } = actCtx;
  openModal('自动整理 · 材料归位', '<div class="muted">正在扫描…</div>',
    '<button class="btn" data-close-modal>取消</button>', true);
  try {
    const p = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/tidy`);
    tidyPlan = p.actions || [];
    if (!tidyPlan.length) {
      $('modalBody').innerHTML = '<div class="notice green">'
        + '材料位置与命名都符合规范，无需整理。</div>';
      if ((p.skipped || []).length) {
        $('modalBody').innerHTML += '<div class="section-title">已跳过</div>'
          + p.skipped.map(x => `<div class="muted">${esc(x.from)} — ${esc(x.reason)}</div>`).join('');
      }
      $('modalFoot').innerHTML = '<button class="btn" data-close-modal>关闭</button>';
      return;
    }
    $('modalBody').innerHTML = '<div class="notice">将执行以下 '
      + tidyPlan.length + ' 项整理（只搬动和改名，不改内容）：</div>'
      + tidyPlan.map(a => `<div class="file-row"><span class="nm">${esc(a.from)}</span>`
        + `<span class="sz">→ ${esc(a.to)}</span>`
        + `<span class="tm">${esc(a.reason)}</span></div>`).join('')
      + ((p.skipped || []).length
        ? '<div class="section-title">已跳过</div>'
          + p.skipped.map(x => `<div class="muted">${esc(x.from)} — ${esc(x.reason)}</div>`).join('')
        : '')
      + '<div id="tidyMsg" style="margin-top:12px"></div>';
    $('modalFoot').innerHTML = '<button class="btn" data-close-modal>取消</button>'
      + '<button class="btn primary" data-act="tidy-apply">执行整理</button>';
  } catch (e) {
    $('modalBody').innerHTML = `<div class="notice">扫描失败：${esc(e.message)}</div>`;
    $('modalFoot').innerHTML = '<button class="btn" data-close-modal>关闭</button>';
  }
}

async function applyTidy() {
  if (!actCtx || !tidyPlan || !tidyPlan.length) return;
  const { projectName, dirname } = actCtx;
  el('#tidyMsg').innerHTML = '<span class="muted">整理中…</span>';
  try {
    const r = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/tidy`, {
      method: 'POST', body: { actions: tidyPlan },
    });
    const errs = r.errors || [];
    el('#tidyMsg').innerHTML = `<div class="notice green">已整理 ${r.moved} 项`
      + `${errs.length ? `，${errs.length} 项失败` : ''}。</div>`
      + errs.map(e => `<div class="muted">${esc(e.from)} — ${esc(e.error)}</div>`).join('');
    $('modalFoot').innerHTML = '<button class="btn" data-close-modal>关闭</button>'
      + '<button class="btn primary" data-act="close-refresh">关闭并刷新</button>';
    toast(`已整理 ${r.moved} 项`, 'ok');
  } catch (e) {
    el('#tidyMsg').innerHTML = `<div class="notice">整理失败：${esc(e.message)}</div>`;
  }
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
      <label class="field" style="flex-direction:row;align-items:center;gap:8px">
        <input type="checkbox" id="llmForce" style="width:auto;margin:0">
        <span style="margin:0">覆盖已存在的同名文件（会丢失人工修改）</span>
      </label>
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
  // 上次的生成结果如果还在，直接读回来显示，省得每次重新生成（100~240 秒）。
  taskSel.addEventListener('change', () => loadLlmCache(taskSel.value));
  loadLlmCache(taskSel.value);
}

/* 读取该任务上次留档的生成结果并回显。没有就保持空白，等用户点「生成」。*/
async function loadLlmCache(task) {
  if (!actCtx || !task) return;
  const { projectName, dirname } = actCtx;
  try {
    const r = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}`
      + `/llm-cache?task=${enc(task)}`);
    if (!r || !r.exists || !r.result) return;
    renderLlmResult(r.result, `（读取自 ${r.saved_at || ''} 的留档，可直接编辑或重新生成）`);
  } catch { /* 读不到就按没有处理 */ }
}

/* 把生成结果渲染成可编辑字段区。runLlm 与 loadLlmCache 共用。*/
function renderLlmResult(all, hint) {
  const check = all._check || null;
  const rewrites = Number(all._rewrites || 0);
  const fields = Object.fromEntries(
    Object.entries(all).filter(([k]) => !k.startsWith('_')));
  actCtx.llm = { result: fields, check };
  el('#llmMsg').textContent = hint || '生成完成，可逐字段修改。';
  el('#llmResult').innerHTML = renderSelfCheck(check, rewrites) +
    `<div class="section-title">生成结果</div>` +
    Object.entries(fields).map(([k, v]) => {
      const isArr = Array.isArray(v);
      const text = isArr ? JSON.stringify(v, null, 2) : String(v ?? '');
      return `<label class="field"><span>${esc(k)}${isArr ? '（数组，JSON）' : ''}</span>
        <textarea data-llm-key="${esc(k)}" data-array="${isArr ? '1' : '0'}">${esc(text)}</textarea>
      </label>`;
    }).join('');
}

/* 文风自检结果条：通过=绿色，未通过=黄色（.notice 默认色） */
function renderSelfCheck(check, rewrites) {
  if (!check || typeof check !== 'object') return '';
  const score = Number(check.score) || 0;
  const words = Number(check.words) || 0;
  if (check.ok) {
    return `<div class="notice green">文风自检通过（${score} 分，${words} 字${rewrites ? `，已自动重写 ${rewrites} 次` : ''}）</div>`;
  }
  const issues = (check.issues || []).map(esc).join('；') || '未达到文风要求';
  return `<div class="notice">自检未通过：${issues}（${words} 字）` +
    `<br>已自动重写 ${rewrites || 0} 次${rewrites ? '，仍未达标，可手动修改后落盘' : ''}</div>`;
}

/* LLM 生成阶段文案。一次调用实测 50~100 秒（含文风自检重写一次），
 * 全程静默会让人以为卡死，所以按后端进度改文案（2026-11-23）。*/
const STAGE_TEXT = {
  generating: s => `生成中（已 ${s}s，约需 1.5–4 分钟）…`,
  checking: s => `正在自检文风（已 ${s}s）…`,
  rewriting: s => `自检未达标，正在重写（已 ${s}s）…`,
  done: s => `生成完成（用时 ${s}s），可逐字段修改。`,
};

/* 轮询 /llm-progress，把「生成中」细化成具体阶段。返回停止函数。*/
function startLlmPoll(projectName, dirname) {
  const started = Date.now();
  let alive = true;
  const tick = async () => {
    if (!alive) return;
    const elapsed = Math.round((Date.now() - started) / 1000);
    try {
      const p = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/llm-progress`);
      const fn = STAGE_TEXT[p && p.stage] || STAGE_TEXT.generating;
      if (alive) el('#llmMsg').textContent = fn(elapsed);
    } catch { /* 进度查询失败不影响生成 */ }
    if (alive) setTimeout(tick, 3000);
  };
  setTimeout(tick, 2000);
  return () => { alive = false; };
}

async function runLlm() {
  if (!actCtx) return;
  const { projectName, dirname } = actCtx;
  const task = el('#llmTask').value;
  const style = el('#llmStyle').value;
  el('#llmMsg').textContent = STAGE_TEXT.generating(0);
  el('#llmResult').innerHTML = '';
  const stopPoll = startLlmPoll(projectName, dirname);
  try {
    const r = await api(`/projects/${enc(projectName)}/activities/${enc(dirname)}/llm`, {
      method: 'POST', body: { task, style },
    });
    stopPoll();
    renderLlmResult(r.result || {}, '生成完成，可逐字段修改。');
  } catch (e) {
    stopPoll();
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
  const forceChecked = !!(el('#llmForce') && el('#llmForce').checked);
  if (!tpl) { toast('请先选择落盘模板（弹窗顶部「落盘模板」）', 'warn'); return; }

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
  if (forceChecked) body.force = true;
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
    // 409 = 覆盖保护（目标文件已存在且活动状态是 imported/generated 等）。
    // 这是要防误覆盖人工修改，但界面之前没有任何确认入口，用户只看到
    // 「不起作用」（2026-11-23 实测：llmMsg 停在「落盘中…」→ 409）。
    // 处理：勾上「覆盖已存在」并提示用户再点一次，下次请求就带 force=true。
    const msg = String(e.message || '');
    if (/409|force=true|覆盖/.test(msg)) {
      // 不依赖 confirm（await 之后调用，部分浏览器会拦截）：
      // 直接把勾选框勾上并提示，用户再点一次即可。
      const box = el('#llmForce');
      if (box) box.checked = true;
      el('#llmMsg').textContent =
        `「${out || tpl}」已存在。已自动勾选「覆盖已存在的同名文件」，再点一次「应用并落盘」即可覆盖。`;
      toast('文件已存在：已勾选覆盖，请再点一次', 'warn');
    } else {
      el('#llmMsg').textContent = '落盘失败：' + e.message;
      toast('落盘失败：' + e.message, 'err');
    }
  }
}

/* ============================================================
 * 4b. 报销校验 / 图注 / 满意度
 * ============================================================ */
function actBase() {
  return `/projects/${enc(actCtx.projectName)}/activities/${enc(actCtx.dirname)}`;
}

/* ---------------- 报销校验 ---------------- */
function invoiceCard(label, value, source, count) {
  return `<div class="money-card">
    <div class="label">${esc(label)}</div>
    <div class="value">${fmtMoney(value)}<span class="unit">元</span></div>
    <div class="muted">${esc(source || '')}${count != null ? ` · ${count} 张` : ''}</div>
  </div>`;
}

function renderInvoiceResult(r) {
  const inv = r['发票'] || {};
  const items = inv.items || [];
  const rows = items.map((it, i) => `<tr>
      <td class="num">${i + 1}</td>
      <td class="nowrap">${esc(it.date || '—')}</td>
      <td class="mono">${esc(it.invoice_no || '—')}</td>
      <td>${esc(it.merchant || '—')}</td>
      <td>${esc(it.item || '—')}</td>
      <td class="num">${fmtMoney(it.amount)}</td>
      <td>${it.parse_error ? `<span class="badge warn" title="${esc(it.parse_error)}">待核对</span>` : ''}</td>
    </tr>`).join('');
  const diffs = Object.entries(r.diff || {})
    .filter(([, v]) => v != null)
    .map(([k, v]) => `${k} ${v > 0 ? '+' : ''}${fmtMoney(v)}`)
    .join(' · ') || '—';
  const issues = (r.issues || []).map(x => `<li>${esc(x)}</li>`).join('');

  $('modalBody').innerHTML = `
    <div class="money-grid">
      ${invoiceCard('决算', (r['决算'] || {}).value, (r['决算'] || {}).source)}
      ${invoiceCard('报销', (r['报销'] || {}).value, (r['报销'] || {}).source)}
      ${invoiceCard('发票', (r['发票'] || {}).value, '', inv.count)}
    </div>
    <div class="row" style="margin:12px 0">
      ${r.consistent ? '<span class="badge ok">三表一致</span>' : '<span class="badge err">不一致</span>'}
      <span class="muted">差异：${esc(diffs)}</span>
    </div>
    ${issues ? `<div class="notice"><b>问题</b><ul class="plain-list">${issues}</ul></div>` : ''}
    <div class="section-title">发票明细（${items.length} 张）</div>
    ${items.length ? `<div class="table-wrap"><table>
      <thead><tr><th>#</th><th>日期</th><th>号码</th><th>商户</th><th>内容</th>
        <th class="num">金额</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>` : empty('12-发票/ 下没有票据')}
    <div id="invMsg" style="margin-top:12px"></div>`;
  $('modalFoot').innerHTML = `<button class="btn" data-close-modal>关闭</button>
    <button class="btn primary" data-act="invoice-report">生成发票明细表</button>`;
}

async function openInvoiceModal() {
  if (!actCtx) return;
  if (!canWrite()) { toast('需 editor/admin 权限', 'warn'); return; }
  openModal('报销校验', '<div class="loading">扫描发票并比对三表…</div>',
    '<button class="btn" data-close-modal>关闭</button>', true);
  try {
    const r = await api(actBase() + '/invoice/scan', { method: 'POST' });
    actCtx.invoice = r;
    renderInvoiceResult(r);
  } catch (e) {
    $('modalBody').innerHTML = `<div class="notice">扫描失败：${esc(e.message)}</div>`;
    toast('扫描失败：' + e.message, 'err');
  }
}

async function submitInvoiceReport() {
  if (!actCtx) return;
  el('#invMsg').innerHTML = '<span class="muted">生成中…</span>';
  try {
    const r = await api(actBase() + '/invoice/report', { method: 'POST', body: {} });
    el('#invMsg').innerHTML = `<div class="notice green">已生成 <b>${esc(r.path)}</b>
      （${r.count} 张，合计 ${fmtMoney(r.total)} 元）</div>`;
    toast('发票明细表已生成：' + r.path, 'ok');
  } catch (e) {
    el('#invMsg').innerHTML = `<div class="notice">生成失败：${esc(e.message)}</div>`;
    toast('生成失败：' + e.message, 'err');
  }
}

/* ---------------- 图注 ---------------- */
const CAPTION_SUB = '11-活动照片资料';

function renderCaptionGrid() {
  const cap = actCtx.cap;
  const texts = new Map((cap.captions || []).map(c => [c.file, c.caption]));
  const tiles = cap.photos.map(p => {
    // 后端 write_captions 返回的是文件名；子目录里的照片用相对路径，兜底按 basename 匹配
    const text = texts.has(p.name) ? texts.get(p.name)
      : texts.get(p.name.split('/').pop());
    return `<div class="photo-tile${text != null ? ' has-cap' : ''}">
      <label class="photo-pick">
        <input type="checkbox" data-photo="${esc(p.name)}" ${p.picked ? 'checked' : ''}>
        <span class="ph-name" title="${esc(p.name)}">${esc(p.name)}</span>
      </label>
      ${p.thumb ? `<img src="${p.thumb}" alt="${esc(p.name)}" loading="lazy">`
                : '<div class="ph-noimg">无预览</div>'}
      ${text != null ? `<input class="cap-input" data-cap-file="${esc(p.name)}"
        value="${esc(text)}" title="${esc(text)}">` : ''}
    </div>`;
  }).join('');

  const tplOptions = (cap.tpls || []).map(t =>
    `<option value="${esc(t.name)}" ${t.name === cap.tpl ? 'selected' : ''}>${esc(t.name)}</option>`
  ).join('') || '<option value="">（模板库为空）</option>';

  $('modalBody').innerHTML = `
    <div class="form-grid">
      <label class="field"><span>文风</span>
        <select id="capStyle">${STYLES.map(s =>
          `<option value="${esc(s)}" ${s === cap.style ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select>
      </label>
      <label class="field"><span>落盘模板</span><select id="capTpl">${tplOptions}</select></label>
    </div>
    <div class="notice blue">勾选要配图注的照片（默认程序按文件名挑好的）；生成后可逐条修改。</div>
    <div class="section-title">照片（${cap.photos.length} 张，已选 <span id="capCount">0</span> 张）</div>
    <div class="photo-grid" id="capGrid">${tiles || empty('照片目录为空')}</div>
    <div id="capMsg" style="margin-top:12px"></div>`;
  $('modalFoot').innerHTML = `<button class="btn" data-close-modal>关闭</button>
    <button class="btn" data-act="cap-generate">生成图注</button>
    <button class="btn primary" data-act="cap-apply">应用到模板</button>`;
  syncCapCount();
}

function syncCapCount() {
  const box = el('#capCount');
  if (!box) return;
  box.textContent = els('[data-photo]').filter(x => x.checked).length;
}

async function openCaptionModal() {
  if (!actCtx) return;
  if (!canWrite()) { toast('需 editor/admin 权限', 'warn'); return; }
  openModal('生成图注', '<div class="loading">读取照片…</div>',
    '<button class="btn" data-close-modal>关闭</button>', true);
  try {
    const [photos, tpls] = await Promise.all([
      api(`${actBase()}/photos?sub=${enc(CAPTION_SUB)}`),
      ensureTemplates().catch(() => []),
    ]);
    const docx = (tpls || []).filter(t => t.kind === 'docx');
    const preferred = docx.find(t => t.name.includes('简报')) || docx[0];
    actCtx.cap = {
      sub: CAPTION_SUB,
      photos: photos || [], captions: [], style: STYLES[0],
      tpls: docx, tpl: preferred ? preferred.name : '',
    };
    renderCaptionGrid();
  } catch (e) {
    $('modalBody').innerHTML = `<div class="notice">读取照片失败：${esc(e.message)}</div>`;
    toast('读取照片失败：' + e.message, 'err');
  }
}

async function generateCaptions() {
  if (!actCtx || !actCtx.cap) return;
  const cap = actCtx.cap;
  cap.style = el('#capStyle') ? el('#capStyle').value : cap.style;
  cap.tpl = el('#capTpl') ? el('#capTpl').value : cap.tpl;
  const picked = els('[data-photo]').filter(x => x.checked).map(x => x.dataset.photo);
  if (!picked.length) { toast('至少勾选一张照片', 'warn'); return; }
  el('#capMsg').innerHTML = '<span class="muted">生成中，约需 1.5–4 分钟（LLM 生成 + 文风自检重写）…</span>';
  try {
    const r = await api(actBase() + '/captions', {
      method: 'POST', body: { sub: cap.sub || CAPTION_SUB, style: cap.style, picked },
    });
    cap.captions = r || [];
    const chosen = new Set(picked);
    cap.photos.forEach(p => { p.picked = chosen.has(p.name); });
    renderCaptionGrid();
    toast(`已生成 ${cap.captions.length} 条图注`, 'ok');
  } catch (e) {
    el('#capMsg').innerHTML = `<div class="notice">生成失败：${esc(e.message)}</div>`;
    toast('图注生成失败：' + e.message, 'err');
  }
}

async function applyCaptions() {
  if (!actCtx || !actCtx.cap) return;
  const captions = els('[data-cap-file]').map(inp => ({
    file: inp.dataset.capFile, caption: inp.value.trim(),
  })).filter(c => c.caption);
  if (!captions.length) { toast('还没有图注，先点「生成图注」', 'warn'); return; }
  const tpl = el('#capTpl') ? el('#capTpl').value : '';
  if (!tpl) { toast('请先选择落盘模板（弹窗顶部「落盘模板」）', 'warn'); return; }
  el('#capMsg').innerHTML = '<span class="muted">落盘中…</span>';
  try {
    const r = await api(actBase() + '/captions/fill', {
      method: 'POST', body: { captions, template: tpl },
    });
    const missing = r.missing || [];
    el('#capMsg').innerHTML = `<div class="notice green">已落盘 <b>${esc(r.path)}</b>，
      填充 ${r.filled} 处${missing.length ? `；缺 ${missing.length} 个占位符：${esc(missing.join('、'))}` : ''}</div>`;
    toast('图注已应用：' + r.path, 'ok');
  } catch (e) {
    el('#capMsg').innerHTML = `<div class="notice">落盘失败：${esc(e.message)}</div>`;
    toast('落盘失败：' + e.message, 'err');
  }
}

/* ---------------- 满意度 ---------------- */
function satRow(entry, index) {
  const score = entry.score == null ? '' : entry.score;
  const checked = entry.satisfied === true
    || (entry.satisfied == null && score !== '' && Number(score) >= 80);
  return `<tr data-sat-row data-file="${esc(entry.file)}">
    <td class="num">${index + 1}</td>
    <td class="mono">${esc(entry.file)}</td>
    <td><input type="number" min="0" max="100" step="1" class="sat-score"
      data-sat-score value="${esc(score)}" placeholder="0-100"></td>
    <td><input type="checkbox" data-sat-ok ${checked ? 'checked' : ''}></td>
    <td><button class="btn sm danger" data-act="sat-del">删除</button></td>
  </tr>`;
}

function renderSatTable() {
  const rows = actCtx.sat || [];
  $('modalBody').innerHTML = `
    <div class="sat-stats" id="satStats"></div>
    <div class="notice blue">按问卷照片逐张录分；分数 ≥80 时「三题满意」自动勾上，可手动改。</div>
    ${rows.length ? `<div class="table-wrap"><table>
      <thead><tr><th>#</th><th>文件名</th><th class="num">分数</th><th>三题满意</th><th></th></tr></thead>
      <tbody>${rows.map(satRow).join('')}</tbody></table></div>`
    : empty('04-满意度调查表资料/ 下没有问卷照片')}
    <div id="satMsg" style="margin-top:12px"></div>`;
  $('modalFoot').innerHTML = `<button class="btn" data-close-modal>关闭</button>
    <button class="btn" data-act="sat-save">保存分数</button>
    <button class="btn primary" data-act="sat-fill">生成汇总表</button>`;
  refreshSatStats();
}

function refreshSatStats() {
  const box = el('#satStats');
  if (!box) return;
  let count = 0, total = 0, hit = 0;
  for (const tr of els('[data-sat-row]')) {
    const raw = tr.querySelector('[data-sat-score]').value.trim();
    const score = Number(raw);
    if (raw === '' || isNaN(score)) continue;
    count += 1;
    total += score;
    if (tr.querySelector('[data-sat-ok]').checked) hit += 1;
  }
  const avg = count ? total / count : null;
  const rate = count ? `${Math.round(hit / count * 100)}%（${hit}/${count}）` : '—';
  box.innerHTML = `
    <span>份数 <b>${count}</b></span>
    <span>总得分 <b>${count ? fmtMoney(total).replace(/\.00$/, '') : '—'}</b></span>
    <span>平均分 <b>${avg == null ? '—' : avg.toFixed(2)}</b></span>
    <span>满意率 <b>${rate}</b></span>
    <span>等级 <b>${SAT_GRADE(avg) || '—'}</b></span>`;
}

function satPayload() {
  return els('[data-sat-row]').map(tr => {
    const raw = tr.querySelector('[data-sat-score]').value.trim();
    return {
      file: tr.dataset.file,
      score: raw === '' ? null : Number(raw),
      satisfied: tr.querySelector('[data-sat-ok]').checked,
    };
  });
}

async function openSatModal() {
  if (!actCtx) return;
  if (!canWrite()) { toast('需 editor/admin 权限', 'warn'); return; }
  openModal('满意度统计', '<div class="loading">读取问卷清单…</div>',
    '<button class="btn" data-close-modal>关闭</button>', true);
  try {
    let rows = await api(actBase() + '/satisfaction');
    if (!rows || !rows.length) {
      rows = await api(actBase() + '/satisfaction/list', { method: 'POST' });
    }
    actCtx.sat = rows || [];
    renderSatTable();
  } catch (e) {
    $('modalBody').innerHTML = `<div class="notice">读取失败：${esc(e.message)}</div>`;
    toast('读取失败：' + e.message, 'err');
  }
}

async function saveScores(quiet = false) {
  try {
    const r = await api(actBase() + '/satisfaction', {
      method: 'PUT', body: { scores: satPayload() },
    });
    if (!quiet) {
      el('#satMsg').innerHTML = `<div class="notice green">已保存 ${r.count} 条分数到
        <b>${esc(r.path)}</b></div>`;
      toast('分数已保存', 'ok');
    }
    return r;
  } catch (e) {
    el('#satMsg').innerHTML = `<div class="notice">保存失败：${esc(e.message)}</div>`;
    toast('保存失败：' + e.message, 'err');
    throw e;
  }
}

async function fillSatSummary() {
  if (!actCtx) return;
  el('#satMsg').innerHTML = '<span class="muted">保存并生成中…</span>';
  try {
    await saveScores(true);
  } catch {
    return;
  }
  try {
    const r = await api(actBase() + '/satisfaction/fill', { method: 'POST' });
    el('#satMsg').innerHTML = `<div class="notice green">
      已生成 <b>${esc(r.path)}</b>：${r.份数} 份 · 总得分 ${r.总得分} · 平均
      ${r.平均分} · 满意率 ${esc(r.满意率)} · ${esc(r.评定等级)}</div>`;
    toast('汇总表已生成：' + r.path, 'ok');
  } catch (e) {
    el('#satMsg').innerHTML = `<div class="notice">生成失败：${esc(e.message)}</div>`;
    toast('生成失败：' + e.message, 'err');
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
/* 项目权限下拉：每个项目一个「无 / 只读 / 读写」。
 * 选项来自 /api/projects（不写死项目名），value 用目录名，后端会归一。 */
function projectPermSelects(projects, selected, attr) {
  const cur = selected || {};
  const rows = (projects || []).map(p => {
    const name = p.name;
    const label = projectShort(p) || name;
    const access = cur[name] || cur[label] || '';
    return `<label class="muted" style="display:inline-flex;align-items:center;gap:6px;margin:4px 14px 4px 0">
      <span>${esc(label)}</span>
      <select ${attr}="${esc(name)}" style="width:auto">
        <option value=""${access === '' ? ' selected' : ''}>无</option>
        <option value="readonly"${access === 'readonly' ? ' selected' : ''}>只读</option>
        <option value="write"${access === 'write' ? ' selected' : ''}>读写</option>
      </select>
    </label>`;
  }).join('');
  return rows || '<span class="muted">暂无项目</span>';
}

/* 从一组 data-* 下拉收集 {项目名: readonly|write}，选择「无」的不提交 */
function readProjectPerms(dsKey) {
  const out = {};
  els(`[data-${dsKey}]`).forEach(sel => {
    if (sel.value) out[sel.dataset[dsKey]] = sel.value;
  });
  return out;
}

async function pageUsers() {
  setTitle('用户管理');
  if (!isAdmin()) { $('view').innerHTML = empty('需要 admin 权限'); return; }
  $('view').innerHTML = loading();
  const [d, projects, kd] = await Promise.all([
    api('/admin/users'), ensureProjects(true),
    // Key 列表失败不拖垮整个用户管理页
    api('/sw/keys').catch(() => ({ items: [] })),
  ]);
  const users = d.items || [];
  usersCache = users;

  // 每个用户取最新一把有效 Key；keys 已按 id DESC，第一条即最新
  const keys = (kd && kd.items) || [];
  keysCache = {};
  const latestKey = {};
  for (const k of keys) {
    if (k.secret) keysCache[k.id] = k.secret;
    if (k.active && !latestKey[k.user_id]) latestKey[k.user_id] = k;
  }

  const rows = users.map(u => `<tr>
      <td>${esc(u.real_name || '—')}</td>
      <td>${esc(u.username)}</td>
      <td><span class="badge ${u.role === 'admin' ? 'info' : u.role === 'editor' ? 'ok' : 'muted'}">${esc(u.role)}</span></td>
      <td>${u.enabled ? '<span class="badge ok">启用</span>' : '<span class="badge err">禁用</span>'}</td>
      <td class="muted" title="${esc(permText(u.projects))}">${esc(permText(u.projects))}</td>
      <td class="nowrap">${keyCell(u, latestKey[u.id])}</td>
      <td class="nowrap">
        <button class="btn sm" data-act="user-edit" data-uid="${u.id}">编辑</button>
        <button class="btn sm" data-act="user-toggle" data-uid="${u.id}" data-enabled="${u.enabled ? '1' : '0'}">${u.enabled ? '禁用' : '启用'}</button>
        <button class="btn sm" data-act="user-pass" data-uid="${u.id}" data-name="${esc(u.username)}">重置密码</button>
        <button class="btn sm" data-act="user-kick" data-uid="${u.id}" data-name="${esc(u.username)}">踢下线</button>
        ${u.id === me.id ? '' : `<button class="btn sm danger" data-act="user-del" data-uid="${u.id}" data-name="${esc(u.username)}">删除</button>`}
      </td>
    </tr>`).join('');

  $('view').innerHTML = `
    ${pageHead('用户管理', '真实姓名、角色与项目权限')}
    <div class="muted" style="margin:-4px 0 12px">API Key 明文以加密形式存储，可随时查看。分享请走安全渠道。</div>
    <div class="card">
      <div class="card-head"><h3>新建用户</h3></div>
      <div class="row">
        <input id="nuName" placeholder="用户名" style="width:140px">
        <input id="nuReal" placeholder="真实姓名" style="width:140px">
        <input id="nuPass" type="password" placeholder="密码" style="width:150px">
        <select id="nuRole">
          <option value="viewer">viewer（只读）</option>
          <option value="editor">editor（读写）</option>
          <option value="admin">admin（全部）</option>
        </select>
        <button class="btn primary" data-act="user-create">建号</button>
      </div>
      <div class="row" style="margin-top:10px;flex-wrap:wrap">
        <span class="muted">项目权限：</span>${projectPermSelects(projects, {}, 'data-newproj')}
      </div>
      <div id="nuMsg" style="margin-top:8px"></div>
    </div>
    <div class="card pad0">
      <div class="card-head" style="padding:16px 16px 0"><h3>用户列表</h3><span class="muted">${users.length} 个</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>真实姓名</th><th>用户名</th><th>角色</th><th>状态</th><th>项目权限</th><th>API Key</th><th>操作</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="7">${empty('暂无用户')}</td></tr>`}</tbody>
      </table></div>
    </div>`;
}

function keyCell(u, k) {
  if (!k) {
    return `<button class="btn sm" data-act="user-key-issue" data-uid="${u.id}">签发</button>`;
  }
  if (!k.secret) {
    return `<span class="muted">（旧 Key，无法查看明文，请重新签发）</span>
      <button class="btn sm" data-act="user-key-issue" data-uid="${u.id}">重新签发</button>`;
  }
  return `<span class="mono key-secret" id="keysec-${k.id}">${esc(maskSecret(k.secret))}</span>
    <button class="btn sm" data-act="key-show" data-keyid="${k.id}">显示</button>
    <button class="btn sm" data-act="key-copy" data-keyid="${k.id}">复制</button>`;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  toast('已复制到剪贴板', 'ok');
}

async function issueUserKey(uid) {
  const u = (usersCache || []).find(x => String(x.id) === String(uid));
  if (!u) return;
  const name = 'web-' + new Date().toISOString().slice(0, 10);
  const r = await api('/sw/keys', { method: 'POST', body: { user: u.username, name } });
  await pageUsers();
  // 签发后直接把新 Key 明文展开，省一次点「显示」
  keysCache[r.id] = r.key;
  const span = $('keysec-' + r.id);
  const btn = el(`[data-act="key-show"][data-keyid="${r.id}"]`);
  if (span) span.textContent = r.key;
  if (btn) { btn.dataset.shown = '1'; btn.textContent = '隐藏'; }
  toast('已签发，可点「复制」发送', 'ok');
}

async function createUser() {
  try {
    await api('/admin/users', {
      method: 'POST',
      body: {
        username: el('#nuName').value.trim(),
        real_name: el('#nuReal').value.trim(),
        password: el('#nuPass').value,
        role: el('#nuRole').value,
        projects: readProjectPerms('newproj'),
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

async function openUserEdit(uid) {
  const u = (usersCache || []).find(x => String(x.id) === String(uid));
  if (!u) return;
  const projects = await ensureProjects();
  openModal('编辑用户', `
    <div class="row" style="margin-bottom:12px">
      <label class="muted">用户名 <input value="${esc(u.username)}" disabled style="width:140px"></label>
      <label class="muted">真实姓名 <input id="euReal" value="${esc(u.real_name || '')}" style="width:140px"></label>
    </div>
    <div class="row" style="margin-bottom:12px">
      <label class="muted">角色
        <select id="euRole">
          <option value="viewer"${u.role === 'viewer' ? ' selected' : ''}>viewer（只读）</option>
          <option value="editor"${u.role === 'editor' ? ' selected' : ''}>editor（读写）</option>
          <option value="admin"${u.role === 'admin' ? ' selected' : ''}>admin（全部）</option>
        </select>
      </label>
      <label class="muted">状态
        <select id="euEnabled">
          <option value="1"${u.enabled ? ' selected' : ''}>启用</option>
          <option value="0"${u.enabled ? '' : ' selected'}>禁用</option>
        </select>
      </label>
    </div>
    <div class="muted" style="margin-bottom:6px">项目权限</div>
    <div style="display:flex;flex-wrap:wrap">${projectPermSelects(projects, u.projects, 'data-editproj')}</div>
  `, `<button class="btn" data-close-modal>取消</button>
      <button class="btn primary" data-act="user-save" data-uid="${u.id}">保存</button>`);
}

async function saveUser(uid) {
  await api('/admin/users/' + uid, {
    method: 'PATCH',
    body: {
      real_name: el('#euReal').value.trim(),
      role: el('#euRole').value,
      enabled: el('#euEnabled').value === '1',
      projects: readProjectPerms('editproj'),
    },
  });
  closeModal();
  toast('用户已更新', 'ok');
  await pageUsers();
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
    if (act === 'preview') { e.preventDefault(); await openPreview(node.dataset.path); return; }
    if (act === 'preview-download') { await downloadActivityFile(node.dataset.path); return; }
    if (act === 'del-file') { await deleteActivityFile(node.dataset.path); return; }
    if (act === 'open-trash') { await openTrash(); return; }
    if (act === 'restore-file') { await restoreFromTrash(node.dataset.trash); return; }
    if (act === 'do-check') { await recheckActivity(); return; }
    if (act === 'rev-review') { await openReviewModal(node.dataset.no, 'review'); return; }
    if (act === 'rev-sign') { await openReviewModal(node.dataset.no, 'sign'); return; }
    if (act === 'rev-clear') { await clearReview(node.dataset.no); return; }
    if (act === 'rev-submit') { await submitReview(node); return; }
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
    if (act === 'open-photo-upload') { await openPhotoUploadModal(); return; }
    if (act === 'open-projfile') { await openProjectFileModal(node.dataset.project || parseHash().parts[1] || ''); return; }
    if (act === 'pf-upload') { await doProjectFileUpload(); return; }
    if (act === 'photo-pick') { e.preventDefault(); el('#photoFiles').click(); return; }
    if (act === 'photo-precheck') { await runPhotoPrecheck(); return; }
    if (act === 'photo-upload') { await uploadPhotos(); return; }
    if (act === 'photo-done') { closeModal(); await refreshActivity(); return; }
    if (act === 'fill-submit') { await submitFill(); return; }
    if (act === 'close-refresh') { closeModal(); await refreshActivity(); return; }
    if (act === 'open-tidy') { await openTidyModal(); return; }
    if (act === 'tidy-apply') { await applyTidy(); return; }
    if (act === 'nav-fold') {
      e.preventDefault(); e.stopPropagation();
      const key = node.dataset.fold;
      navFoldToggle(key);
      // 三类折叠目标的 class 不同：项目 .nav-project、社区 .nav-community、
      // 一级分组 .nav-group。按 fold key 前缀 + DOM 祖先定位。
      const wrap = node.closest('.nav-project, .nav-community, .nav-group');
      if (!wrap) return;
      const open = !wrap.classList.contains('open');
      wrap.classList.toggle('open', open);
      const arrow = node.classList.contains('nav-fold') ? node
        : (node.querySelector(':scope > .nav-fold') || node.querySelector(':scope > .nav-fold-ico'));
      if (arrow) arrow.textContent = open ? '▾' : '▸';
      const label = open ? '收起' : '展开';
      if (node.title !== undefined) node.title = label;
      if (arrow) arrow.title = label;
      return;
    }
    if (act === 'llm-run') { await runLlm(); return; }
    if (act === 'llm-apply') { await applyLlm(); return; }
    if (act === 'open-invoice') { await openInvoiceModal(); return; }
    if (act === 'invoice-report') { await submitInvoiceReport(); return; }
    if (act === 'open-caption') { await openCaptionModal(); return; }
    if (act === 'cap-generate') { await generateCaptions(); return; }
    if (act === 'cap-apply') { await applyCaptions(); return; }
    if (act === 'open-sat') { await openSatModal(); return; }
    if (act === 'sat-save') { await saveScores(); return; }
    if (act === 'sat-fill') { await fillSatSummary(); return; }
    if (act === 'sat-del') {
      const tr = node.closest('[data-sat-row]');
      if (tr) tr.remove();
      refreshSatStats();
      return;
    }
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
    if (act === 'key-show') {
      const secret = keysCache[node.dataset.keyid];
      const span = $('keysec-' + node.dataset.keyid);
      if (!secret || !span) return;
      const shown = node.dataset.shown === '1';
      span.textContent = shown ? maskSecret(secret) : secret;
      node.dataset.shown = shown ? '0' : '1';
      node.textContent = shown ? '显示' : '隐藏';
      return;
    }
    if (act === 'key-copy') {
      const secret = keysCache[node.dataset.keyid];
      if (secret) await copyText(secret);
      return;
    }
    if (act === 'user-key-issue') { await issueUserKey(node.dataset.uid); return; }
    if (act === 'user-create') { await createUser(); return; }
    if (act === 'user-edit') { await openUserEdit(node.dataset.uid); return; }
    if (act === 'user-save') { await saveUser(node.dataset.uid); return; }
    if (act === 'user-del') {
      if (!confirm(`硬删除用户「${node.dataset.name}」？其 API Key 和会话会一并删除，不可恢复。`)) return;
      await api('/admin/users/' + node.dataset.uid, { method: 'DELETE' });
      toast('用户已删除', 'ok');
      await pageUsers();
      return;
    }
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

/* 图注勾选计数 / 满意度分数与满意勾选联动 */
document.addEventListener('change', e => {
  if (e.target.closest('[data-photo]')) { syncCapCount(); return; }
  const ok = e.target.closest('[data-sat-ok]');
  if (ok) {
    ok.dataset.touched = '1';
    refreshSatStats();
  }
});
document.addEventListener('input', e => {
  const score = e.target.closest('[data-sat-score]');
  if (!score) return;
  const tr = score.closest('[data-sat-row]');
  const ok = tr && tr.querySelector('[data-sat-ok]');
  // 用户没手动勾过「三题满意」时，按分数 ≥80 自动带出，与后端默认口径一致
  if (ok && ok.dataset.touched !== '1') {
    const raw = score.value.trim();
    ok.checked = raw !== '' && Number(raw) >= 80;
  }
  refreshSatStats();
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

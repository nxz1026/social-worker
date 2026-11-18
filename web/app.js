const API = '/sw-api/api';
let token = localStorage.getItem('sw_token') || '';
let me = null;
let healthMap = {};
let relLoadedFor = '';
const CATS = ['发票票据','项目计划','项目执行','日常工作佐证','政策文件','其他'];

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtBytes = n => {
  if (n == null || isNaN(n)) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
};

const STATE_LABEL = {
  source: { ok: '原件正常', missing: '原件缺失' },
  md: { ok: 'md 正常', missing: 'md 缺失' },
  feishu: { moved: '飞书已移动', uploaded: '飞书已补传', missing: '飞书缺失', deleted: '飞书已删除', none: '未归档' },
  index: { indexed: '已索引', stale: '索引过期', missing: '未索引', unknown: '索引未知' },
};
const BADGE_CLASS = { ok: 'ok', moved: 'ok', uploaded: 'ok', indexed: 'ok',
  stale: 'warn', missing: 'err', deleted: 'muted', unknown: 'warn', none: 'muted' };

function badge(kind, state) {
  const text = (STATE_LABEL[kind] || {})[state] || state || '—';
  return `<span class="badge ${BADGE_CLASS[state] || 'muted'}">${esc(text)}</span>`;
}

async function api(path, opts = {}) {
  opts.headers = Object.assign({}, opts.headers, token ? { 'x-token': token } : {});
  if (opts.body && typeof opts.body === 'object' && !(opts.body instanceof FormData)) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(API + path, opts);
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = text; }
  if (!r.ok) throw new Error((data && data.detail) || r.status + ' ' + text.slice(0, 200));
  return data;
}

async function boot() {
  if (!token) return showLogin();
  try {
    me = await api('/me');
    showMain();
    await route();
  } catch (e) {
    localStorage.removeItem('sw_token'); token = ''; showLogin();
  }
}
function showLogin() {
  $('loginCard').classList.remove('hidden');
  $('mainArea').classList.add('hidden');
}
function showMain() {
  $('loginCard').classList.add('hidden');
  $('mainArea').classList.remove('hidden');
  $('me').textContent = `${me.username} · ${me.role} · ${me.projects.join('/')}`;
  $('btnLogout').classList.remove('hidden');
  const isAdmin = me.role === 'admin';
  $('tabUsers').classList.toggle('hidden', !isAdmin);
  $('tabAudit').classList.toggle('hidden', !isAdmin);
  fillProjectSelects();
}

function fillProjectSelects() {
  const opts = '<option value="">全部项目</option>' + me.projects.map(p =>
    `<option value="${p}">${p}</option>`).join('');
  $('fProject').innerHTML = opts;
  $('searchProject').innerHTML = opts;
  $('upProject').innerHTML = me.projects.map(p => `<option value="${p}">${p}</option>`).join('');
  $('fCategory').innerHTML = '<option value="">全部类别</option>' +
    CATS.map(c => `<option value="${c}">${c}</option>`).join('');
}

// ---------- 文档列表 ----------
async function loadDocs() {
  const p = $('fProject').value, c = $('fCategory').value, q = $('fQ').value.trim();
  const params = new URLSearchParams();
  if (p) params.set('project', p);
  if (c) params.set('category', c);
  if (q) params.set('q', q);
  const [data, health] = await Promise.all([
    api('/docs?' + params),
    api('/docs/health' + (p ? '?project=' + encodeURIComponent(p) : '')).catch(() => ({ items: {} })),
  ]);
  healthMap = health.items || {};
  const hf = $('fHealth').value;
  const rows = data.items.filter(d => {
    const h = healthMap[d.token] || {};
    if (!hf) return true;
    if (hf === 'source_missing') return h.source === 'missing';
    if (hf === 'feishu_missing') return h.feishu === 'missing';
    if (hf === 'index_stale') return h.index === 'stale';
    if (hf === 'index_missing') return h.index === 'missing';
    return true;
  });
  $('docCount').textContent = `共 ${rows.length} / ${data.total} 条`;
  const counts = { stale: 0, missing: 0, feishu: 0, source: 0 };
  Object.values(healthMap).forEach(h => {
    if (h.index === 'stale') counts.stale++;
    if (h.index === 'missing') counts.missing++;
    if (h.feishu === 'missing') counts.feishu++;
    if (h.source === 'missing') counts.source++;
  });
  $('healthSummary').textContent =
    `索引过期 ${counts.stale} · 未索引 ${counts.missing} · 飞书缺失 ${counts.feishu} · 原件缺失 ${counts.source}`;
  const isAdmin = me.role === 'admin';
  $('docList').innerHTML = rows.map(d => {
    const h = healthMap[d.token] || {};
    const badges = [
      h.source === 'missing' ? badge('source', 'missing') : '',
      h.feishu === 'missing' ? badge('feishu', 'missing') : '',
      h.index === 'stale' ? badge('index', 'stale') : (h.index === 'missing' ? badge('index', 'missing') : ''),
    ].filter(Boolean).join(' ')
      || (h.source === 'ok' ? '<span class="badge ok">正常</span>' : '<span class="muted">—</span>');
    return `<tr>
      <td><a href="#/doc/${encodeURIComponent(d.token)}/overview" data-token="${esc(d.token)}">${esc(d.name)}</a></td>
      <td>${esc(d.project)}</td><td>${esc(d.category)}</td>
      <td>${badges}</td>
      <td>${esc(String(d.archived_at || '').slice(0,16).replace('T',' '))}</td>
      <td>${isAdmin ? `<button class="ghost" data-del="${esc(d.token)}">删</button>` : ''}</td>
    </tr>`;
  }).join('');
}

// ---------- 文档详情 ----------
async function showDoc(tok, dtab = 'overview') {
  const d = await api('/docs/' + encodeURIComponent(tok));
  $('docName').textContent = d.name;
  $('docMeta').textContent = `${d.project} / ${d.category}`;
  $('docDetail').dataset.token = tok;
  $('docDetail').dataset.name = d.name;
  $('btnEdit').disabled = me.role === 'viewer';
  $('btnDelete').disabled = me.role !== 'admin';
  $('btnDelete').classList.toggle('hidden', me.role !== 'admin');
  $('docContent').textContent = d.content || '（无 md 内容）';
  $('docEditor').value = d.content || '';
  $('docEditor').classList.add('hidden');
  $('docContent').classList.remove('hidden');
  $('editBar').classList.add('hidden');
  renderSource(d);
  switchDocTab(dtab, false);
  document.querySelectorAll('.tabs button').forEach(x => x.classList.remove('active'));
  ['docs','upload','search','generate','users','audit'].forEach(t =>
    $('tab-' + t).classList.add('hidden'));
  $('docDetail').classList.remove('hidden');
}

function renderSource(d, st) {
  const s = st.source || {};
  $('dtab-source').innerHTML = `
    <div class="kv">
      <div class="kv-row"><span class="k">状态</span><span class="v">${badge('source', s.state)}</span></div>
      <div class="kv-row"><span class="k">路径</span><span class="v mono">${esc(s.path || '—')}</span></div>
      <div class="kv-row"><span class="k">大小</span><span class="v">${esc(fmtBytes(s.size))}</span></div>
      <div class="kv-row"><span class="k">sha256</span><span class="v mono">${esc(s.sha256 || '—')}</span></div>
    </div>
    <div class="row" style="margin-top:12px">
      <button id="btnDownload" ${s.state === 'ok' ? '' : 'disabled'}>下载原件</button>
      <span class="muted">下载会记录审计</span>
    </div>`;
  const b = $('btnDownload');
  if (b) b.onclick = () => downloadSource(d.token, d.name);
}

function switchDocTab(tab, push = true) {
  document.querySelectorAll('#docTabs button').forEach(b => {
    b.classList.toggle('active', b.dataset.dtab === tab);
  });
  ['overview','source'].forEach(t => {
    const el = $('dtab-' + t);
    if (el) el.classList.toggle('hidden', t !== tab);
  });
  if (push) {
    const tok = $('docDetail').dataset.token;
    if (tok) history.replaceState(null, '', `#/doc/${encodeURIComponent(tok)}/${tab}`);
  }
}

async function downloadSource(tok, name) {
  const r = await fetch(API + '/docs/' + encodeURIComponent(tok) + '/source',
    { headers: token ? { 'x-token': token } : {} });
  if (!r.ok) {
    let msg = r.status + '';
    try { const d = await r.json(); msg = d.detail || msg; } catch {}
    alert('下载失败：' + msg);
    return;
  }
  const blob = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name || 'source';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

// ---------- 检索结果解析 ----------
// 将 /api/search 的纯文本 output 按 "### 标题 (score X)" 分段
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
    $('searchOut').innerHTML = head || raw
      ? `<pre class="rawbox">${esc(head || raw)}</pre>`
      : '<div class="muted">无结果</div>';
    return;
  }
  const cards = blocks.map(b => `<div class="result">
    <h4>${esc(b.title)}${b.score != null ? `<span class="score">score ${esc(b.score)}</span>` : ''}</h4>
    <p class="snippet">${esc(b.content.slice(0, 240))}${b.content.length > 240 ? '…' : ''}</p>
    ${b.content.length > 240 ? `<details><summary>展开全文</summary>
      <pre class="rawbox">${esc(b.content)}</pre></details>` : ''}
  </div>`).join('');
  const meta = `<div class="muted" style="margin-bottom:8px">共 ${blocks.length} 条结果${
    head ? ' · ' + esc(head.split('\n')[0]) : ''}</div>`;
  const rawToggle = `<details style="margin-top:10px"><summary style="cursor:pointer;color:#2563eb;font-size:12px">查看原始输出</summary>
    <pre class="rawbox">${esc(raw)}</pre></details>`;
  $('searchOut').innerHTML = meta + cards + rawToggle;
}

// ---------- 路由 ----------
function setTab(tab, push = true) {
  document.querySelectorAll('.tabs button').forEach(x => {
    x.classList.toggle('active', x.dataset.tab === tab);
  });
  ['docs','upload','search','generate','users','audit'].forEach(t => {
    const el = $('tab-' + t);
    if (el) el.classList.add('hidden');
  });
  const el = $('tab-' + tab);
  if (el) el.classList.remove('hidden');
  $('docDetail').classList.add('hidden');
  if (tab === 'users') loadUsers();
  if (tab === 'audit') loadAudit();
  if (push) history.replaceState(null, '', '#/' + tab);
}

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, qs] = raw.split('?');
  return { parts: path.split('/').filter(Boolean), params: new URLSearchParams(qs || '') };
}

async function route() {
  if (!me) return;
  const { parts, params } = parseHash();
  if (parts[0] === 'doc' && parts[1]) {
    await showDoc(decodeURIComponent(parts[1]), parts[2] || 'overview');
    return;
  }
  const tab = parts[0] || 'docs';
  if (tab === 'docs') {
    setTab('docs', false);
    if (params.get('health')) $('fHealth').value = params.get('health');
    await loadDocs();
    return;
  }
  if (['issues','upload','search','generate','users','audit','trash'].includes(tab)) {
    setTab(tab, false);
    return;
  }
  setTab('docs', false);
  await loadDocs();
}

// ---------- 事件 ----------
$('btnLogin').onclick = async () => {
  $('loginErr').textContent = '';
  try {
    const d = await api('/login', { method: 'POST',
      body: { username: $('loginUser').value.trim(), password: $('loginPass').value } });
    token = d.token; localStorage.setItem('sw_token', token);
    me = await api('/me'); showMain(); await route();
  } catch (e) { $('loginErr').textContent = e.message; }
};
$('btnLogout').onclick = async () => {
  try { await api('/logout', { method: 'POST' }); } catch (e) { /* 服务端会话可能已失效，本地照样清理 */ }
  localStorage.removeItem('sw_token'); token = ''; me = null; location.reload();
};
$('btnFilter').onclick = () => { history.replaceState(null, '', '#/docs'); loadDocs(); };
$('docList').onclick = async e => {
  const del = e.target.dataset.del;
  if (del) {
    if (me.role !== 'admin') { alert('仅 admin 可删除'); return; }
    if (confirm('确认删除（移入回收站）？')) { await api('/docs/' + del, { method: 'DELETE' }); loadDocs(); }
    return;
  }
  const link = e.target.closest('a[data-token]');
  if (link) { e.preventDefault(); location.hash = '#/doc/' + encodeURIComponent(link.dataset.token) + '/overview'; }
};
$('btnBack').onclick = () => { location.hash = '#/docs'; };
$('btnEdit').onclick = () => {
  $('docEditor').value = $('docContent').textContent;
  $('docContent').classList.add('hidden'); $('docEditor').classList.remove('hidden');
  $('editBar').classList.remove('hidden');
};
$('btnCancel').onclick = () => {
  $('docEditor').classList.add('hidden'); $('docContent').classList.remove('hidden');
  $('editBar').classList.add('hidden');
};
$('btnSave').onclick = async () => {
  await api('/docs/' + $('docDetail').dataset.token, { method: 'PATCH',
    body: { content: $('docEditor').value } });
  $('btnCancel').onclick();
  await showDoc($('docDetail').dataset.token, 'overview');
};
$('btnDelete').onclick = async () => {
  if (me.role !== 'admin') { alert('仅 admin 可删除'); return; }
  if (!confirm('确认删除（移入回收站）？')) return;
  await api('/docs/' + $('docDetail').dataset.token, { method: 'DELETE' });
  location.hash = '#/docs';
};
$('docTabs').onclick = e => {
  const b = e.target.closest('button[data-dtab]');
  if (b) switchDocTab(b.dataset.dtab);
};
$('btnUpload').onclick = async () => {
  const f = $('upFile').files[0];
  if (!f) { $('upResult').textContent = '请选择文件'; return; }
  const fd = new FormData();
  fd.append('file', f);
  fd.append('project', $('upProject').value);
  try {
    await api('/docs', { method: 'POST', body: fd });
    $('upResult').textContent = `已上传：${f.name}`;
    loadDocs();
  } catch (e) { $('upResult').textContent = '失败：' + e.message; }
};
$('btnSearch').onclick = async () => {
  $('searchOut').innerHTML = '<div class="muted">检索中…</div>';
  try {
    const d = await api('/search?q=' + encodeURIComponent($('searchQ').value) +
      '&project=' + $('searchProject').value);
    renderSearchResults(d.output);
  } catch (e) {
    $('searchOut').innerHTML = `<div class="err">失败：${esc(e.message)}</div>`;
  }
};
$('btnGenerate').onclick = async () => {
  const type = $('genType').value;
  const context = $('genContext').value.trim();
  if (!context) { $('genMsg').textContent = '请输入主题 / 关键词'; return; }
  $('genMsg').textContent = '生成中…';
  $('genOut').classList.add('hidden');
  try {
    const d = await api('/generate', { method: 'POST', body: { type, context } });
    const text = (d && (d.content || d.output || d.result || d.text)) ||
      (typeof d === 'string' ? d : JSON.stringify(d, null, 2));
    $('genOut').innerHTML = `<div class="muted" style="margin-bottom:6px">${esc(type)} · ${esc(context)}</div>
      <pre class="genout">${esc(text)}</pre>`;
    $('genOut').classList.remove('hidden');
    $('genMsg').textContent = '';
  } catch (e) {
    const msg = String(e.message || '');
    if (/\b(404|500|501|502|503)\b/.test(msg) || /not found|internal error|未实现/i.test(msg)) {
      $('genMsg').textContent = '生成服务未就绪';
    } else {
      $('genMsg').textContent = '生成失败：' + msg;
    }
  }
};
$('btnCreateUser').onclick = async () => {
  const projects = ['nuHuadu','nuHuimin'].filter(id => $(id).checked).map(id => $(id).value);
  try {
    await api('/admin/users', { method: 'POST', body: {
      username: $('nuName').value.trim(), password: $('nuPass').value,
      role: $('nuRole').value, projects } });
    $('nuMsg').textContent = '已创建';
    loadUsers();
  } catch (e) { $('nuMsg').textContent = '失败：' + e.message; }
};
$('userList').onclick = async e => {
  const b = e.target.closest('button[data-uid]');
  if (!b) return;
  await api('/admin/users/' + b.dataset.uid, { method: 'PATCH', body: { enabled: b.dataset.en === '1' } });
  loadUsers();
};

document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => {
  setTab(b.dataset.tab);
  if (b.dataset.tab === 'docs') loadDocs();
});
window.addEventListener('hashchange', route);

boot();

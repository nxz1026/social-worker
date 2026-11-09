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
  $('tabIssues').classList.toggle('hidden', !isAdmin);
  $('tabTrash').classList.toggle('hidden', !isAdmin);
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
  const [d, st] = await Promise.all([
    api('/docs/' + encodeURIComponent(tok)),
    api('/docs/' + encodeURIComponent(tok) + '/status').catch(e => ({ error: e.message })),
  ]);
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
  renderDocStatus(d, st);
  renderSource(d, st);
  renderFeishu(d, st);
  renderIndex(d, st);
  $('dtab-relations').innerHTML = '<p class="muted">加载关系…</p>';
  relLoadedFor = '';
  switchDocTab(dtab, false);
  if (dtab === 'relations') loadRelations(tok);
  document.querySelectorAll('.tabs button').forEach(x => x.classList.remove('active'));
  ['docs','issues','trash','upload','search','generate','users','audit'].forEach(t =>
    $('tab-' + t).classList.add('hidden'));
  $('docDetail').classList.remove('hidden');
}

function renderDocStatus(d, st) {
  const s = st.source || {}, m = st.md || {}, f = st.feishu || {}, i = st.index || {};
  $('docBadges').innerHTML = [
    badge('source', s.state), badge('md', m.state), badge('feishu', f.state), badge('index', i.state),
  ].join(' ');
  const rows = [
    ['Token', d.token],
    ['项目 / 类别', `${d.project || '—'} / ${d.category || '—'}`],
    ['原件路径', s.path || '—'],
    ['原件大小', fmtBytes(s.size)],
    ['原件 sha256', s.sha256 || '—'],
    ['md 路径', m.path || '—'],
    ['md md5', m.md5 || '—'],
    ['飞书状态', `${(STATE_LABEL.feishu || {})[f.state] || f.state || '—'}${f.url ? '' : ''}`],
    ['索引状态', `${(STATE_LABEL.index || {})[i.state] || i.state || '—'}${i.name ? ' · ' + i.name : ''}`],
    ['关系数', String((st.relations || {}).count ?? 0)],
  ];
  $('docStatus').innerHTML = rows.map(([k, v]) =>
    `<div class="kv-row"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join('');
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

function renderFeishu(d, st) {
  const f = st.feishu || {};
  const admin = me.role === 'admin';
  $('dtab-feishu').innerHTML = `
    <div class="kv">
      <div class="kv-row"><span class="k">状态</span><span class="v">${badge('feishu', f.state)}</span></div>
      <div class="kv-row"><span class="k">token</span><span class="v mono">${esc(f.token || '—')}</span></div>
      <div class="kv-row"><span class="k">上传时间</span><span class="v">${esc(f.uploaded_at || '—')}</span></div>
      <div class="kv-row"><span class="k">链接</span><span class="v">${f.url
        ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">打开飞书文档 ↗</a>` : '—'}</span></div>
    </div>
    ${admin ? `<div class="row" style="margin-top:12px">
      <button id="btnFeishuUp">重新上传到飞书</button>
      <button id="btnFeishuDel" class="danger" ${f.token ? '' : 'disabled'}>删除飞书副本</button>
      <span class="muted" id="feishuMsg"></span>
    </div>` : '<p class="muted" style="margin-top:12px">只读：重新上传 / 删除需 admin。</p>'}`;
  if (!admin) return;
  $('btnFeishuUp').onclick = async () => {
    if (!confirm('重新上传一份到飞书「已分类」？\n旧副本会先改名（加 .旧-时间戳），新副本传成功后移入飞书回收站。')) return;
    $('feishuMsg').textContent = '上传中…';
    try {
      const r = await api('/docs/' + encodeURIComponent(d.token) + '/feishu', { method: 'POST' });
      const tail = r.old_trashed ? '；旧副本已移入飞书回收站'
        : (r.renamed_to ? '；旧副本未删掉，请手动清理' : '');
      $('feishuMsg').textContent = '已上传：' + (r.file_token || '') + tail;
      await showDoc(d.token, 'feishu');
    } catch (e) { $('feishuMsg').textContent = '失败：' + e.message; }
  };
  $('btnFeishuDel').onclick = async () => {
    if (!confirm('删除飞书上的这份副本？本地原件 / md / 索引不动。')) return;
    $('feishuMsg').textContent = '删除中…';
    try {
      await api('/docs/' + encodeURIComponent(d.token) + '/feishu', { method: 'DELETE' });
      await showDoc(d.token, 'feishu');
    } catch (e) { $('feishuMsg').textContent = '失败：' + e.message; }
  };
}

function renderIndex(d, st) {
  const i = st.index || {};
  const admin = me.role === 'admin';
  $('dtab-index').innerHTML = `
    <div class="kv">
      <div class="kv-row"><span class="k">状态</span><span class="v">${badge('index', i.state)}</span></div>
      <div class="kv-row"><span class="k">Cognee 条目名</span><span class="v">${esc(i.name || '—')}</span></div>
      <div class="kv-row"><span class="k">md5（contentHash）</span><span class="v mono">${esc(i.content_hash || '—')}</span></div>
      <div class="kv-row"><span class="k">同名条目数</span><span class="v">${esc(i.entries ?? 0)}</span></div>
      ${i.error ? `<div class="kv-row"><span class="k">错误</span><span class="v err">${esc(i.error)}</span></div>` : ''}
    </div>
    ${admin ? `<div class="row" style="margin-top:12px">
      <button id="btnReindex">重建索引</button>
      <span class="muted" id="indexMsg">按项目数据集跑一次对账（md → Cognee，幂等）</span>
    </div>` : '<p class="muted" style="margin-top:12px">只读：重建索引需 admin。</p>'}`;
  if (!admin) return;
  $('btnReindex').onclick = async () => {
    $('indexMsg').textContent = '重建中（可能要几十秒）…';
    try {
      const r = await api('/docs/' + encodeURIComponent(d.token) + '/reindex', { method: 'POST' });
      $('indexMsg').textContent = `完成（数据集 ${r.dataset}）`;
      await showDoc(d.token, 'index');
    } catch (e) { $('indexMsg').textContent = '失败：' + e.message; }
  };
}

async function loadRelations(tok) {
  if (relLoadedFor === tok) return;
  relLoadedFor = tok;
  const TYPE_CN = { projects: '项目', orgs: '机构', persons: '人员', places: '地点', '其他': '其他' };
  const box = $('dtab-relations');
  try {
    const r = await api('/docs/' + encodeURIComponent(tok) + '/relations');
    const groups = Object.entries(r.by_type || {});
    const ents = groups.length
      ? groups.map(([kind, names]) => `<div class="rel-group">
          <div class="muted">${esc(TYPE_CN[kind] || kind)}（${names.length}）</div>
          <div class="chips">${names.map(n => `<span class="chip">${esc(n)}</span>`).join('')}</div>
        </div>`).join('')
      : '<p class="muted">这篇 md 里没有 [[实体]] 链接。</p>';
    const bl = (r.backlinks || []).length
      ? `<ul class="linklist">${r.backlinks.map(x =>
          `<li><a href="#/doc/${encodeURIComponent(x.token)}/overview">${esc(x.name)}</a></li>`).join('')}</ul>`
      : '<p class="muted">没有其它文档链接到这一篇。</p>';
    const shared = (r.shared || []).length
      ? r.shared.map(s => `<div class="rel-group">
          <div class="muted">${esc(s.entity)}（${s.docs.length}）</div>
          <ul class="linklist">${s.docs.map(x =>
            `<li><a href="#/doc/${encodeURIComponent(x.token)}/overview">${esc(x.name)}</a></li>`).join('')}</ul>
        </div>`).join('')
      : '<p class="muted">没有共享实体。</p>';
    const rs = r.roster || {};
    const miss = (rs.missing || []).map(k => TYPE_CN[k] || k);
    const admin = me.role === 'admin';
    const notice = (miss.length || !r.count) ? `
      <div class="notice">
        <div><b>关系为什么这么少？</b>
          ${miss.length ? `实体名册还缺：<b>${miss.map(esc).join('、')}</b>。` : ''}
          ${!r.count ? '这篇 md 里没有可识别的实体名。' : ''}
        </div>
        <div class="muted" style="margin-top:6px">
          补法：编辑 <span class="mono">${esc(rs.path || 'entities.yaml')}</span>
          （在部署根目录，按 <span class="mono">projects / orgs / persons / places</span> 分组填名字），
          然后${admin ? '点下面按钮' : '让管理员'}重建实体链接。
          名册里的名字至少 2 个字，长的优先匹配。
        </div>
        ${admin ? `<div class="row" style="margin-top:8px">
          <button id="btnRelink">重建实体链接</button>
          <span class="muted" id="relinkMsg">会给所有已有 md 补 [[实体]]，再重建索引（不重转、不覆盖正文）</span>        </div>` : ''}
      </div>` : '';
    box.innerHTML = notice + `
      <h3>实体（${r.count}）</h3>${ents}
      <h3 style="margin-top:18px">反链（${r.backlink_count}）</h3>
      <p class="muted">正文里写了 [[这一篇]] 的其它文档</p>${bl}
      <h3 style="margin-top:18px">共享实体</h3>
      <p class="muted">也提到同一个实体的文档</p>${shared}`;
    const rb = $('btnRelink');
    if (rb) rb.onclick = async () => {
      if (!confirm('按当前 entities.yaml 给所有已有 md 补 [[实体]]，并重建索引？\n不会重新转换，也不会覆盖正文。')) return;
      $('relinkMsg').textContent = '补链并重建索引中（文档多时要几分钟）…';
      try {
        const out = await api('/meta/relink', { method: 'POST', body: {} });
        $('relinkMsg').textContent = (out.output || '完成').trim().split('\n').join(' · ')
          + (out.warning ? '（' + out.warning + '）' : '');
        relLoadedFor = '';
        await loadRelations(tok);
      } catch (e) { $('relinkMsg').textContent = '失败：' + e.message; }
    };
  } catch (e) {
    box.innerHTML = `<p class="err">关系加载失败：${esc(e.message)}</p>`;
  }
}

function switchDocTab(tab, push = true) {
  document.querySelectorAll('#docTabs button').forEach(b => {
    b.classList.toggle('active', b.dataset.dtab === tab);
  });
  ['overview','source','feishu','index','relations'].forEach(t => {
    const el = $('dtab-' + t);
    if (el) el.classList.toggle('hidden', t !== tab);
  });
  if (push) {
    const tok = $('docDetail').dataset.token;
    if (tok) history.replaceState(null, '', `#/doc/${encodeURIComponent(tok)}/${tab}`);
  }
  if (tab === 'relations') {
    const tok = $('docDetail').dataset.token;
    if (tok) loadRelations(tok);
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

// ---------- 待确认 / 失败 ----------
async function loadIssues() {
  $('issuesSummary').textContent = '加载中…';
  try {
    const d = await api('/issues');
    $('issuesSummary').textContent =
      `待确认 ${d.pending_count} · 已归档残留 ${d.stale_count} · 今日失败 ${d.failure_count} · 错误队列 ${d.error_queue_count}`;
    $('issuePending').innerHTML = (d.pending || []).map(x => `<tr>
      <td>${esc(x.name)}</td>
      <td class="mono">${esc(x.path)}</td>
      <td>${x.status === 'stale'
        ? '<span class="badge muted">已归档残留</span>'
        : '<span class="badge err">待确认</span>'}</td>
      <td>${esc(x.reason || '')}${x.suggested_project
        ? `<div class="muted">建议 ${esc(x.suggested_project)}${x.suggested_reason ? '（' + esc(x.suggested_reason) + '）' : ''}</div>`
        : ''}</td>
      <td>${esc(fmtBytes(x.size))}</td>
      <td>${x.status === 'stale'
        ? `<button class="ghost" data-discard="${esc(x.path)}">清理</button>`
        : `<select data-proj="${esc(x.path)}">${me.projects.map(p =>
            `<option value="${esc(p)}"${p === x.suggested_project ? ' selected' : ''}>${esc(p)}${p === x.suggested_project ? '（建议）' : ''}</option>`).join('')}</select>
           <button data-archive="${esc(x.path)}">归档</button>
           <button class="ghost" data-discard="${esc(x.path)}">丢弃</button>`}</td>
    </tr>`).join('') || '<tr><td colspan="6" class="muted">没有待确认文件</td></tr>';
    $('issueFailures').innerHTML = (d.failures || []).map(x => `<tr>
      <td>${esc(x.ts || '')}</td>
      <td>${x.token
        ? `<a href="#/doc/${encodeURIComponent(x.token)}/overview">${esc(x.file || '?')}</a>`
        : esc(x.file || '?')}</td>
      <td class="err">${esc(x.error || '')}</td>
      <td>${esc(x.status || '')}</td>
    </tr>`).join('') || '<tr><td colspan="4" class="muted">今天没有失败</td></tr>';
    $('issueErrors').innerHTML = (d.error_queue || []).map(x => `<tr>
      <td>${esc(x.ts || '')}</td>
      <td>${esc(x.file || '?')}</td>
      <td class="err">${esc(x.error || '')}</td>
    </tr>`).join('') || '<tr><td colspan="3" class="muted">错误队列为空</td></tr>';
  } catch (e) {
    $('issuesSummary').textContent = '加载失败：' + e.message;
  }
}

// ---------- 回收站 ----------
async function loadTrash() {
  $('trashSummary').textContent = '加载中…';
  try {
    const d = await api('/trash');
    $('trashSummary').textContent = `共 ${d.total} 个文件`;
    $('trashList').innerHTML = d.items.map(x => `<tr>
      <td>${esc(x.name)}</td>
      <td>${x.kind === 'md' ? 'md' : '原件/兜底'}</td>
      <td>${esc(String(x.deleted_at || '').slice(0,16).replace('T',' '))}</td>
      <td>${esc(fmtBytes(x.size))}</td>
      <td class="mono">${esc(x.path)}</td>
      <td>
        <button data-restore="${esc(x.path)}">恢复</button>
        <button class="danger" data-purge="${esc(x.path)}">彻底删除</button>
      </td></tr>`).join('') || '<tr><td colspan="6" class="muted">回收站是空的</td></tr>';
  } catch (e) { $('trashSummary').textContent = '加载失败：' + e.message; }
}

$('btnTrashRefresh').onclick = loadTrash;
$('trashList').onclick = async e => {
  const r = e.target.closest('button[data-restore]');
  const p = e.target.closest('button[data-purge]');
  try {
    if (r) {
      if (!confirm('恢复到原位置？会自动跑一次对账（飞书副本一并恢复）。')) return;
      r.disabled = true;
      const out = await api('/trash/restore', { method: 'POST', body: { path: r.dataset.restore } });
      alert('已恢复到 ' + out.restored_to);
      loadTrash();
    } else if (p) {
      if (!confirm('彻底删除，不可恢复。确认？')) return;
      await api('/trash/purge', { method: 'POST', body: { path: p.dataset.purge } });
      loadTrash();
    }
  } catch (err) { alert('失败：' + err.message); }
};

$('issuePending').onclick = async e => {
  const arch = e.target.closest('button[data-archive]');
  const disc = e.target.closest('button[data-discard]');
  try {
    if (arch) {
      const path = arch.dataset.archive;
      const sel = arch.closest('td').querySelector('select[data-proj]');
      const project = sel ? sel.value : '';
      if (!confirm(`按 ${project} 重新归档「${path.split('/').pop()}」？`)) return;
      arch.disabled = true; arch.textContent = '归档中…';
      const r = await api('/pending/archive', { method: 'POST', body: { path, project } });
      alert('归档完成' + (r.moved_to ? `，兜底副本已移到 ${r.moved_to}` : ''));
      loadIssues();
    } else if (disc) {
      const path = disc.dataset.discard;
      if (!confirm(`丢弃「${path.split('/').pop()}」？会移到 sources/.trash，可找回。`)) return;
      await api('/pending/delete', { method: 'POST', body: { path } });
      loadIssues();
    }
  } catch (err) { alert('失败：' + err.message); }
};

// ---------- 用户 / 审计 ----------
async function loadUsers() {
  const d = await api('/admin/users');
  $('userList').innerHTML = d.items.map(u => `<tr>
    <td>${u.id}</td><td>${esc(u.username)}</td><td>${u.role}</td>
    <td>${u.projects.join(',') || '（无）'}</td><td>${u.enabled ? '启用' : '禁用'}</td>
    <td><button class="ghost" data-uid="${u.id}" data-en="${u.enabled ? 0 : 1}">${u.enabled ? '禁用' : '启用'}</button></td>
    </tr>`).join('');
}

async function loadAudit() {
  const d = await api('/admin/audit?limit=100');
  $('auditList').innerHTML = d.items.map(a => `<tr>
    <td>${esc(a.ts)}</td><td>${esc(a.username)}</td><td>${esc(a.action)}</td>
    <td>${esc(a.project)}</td><td>${esc(a.target)}</td><td>${esc(a.ip)}</td></tr>`).join('');
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
  ['docs','issues','trash','upload','search','generate','users','audit'].forEach(t => {
    const el = $('tab-' + t);
    if (el) el.classList.add('hidden');
  });
  const el = $('tab-' + tab);
  if (el) el.classList.remove('hidden');
  $('docDetail').classList.add('hidden');
  if (tab === 'users') loadUsers();
  if (tab === 'audit') loadAudit();
  if (tab === 'issues') loadIssues();
  if (tab === 'trash') loadTrash();
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
  // 门户告警深链用 `#/pending`（人话）⇒ 落到 admin 的「待确认」页
  const tab = parts[0] === 'pending' ? 'issues' : (parts[0] || 'docs');
  if ((tab === 'issues' || tab === 'trash') && me.role !== 'admin') { setTab('docs', false); return; }
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
$('btnLogout').onclick = () => { localStorage.removeItem('sw_token'); token = ''; me = null; location.reload(); };
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
$('btnIssuesRefresh').onclick = loadIssues;
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

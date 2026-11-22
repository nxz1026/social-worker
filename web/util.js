'use strict';

/* ============================================================
 * 社工管理界面 · 纯工具函数
 * 从 app.js 原样搬出：不碰 DOM、不依赖全局状态，便于用 Node 单测。
 * 加载顺序：index.html 中 util.js 必须排在 app.js 之前。
 * ============================================================ */

/* 活动状态词典：与后端 activity.yaml 的 status 取值保持一致。
 * 此前只映射 imported/generated，reviewed/signed/edited 会落进兜底的
 * 「新目录」，把已审核、已签字的材料显示成没开始处理（审计 PR-04）。*/
const STATUS_TEXT = {
  imported: '已入库',
  generated: '已生成',
  edited: '人工修改',
  reviewed: '已核对',
  signed: '已签字',
};
const STATUS_CLASS = {
  imported: 'info',
  generated: 'ok',
  edited: 'warn',
  reviewed: 'ok',
  signed: 'ok',
};

/* ---------------- 基础工具 ---------------- */
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

const fmtMoney = n => (n == null || isNaN(n) ? '—' : Number(n).toFixed(2));

const todayCn = () => {
  const d = new Date();
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
};

const phKey = p => String(p || '').replace(/^\{\{/, '').replace(/\}\}$/, '').trim();

function projectShort(p) {
  const d = (p && p.data) || {};
  return (d.project && d.project.short) || p.name || '';
}

function statusBadge(status) {
  const raw = String(status ?? '').trim();
  // 空状态：活动目录刚建，确实还没处理
  if (!raw) return '<span class="badge muted">新目录</span>';
  const text = STATUS_TEXT[raw];
  // 未知状态照实显示原值，不伪装成有业务含义的「新目录」
  if (!text) return `<span class="badge muted">未知状态（${esc(raw)}）</span>`;
  const cls = STATUS_CLASS[raw] || 'info';
  return `<span class="badge ${cls}">${esc(text)}</span>`;
}

function checkBadge(check) {
  if (!check) return '<span class="badge muted">—</span>';
  if (check.complete) return '<span class="badge ok">完整</span>';
  const missing = Number(check.missing_count || 0);
  const partial = Number(check.partial_count || 0);
  if (missing > 0) return `<span class="badge warn">缺 ${missing} 项</span>`;
  if (partial > 0) return `<span class="badge warn">部分不完整（${partial} 项待补）</span>`;
  return '<span class="badge warn">未完整</span>';
}

/* 项目级没有「一个编号」：编号是按活动生成的（CDYJR{立项年}{立项月:03d}-C-{类型}{活动年}{月}{流水}）。
 * 项目这一格只展示立项年月规则，不再显示误导性的「未生成」（E2E-3 发现）。 */
function projectCodeHint(p) {
  const cr = (p && p.code_rules) || {};
  const m = cr.establish_month;
  if (!m) return '未配置立项月';
  return `CDYJR<立项年>${String(m).padStart(3, '0')}-C-…（活动级编号）`;
}

function reviewText(it) {
  if (!it.review) return '';
  const date = String(it.review.reviewed_at || '').slice(0, 10);
  const by = it.review.reviewed_by || '—';
  return it.signed ? `✓✓ 已签字（${esc(by)} ${esc(date)}）`
                   : `✓ ${esc(by)} ${esc(date)}`;
}

function checkSummary(check) {
  const verdict = check.complete ? '<span class="badge ok">完整</span>' : '<span class="badge warn">未完整</span>';
  return `齐 ${check.ok_count} 项 · 待补 ${check.partial_count || 0} 项 · 缺 ${check.missing_count} 项 · 不适用 ${check.na_count} 项 ${verdict}`;
}

function reviewSummary(check) {
  /* 计数一律用后端返回的值：前端自己 filter 会与 deliverable 判据打架。
   * 原来前端数「必需项里有 review 的」，后端数「所有有 review 的」，
   * 于是出现「已核对 13/14」旁边挂绿色「可交付」的矛盾（审计 R-03）。*/
  const required = (check.items || []).filter(it => it.status !== 'na');
  const done = check.reviewed_required_count != null
    ? check.reviewed_required_count
    : required.filter(it => it.review).length;
  const total = check.required_count != null ? check.required_count : required.length;
  const signed = (check.items || []).filter(it => it.signed).length;
  const deliv = check.deliverable ? '<span class="badge ok">可交付</span>'
                                 : '<span class="badge muted">未齐备</span>';
  return `已核对 ${done}/${total} · 已签字 ${signed} ${deliv}`;
}

// 本地日期（toISOString 是 UTC，跨时区会差一天）
const todayIso = () => {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
};

function activityNameFromDir(dirname) {
  const raw = String(dirname || '').replace(/^\d+[-_]/, '');
  const m = raw.match(/^(.*?)[-_]第(.+期)$/);
  return m ? `${m[1]}（第${m[2]}）` : raw;
}

function pickTemplate(tpls, task) {
  const want = task === 'brief' ? ['简报'] : task === 'plan' ? ['计划书'] : ['总结报告'];
  return tpls.find(t => want.some(w => t.name.includes(w))) || tpls[0] || null;
}

const SAT_GRADE = avg => {
  if (avg == null || isNaN(avg)) return '';
  if (avg >= 90) return '优秀';
  if (avg >= 80) return '良好';
  if (avg >= 70) return '一般';
  if (avg >= 60) return '合格';
  return '不合格';
};

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

function permText(projects) {
  const parts = Object.entries(projects || {})
    .map(([name, access]) => `${name}：${access === 'write' ? '读写' : '只读'}`);
  return parts.join('；') || '—';
}

function maskSecret(secret) {
  return String(secret || '').slice(0, 12) + '••••••••';
}

/* ---------------- 挂载到全局 ---------------- */
/* app.js 是经典脚本，直接用全局名调用；这里显式挂到 globalThis，
 * 既保证浏览器里 app.js 能取到，也让 Node 单测 require 后能访问。 */
globalThis.STATUS_TEXT = STATUS_TEXT;
globalThis.STATUS_CLASS = STATUS_CLASS;
globalThis.esc = esc;
globalThis.fmtBytes = fmtBytes;
globalThis.fmtTime = fmtTime;
globalThis.fmtMoney = fmtMoney;
globalThis.todayCn = todayCn;
globalThis.phKey = phKey;
globalThis.projectShort = projectShort;
globalThis.statusBadge = statusBadge;
globalThis.checkBadge = checkBadge;
globalThis.projectCodeHint = projectCodeHint;
globalThis.reviewText = reviewText;
globalThis.checkSummary = checkSummary;
globalThis.reviewSummary = reviewSummary;
globalThis.todayIso = todayIso;
globalThis.activityNameFromDir = activityNameFromDir;
globalThis.pickTemplate = pickTemplate;
globalThis.SAT_GRADE = SAT_GRADE;
globalThis.parseSearchOutput = parseSearchOutput;
globalThis.permText = permText;
globalThis.maskSecret = maskSecret;

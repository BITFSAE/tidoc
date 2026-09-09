/* tidoc 前端主逻辑 — 整理 / 筛选 / 便捷操作。 */

const State = {
  profiles: [],
  profilesLoaded: false,
  profileById: {},
  currentProfileId: null,
  activeTitle: '',
  titleOptions: [],
  titleProfiles: [{ name: '北京理工大学' }, { name: '北京理工大学教育基金会' }], // 启动时由后端覆盖
  quickView: 'all',
  entries: [],
  selected: new Set(),
  lastSelectedId: null,
  focusedEntryId: null,
  suppressListAnimation: false,
  density: 'comfortable',
  themeMode: document.documentElement.dataset.themeMode || 'system',
  groupBy: 'none',       // 'none' | 'profile' | 'title' —— 列表分组浏览
  tagFilter: '',         // 工具栏筛选：按标签
  notesFilter: '',       // 高级筛选：'' | 'yes' | 'no'（有 / 无记账备注）
  paymentCountFilter: '', // 高级筛选：'' | 'multiple'（多张付款截图）
  batchFilter: '',       // 当前聚焦的批次 id；'unbatched' 未进批次；'archived' 已归档
  batches: [],           // 批次列表缓存
  unbatchedCount: 0,     // 未进任何批次的条目数
  currentBatch: null,    // 当前批次详情（含批次级催办备注）
  allTags: [],           // 全库用过的标签
  multiClaimantMode: false,
  paymentOcrEnabled: true,
  defaultPaidToInvoice: true,
  defaultEntryTitle: '',
  materialRequirements: {
    invoice: true,
    payment_screenshot: true,
    physical_image: false,
    inspection_pdf: true,
    paid_amount: true,
  },
  verificationWatchDirectory: '',
  verificationTrashSource: false,
  activeDetailEntryId: null,
  updateStatus: null,
  ocrStatus: null,      // 阿里云 OCR：组件安装 + 密钥配置状态（本地检查，不联网）
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const CLOSE_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';

function toast(msg, kind) {
  const t = el('div', 'toast' + (kind ? ' ' + kind : ''), esc(msg));
  $('#toastRoot').appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .25s'; setTimeout(() => t.remove(), 280); }, 2600);
}

function taskProgress(message) {
  const node = el('div', 'toast working', `
    <span class="task-spinner" aria-hidden="true"></span>
    <span data-task-progress>${esc(message)}</span>`);
  node.setAttribute('role', 'status');
  $('#toastRoot').appendChild(node);
  let closed = false;
  return {
    update(next) {
      if (closed) return;
      const text = node.querySelector('[data-task-progress]');
      if (text) text.textContent = next;
    },
    close() {
      if (closed) return;
      closed = true;
      node.remove();
    },
  };
}

const TITLE_CLASS = { '北京理工大学': 'univ', '北京理工大学教育基金会': 'found' };
const TITLE_SHORT = { '北京理工大学': '北京理工大学', '北京理工大学教育基金会': '教育基金会' };
const BUILTIN_TITLES = ['北京理工大学', '北京理工大学教育基金会'];

function configuredTitleNames() {
  // 设置里维护的报账抬头（多学校可用）；为空时列表只显示条目里出现过的抬头。
  return State.titleProfiles.map((p) => p.name).filter(Boolean);
}
const BILIBILI_GUIDE_URL = 'https://www.bilibili.com/video/BV1XN3q69EPi/';
const DOC_GUIDE_URL = 'https://www.bitfsae.com/news/tidoc-guide';
const STATUS_LABEL = { draft: '草稿', partial: '部分材料', complete: '完整' };
const CHECK_LABEL = { pass: '校验通过', warning: '识别提醒', blocked: '严重问题' };
const USAGE_GUIDE_SEEN_KEY = 'tidoc.usageGuide.seen.v2';
const MULTI_CLAIMANT_KEY = 'tidoc.multiClaimantMode';
const PAYMENT_OCR_KEY = 'tidoc.paymentScreenshotOcr';
const DEFAULT_PAID_TO_INVOICE_KEY = 'tidoc.defaultPaidToInvoiceTotal';
const DEFAULT_ENTRY_TITLE_KEY = 'tidoc.defaultEntryTitle';
const THEME_KEY = 'tidoc.themeMode';
const DEFAULT_MATERIAL_REQUIREMENTS = {
  invoice: true,
  payment_screenshot: true,
  physical_image: false,
  inspection_pdf: true,
  paid_amount: true,
};
const BINDLE_INCLUDE_NOTES_KEY = 'tidoc.bindle.includeNotes';
const BINDLE_INCLUDE_TAGS_KEY = 'tidoc.bindle.includeTags';
const VERIFICATION_WATCH_DIR_KEY = 'tidoc.invoiceVerification.watchDirectory';
const AUTO_UPDATE_KEY = 'tidoc.update.autoCheck';
const OPERATOR_PREF_KEYS = {
  name: 'tidoc.operator.name',
  student_id: 'tidoc.operator.student_id',
  contact: 'tidoc.operator.contact',
  bank_name: 'tidoc.operator.bank_name',
  bank_card: 'tidoc.operator.bank_card',
};

const systemThemeMedia = window.matchMedia('(prefers-color-scheme: dark)');

function normalizeThemeMode(value) {
  return value === 'light' || value === 'dark' || value === 'system' ? value : 'system';
}

function applyTheme(mode, { persist = false } = {}) {
  const nextMode = normalizeThemeMode(mode);
  const resolved = nextMode === 'dark' || (nextMode === 'system' && systemThemeMedia.matches)
    ? 'dark'
    : 'light';
  State.themeMode = nextMode;
  document.documentElement.dataset.themeMode = nextMode;
  document.documentElement.dataset.theme = resolved;
  document.documentElement.style.colorScheme = resolved;
  syncThemeToggle();
  if (persist) {
    try { localStorage.setItem(THEME_KEY, nextMode); } catch (e) {}
  }
}

async function animateThemeChange(mode, trigger, options = {}) {
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduceMotion || !document.body) {
    applyTheme(mode, options);
    return;
  }

  const root = document.documentElement;
  if (typeof document.startViewTransition === 'function') {
    const rect = trigger?.getBoundingClientRect();
    const x = rect ? rect.left + rect.width / 2 : window.innerWidth / 2;
    const y = rect ? rect.top + rect.height / 2 : window.innerHeight / 2;
    const radius = Math.hypot(
      Math.max(x, window.innerWidth - x),
      Math.max(y, window.innerHeight - y),
    );
    root.style.setProperty('--theme-origin-x', `${x}px`);
    root.style.setProperty('--theme-origin-y', `${y}px`);
    root.style.setProperty('--theme-reveal-radius', `${Math.ceil(radius)}px`);
    try {
      const transition = document.startViewTransition(() => applyTheme(mode, options));
      await transition.updateCallbackDone;
      return;
    } catch (e) {}
  }

  root.classList.add('theme-transition');
  document.body.offsetWidth;
  applyTheme(mode, options);
  window.setTimeout(() => root.classList.remove('theme-transition'), 280);
}

function syncThemeToggle() {
  const button = $('#themeToggle');
  if (!button) return;
  const dark = document.documentElement.dataset.theme === 'dark';
  const label = dark ? '切换到浅色模式' : '切换到深色模式';
  button.title = label;
  button.setAttribute('aria-label', label);
}

async function toggleTheme() {
  const button = $('#themeToggle');
  const previousMode = State.themeMode;
  const nextMode = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  button.disabled = true;
  await animateThemeChange(nextMode, button, { persist: true });
  try {
    await Api.setAppPreference(THEME_KEY, nextMode);
  } catch (e) {
    applyTheme(previousMode, { persist: true });
    toast(e.message, 'err');
  } finally {
    button.disabled = false;
  }
}

function handleSystemThemeChange() {
  if (State.themeMode === 'system') applyTheme('system');
}

if (systemThemeMedia.addEventListener) systemThemeMedia.addEventListener('change', handleSystemThemeChange);
else if (systemThemeMedia.addListener) systemThemeMedia.addListener(handleSystemThemeChange);
const FIELD_LABEL = {
  paid_amount: '实付金额', actual_item_name: '实际物资名称', notes: '备注',
  invoice_no: '发票号码', total: '价税合计', buyer_name: '购买方抬头',
  buyer_tax_id: '税号', title: '抬头',
};
const OCR_FIELD_LABEL = {
  invoice_no: '发票号码', invoice_date: '发票日期', seller: '销售方',
  total: '价税合计', buyer_name: '购买方抬头', buyer_tax_id: '购买方税号',
  items: '物品明细',
};
const OCR_CONSOLE_URL = 'https://ocr.console.aliyun.com/overview';
const OCR_QUOTA_NOTE = '每个阿里云账号每月有免费额度，超出后按量计费。免费额度与费用可在阿里云 OCR 控制台查看。';

function fmtMoney(v) {
  if (v == null || v === '') return '—';
  const n = Number(v);
  return isNaN(n) ? v : '¥' + n.toFixed(2);
}

function fmtQuantity(v) {
  if (v == null || v === '') return '—';
  const text = String(v).trim();
  if (!/^-?\d+(\.\d+)?$/.test(text)) return text;
  // 只对带小数点的数值去尾零，整数（10、100…）原样展示
  if (!text.includes('.')) return text;
  return text.replace(/0+$/, '').replace(/\.$/, '') || '0';
}
function initials(name) {
  if (!name) return '—';
  return Array.from(name).slice(0, 2).join('');
}
function baseName(p) { return String(p).split(/[/\\]/).pop(); }
function dirName(p) {
  const s = String(p || '');
  const idx = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
  return idx > 0 ? s.slice(0, idx) : s;
}
function fmtBytes(value) {
  const size = Number(value || 0);
  if (!size) return '';
  if (size < 1024 * 1024) return `${Math.max(1, Math.round(size / 1024))} KB`;
  return `${(size / 1024 / 1024).toFixed(size >= 10 * 1024 * 1024 ? 0 : 1)} MB`;
}
function fmtCheckTime(value) {
  if (!value) return '';
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
function dateShort(s) {
  if (!s) return '无日期';
  return s.length > 10 ? s.slice(0, 10) : s;
}

function filenameTimestamp(date = new Date()) {
  const pad = (value) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}-${pad(date.getHours())}-${pad(date.getMinutes())}`;
}

// inline SVG icons (no emoji)
const I = {
  pencil: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="m4 20 4-1 11-11-3-3L5 16l-1 4z"/><path d="m14 5 3 3"/></svg>',
  note: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h14v16H5z"/><path d="M8 9h8M8 13h6M8 17h4"/></svg>',
  box: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 7v10l9 4M21 7v10l-9 4M12 11v10"/></svg>',
  pdf: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4"/><text x="9" y="16" font-size="6" fill="currentColor" stroke="none" font-family="JetBrains Mono, monospace">PDF</text></svg>',
  xml: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4"/><path d="m9 12-2 2 2 2M13 12l2 2-2 2" stroke-width="1.5"/></svg>',
  image: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="16" height="14" rx="2"/><circle cx="9" cy="10" r="1.4"/><path d="m4 17 5-4 4 3 3-2 4 4"/></svg>',
  inspect: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="6"/><path d="m20 20-3.5-3.5M8 11h6"/></svg>',
  search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3-3"/></svg>',
  lock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
  github: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2C6.48 2 2 6.58 2 12.23c0 4.52 2.87 8.35 6.84 9.71.5.1.68-.22.68-.49 0-.24-.01-1.05-.01-1.9-2.78.62-3.37-1.21-3.37-1.21-.45-1.18-1.11-1.49-1.11-1.49-.91-.64.07-.63.07-.63 1 .08 1.53 1.06 1.53 1.06.9 1.57 2.35 1.12 2.92.85.09-.66.35-1.12.64-1.37-2.22-.26-4.56-1.14-4.56-5.06 0-1.12.39-2.03 1.03-2.75-.1-.26-.45-1.3.1-2.71 0 0 .84-.28 2.75 1.05A9.3 9.3 0 0 1 12 6.95a9.3 9.3 0 0 1 2.5.35c1.91-1.33 2.75-1.05 2.75-1.05.55 1.41.2 2.45.1 2.71.64.72 1.03 1.63 1.03 2.75 0 3.93-2.34 4.8-4.57 5.05.36.32.68.94.68 1.9 0 1.37-.01 2.48-.01 2.82 0 .27.18.59.69.49A10.24 10.24 0 0 0 22 12.23C22 6.58 17.52 2 12 2Z"/></svg>',
  bilibili: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m8 3 3 3M16 3l-3 3"/><rect x="3" y="6" width="18" height="14" rx="3"/><path d="M8 12v2M16 12v2M9 17h6"/></svg>',
  doc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 3h8l4 4v14H7z"/><path d="M15 3v4h4"/><path d="M10 12h6M10 16h6"/></svg>',
};
function iconPencil(s) { return wrapSvg(I.pencil, s); }
function iconNote(s) { return wrapSvg(I.note, s); }
function iconBox(s) { return wrapSvg(I.box, s); }
function iconPdf() { return I.pdf; }
function iconXml() { return I.xml; }
function iconImage() { return I.image; }
function iconInspect() { return I.inspect; }
function wrapSvg(svg, s) {
  return svg.replace('<svg ', `<svg width="${s}" height="${s}" `);
}

// ------------------------------------------------------------------ 初始化
async function init() {
  setupFastTooltips();
  applyPreferences();
  bindEvents();
  let startupUpdate = null;
  try { startupUpdate = await Api.startupUpdateState(); } catch (e) {}
  refreshOcrStatus();
  await loadWorkflowPreferences();
  await loadProfiles();
  await loadBatches();
  await refreshEntries();
  await refreshTagOptions();
  showSearchHintIfEmpty();
  try {
    const launch = await Api.takeLaunchFile();
    if (launch?.path) await importBindleFlow(launch.path);
  } catch (e) { toast(e.message, 'err'); }
  if (startupUpdate?.upgraded) setTimeout(() => { openReleaseHighlights('updated', startupUpdate); }, 450);
  else await maybeShowFirstUseGuide();
  setTimeout(() => { maybeAutoCheckUpdates(); }, 1200);
}

function refreshOcrStatus() {
  Api.ocrComponentStatus()
    .then((status) => { State.ocrStatus = status; applyOcrUiVisibility(); updateOcrSuggestBar(); })
    .catch(() => { State.ocrStatus = null; applyOcrUiVisibility(); updateOcrSuggestBar(); });
}

function applyOcrUiVisibility() {
  const enabled = !!State.ocrStatus?.available;
  document.querySelectorAll('[data-ocr-ui]').forEach((el) => {
    el.classList.toggle('hidden', !enabled);
  });
}

function ocrReady() {
  return !!(State.ocrStatus?.available && State.ocrStatus?.credentials_configured);
}

function setupFastTooltips() {
  const tooltip = el('div', 'fast-tooltip');
  tooltip.setAttribute('role', 'tooltip');
  document.body.appendChild(tooltip);

  const prepare = (root) => {
    const nodes = [];
    if (root.nodeType === Node.ELEMENT_NODE && root.hasAttribute('title')) nodes.push(root);
    if (root.querySelectorAll) nodes.push(...root.querySelectorAll('[title]'));
    nodes.forEach((node) => {
      const value = node.getAttribute('title');
      if (!value) return;
      node.dataset.tooltip = value;
      node.removeAttribute('title');
      if (!node.getAttribute('aria-label') && !node.textContent.trim()) node.setAttribute('aria-label', value);
    });
  };
  prepare(document.body);
  new MutationObserver((records) => {
    records.forEach((record) => {
      if (record.type === 'attributes') prepare(record.target);
      else record.addedNodes.forEach(prepare);
    });
    if (active && !active.isConnected) hide();
  }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['title'] });

  let active = null;
  const hide = () => {
    active = null;
    tooltip.classList.remove('visible');
  };
  const show = (target) => {
    const overflowText = target?.dataset.tooltipOverflow;
    if (overflowText && target.scrollWidth <= target.clientWidth && target.scrollHeight <= target.clientHeight) {
      hide();
      return;
    }
    const text = target?.dataset.tooltip || overflowText;
    if (!text) {
      hide();
      return;
    }
    active = target;
    tooltip.textContent = text;
    tooltip.classList.add('visible');
    const rect = target.getBoundingClientRect();
    const tip = tooltip.getBoundingClientRect();
    const left = Math.max(8, Math.min(rect.left + rect.width / 2 - tip.width / 2, window.innerWidth - tip.width - 8));
    const below = rect.bottom + 7;
    const top = below + tip.height <= window.innerHeight - 8 ? below : Math.max(8, rect.top - tip.height - 7);
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  };
  const tooltipTarget = (node) => node?.closest?.('[data-tooltip], [data-tooltip-overflow]');
  document.addEventListener('pointerover', (ev) => {
    const target = tooltipTarget(ev.target);
    if (target && target !== active) show(target);
  });
  document.addEventListener('pointerout', (ev) => {
    const next = tooltipTarget(ev.relatedTarget);
    if (next !== active) next ? show(next) : hide();
  });
  document.addEventListener('focusin', (ev) => show(tooltipTarget(ev.target)));
  document.addEventListener('focusout', hide);
  document.addEventListener('scroll', hide, true);
  window.addEventListener('blur', hide);
}

async function maybeAutoCheckUpdates(showCurrent = false) {
  try {
    const status = await Api.autoCheckUpdates();
    if (status.reason === 'disabled') {
      setUpdateNotice(null);
      return;
    }
    setUpdateNotice(status);
    const available = (status.updates || []).filter((item) => item.available);
    if ((status.checked || showCurrent) && available.length) {
      const core = available.find((item) => item.component === 'core');
      if (core) await maybeShowAvailableUpdate(core);
      else toast('发现可用组件更新', 'ok');
    } else if (status.checked && showCurrent) {
      toast('已是最新版本', 'ok');
    }
  } catch (e) {
    if (showCurrent) toast(e.message, 'err');
  }
}

async function maybeShowAvailableUpdate(core) {
  const key = `tidoc.update.availableNotice.${core.latest_version}`;
  try {
    if (await Api.appPreference(key, '')) return;
  } catch (e) {}
  const showWhenFree = (attempt = 0) => {
    if ($('#modalRoot').lastChild) {
      if (attempt < 12) setTimeout(() => showWhenFree(attempt + 1), 800);
      return;
    }
    openReleaseHighlights('available', {
      current_version: core.current_version,
      latest_version: core.latest_version,
      notes: core.asset?.notes || [],
      seen_key: key,
    });
  };
  showWhenFree();
}

function openReleaseHighlights(mode, data) {
  const available = mode === 'available';
  const version = available ? data.latest_version : data.current_version;
  const rawNotes = data.notes || [];
  const notes = Array.isArray(rawNotes) ? rawNotes : [rawNotes];
  const seen = () => {
    if (data.seen_key) Api.setAppPreference(data.seen_key, '1').catch(() => {});
  };
  const body = el('div', 'release-highlights');
  const previousVersion = available ? data.current_version : data.previous_version;
  const versionLine = previousVersion
    ? `<span>v${esc(previousVersion)}</span><i aria-hidden="true">→</i><strong>v${esc(version || '')}</strong>`
    : `<strong>v${esc(version || '')}</strong>`;
  body.innerHTML = `
    <div class="release-version-line">
      <span class="release-version-label">${available ? '可更新版本' : '已更新版本'}</span>
      <div>${versionLine}</div>
    </div>
    <div class="release-change-list">
      <b>本次更新</b>
      ${notes.length
        ? `<ul>${notes.slice(0, 6).map((note) => `<li>${esc(note)}</li>`).join('')}</ul>`
        : '<p>此版本没有附加更新说明。</p>'}
    </div>
    <details class="release-guide">
      <summary><span><b>使用指南</b><small>从导入发票到整理、打印的完整流程</small></span><em>6 步</em></summary>
      <div class="guide-steps">${usageGuideStepsMarkup()}</div>
    </details>`;
  let m;
  const close = () => { seen(); m.close(); };
  const footer = available
    ? [
        mkBtn('稍后', 'ghost', close),
        mkBtn('前往更新', 'primary', () => { seen(); m.close(); openUpdateDialog(); }),
      ]
    : [mkBtn('继续使用', 'primary', close)];
  m = modal({
    title: available ? '发现新版本' : '更新完成',
    body, wide: true,
    footer,
    onClose: seen,
  });
}

function setUpdateNotice(status) {
  State.updateStatus = status;
  const available = (status?.updates || []).filter((item) => item.available);
  const btn = $('#settingsBtn');
  if (!btn) return;
  btn.classList.toggle('has-update', available.length > 0);
  btn.title = available.length ? `设置 · ${available.length} 项可更新` : '设置';
}

async function loadWorkflowPreferences() {
  const local = localStorage.getItem(MULTI_CLAIMANT_KEY);
  const localTheme = normalizeThemeMode(localStorage.getItem(THEME_KEY));
  const legacyVerificationWatch = localStorage.getItem(VERIFICATION_WATCH_DIR_KEY) || '';
  const localDefaultEntryTitle = localStorage.getItem(DEFAULT_ENTRY_TITLE_KEY) || '';
  State.multiClaimantMode = local === '1';
  try {
    const [multiMode, paymentOcr, defaultPaidToInvoice, defaultEntryTitle, themeMode, materialRequirements, verification, titleProfiles] = await Promise.all([
      Api.appPreference(MULTI_CLAIMANT_KEY, local || ''),
      Api.appPreference(PAYMENT_OCR_KEY, '1'),
      Api.appPreference(DEFAULT_PAID_TO_INVOICE_KEY, '1'),
      Api.appPreference(DEFAULT_ENTRY_TITLE_KEY, localDefaultEntryTitle),
      Api.appPreference(THEME_KEY, localTheme),
      Api.materialRequirements(),
      Api.invoiceVerificationPreferences(),
      Api.titleProfiles(),
    ]);
    State.titleProfiles = Array.isArray(titleProfiles?.profiles) && titleProfiles.profiles.length
      ? titleProfiles.profiles
      : [];
    State.multiClaimantMode = multiMode === '1';
    State.paymentOcrEnabled = paymentOcr !== '0';
    State.defaultPaidToInvoice = defaultPaidToInvoice !== '0';
    State.defaultEntryTitle = defaultEntryTitle || localDefaultEntryTitle || '';
    applyTheme(themeMode, { persist: true });
    State.materialRequirements = {
      ...DEFAULT_MATERIAL_REQUIREMENTS,
      ...(materialRequirements || {}),
      invoice: true,
    };
    State.verificationWatchDirectory = verification.watch_directory || '';
    State.verificationTrashSource = !!verification.trash_source_after_archive;
    if (!State.verificationWatchDirectory && legacyVerificationWatch) {
      const migrated = await Api.setInvoiceVerificationPreferences({
        watch_directory: legacyVerificationWatch,
      });
      State.verificationWatchDirectory = migrated.watch_directory || '';
    }
    if (legacyVerificationWatch) localStorage.removeItem(VERIFICATION_WATCH_DIR_KEY);
    if (multiMode) localStorage.setItem(MULTI_CLAIMANT_KEY, multiMode);
    if (State.defaultEntryTitle) localStorage.setItem(DEFAULT_ENTRY_TITLE_KEY, State.defaultEntryTitle);
  } catch (e) {}
}

// 启动时套用用户在设置里选的默认抬头 / 密度
function applyPreferences() {
  applyTheme(localStorage.getItem(THEME_KEY));
  const dt = localStorage.getItem('tidoc.defaultTitle') || '';
  if (dt) {
    State.activeTitle = dt;
    const titleSel = $('#filterTitle');
    if (titleSel) titleSel.value = dt;
  }
  const dd = localStorage.getItem('tidoc.defaultDensity');
  if (dd) {
    State.density = dd;
    $$('.density-btn').forEach((b) => b.classList.toggle('active', b.dataset.density === dd));
  }
}

async function refreshTagOptions() {
  try { State.allTags = await Api.listTags(); } catch (e) { State.allTags = []; }
  const sel = $('#filterTag');
  if (!sel) return;
  const cur = State.tagFilter;
  sel.innerHTML = '<option value="">全部</option>' +
    State.allTags.map((t) => `<option value="${esc(t)}"${t === cur ? ' selected' : ''}>${esc(t)}</option>`).join('');
}

async function refreshTitleOptions() {
  const sel = $('#filterTitle');
  if (!sel) return;
  const current = State.activeTitle || sel.value || '';
  try {
    const usedTitles = await Api.listTitles();
    const configured = configuredTitleNames();
    const customTitles = usedTitles.filter((title) => !configured.includes(title));
    const titles = [...configured, ...customTitles];
    State.titleOptions = titles;
    sel.innerHTML = '<option value="">全部</option>' + titles.map((title) =>
      `<option value="${esc(title)}">${esc(TITLE_SHORT[title] || title)}</option>`
    ).join('');
    const available = !current || titles.includes(current);
    sel.value = available ? current : '';
    if (!available) State.activeTitle = '';
    const chip = sel.closest('.title-filter-chip');
    chip?.classList.toggle('has-custom-title', customTitles.length > 0);
  } catch (e) {
    // 抬头选项刷新失败不阻断条目列表，保留当前静态选项。
  }
}

function knownTitleValues() {
  return [...new Set([
    ...configuredTitleNames(),
    ...State.titleOptions,
    ...State.entries.map((entry) => entry.title).filter(Boolean),
    State.defaultEntryTitle,
  ].filter(Boolean))];
}

function titleChoiceOptions(selected = '', autoLabel = '自动识别') {
  const titles = [...new Set([...knownTitleValues(), selected].filter(Boolean))];
  return `<option value="">${esc(autoLabel)}</option>` + titles.map((title) =>
    `<option value="${esc(title)}"${title === selected ? ' selected' : ''}>${esc(TITLE_SHORT[title] || title)}</option>`
  ).join('');
}

async function loadProfiles() {
  const previousCount = State.profiles.length;
  const hadLoaded = State.profilesLoaded;
  State.profiles = await Api.listProfiles();
  State.profilesLoaded = true;
  State.profileById = Object.fromEntries(State.profiles.map((p) => [p.id, p]));
  if (hadLoaded && previousCount < 2 && State.profiles.length >= 2 && !State.multiClaimantMode) {
    State.multiClaimantMode = true;
    localStorage.setItem(MULTI_CLAIMANT_KEY, '1');
    try { await Api.setAppPreference(MULTI_CLAIMANT_KEY, '1'); } catch (e) {}
    toast('已自动开启代填模式', 'ok');
  }
  renderProfileSelects();
  if (!State.profiles.length) {
    openProfileManager(true);
    return;
  }
  const def = State.profiles.find((p) => p.is_default) || State.profiles[0];
  State.currentProfileId = def.id;
  renderProfilePill();
}

function renderProfilePill() {
  if (!$('#profileName') || !$('#profileReviewer') || !$('#profileAvatar')) return;
  const p = State.profileById[State.currentProfileId];
  if (!p) {
    $('#profileName').textContent = '未选择';
    $('#profileReviewer').textContent = '';
    $('#profileAvatar').textContent = '—';
    return;
  }
  $('#profileName').textContent = p.name;
  $('#profileReviewer').textContent = '审核 · ' + p.reviewer + (p.is_default ? ' · 默认' : '');
  $('#profileAvatar').textContent = initials(p.name);
}

function renderProfileSelects() {
  const sel = $('#filterProfile');
  sel.innerHTML = '<option value="">全部</option>';
  State.profiles.forEach((p) => {
    const o = el('option');
    o.value = p.id; o.textContent = p.name;
    sel.appendChild(o);
  });
}

function profileOptionsHtml(selectedId) {
  const selected = selectedId || State.currentProfileId || State.profiles[0]?.id || '';
  return State.profiles.map((p) =>
    `<option value="${esc(p.id)}"${p.id === selected ? ' selected' : ''}>${esc(p.name)} · ${esc(p.reviewer)}</option>`
  ).join('');
}

function selectedClaimantId(root) {
  return root.querySelector('[data-claimant-select]')?.value || State.currentProfileId || State.profiles[0]?.id || '';
}

function claimantConfirmHtml() {
  if (!State.multiClaimantMode || State.profiles.length <= 1) return '';
  return `
    <div class="form-row claimant-row">
      <label>报账人</label>
      <select data-claimant-select>${profileOptionsHtml(State.profiles[0]?.id)}</select>
    </div>`;
}

// ------------------------------------------------------------------ 筛选
const UNBATCHED_BATCH_ID = 'unbatched';
const ARCHIVED_BATCH_ID = 'archived';
const inUnbatchedView = () => State.batchFilter === UNBATCHED_BATCH_ID;
const inArchivedView = () => State.batchFilter === ARCHIVED_BATCH_ID;
const activeBatchId = () => State.batchFilter || '';
const actualBatchId = () => (
  inUnbatchedView() || inArchivedView() ? '' : activeBatchId()
);
const focusedArchivedBatch = () => State.batches.find(
  (batch) => batch.id === State.batchFilter && batch.archived
) || null;
const inArchivedShelf = () => inArchivedView() || !!focusedArchivedBatch();

function currentFilters() {
  const val = (id) => $('#' + id)?.value || '';
  State.activeTitle = val('filterTitle');
  const f = { title: State.activeTitle || undefined };
  const status = val('filterStatus');
  const check = val('filterCheck');
  const profile = val('filterProfile');
  const kw = val('filterKeyword').trim();
  const amin = val('filterAmountMin');
  const amax = val('filterAmountMax');
  const dfrom = val('filterDateFrom');
  const dto = val('filterDateTo');
  const sort = val('sortSelect');

  if (State.quickView === 'warning') f.check_status = 'warning';
  else if (State.quickView === 'modified') f.modified_only = true;
  else if (State.quickView === 'complete') f.status = 'complete';
  else if (State.quickView === 'ocr') f.ocr_recognized = true;
  else if (State.quickView === 'ocr_pending') f.ocr_pending = true;

  if (status) f.status = status;
  if (check) f.check_status = check;
  if (profile) f.profile_id = profile;
  if (kw) f.keyword = kw;
  if (amin) f.amount_min = amin;
  if (amax) f.amount_max = amax;
  if (dfrom) f.date_from = dfrom;
  if (dto) f.date_to = dto;
  if (sort) f.sort = sort;
  if (State.tagFilter) f.tags = [State.tagFilter];
  if (State.notesFilter) f.has_notes = State.notesFilter;
  if (State.paymentCountFilter) f.payment_count = State.paymentCountFilter;
  if (inUnbatchedView()) f.unbatched = true;
  else if (inArchivedView()) f.archived_only = true;
  else if (State.batchFilter) {
    // 聚焦具体批次时查看该批次完整成员；全局“已归档”才限定为只属于已归档批次的条目。
    f.batch_id = State.batchFilter;
  } else f.active_only = true;
  return f;
}

async function refreshEntries() {
  try {
    await refreshTitleOptions();
    State.currentBatch = actualBatchId() ? await Api.getBatch(actualBatchId()) : null;
    State.entries = await Api.listEntries(currentFilters());
    if (State.quickView === 'incomplete') {
      State.entries = State.entries.filter((e) => (e.completeness?.status || e.status) !== 'complete');
    }
  } catch (e) {
    toast(e.message, 'err');
    State.entries = [];
  }
  renderEntries();
  syncFilterControlStates();
}

// ------------------------------------------------------------------ 渲染列表
function renderEntries() {
  const list = $('#entryList');
  const activeCard = document.activeElement?.closest?.('.entry-card');
  const restoreFocusId = activeCard?.dataset.entryId || null;
  list.dataset.density = State.density;
  list.classList.toggle('no-anim', State.suppressListAnimation);
  list.innerHTML = '';
  const empty = $('#emptyState');
  const emptyAll = State.entries.length === 0;

  if (State.groupBy !== 'none' && !emptyAll) {
    renderGroupedEntries(list);
  } else {
    State.entries.forEach((e) => list.appendChild(entryCard(e)));
  }

  updateListSummary();
  updateOcrSuggestBar();

  empty.hidden = !emptyAll;
  if (emptyAll) renderEmptyState();
  updateSelectionBar();
  if (!State.entries.some((entry) => entry.id === State.focusedEntryId)) {
    State.focusedEntryId = State.entries[0]?.id || null;
  }
  $$('.entry-card', list).forEach((card) => {
    card.tabIndex = card.dataset.entryId === State.focusedEntryId ? 0 : -1;
  });
  if (restoreFocusId) focusEntryCard(restoreFocusId, false);
  State.suppressListAnimation = false;
}

function updateSelectionBar() {
  const hasSelection = State.selected.size > 0;
  const allSelected = State.entries.length > 0 && State.entries.every((entry) => State.selected.has(entry.id));
  const reparseBtn = $('#batchReparseBtn');
  reparseBtn?.classList.remove('hidden');
  $('#selectionBar').classList.toggle('empty', !hasSelection);
  $('#selCount').textContent = hasSelection ? `已选 ${State.selected.size}` : '选择条目';
  const selectAllBtn = $('#selectAllBtn');
  if (selectAllBtn) {
    selectAllBtn.disabled = !State.entries.length;
    selectAllBtn.title = allSelected ? '取消选择当前列表' : '选择当前列表';
    selectAllBtn.setAttribute('aria-label', selectAllBtn.title);
    const label = selectAllBtn.querySelector('span');
    if (label) label.textContent = allSelected ? '取消全选' : '全选';
  }
  $('#clearSelBtn').classList.toggle('hidden', !hasSelection || allSelected);
  ['clearSelBtn', 'addToBatchBtn', 'tagBtn', 'changeProfileBtn', 'batchReparseBtn', 'batchOcrBtn', 'batchSummaryBtn', 'batchExportBtn', 'batchPrintBtn', 'batchDeleteBtn'].forEach((id) => {
    const btn = $('#' + id);
    if (btn) btn.disabled = !hasSelection;
  });
}

function updateListSummary() {
  let sum = 0, paidSum = 0, modifiedCount = 0;
  State.entries.forEach((entry) => {
    sum += Number(entry.total) || 0;
    const paid = Number(entry.fields?.paid_amount?.current);
    paidSum += isNaN(paid) ? 0 : paid;
    if (entryHasPaidDifference(entry)) modifiedCount++;
  });
  const batch = actualBatchId() ? (State.currentBatch || State.batches.find((item) => item.id === actualBatchId()) || null) : null;
  const parts = [];
  if (State.entries.length) {
    parts.push(`<span><b>${State.entries.length}</b> 条</span>`);
    parts.push(`<span class="sep">·</span><span>合计 <b class="stats-sum">${fmtMoney(sum)}</b></span>`);
    if (paidSum) parts.push(`<span class="sep">·</span><span>实付 <b style="color:var(--pass)">${fmtMoney(paidSum)}</b></span>`);
    if (modifiedCount) parts.push(`<span class="sep">·</span><span>已改 <b>${modifiedCount}</b></span>`);
  }
  if (batch?.note) {
    if (parts.length) parts.push(`<span class="sep">·</span>`);
    parts.push(`<span class="batch-stats-note" data-tooltip="批次备注：${esc(batch.note)}">${iconNote(11)}${esc(batch.note)}</span>`);
  }
  if (batch?.stats?.by_person?.length) {
    batch.stats.by_person.forEach((person) => {
      if (parts.length) parts.push(`<span class="sep">·</span>`);
      parts.push(`<span><b>${esc(person.name)}</b> ${person.count} 条 · ${fmtMoney(person.total)}${person.incomplete ? ` · <i style="color:var(--warn);font-style:normal">缺 ${person.incomplete}</i>` : ''}</span>`);
    });
  }
  $('#stats').innerHTML = parts.join('');
}

function listEntryFromDetail(entry) {
  const fields = entry.fields || {};
  const attachmentTypes = {};
  (entry.attachments || []).forEach((attachment) => {
    attachmentTypes[attachment.type] = (attachmentTypes[attachment.type] || 0) + 1;
  });
  return {
    ...entry,
    fields,
    modified_fields: entryHasPaidDifference(entry) ? ['paid_amount'] : [],
    attachment_count: Object.values(attachmentTypes).reduce((sum, count) => sum + count, 0),
    attachment_types: attachmentTypes,
    has_invoice: !!(attachmentTypes.invoice_pdf || attachmentTypes.invoice_xml),
    has_payment: !!attachmentTypes.payment_screenshot,
    has_physical: !!attachmentTypes.physical_image,
    has_inspection: !!attachmentTypes.inspection_pdf,
  };
}

async function refreshEntryCard(entryId, currentDetail = null) {
  const detail = currentDetail || await Api.getEntry(entryId);
  if (!detail) return;
  const entry = listEntryFromDetail(detail);
  const index = State.entries.findIndex((item) => item.id === entryId);
  if (index < 0) return;
  State.entries[index] = entry;

  if (State.quickView === 'incomplete' && (entry.completeness?.status || entry.status) === 'complete') {
    State.entries.splice(index, 1);
    renderEntries();
    return;
  }
  if (State.quickView === 'modified' && !entryHasPaidDifference(entry)) {
    State.entries.splice(index, 1);
    renderEntries();
    return;
  }
  if (State.quickView === 'ocr' && !entry.ocr_recognized) {
    State.entries.splice(index, 1);
    renderEntries();
    return;
  }
  if (State.quickView === 'ocr_pending' && !entry.ocr_pending) {
    State.entries.splice(index, 1);
    renderEntries();
    return;
  }
  if (State.groupBy !== 'none') {
    renderEntries();
    return;
  }

  const current = entryCards().find((card) => card.dataset.entryId === entryId);
  if (!current) return;
  const hadFocus = current.contains(document.activeElement);
  const replacement = entryCard(entry);
  replacement.tabIndex = entryId === State.focusedEntryId ? 0 : -1;
  current.replaceWith(replacement);
  updateListSummary();
  if (hadFocus) focusEntryCard(entryId, false);
}

async function syncEntryAfterChange(entryId, { searchable = false, notes = false, affectsStatus = false, relist = false } = {}, detail = null) {
  const keywordActive = !!$('#filterKeyword')?.value;
  const statusActive = !!$('#filterStatus')?.value;
  const derivedViewActive = ['incomplete', 'warning', 'complete'].includes(State.quickView);
  if (relist || (searchable && keywordActive) || (notes && State.notesFilter) ||
      (affectsStatus && (statusActive || derivedViewActive || State.paymentCountFilter))) {
    await refreshEntries();
    return;
  }
  await refreshEntryCard(entryId, detail);
}

async function openCardAttachment(entry, action) {
  const typeGroups = {
    invoice: ['invoice_pdf', 'invoice_xml'],
    pay: ['payment_screenshot'],
    physical: ['physical_image'],
    inspect: ['inspection_pdf'],
  };
  const types = typeGroups[action] || [];
  try {
    const detail = await Api.getEntry(entry.id);
    const attachments = detail?.attachments || [];
    const matches = attachments.filter((item) => types.includes(item.type));
    const attachment = action === 'invoice'
      ? (matches.find((item) => item.type === 'invoice_pdf') || matches[matches.length - 1])
      : matches[matches.length - 1];
    if (!attachment) {
      const labels = { invoice: '发票材料', pay: '付款截图', physical: '实物图', inspect: '查验单' };
      toast(`当前条目还没有${labels[action] || '该材料'}`, 'err');
      return;
    }
    await Api.openAttachment(attachment.id);
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function reopenEntryDetail(modalRef, entryId, options = {}) {
  modalRef.close();
  const detail = await Api.getEntry(entryId);
  await syncEntryAfterChange(entryId, options, detail);
  await openEntryDetail(entryId, detail);
}

function entryCard(e) {
  const tcls = TITLE_CLASS[e.title] || '';
  const card = el('div', 'entry-card' + (tcls ? ' title-' + tcls : '') +
    (State.selected.has(e.id) ? ' selected' : ''));
  card.dataset.entryId = e.id;
  card.setAttribute('role', 'option');
  card.setAttribute('aria-selected', State.selected.has(e.id) ? 'true' : 'false');
  card.setAttribute('aria-label', `${itemCardLabel(e)}，${fmtMoney(e.total)}`);
  card.setAttribute('aria-keyshortcuts', 'Enter Space ArrowUp ArrowDown Home End');

  const check = el('div', 'entry-check');
  check.onclick = (ev) => { ev.stopPropagation(); if (ev.detail > 1) return; toggleSelect(e.id, ev.shiftKey); };
  check.ondblclick = (ev) => { ev.preventDefault(); ev.stopPropagation(); };
  const cb = el('input');
  cb.type = 'checkbox'; cb.checked = State.selected.has(e.id);
  cb.onclick = (ev) => { ev.stopPropagation(); if (ev.detail > 1) { ev.preventDefault(); return; } toggleSelect(e.id, ev.shiftKey); };
  cb.ondblclick = (ev) => { ev.preventDefault(); ev.stopPropagation(); };
  check.appendChild(cb);

  const stripe = el('div', 'entry-stripe');
  stripe.title = '切换选中';
  stripe.onclick = (ev) => { ev.stopPropagation(); if (ev.detail > 1) return; toggleSelect(e.id, ev.shiftKey); };
  stripe.ondblclick = (ev) => { ev.preventDefault(); ev.stopPropagation(); };

  // 校验状态：仅在 warning/blocked 时突出显示（pass 不占视觉）
  const checkBadge = (e.check_status && e.check_status !== 'pass')
    ? `<span class="badge ${e.check_status}" title="${esc(e.check_message || '')}">${CHECK_LABEL[e.check_status] || ''}</span>` : '';

  const fields = e.fields || {};
  const notesCur = fields.notes ? fields.notes.current : '';
  const actualCur = fields.actual_item_name ? fields.actual_item_name.current : '';
  const paidCur = fields.paid_amount ? fields.paid_amount.current : '';
  const paidDiff = entryHasPaidDifference(e);
  const modified = paidDiff
    ? `<span class="badge modified" data-tooltip="${esc(entryModifiedTooltip(e))}">${iconPencil(11)}已修改</span>` : '';
  const ocrBadge = e.ocr_pending
    ? `<button class="badge ocr badge-action" data-card-ocr="1" title="阿里云识别存在待确认差异，点击查看">OCR</button>` : '';
  const recognizedBadge = (e.ocr_recognized && !e.ocr_pending)
    ? `<button class="badge ocr badge-action" data-card-ocr="1" title="已用阿里云识别，点击查看">已识别</button>` : '';
  const owner = State.profileById[e.profile_id];
  const ownerBadge = owner
    ? `<button class="badge person badge-action" data-card-owner="${esc(e.profile_id)}"${owner.reviewer ? ` title="审核人：${esc(owner.reviewer)} · 点击编辑"` : ' title="点击编辑报账人"'}>${esc(owner.name)}</button>` : '';
  const batchBadges = (e.batches || []).length
    ? e.batches.map((batch) =>
      `<button class="badge batch badge-action${batch.archived ? ' archived' : ''}" data-card-batch="${esc(batch.id)}" ` +
      `title="${batch.archived ? '已归档批次' : '报账批次'}：${esc(batch.name)}">${esc(batch.name)}</button>`
    ).join('')
    : '<button class="badge batch empty badge-action" data-card-batch="" title="点击设置报账批次">批次</button>';

  const itemTitle = actualCur || (e.items && e.items[0] && (e.items[0].actual_name || e.items[0].name)) || '未填物资名称';
  const notesPreview = notesCur
    ? `<span class="notes-preview important" data-tooltip-overflow="${esc(notesCur)}">${iconNote(12)}${esc(notesCur)}</span>`
    : '';
  const actionBtn = (action, label, on, title) => (
    `<button class="${on ? 'done' : ''}" data-card-action="${action}" title="${esc(title)}">` +
    `<span class="action-state"></span>${label}</button>`
  );

  const main = el('div', 'entry-main', `
    <div class="entry-line1">
      <span class="entry-item-title" data-tooltip-overflow="${esc(itemTitle)}">${esc(itemTitle)}</span>
      ${ownerBadge}
      ${batchBadges}
      ${checkBadge}
      ${modified}
      ${ocrBadge}
      ${recognizedBadge}
    </div>
    <div class="entry-line2">
      <span class="seller-muted" data-tooltip-overflow="${esc(e.seller || '')}">${esc(e.seller || '未识别销售方')}</span>
      <span class="mono">${esc(e.invoice_no || '无发票号')}</span>
    </div>
    <div class="entry-line3">
      <span>${esc(dateShort(e.invoice_date))}</span>
      ${notesPreview}
    </div>`);
  const tags = Array.isArray(e.tags) ? e.tags : [];
  if (tags.length) {
    main.insertAdjacentHTML('beforeend',
      `<div class="entry-tags">${tags.map((t) => `<span class="entry-tag">${esc(t)}</span>`).join('')}</div>`);
  }
  const batchNote = State.currentBatch?.entry_notes?.[e.id];
  if (batchNote) {
    main.insertAdjacentHTML('beforeend', `<div class="batch-entry-note" data-tooltip-overflow="${esc(batchNote)}">${iconNote(12)}${esc(batchNote)}</div>`);
  }

  const right = el('div', 'entry-right');
  const showPhysicalAction = State.materialRequirements.physical_image || e.has_physical;
  const detailAction = `<button class="entry-detail-action" data-card-action="detail">${showPhysicalAction ? '详情' : '打开详情'}</button>`;
  const paymentCount = Number(e.attachment_types?.payment_screenshot || 0);
  const paymentActionLabel = paymentCount > 1
    ? `付款<span class="payment-count">×${paymentCount}</span>`
    : '付款';
  const commonActions = `
      ${actionBtn('invoice', '发票', e.has_invoice, e.has_invoice ? '左键补充发票 PDF；右键打开已有材料' : '添加发票 PDF')}
      ${actionBtn('paid', '实付', !!paidCur, paidCur ? '已填写实付金额；点击修改' : '填写实付金额')}
      ${actionBtn('pay', paymentActionLabel, e.has_payment, e.has_payment ? `已有 ${paymentCount} 张付款截图；左键继续添加，右键打开最近一张` : '添加付款截图')}
      ${actionBtn('inspect', '查验', e.has_inspection, e.has_inspection ? '左键重新查验或补充；右键打开已有查验单' : '打开官网查验并自动归档 PDF')}`;
  const physicalAction = actionBtn('physical', '实物', e.has_physical, e.has_physical ? '左键继续添加实物图；右键打开已有实物图' : '添加实物图');
  right.innerHTML = `
    <div class="entry-total">${fmtMoney(e.total)}</div>
    ${paidDiff ? `<div class="entry-paid diff">实付 <b>${fmtMoney(paidCur)}</b><span>差异</span></div>` : ''}
    ${showPhysicalAction
      ? `<div class="entry-inline-actions with-detail">${commonActions}${detailAction}${physicalAction}</div>`
      : `${detailAction}<div class="entry-inline-actions">${commonActions}</div>`}`;
  right.querySelectorAll('[data-card-action]').forEach((b) => {
    b.onclick = async (ev) => {
      ev.stopPropagation();
      if (ev.detail > 1) { ev.preventDefault(); return; }
      const action = b.dataset.cardAction;
      if (action === 'detail') await openEntryDetail(e.id);
      else if (action === 'invoice') await quickAddAttachment(e.id, 'invoice_pdf');
      else if (action === 'paid') await quickPaidFlow(e);
      else if (action === 'pay') await quickAddAttachment(e.id, 'payment_screenshot');
      else if (action === 'physical') await quickAddAttachment(e.id, 'physical_image');
      else if (action === 'inspect') await onlineVerificationFlow(e.id);
    };
    if (['invoice', 'pay', 'physical', 'inspect'].includes(b.dataset.cardAction)) {
      b.oncontextmenu = async (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        await openCardAttachment(e, b.dataset.cardAction);
      };
    }
    b.ondblclick = (ev) => { ev.preventDefault(); ev.stopPropagation(); };
  });

  main.querySelector('[data-card-owner]')?.addEventListener('click', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    changeSelectionProfile([e.id], e.profile_id);
  });
  main.querySelector('[data-card-ocr]')?.addEventListener('click', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    openEntryDetail(e.id);
  });
  main.querySelectorAll('[data-card-batch]').forEach((badge) => {
    badge.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      openEntryBatchFlow(e);
    });
  });

  card.append(check, stripe, main, right);
  card.onclick = (ev) => {
    if (ev.detail > 1) {
      ev.preventDefault();
      return;
    }
    State.focusedEntryId = e.id;
    card.focus({ preventScroll: true });
    selectEntryFromCard(e.id, ev.shiftKey);
  };
  card.onfocus = () => {
    State.focusedEntryId = e.id;
    $$('.entry-card', $('#entryList')).forEach((node) => { node.tabIndex = node === card ? 0 : -1; });
  };
  card.onkeydown = (ev) => handleEntryCardKeydown(ev, e);
  card.oncontextmenu = (ev) => {
    ev.preventDefault();
    openEntryMenu(ev.clientX, ev.clientY, e);
  };
  card.ondragover = (ev) => {
    ev.preventDefault();
    card.classList.add('card-dragging');
  };
  card.ondragleave = () => card.classList.remove('card-dragging');
  card.ondrop = async (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    card.classList.remove('card-dragging');
    const progress = taskProgress('正在读取拖入材料…');
    try {
      const added = await addDroppedMaterialFiles(e.id, [...(ev.dataTransfer?.files || [])], progress);
      if (added) {
        progress.update('正在刷新条目状态…');
        await refreshEntries();
        toast('材料已添加', 'ok');
      }
    } catch (err) { toast(err.message, 'err'); }
    finally { progress.close(); }
  };
  return card;
}

function itemCardLabel(entry) {
  const fields = entry.fields || {};
  const actual = fields.actual_item_name?.current;
  const item = actual || entry.items?.[0]?.actual_name || entry.items?.[0]?.name || '未填物资名称';
  return `${item}，${entry.seller || '未识别销售方'}，发票号 ${entry.invoice_no || '无'}`;
}

function entryCards() {
  return $$('.entry-card', $('#entryList'));
}

function focusEntryCard(entryId, scroll = true) {
  const card = entryCards().find((node) => node.dataset.entryId === entryId);
  if (!card) return;
  State.focusedEntryId = entryId;
  entryCards().forEach((node) => { node.tabIndex = node === card ? 0 : -1; });
  card.focus({ preventScroll: !scroll });
  if (scroll) card.scrollIntoView({ block: 'nearest' });
}

function moveEntryFocus(currentId, targetIndex, extendSelection) {
  const cards = entryCards();
  if (!cards.length) return;
  const bounded = Math.max(0, Math.min(cards.length - 1, targetIndex));
  const targetId = cards[bounded].dataset.entryId;
  if (extendSelection) {
    if (!State.lastSelectedId) State.lastSelectedId = currentId;
    toggleSelect(targetId, true);
  }
  focusEntryCard(targetId);
}

function handleEntryCardKeydown(ev, entry) {
  if (ev.target !== ev.currentTarget) return;
  const cards = entryCards();
  const index = cards.indexOf(ev.currentTarget);
  const key = ev.key;
  let targetIndex = null;
  if (key === 'ArrowDown' || key === 'ArrowRight') targetIndex = index + 1;
  else if (key === 'ArrowUp' || key === 'ArrowLeft') targetIndex = index - 1;
  else if (key === 'Home') targetIndex = 0;
  else if (key === 'End') targetIndex = cards.length - 1;
  else if (key === 'PageDown') targetIndex = index + 5;
  else if (key === 'PageUp') targetIndex = index - 5;

  if (targetIndex != null) {
    ev.preventDefault();
    moveEntryFocus(entry.id, targetIndex, ev.shiftKey);
    return;
  }
  if (key === ' ' || key === 'Spacebar') {
    ev.preventDefault();
    toggleSelect(entry.id, ev.shiftKey);
    focusEntryCard(entry.id, false);
  } else if (key === 'Enter') {
    ev.preventDefault();
    openEntryDetail(entry.id);
  } else if ((ev.metaKey || ev.ctrlKey) && key.toLowerCase() === 'a') {
    ev.preventDefault();
    ev.stopPropagation();
    selectAllVisible().then(() => focusEntryCard(entry.id, false));
  } else if (key === 'Escape' && State.selected.size) {
    ev.preventDefault();
    ev.stopPropagation();
    State.selected.clear();
    State.lastSelectedId = null;
    renderEntries();
    focusEntryCard(entry.id, false);
  } else if (key === 'ContextMenu' || (ev.shiftKey && key === 'F10')) {
    ev.preventDefault();
    const rect = ev.currentTarget.getBoundingClientRect();
    openEntryMenu(rect.left + 24, rect.top + 24, entry);
  }
}

function sameMoney(a, b) {
  const x = Number(a);
  const y = Number(b);
  if (!isFinite(x) || !isFinite(y)) return String(a || '') === String(b || '');
  return Math.round(x * 100) === Math.round(y * 100);
}

function entryHasPaidDifference(entry) {
  const paid = entry?.fields?.paid_amount?.current;
  return !!String(paid ?? '').trim() && !sameMoney(paid, entry?.total);
}

function entryModifiedTooltip(entry) {
  const fields = entry?.fields || {};
  const changes = [];
  const paid = fields.paid_amount;
  if (entryHasPaidDifference(entry)) {
    changes.push(`实付金额：${fmtMoney(paid?.origin || entry?.total)} → ${fmtMoney(paid?.current)}`);
  }
  const describeText = (key, label) => {
    const row = fields[key];
    if (!row?.modified) return;
    const before = String(row.origin || '空');
    const after = String(row.current || '空');
    changes.push(before === after ? `${label}曾修改，当前为“${after}”` : `${label}：“${before}”→“${after}”`);
  };
  describeText('actual_item_name', '实际物资名称');
  describeText('notes', '条目备注');
  return changes.length ? `已修改：${changes.join('\n')}` : '实付金额与发票金额不一致';
}

// 按报账人 / 抬头分组渲染，每组头显示条数、合计、齐备率，可整组选中
function renderGroupedEntries(list) {
  const keyOf = (e) => State.groupBy === 'profile'
    ? (State.profileById[e.profile_id]?.name || '未知报账人')
    : (e.title || '未标注抬头');
  const groups = new Map();
  State.entries.forEach((e) => {
    const k = keyOf(e);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(e);
  });

  [...groups.entries()].forEach(([name, items]) => {
    const total = items.reduce((s, e) => s + (Number(e.total) || 0), 0);
    const ready = items.filter((e) => (e.completeness?.ready)).length;
    const allSel = items.every((e) => State.selected.has(e.id));
    const tcls = State.groupBy === 'title' ? (TITLE_CLASS[name] || '') : '';

    const head = el('div', 'group-head' + (tcls ? ' title-' + tcls : ''));
    head.innerHTML = `
      <button class="group-sel" title="选中/取消这组">${allSel ? '✓' : ''}</button>
      <span class="group-name">${esc(name)}</span>
      <span class="group-meta"><b>${items.length}</b> 条 · 合计 <b>${fmtMoney(total)}</b> · 齐备 ${ready}/${items.length}</span>`;
    head.querySelector('.group-sel').onclick = () => {
      if (allSel) items.forEach((e) => State.selected.delete(e.id));
      else items.forEach((e) => State.selected.add(e.id));
      renderEntries();
    };
    list.appendChild(head);
    items.forEach((e) => list.appendChild(entryCard(e)));
  });
}

function renderEmptyState() {
  const hasFilter = hasAnyFilter();
  // 批次栏聚焦（含「已归档」）本身不算普通筛选；只有搜索/状态/日期等条件才应显示“没有匹配”。
  const hasNonBatchFilter = !!(
    ($('#filterStatus')?.value || '') || $('#filterCheck').value || $('#filterProfile').value ||
    $('#filterTitle').value || $('#filterKeyword').value || $('#filterAmountMin').value ||
    $('#filterAmountMax').value || $('#filterDateFrom').value || $('#filterDateTo').value ||
    State.tagFilter || State.notesFilter || State.paymentCountFilter || State.quickView !== 'all'
  );
  const hasArchivedEntries = State.batches.some((batch) => batch.archived && (batch.stats?.count || 0) > 0);
  const illus = $('#emptyIllus');
  if (inArchivedShelf()) {
    if (hasNonBatchFilter) {
      illus.innerHTML = `<svg viewBox="0 0 48 48" width="44" height="44" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="21" cy="21" r="13"/><path d="m31 31 7 7M16 21h10M21 16v10"/></svg>`;
      $('#emptyTitle').textContent = '没有匹配的条目';
      $('#emptySub').textContent = '清掉一些筛选，或换个关键词、抬头。';
      $('#emptyNew').textContent = '清空筛选';
      $('#emptyNew').onclick = () => clearAllFilters();
    } else {
      illus.innerHTML = `<svg viewBox="0 0 48 48" width="44" height="44" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17h34v24H7z"/><path d="M5 11h38v6H5zM18 25h12"/></svg>`;
      $('#emptyTitle').textContent = '还没有归档条目';
      $('#emptySub').textContent = '批次归档后，只属于已归档批次的条目会放在这里。';
      $('#emptyNew').textContent = '返回在办';
      $('#emptyNew').onclick = () => focusBatch('');
    }
  } else if (!hasFilter && hasArchivedEntries) {
    illus.innerHTML = `<svg viewBox="0 0 48 48" width="44" height="44" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17h34v24H7z"/><path d="M5 11h38v6H5zM18 25h12"/></svg>`;
    $('#emptyTitle').textContent = '在办没有条目';
    $('#emptySub').textContent = '当前要处理的条目都已完成批次并归档；可到「已归档」查看或恢复。';
    $('#emptyNew').textContent = '查看已归档';
    $('#emptyNew').onclick = () => focusBatch(ARCHIVED_BATCH_ID);
  } else if (hasFilter) {
    illus.innerHTML = `<svg viewBox="0 0 48 48" width="44" height="44" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="21" cy="21" r="13"/><path d="m31 31 7 7M16 21h10M21 16v10"/></svg>`;
    $('#emptyTitle').textContent = '没有匹配的条目';
    $('#emptySub').textContent = '清掉一些筛选，或换个关键词、抬头。';
    $('#emptyNew').textContent = '清空筛选';
    $('#emptyNew').onclick = () => clearAllFilters();
  } else {
    illus.innerHTML = `<svg viewBox="0 0 48 48" width="42" height="42" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14h22l6 6v22a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V16a2 2 0 0 1 2-2z"/><path d="M31 14v6h6M14 26h16M14 32h12M14 38h8"/><path d="M9 14l3-5h18l3 5" opacity=".6"/></svg>`;
    $('#emptyTitle').textContent = '还没有报账条目';
    $('#emptySub').textContent = '上传一张发票，开始整理报账凭证。';
    $('#emptyNew').textContent = '新建第一条';
    $('#emptyNew').onclick = () => openNewEntry();
  }
}

function syncFilterControlStates() {
  ['filterTitle', 'filterProfile', 'filterTag'].forEach((id) => {
    const control = $('#' + id);
    control?.closest('.chip-select')?.classList.toggle('is-filtered', !!control.value);
  });

  const advancedGroups = [
    ['filterDateFrom', 'filterDateTo'],
    ['filterAmountMin', 'filterAmountMax'],
    ['filterCheck'],
    ['filterNotes'],
    ['filterPaymentCount'],
  ];
  let hasAdvancedFilter = false;
  advancedGroups.forEach((ids) => {
    const filtered = ids.some((id) => !!$('#' + id)?.value);
    const field = $('#' + ids[0])?.closest('.adv-field');
    field?.classList.toggle('is-filtered', filtered);
    hasAdvancedFilter ||= filtered;
  });

  const toggle = $('#advancedToggle');
  toggle?.classList.toggle('is-filtered', hasAdvancedFilter);
  if (toggle) toggle.title = hasAdvancedFilter ? '已启用高级筛选' : '高级筛选';

  const clearButton = $('#clearFilters');
  if (clearButton) clearButton.disabled = !hasAnyFilter();
}

function hasAnyFilter() {
  return !!(($('#filterStatus')?.value || '') || $('#filterCheck').value || $('#filterProfile').value || $('#filterTitle').value ||
    $('#filterKeyword').value || $('#filterAmountMin').value || $('#filterAmountMax').value ||
    $('#filterDateFrom').value || $('#filterDateTo').value ||
    State.tagFilter || State.notesFilter || State.paymentCountFilter || State.batchFilter ||
    State.quickView !== 'all');
}

function updateQuickViewButtons() {
  $$('[data-view]').forEach((b) => b.classList.toggle('active', b.dataset.view === State.quickView));
}

function setQuickView(v) {
  State.quickView = v || 'all';
  updateQuickViewButtons();
  State.selected.clear();
  refreshEntries();
}

function showSearchHintIfEmpty() {
  const hasText = !!$('#filterKeyword').value;
  const kb = $('#searchKbd');
  kb.hidden = hasText;
  const clear = $('#searchClear');
  if (clear) clear.classList.toggle('hidden', !hasText);
}

// ------------------------------------------------------------------ 选择 / 批量
function toggleSelect(id, range) {
  State.suppressListAnimation = true;
  if (range && State.lastSelectedId) {
    const ids = State.entries.map((e) => e.id);
    const a = ids.indexOf(State.lastSelectedId);
    const b = ids.indexOf(id);
    if (a >= 0 && b >= 0) {
      const [from, to] = a < b ? [a, b] : [b, a];
      ids.slice(from, to + 1).forEach((eid) => State.selected.add(eid));
      State.lastSelectedId = id;
      renderEntries();
      return;
    }
  }
  if (State.selected.has(id)) State.selected.delete(id);
  else State.selected.add(id);
  State.lastSelectedId = id;
  renderEntries();
}

function selectEntryFromCard(id, range) {
  if (range && State.lastSelectedId) {
    const ids = State.entries.map((entry) => entry.id);
    const start = ids.indexOf(State.lastSelectedId);
    const end = ids.indexOf(id);
    if (start >= 0 && end >= 0) {
      const [from, to] = start < end ? [start, end] : [end, start];
      ids.slice(from, to + 1).forEach((entryId) => State.selected.add(entryId));
    }
  } else {
    if (State.selected.has(id)) State.selected.delete(id);
    else State.selected.add(id);
  }
  State.lastSelectedId = id;
  syncVisibleSelectionState();
}

function syncVisibleSelectionState() {
  entryCards().forEach((card) => {
    const selected = State.selected.has(card.dataset.entryId);
    card.classList.toggle('selected', selected);
    card.setAttribute('aria-selected', selected ? 'true' : 'false');
    const checkbox = card.querySelector('.entry-check input');
    if (checkbox) checkbox.checked = selected;
  });
  updateSelectionBar();
}
async function selectAllVisible() {
  State.suppressListAnimation = true;
  State.selected.clear();
  State.entries.forEach((e) => State.selected.add(e.id));
  State.lastSelectedId = State.entries.length ? State.entries[State.entries.length - 1].id : null;
  renderEntries();
}
function toggleSelectAllVisible() {
  const allSelected = State.entries.length > 0 && State.entries.every((entry) => State.selected.has(entry.id));
  State.suppressListAnimation = true;
  if (allSelected) {
    State.selected.clear();
    State.lastSelectedId = null;
    renderEntries();
    return;
  }
  selectAllVisible();
}
// ------------------------------------------------------------------ 批次（运营组）
async function loadBatches() {
  try {
    const result = await Api.listBatches(true);
    State.batches = Array.isArray(result) ? result : (result?.batches || []);
    State.unbatchedCount = Array.isArray(result) ? 0 : Number(result?.unbatched_count || 0);
  } catch (e) {
    State.batches = [];
    State.unbatchedCount = 0;
  }
  renderBatchFolders();
}

function renderBatchFolders() {
  const wrap = $('#batchFolders');
  if (!wrap) return;
  const folders = State.batches.filter((b) => !b.archived);
  const archived = State.batches.filter((b) => b.archived);
  const shelf = inArchivedShelf();
  if (!folders.length && !archived.length && !State.batchFilter && !State.unbatchedCount) {
    wrap.innerHTML = '';
    wrap.classList.add('hidden');
    return;
  }
  wrap.classList.remove('hidden');

  const scopeChip = (id, label, active) =>
    `<button type="button" class="batch-scope-btn${active ? ' active' : ''}" data-folder="${id}" role="tab" aria-selected="${active ? 'true' : 'false'}" title="${id === ARCHIVED_BATCH_ID ? '装入批次后可点击批次右侧 ⋯ 归档' : '查看在办条目'}">` +
      `<span>${label}</span></button>`;
  const folderChip = (batch) => {
    const st = batch.stats || {};
    const meta = batch.archived
      ? ''
      : `${st.count || 0} 条${st.incomplete ? ` · <span class="miss">缺 ${st.incomplete}</span>` : ' · 齐'}`;
    const hint = batch.archived ? '恢复到在办' : '装入批次后可将批次归档';
    return `<span class="batch-folder${batch.archived ? ' archived' : ''}${State.batchFilter === batch.id ? ' active' : ''}" data-folder="${esc(batch.id)}">
      <button type="button" class="folder-open"><span data-tooltip-overflow="${esc(batch.name)}">${esc(batch.name)}</span>${meta ? `<small>${meta}</small>` : ''}</button>
      ${batch.note ? `<span class="batch-folder-note" data-tooltip="批次备注：${esc(batch.note)}">${iconNote(11)}</span>` : ''}
      <button type="button" class="folder-menu" data-folder-menu="${esc(batch.id)}" title="${hint}">⋯</button>
    </span>`;
  };

  let track = '';
  if (shelf) {
    track = archived.map(folderChip).join('');
  } else {
    const showUnbatched = folders.length > 0 || inUnbatchedView();
    track = `
      ${showUnbatched ? `<button type="button" class="batch-folder unbatched${inUnbatchedView() ? ' active' : ''}" data-folder="${UNBATCHED_BATCH_ID}">
        <span>未进批次</span><small>${State.unbatchedCount} 条</small>
      </button>` : ''}
      ${folders.map(folderChip).join('')}
      <button type="button" class="batch-folder new" id="folderNewBatch">新建批次</button>`;
  }
  wrap.innerHTML = `
    <div class="batch-scope" role="tablist" aria-label="报账批次视图">
      ${scopeChip('', '在办', !shelf)}
      ${scopeChip(ARCHIVED_BATCH_ID, '已归档', shelf)}
    </div>
    <span class="batch-scope-divider" aria-hidden="true"></span>
    <div class="batch-folder-track">${track}</div>`;

  wrap.querySelectorAll('[data-folder]').forEach((node) => {
    node.onclick = (ev) => {
      if (ev.target instanceof Element && ev.target.closest('[data-folder-menu]')) return;
      focusBatch(node.dataset.folder || '');
    };
  });
  wrap.querySelectorAll('[data-folder-menu]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      const b = State.batches.find((x) => x.id === btn.dataset.folderMenu);
      if (b) openBatchMenu(ev.clientX, ev.clientY, b);
    };
  });
  const newBtn = $('#folderNewBatch');
  if (newBtn) newBtn.onclick = () => newBatchFlow();
}

function focusBatch(batchId) {
  State.batchFilter = batchId || '';
  State.currentBatch = null;
  State.selected.clear();
  renderBatchFolders();
  refreshEntries();
}

function hasArchivedBatches() {
  return State.batches.some((batch) => batch.archived);
}

function openBatchMenu(x, y, b) {
  closeEntryMenu();
  const menu = el('div', 'entry-context-menu');
  const item = (label, fn, danger) => {
    const btn = el('button', danger ? 'danger' : '', esc(label));
    btn.onclick = async () => { closeEntryMenu(); await fn(); };
    menu.appendChild(btn);
  };
  item('打开这批', () => focusBatch(b.id));
  item('编辑批次', () => renameBatchFlow(b));
  item('批次备注', () => batchNoteFlow(b));
  item(b.archived ? '恢复到在办' : '归档批次', () => archiveBatchFlow(b));
  item('删除批次', async () => {
    if (!confirm(`删除批次「${b.name}」？条目本身不会被删除。`)) return;
    const wasFocused = State.batchFilter === b.id;
    await Api.deleteBatch(b.id);
    await loadBatches();
    if (wasFocused) focusBatch(b.archived && hasArchivedBatches() ? ARCHIVED_BATCH_ID : '');
    else await refreshEntries();
    toast('批次已删除', 'ok');
  }, true);
  menu.style.left = Math.min(x, window.innerWidth - 190) + 'px';
  menu.style.top = Math.min(y, window.innerHeight - 200) + 'px';
  document.body.appendChild(menu);
  setTimeout(() => document.addEventListener('click', closeEntryMenu, { once: true }), 0);
}

async function archiveBatchFlow(batch) {
  if (batch.archived) {
    try {
      await Api.archiveBatch(batch.id, false);
      await loadBatches();
      focusBatch(batch.id);
      toast(`「${batch.name}」已恢复到在办`, 'ok');
    } catch (e) { toast(e.message, 'err'); }
    return;
  }

  const stats = batch.stats || {};
  const body = el('div');
  body.innerHTML = `
    <div class="archive-confirm">
      <div class="archive-confirm-summary">
        <b>${esc(batch.name)}</b>
        <span>${Number(stats.count || 0)} 条 · ${fmtMoney(stats.total || 0)}${stats.incomplete ? ` · 缺 ${stats.incomplete}` : ' · 材料齐备'}</span>
      </div>
      ${stats.incomplete ? `<div class="archive-confirm-warning">仍有 ${stats.incomplete} 条材料未齐</div>` : ''}
      ${batch.note ? `<div class="archive-confirm-note"><b>批次备注</b><span>${esc(batch.note)}</span></div>` : ''}
    </div>`;
  const m = modal({
    title: '确认归档批次',
    body,
    footer: [
      mkBtn('取消', 'ghost', () => m.close()),
      mkBtn('确认归档', 'primary', async () => {
        try {
          await Api.archiveBatch(batch.id, true);
          m.close();
          await loadBatches();
          if (State.batchFilter === batch.id) focusBatch(batch.id);
          else await refreshEntries();
          toast(`已归档「${batch.name}」`, 'ok');
        } catch (e) { toast(e.message, 'err'); }
      }),
    ],
  });
}

async function renameBatchFlow(b) {
  const body = el('div');
  body.innerHTML = `
    <div class="form-row"><label>批次名称</label><input id="bName" value="${esc(b.name)}"/></div>
    <div class="form-row"><label>批次备注（可选）</label><textarea id="bNote" rows="3" style="width:100%;font-family:inherit;font-size:13px;padding:10px;border-radius:9px;border:1px solid var(--line);resize:vertical">${esc(b.note || '')}</textarea></div>`;
  const m = modal({
    title: '编辑批次', body,
    footer: [mkBtn('取消', 'ghost', () => m.close()), mkBtn('保存', 'primary', async () => {
      const name = body.querySelector('#bName').value.trim();
      if (!name) { toast('名称不能为空', 'err'); return; }
      try {
        await Api.updateBatch(b.id, { name, note: body.querySelector('#bNote').value });
        m.close(); await loadBatches();
        await refreshEntries();
        toast('已保存', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    })],
  });
  setTimeout(() => body.querySelector('#bName')?.focus(), 20);
}

async function batchNoteFlow(b) {
  const body = el('div');
  body.innerHTML = `<div class="form-row"><label>批次备注</label><textarea id="batchNote" rows="4" placeholder="记录这批的用途、注意事项或交接说明">${esc(b.note || '')}</textarea></div>`;
  const m = modal({
    title: '批次备注',
    body,
    footer: [
      mkBtn('取消', 'ghost', () => m.close()),
      mkBtn('保存', 'primary', async () => {
        try {
          await Api.updateBatch(b.id, { note: body.querySelector('#batchNote').value });
          m.close();
          await loadBatches();
          await refreshEntries();
          toast('批次备注已保存', 'ok');
        } catch (e) { toast(e.message, 'err'); }
      }),
    ],
  });
  setTimeout(() => body.querySelector('#batchNote')?.focus(), 20);
}

async function newBatchFlow(presetIds) {
  const ids = presetIds || [...State.selected];
  const body = el('div');
  body.innerHTML = `
    <div class="form-row"><label>批次名称</label><input id="bName" placeholder="如：7月第一批 / 张三这次的"/></div>
    <div class="form-row"><label>批次备注（可选）</label><input id="bNote" placeholder="备注这批的用途或注意事项"/></div>`;
  const m = modal({
    title: ids.length ? `新建批次 · ${ids.length} 条` : '新建批次', body,
    footer: [mkBtn('取消', 'ghost', () => m.close()), mkBtn('创建', 'primary', async () => {
      const name = body.querySelector('#bName').value.trim();
      if (!name) { toast('请填批次名称', 'err'); return; }
      try {
        const b = await Api.createBatch(name, body.querySelector('#bNote').value, ids);
        m.close(); await loadBatches(); focusBatch(b.id);
        toast(`批次「${name}」已创建`, 'ok');
      } catch (e) { toast(e.message, 'err'); }
    })],
  });
  setTimeout(() => body.querySelector('#bName')?.focus(), 20);
}

// 调整选中条目的批次归属：加入、移出当前批次，或从当前批次移动到另一批次。
async function addSelectionToBatch(idsArg) {
  const fromSelection = !Array.isArray(idsArg);
  const ids = fromSelection ? [...State.selected] : idsArg;
  if (!ids.length) { toast('请先选择条目', 'err'); return; }
  await loadBatches();
  let memberships = [];
  try { memberships = await Promise.all(ids.map((id) => Api.batchesOfEntry(id))); }
  catch (e) { toast(e.message, 'err'); return; }
  const memberCounts = new Map();
  memberships.forEach((rows) => rows.forEach((batch) => {
    memberCounts.set(batch.id, (memberCounts.get(batch.id) || 0) + 1);
  }));
  const body = el('div');
  const current = actualBatchId() && State.batches.find((b) => b.id === actualBatchId());
  const rows = State.batches.filter((batch) => !batch.archived).map((batch) => {
    const count = memberCounts.get(batch.id) || 0;
    const toggleLabel = count === ids.length ? '移出' : count ? '补齐' : '装入';
    const state = count === ids.length ? '已装入' : count ? `${count}/${ids.length} 条` : `${batch.stats?.count || 0} 条`;
    return `<div class="batch-membership-row${batch.id === State.batchFilter ? ' current' : ''}">
      <div class="batch-membership-name"><b>${esc(batch.name)}</b><span>${state}</span></div>
      <div class="batch-membership-actions">
        <button class="btn small ghost${count === ids.length ? ' danger-text' : ''}" data-toggle-batch="${esc(batch.id)}" data-member-count="${count}">${toggleLabel}</button>
        ${current && batch.id !== current.id ? `<button class="btn small ghost" data-move-batch="${esc(batch.id)}">移动</button>` : ''}
      </div>
    </div>`;
  }).join('');
  body.innerHTML = `<div class="batch-membership-list">${rows || '<div class="hint">还没有批次。</div>'}</div>`;
  const footer = [mkBtn('关闭', 'ghost', () => m.close()), mkBtn('新建批次', 'primary', () => { m.close(); newBatchFlow(ids); })];
  const m = modal({
    title: `批次 · ${ids.length} 条`, body, footer,
  });
  body.querySelectorAll('[data-toggle-batch]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const batchId = btn.dataset.toggleBatch;
        const allInside = Number(btn.dataset.memberCount) === ids.length;
        const r = allInside
          ? await Api.removeEntriesFromBatch(batchId, ids)
          : await Api.addEntriesToBatch(batchId, ids);
        m.close(); if (fromSelection) State.selected.clear(); await loadBatches();
        await refreshEntries();
        toast(allInside ? `已移出 ${r.removed} 条` : `已装入 ${r.added} 条`, 'ok');
      } catch (e) { toast(e.message, 'err'); }
    };
  });
  body.querySelectorAll('[data-move-batch]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const targetId = btn.dataset.moveBatch;
        await Api.moveEntriesBetweenBatches(current.id, targetId, ids);
        m.close(); if (fromSelection) State.selected.clear(); await loadBatches(); focusBatch(targetId);
        toast(`已移动 ${ids.length} 条`, 'ok');
      } catch (e) { toast(e.message, 'err'); }
    };
  });
}

async function openEntryBatchFlow(entry) {
  try {
    await loadBatches();
    const memberships = await Api.batchesOfEntry(entry.id);
    const currentIds = new Set(memberships.map((batch) => batch.id));
    const body = el('div');
    const activeBatches = State.batches.filter((batch) => !batch.archived);
    const rows = activeBatches.map((batch) => {
      const current = currentIds.has(batch.id);
      return `<div class="batch-membership-row${current ? ' current' : ''}">
        <div class="batch-membership-name"><b>${esc(batch.name)}</b><span>${current ? '当前批次' : `${batch.stats?.count || 0} 条`}</span></div>
        <button class="btn small ghost" data-entry-batch="${esc(batch.id)}">${current && memberships.length === 1 ? '当前' : '改为此批次'}</button>
      </div>`;
    }).join('');
    const currentLabel = memberships.length
      ? memberships.map((batch) => `${esc(batch.name)}${batch.archived ? ' · 已归档' : ''}`).join('、')
      : '不在任何批次';
    body.innerHTML = `
      <div class="entry-batch-current"><span>当前归属</span><b>${currentLabel}</b></div>
      <div class="batch-membership-list">${rows || '<div class="hint">还没有可用批次。</div>'}</div>`;
    const m = modal({
      title: '编辑批次归属',
      body,
      footer: [
        ...(memberships.length ? [mkBtn('不进任何批次', 'ghost', async () => {
          try {
            await Api.setEntryBatch(entry.id, '');
            m.close(); await loadBatches(); await refreshEntries();
            toast('已移出所有批次', 'ok');
          } catch (err) { toast(err.message, 'err'); }
        })] : []),
        mkBtn('新建批次', 'primary', () => { m.close(); newBatchFlow([entry.id]); }),
        mkBtn('关闭', 'ghost', () => m.close()),
      ],
    });
    body.querySelectorAll('[data-entry-batch]').forEach((button) => {
      button.onclick = async () => {
        if (button.textContent.trim() === '当前') return;
        try {
          await Api.setEntryBatch(entry.id, button.dataset.entryBatch);
          m.close(); await loadBatches(); await refreshEntries();
          toast('批次归属已更新', 'ok');
        } catch (err) { toast(err.message, 'err'); }
      };
    });
  } catch (err) {
    toast(err.message, 'err');
  }
}

// 批量打标签
async function tagSelectionFlow(idsArg) {
  const ids = Array.isArray(idsArg) ? idsArg : [...State.selected];
  if (!ids.length) { toast('请先选择条目', 'err'); return; }
  try { State.allTags = await Api.listTags(); } catch (e) { State.allTags = []; }
  const body = el('div');
  const selectedEntries = State.entries.filter((entry) => ids.includes(entry.id));
  const tagCounts = new Map();
  selectedEntries.forEach((entry) => (entry.tags || []).forEach((tag) => tagCounts.set(tag, (tagCounts.get(tag) || 0) + 1)));
  const existing = State.allTags.map((t) => {
    const count = tagCounts.get(t) || 0;
    return `<button class="tag-pick${count === ids.length ? ' applied' : count ? ' partial' : ''}" data-tag="${esc(t)}" data-count="${count}">${esc(t)}</button>`;
  }).join('');
  body.innerHTML = `
    <div class="tag-toolbar">
      <div class="segmented compact">
      <button class="seg active" data-mode="add">添加</button>
      <button class="seg" data-mode="remove">移除</button>
      </div>
    </div>
    <div class="form-row"><input id="tagInput" placeholder="输入新标签，按回车应用"/></div>
    ${existing ? `<div class="tag-cloud">${existing}</div>` : ''}`;
  let mode = 'add';
  const syncTagChoices = () => {
    body.querySelectorAll('[data-tag]').forEach((btn) => {
      const count = Number(btn.dataset.count || 0);
      btn.disabled = mode === 'add' ? count === ids.length : count === 0;
    });
  };
  const apply = async (tag) => {
    tag = (tag || '').trim();
    if (!tag) return;
    try {
      const r = mode === 'remove' ? await Api.removeTag(ids, tag) : await Api.addTag(ids, tag);
      await refreshTagOptions(); await refreshEntries(); await loadBatches();
      toast(mode === 'remove' ? `已从 ${r.changed} 条移除「${tag}」` : `已给 ${r.changed} 条打上「${tag}」`, 'ok');
      m.close();
    } catch (e) { toast(e.message, 'err'); }
  };
  const m = modal({
    title: `标签 · ${ids.length} 条`, body,
    footer: [
      ...(State.allTags.length ? [mkBtn('管理标签', 'ghost', () => { m.close(); manageTagsFlow(); })] : []),
      mkBtn('关闭', 'ghost', () => m.close()),
    ],
  });
  const input = body.querySelector('#tagInput');
  body.querySelectorAll('[data-mode]').forEach((btn) => {
    btn.onclick = () => {
      mode = btn.dataset.mode;
      body.querySelectorAll('[data-mode]').forEach((b) => b.classList.toggle('active', b === btn));
      input.placeholder = mode === 'add' ? '输入新标签，按回车应用' : '输入要移除的标签';
      syncTagChoices();
    };
  });
  input.onkeydown = (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); apply(input.value); } };
  body.querySelectorAll('[data-tag]').forEach((btn) => { btn.onclick = () => apply(btn.dataset.tag); });
  syncTagChoices();
  setTimeout(() => input.focus(), 20);
}

async function changeSelectionProfile(idsArg, selectedProfileId = '') {
  const fromSelection = !Array.isArray(idsArg);
  const ids = fromSelection ? [...State.selected] : idsArg;
  if (!ids.length) { toast('请先选择条目', 'err'); return; }
  if (!State.profiles.length) { toast('还没有可选择的报账人', 'err'); return; }
  const body = el('div');
  body.innerHTML = `
    <div class="form-row">
      <label>改为报账人</label>
      <select id="batchProfileSelect">${profileOptionsHtml(selectedProfileId)}</select>
    </div>`;
  const m = modal({
    title: `修改报账人 · ${ids.length} 条`,
    body,
    footer: [
      mkBtn('取消', 'ghost', () => m.close()),
      mkBtn('确认修改', 'primary', async () => {
        const profileId = body.querySelector('#batchProfileSelect').value;
        try {
          const result = await Api.updateEntryProfiles(ids, profileId, State.currentProfileId);
          m.close();
          if (fromSelection) {
            State.selected.clear();
            State.lastSelectedId = null;
          }
          await refreshEntries();
          await loadBatches();
          toast(result.changed ? `已修改 ${result.changed} 条报账人` : '所选条目已经属于该报账人', 'ok');
        } catch (e) { toast(e.message, 'err'); }
      }),
    ],
  });
}

async function manageTagsFlow() {
  try { State.allTags = await Api.listTags(); } catch (e) { State.allTags = []; }
  const body = el('div');
  body.innerHTML = State.allTags.length ? `<div class="tag-manage-list">${State.allTags.map((tag) => `
    <div class="tag-manage-row" data-managed-tag="${esc(tag)}">
      <button class="tag-name-btn" data-rename-tag="${esc(tag)}" title="重命名">${esc(tag)}</button>
      <button class="tag-delete-btn" data-delete-tag="${esc(tag)}" title="删除标签" aria-label="删除标签 ${esc(tag)}">${CLOSE_ICON}</button>
    </div>`).join('')}</div>` : '<div class="hint">还没有标签。</div>';
  const m = modal({ title: '管理标签', body, footer: [mkBtn('关闭', 'ghost', () => m.close())] });
  body.querySelectorAll('[data-rename-tag]').forEach((btn) => {
    btn.onclick = () => { m.close(); renameTagFlow(btn.dataset.renameTag); };
  });
  body.querySelectorAll('[data-delete-tag]').forEach((btn) => {
    btn.onclick = async () => {
      const tag = btn.dataset.deleteTag;
      if (!confirm(`从所有条目删除标签「${tag}」？`)) return;
      try {
        const r = await Api.deleteTag(tag);
        if (State.tagFilter === tag) State.tagFilter = '';
        m.close(); await refreshTagOptions(); await refreshEntries(); await loadBatches();
        toast(`已从 ${r.changed} 条删除「${tag}」`, 'ok'); manageTagsFlow();
      } catch (e) { toast(e.message, 'err'); }
    };
  });
}

function renameTagFlow(oldTag) {
  const body = el('div');
  body.innerHTML = `<div class="form-row"><input id="renameTagInput" value="${esc(oldTag)}"/></div>`;
  const m = modal({
    title: '重命名标签', body,
    footer: [
      mkBtn('取消', 'ghost', () => { m.close(); manageTagsFlow(); }),
      mkBtn('保存', 'primary', async () => {
        const newTag = body.querySelector('#renameTagInput').value.trim();
        if (!newTag || newTag === oldTag) return;
        try {
          const r = await Api.renameTag(oldTag, newTag);
          if (State.tagFilter === oldTag) State.tagFilter = newTag;
          m.close(); await refreshTagOptions(); await refreshEntries(); await loadBatches();
          toast(`已更新 ${r.changed} 条`, 'ok'); manageTagsFlow();
        } catch (e) { toast(e.message, 'err'); }
      }),
    ],
  });
  setTimeout(() => { const input = body.querySelector('#renameTagInput'); input.focus(); input.select(); }, 20);
}

// ------------------------------------------------------------------ 卡片快捷
async function quickPaidFlow(e) {
  const cur = (await Api.getEntry(e.id)).fields?.paid_amount?.current || '';
  const body = el('div');
  body.innerHTML = `
    <div class="form-row">
      <label>实付金额</label>
      <input id="qpInput" type="number" step="0.01" value="${esc(cur)}" placeholder="例如 128.50"/>
    </div>`;
  const m = modal({
    title: '填写实付金额',
    body,
    footer: [
      mkBtn('取消', 'ghost', () => m.close()),
      mkBtn('保存', 'primary', async () => {
        try {
          await Api.updateField(e.id, 'paid_amount', body.querySelector('#qpInput').value, State.currentProfileId);
          m.close(); await syncEntryAfterChange(e.id, { affectsStatus: true }); toast('实付金额已保存', 'ok');
        } catch (err) { toast(err.message, 'err'); }
      }),
    ],
  });
  setTimeout(() => body.querySelector('#qpInput')?.focus(), 20);
}

async function quickAddAttachment(entryId, type) {
  let progress = null;
  try {
    const res = await Api.pickFiles(type === 'payment_screenshot' || type === 'physical_image');
    const paths = res.paths || [];
    if (!paths.length) return;
    progress = taskProgress(type === 'payment_screenshot'
      ? (State.paymentOcrEnabled ? '正在识别付款截图金额…' : '正在添加付款截图…')
      : '正在校验并添加材料…');
    if (type === 'payment_screenshot') {
      const infos = (await materialInfosForPaths(paths)).map((info) => ({ ...info, type }));
      progress.update('正在添加截图并处理实付金额…');
      const result = await addMaterialInfosToEntry(entryId, infos);
      await syncEntryAfterChange(entryId, { affectsStatus: true });
      toast(result.message || '材料已添加', 'ok');
    } else {
      for (const p of paths) await Api.addAttachment(entryId, p, type);
      await syncEntryAfterChange(entryId, { affectsStatus: true });
      toast('材料已添加', 'ok');
    }
  } catch (err) { toast(err.message, 'err'); }
  finally { progress?.close(); }
}

async function onlineVerificationFlow(entryId) {
  let info;
  try {
    info = await Api.invoiceVerificationInfo(entryId);
  } catch (err) {
    toast(err.message, 'err');
    return;
  }

  const watchDirectory = State.verificationWatchDirectory;
  const archiveLocationHint = watchDirectory
    ? '设置中的归档目录，或下载、桌面、文档'
    : '下载、桌面或文档';
  const body = el('div', 'verification-flow');
  body.innerHTML = `
    <div class="verification-fields">
      <label><span>发票号码</span>
        <input data-verification-field="invoice_no" value="${esc(info.invoice_no || '')}"/>
      </label>
      <label><span>开票日期 <small>YYYYMMDD</small></span>
        <input data-verification-field="invoice_date" value="${esc(info.invoice_date || '')}" inputmode="numeric"/>
      </label>
      <label><span>${esc(info.verification_value_label || '价税合计')}</span>
        <input data-verification-field="verification_value" value="${esc(info.verification_value || '')}" inputmode="decimal"
          placeholder="按发票价税合计填写"/>
      </label>
    </div>
    <div class="verification-guide">
      <ul>
        <li>验证码在官网填写，通常不区分大小写</li>
        <li>查验成功后点击官网“打印”，在系统窗口另存为 PDF</li>
        <li>保存到${esc(archiveLocationHint)}后，请等待约 1–2 秒，软件识别处理完成后会自动归入当前条目</li>
        ${State.verificationTrashSource
          ? '<li>归档完成后，原 PDF 会移到系统废纸篓或回收站</li>'
          : ''}
      </ul>
    </div>
    <div class="verification-status hidden" data-verification-status>
      <span class="verification-status-dot"></span>
      <div><b></b><small></small></div>
    </div>`;

  let sessionId = '';
  let pollTimer = null;
  let attached = false;
  let closed = false;
  const status = body.querySelector('[data-verification-status]');
  const fields = () => Object.fromEntries(
    [...body.querySelectorAll('[data-verification-field]')].map((input) => [
      input.dataset.verificationField, input.value.trim(),
    ]),
  );
  const setStatus = (state, title, detail) => {
    status.classList.remove('hidden');
    status.className = `verification-status ${state || ''}`;
    status.querySelector('b').textContent = title;
    const detailEl = status.querySelector('small');
    detailEl.textContent = detail || '';
    detailEl.hidden = !detail;
  };
  const stopPolling = () => {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  };

  const chooseBtn = mkBtn('上传已有 PDF', 'ghost verification-upload', async () => {
    try {
      const res = await Api.pickFiles(false, ['PDF 文件 (*.pdf)']);
      const path = (res.paths || [])[0];
      if (!path) return;
      setStatus('working', '正在核对查验单', '确认是否属于当前发票…');
      await Api.addAttachment(entryId, path, 'inspection_pdf', '手动选择的查验单');
      if (sessionId) await Api.closeInvoiceVerification(sessionId);
      await finishAttachment();
    } catch (err) {
      setStatus('error', '未能添加', err.message);
      toast(err.message, 'err');
    }
  });

  const poll = async () => {
    if (!sessionId || attached || closed) return;
    try {
      const result = await Api.invoiceVerificationStatus(sessionId);
      if (result.state === 'attached') {
        await finishAttachment(result);
        return;
      }
      if (result.state === 'processing') {
        setStatus('working', '正在识别 PDF', '通常需要 1–2 秒，处理完成后会自动归入条目…');
      } else if (result.message) {
        setStatus('error', '还未归档', result.message);
      } else if (result.window_closed) {
        setStatus('waiting', '查验窗口已关闭', '请重新查验，或上传已有 PDF。');
      }
    } catch (err) {
      setStatus('error', '检测中断', err.message);
      return;
    }
    pollTimer = setTimeout(poll, 1200);
  };

  const openBtn = mkBtn('打开查验官网', 'primary', async () => {
    const values = fields();
    if (!values.invoice_no || !values.invoice_date || !values.verification_value) {
      setStatus('error', '信息还不完整', '请填写发票号码、开票日期和价税合计。');
      return;
    }
    openBtn.disabled = true;
    body.querySelectorAll('[data-verification-field]').forEach((input) => { input.readOnly = true; });
    setStatus('working', '正在打开官网', '加载完成后请填写验证码并点击“查验”…');
    try {
      const result = await Api.startInvoiceVerification(entryId, values);
      sessionId = result.session_id;
      openBtn.style.display = 'none';
      setStatus('waiting', '请在官网完成查验并打印', '另存为 PDF 后请等待约 1–2 秒，软件会识别并自动归入当前条目。');
      pollTimer = setTimeout(poll, 800);
    } catch (err) {
      openBtn.disabled = false;
      body.querySelectorAll('[data-verification-field]').forEach((input) => { input.readOnly = false; });
      setStatus('error', '官网未打开', err.message);
    }
  });

  const closeBtn = mkBtn('取消', 'ghost', () => m.close());
  const finishAttachment = async (result = {}) => {
    attached = true;
    stopPolling();
    await syncEntryAfterChange(entryId, { affectsStatus: true });
    const warning = result.cleanup_warning || '';
    const detail = warning || (result.source_trashed
      ? '已归入当前条目，原 PDF 已移到废纸篓或回收站。'
      : '已归入当前条目的查验材料。');
    setStatus(warning ? 'done warning' : 'done', '查验单已保存', detail);
    openBtn.style.display = 'none';
    chooseBtn.style.display = 'none';
    closeBtn.textContent = '完成';
    closeBtn.classList.remove('ghost');
    closeBtn.classList.add('primary');
    toast(result.message || '查验单已保存到当前条目', warning ? 'err' : 'ok');
  };
  const m = modal({
    title: '在线查验',
    body,
    footer: [chooseBtn, closeBtn, openBtn],
    onClose: () => {
      closed = true;
      stopPolling();
      if (sessionId && !attached) Api.closeInvoiceVerification(sessionId).catch(() => {});
    },
  });
  m.foot.classList.add('verification-foot');
}

async function maybeSetPaidFromInvoice(entryId) {
  const entry = await Api.getEntry(entryId);
  const paid = entry.fields?.paid_amount?.current || '';
  if (!entry.total) return;
  if (paid && !sameMoney(paid, entry.total)) return;
  if (await askUseInvoiceTotal(entry.total, !!paid)) {
    if (paid) return;
    await Api.updateField(entryId, 'paid_amount', entry.total, State.currentProfileId);
  } else {
    await quickPaidFlow(entry);
  }
}

function askUseInvoiceTotal(total, alreadyDefault) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      m.close();
      resolve(value);
    };
    const body = el('div', 'hint', '付款截图已添加。');
    const m = modal({
      title: '实付金额',
      body,
      footer: [
        mkBtn('修改实付', 'ghost', () => done(false)),
        mkBtn(`${alreadyDefault ? '保持' : '按'} ${fmtMoney(total)}`, 'primary', () => done(true)),
      ],
      onClose: () => {
        if (!settled) {
          settled = true;
          resolve(false);
        }
      },
    });
  });
}

async function settlePaymentAmountAfterAdd(entryId, paymentInfos) {
  if (!paymentInfos.length) return "";
  const entry = await Api.getEntry(entryId);
  const paidField = entry.fields?.paid_amount || {};
  const paid = paidField.current || '';
  const total = entry.total || '';
  if (!State.paymentOcrEnabled) {
    if (State.defaultPaidToInvoice && total && !paid) {
      await Api.updateField(entryId, 'paid_amount', total, State.currentProfileId);
      return `已按发票金额填写实付 ${fmtMoney(total)}`;
    }
    if (State.defaultPaidToInvoice && total && sameMoney(paid, total)) {
      return `付款截图已添加，实付保持 ${fmtMoney(total)}`;
    }
    return paid ? `付款截图已添加，保留当前实付 ${fmtMoney(paid)}` : '付款截图已添加';
  }
  const currentIsUserSet = !!paid
    && (!total || !sameMoney(paid, total))
    && paidField.value_source !== 'payment_ocr';
  const amounts = paymentInfos
    .map((info) => moneyText(info.paid_amount || info.payment_ocr?.paid_amount || ''))
    .filter(Boolean);

  if (!amounts.length) {
    await maybeSetPaidFromInvoice(entryId);
    return "";
  }
  if (currentIsUserSet) {
    return `已识别付款 ${paymentAmountSummary(amounts)}，保留当前实付 ${fmtMoney(paid)}`;
  }

  const sum = sumMoneyText(amounts);
  const differsFromInvoice = !!total && !sameMoney(sum, total);
  if (paymentInfos.length === 1 && amounts.length === 1 && !differsFromInvoice) {
    await Api.setRecognizedPaidAmount(entryId, amounts[0]);
    return `已按付款截图填写实付 ${fmtMoney(amounts[0])}`;
  }

  const choice = await askUseRecognizedPaymentAmount(amounts, sum, paid, total);
  if (choice === 'use') {
    await Api.setRecognizedPaidAmount(entryId, sum);
    return `已填写实付 ${fmtMoney(sum)}`;
  } else if (choice === 'manual') {
    await quickPaidFlow(entry);
  }
  return "";
}

function moneyText(value) {
  const n = Number(String(value || '').replace(/,/g, ''));
  if (!isFinite(n) || n <= 0) return '';
  return n.toFixed(2);
}

function sumMoneyText(values) {
  const cents = values.reduce((acc, v) => acc + Math.round(Number(v) * 100), 0);
  return (cents / 100).toFixed(2);
}

function paymentAmountSummary(amounts) {
  if (amounts.length === 1) return fmtMoney(amounts[0]);
  return `${amounts.map((a) => fmtMoney(a)).join(' + ')} = ${fmtMoney(sumMoneyText(amounts))}`;
}

function askUseRecognizedPaymentAmount(amounts, sum, currentPaid = '', invoiceTotal = '') {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      m.close();
      resolve(value);
    };
    const body = el('div');
    body.innerHTML = `
      <div class="hint">识别到付款金额：${esc(paymentAmountSummary(amounts))}</div>
      ${invoiceTotal ? `<div class="hint">发票金额：${fmtMoney(invoiceTotal)}${
        sameMoney(sum, invoiceTotal) ? '' : '，与截图金额不一致，请确认。'
      }</div>` : ''}`;
    const m = modal({
      title: '实付金额',
      body,
      footer: [
        mkBtn(currentPaid ? `保持当前 ${fmtMoney(currentPaid)}` : '暂不修改', 'ghost', () => done('keep')),
        mkBtn('手动填写', 'ghost', () => done('manual')),
        mkBtn(`按截图 ${fmtMoney(sum)}`, 'primary', () => done('use')),
      ],
      onClose: () => {
        if (!settled) {
          settled = true;
          resolve('keep');
        }
      },
    });
  });
}

function openEntryMenu(x, y, e) {
  closeEntryMenu();
  const menu = el('div', 'entry-context-menu');
  const item = (label, fn, danger) => {
    const b = el('button', danger ? 'danger' : '', esc(label));
    b.onclick = async () => { closeEntryMenu(); await fn(); };
    menu.appendChild(b);
  };
  item('打开详情', () => openEntryDetail(e.id));
  item('填写实付金额', () => quickPaidFlow(e));
  item('添加付款截图', () => quickAddAttachment(e.id, 'payment_screenshot'));
  item('在线查验', () => onlineVerificationFlow(e.id));
  item('上传已有查验单', () => quickAddAttachment(e.id, 'inspection_pdf'));
  item('添加发票 PDF', () => quickAddAttachment(e.id, 'invoice_pdf'));
  if (State.ocrStatus?.available) item('阿里云识别', () => openOcrDialog([e.id]));
  item('编辑条目备注', () => quickNoteFlow(e));
  item('打标签', () => tagSelectionFlow([e.id]));
  if (actualBatchId()) item('批次催办备注', () => batchEntryNoteFlow(e));
  item(actualBatchId() ? '移出当前批次' : '批次', async () => {
    if (actualBatchId()) {
      try {
        const r = await Api.removeEntriesFromBatch(actualBatchId(), [e.id]);
        State.selected.delete(e.id);
        await loadBatches();
        await refreshEntries();
        toast(r.removed ? '已移出当前批次' : '条目已不在当前批次', 'ok');
      } catch (err) { toast(err.message, 'err'); }
      return;
    }
    await addSelectionToBatch([e.id]);
  });
  item('删除条目', () => quickDelete(e), true);
  document.body.appendChild(menu);
  menu.style.left = Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8)) + 'px';
  setTimeout(() => document.addEventListener('click', closeEntryMenu, { once: true }), 0);
}

async function batchEntryNoteFlow(e) {
  const batch = State.currentBatch || await Api.getBatch(actualBatchId());
  const body = el('div');
  body.innerHTML = `<div class="form-row"><textarea id="batchEntryNote" rows="4" placeholder="这条在本批次中的催办事项">${esc(batch?.entry_notes?.[e.id] || '')}</textarea></div>`;
  const m = modal({
    title: '批次催办备注', body,
    footer: [
      mkBtn('取消', 'ghost', () => m.close()),
      mkBtn('保存', 'primary', async () => {
        try {
          await Api.setBatchEntryNote(actualBatchId(), e.id, body.querySelector('#batchEntryNote').value.trim());
          m.close(); await refreshEntries(); toast('批次备注已保存', 'ok');
        } catch (err) { toast(err.message, 'err'); }
      }),
    ],
  });
  setTimeout(() => body.querySelector('#batchEntryNote')?.focus(), 20);
}

function closeEntryMenu() {
  const old = $('.entry-context-menu');
  if (old) old.remove();
}

async function quickNoteFlow(e) {
  const cur = (await Api.getEntry(e.id)).fields?.notes?.current || '';
  const body = el('div');
  body.innerHTML = `
    <div class="form-row" style="margin-top:14px">
      <label>条目备注</label>
      <textarea id="qnInput" rows="5" style="width:100%;font-family:inherit;font-size:13px;padding:10px;border-radius:9px;border:1px solid var(--line);resize:vertical">${esc(cur)}</textarea>
    </div>`;
  const m = modal({
    title: '条目备注', body,
    footer: [
      mkBtn('取消', 'ghost', () => m.close()),
      mkBtn('保存', 'primary', async () => {
        try { await Api.updateField(e.id, 'notes', body.querySelector('#qnInput').value, State.currentProfileId); m.close(); await syncEntryAfterChange(e.id, { searchable: true, notes: true }); toast('备注已保存', 'ok'); }
        catch (err) { toast(err.message, 'err'); }
      }),
    ],
  });
}
async function quickDelete(e) {
  if (!confirm(`确认删除「${e.seller || '该条目'}」？此操作不可撤销。`)) return;
  try {
    const result = await Api.deleteEntry(e.id);
    await loadBatches();
    await refreshEntries();
    toast(result.cleanup_warning || '已删除', result.cleanup_warning ? 'err' : 'ok');
  }
  catch (err) { toast(err.message, 'err'); }
}

// ------------------------------------------------------------------ 事件
function bindEvents() {
  if ($('#profilePill')) $('#profilePill').onclick = () => openProfileManager(false);
  $$('.chip-select select').forEach((select) => {
    select.addEventListener('change', () => requestAnimationFrame(() => select.blur()));
  });

  $$('[data-view]').forEach((btn) => {
    btn.onclick = () => setQuickView(btn.dataset.view);
  });

  let kwTimer;
  const relist = () => { State.selected.clear(); syncFilterControlStates(); refreshEntries(); };
  const relistFromAdvanced = () => { State.selected.clear(); State.quickView = 'all'; updateQuickViewButtons(); syncFilterControlStates(); refreshEntries(); };
  if ($('#filterStatus')) $('#filterStatus').onchange = relistFromAdvanced;
  $('#filterTitle').onchange = () => { State.activeTitle = $('#filterTitle').value; relist(); };
  $('#filterCheck').onchange = relistFromAdvanced;
  $('#filterProfile').onchange = relist;
  $('#sortSelect').onchange = relist;
  $('#filterKeyword').oninput = () => { showSearchHintIfEmpty(); syncFilterControlStates(); clearTimeout(kwTimer); kwTimer = setTimeout(relist, 220); };
  $('#searchClear').onclick = () => {
    $('#filterKeyword').value = '';
    showSearchHintIfEmpty();
    clearTimeout(kwTimer);
    relist();
    $('#filterKeyword').focus();
  };
  $('#filterAmountMin').oninput = () => { syncFilterControlStates(); clearTimeout(kwTimer); kwTimer = setTimeout(relist, 220); };
  $('#filterAmountMax').oninput = () => { syncFilterControlStates(); clearTimeout(kwTimer); kwTimer = setTimeout(relist, 220); };
  $('#filterDateFrom').onchange = relist;
  $('#filterDateTo').onchange = relist;

  $('#advancedToggle').onclick = () => {
    const p = $('#advancedPanel');
    p.classList.toggle('hidden');
    $('#advancedToggle').classList.toggle('active', !p.classList.contains('hidden'));
    $('#advancedToggle').setAttribute('aria-expanded', p.classList.contains('hidden') ? 'false' : 'true');
  };
  $('#clearFilters').onclick = () => clearAllFilters();

  $$('.density-btn').forEach((b) => {
    b.onclick = () => {
      $$('.density-btn').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      State.density = b.dataset.density;
      $('#entryList').dataset.density = State.density;
    };
  });

  $('#newEntryBtn').onclick = openNewEntry;
  $('#batchImportBtn').onclick = openBatchImport;
  $('#settingsBtn').onclick = openSettings;
  $('#themeToggle').onclick = toggleTheme;
  $('#appTitle').onclick = () => Api.openExternalUrl('https://github.com/totok22/tidoc').catch((e) => toast(e.message, 'err'));
  $('#appTitle').onkeydown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      $('#appTitle').click();
    }
  };
  $('#emptyNew').onclick = () => openNewEntry();
  $('#actionImport').onclick = doImport;

  $('#clearSelBtn').onclick = () => { State.suppressListAnimation = true; State.selected.clear(); State.lastSelectedId = null; renderEntries(); };
  $('#selectAllBtn').onclick = toggleSelectAllVisible;
  $('#addToBatchBtn').onclick = addSelectionToBatch;
  $('#tagBtn').onclick = () => tagSelectionFlow();
  $('#changeProfileBtn').onclick = () => changeSelectionProfile();
  $('#batchReparseBtn').onclick = batchReparse;
  $('#batchOcrBtn').onclick = () => openOcrDialog([...State.selected]);
  $('#batchSummaryBtn').onclick = () => exportSummary([...State.selected]);
  $('#batchExportBtn').onclick = () => doExport([...State.selected]);
  $('#batchPrintBtn').onclick = () => openPrintDialog([...State.selected]);
  $('#batchDeleteBtn').onclick = batchDelete;

  // 分组浏览
  $$('[data-group]').forEach((b) => {
    b.onclick = () => {
      $$('[data-group]').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      State.groupBy = b.dataset.group;
      renderEntries();
    };
  });

  // 新增筛选维度
  $('#filterTag').onchange = () => { State.tagFilter = $('#filterTag').value; relistFromAdvanced(); };
  $('#filterNotes').onchange = () => { State.notesFilter = $('#filterNotes').value; relistFromAdvanced(); };
  $('#filterPaymentCount').onchange = () => { State.paymentCountFilter = $('#filterPaymentCount').value; relistFromAdvanced(); };
  setupGlobalDrop();
  setupClipboardUpload();

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const activeModal = $('#modalRoot').lastChild;
      if (activeModal?._closeModal) {
        e.preventDefault();
        activeModal._closeModal();
        return;
      }
    }
    if (e.target.matches('input, textarea, select')) {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    if (e.key === '/') { e.preventDefault(); $('#filterKeyword').focus(); }
    else if (e.key.toLowerCase() === 'n') openNewEntry();
    else if (e.key.toLowerCase() === 't' && State.selected.size) tagSelectionFlow();
    else if (e.key === 'Escape' && State.selected.size) {
      State.selected.clear(); State.lastSelectedId = null; renderEntries();
    }
    else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'a') { e.preventDefault(); selectAllVisible(); }
  });
}

function clearAllFilters() {
  ['filterStatus', 'filterCheck', 'filterProfile', 'filterTitle', 'filterKeyword', 'filterAmountMin', 'filterAmountMax', 'filterDateFrom', 'filterDateTo', 'filterTag', 'filterNotes', 'filterPaymentCount'].forEach((id) => { const n = $('#' + id); if (n) n.value = ''; });
  State.quickView = 'all';
  updateQuickViewButtons();
  State.activeTitle = '';
  State.batchFilter = '';
  State.tagFilter = '';
  State.notesFilter = '';
  State.paymentCountFilter = '';
  showSearchHintIfEmpty();
  syncFilterControlStates();
  renderBatchFolders();
  refreshEntries();
}

// ------------------------------------------------------------------ 通用弹层
function modal({ title, subhead, titleChip, body, footer, wide, onClose }) {
  const mask = el('div', 'modal-mask');
  const box = el('div', 'modal' + (wide ? ' wide' : ''));
  const head = el('div', 'modal-head');
  const titleRow = el('div', 'modal-title-row');
  if (titleChip) titleRow.appendChild(el('span', 'title-chip lg ' + titleChip.cls, esc(titleChip.text)));
  titleRow.appendChild(el('h2', null, esc(title)));
  if (subhead) titleRow.appendChild(el('div', 'modal-subhead', esc(subhead)));
  head.appendChild(titleRow);
  const closeBtn = el('button', 'modal-close', CLOSE_ICON);
  head.appendChild(closeBtn);
  const bodyEl = el('div', 'modal-body');
  if (typeof body === 'string') bodyEl.innerHTML = body; else bodyEl.appendChild(body);
  const foot = el('div', 'modal-foot');
  (footer || []).forEach((b) => foot.appendChild(b));

  box.append(head, bodyEl, foot);
  mask.appendChild(box);
  $('#modalRoot').appendChild(mask);
  mask.style.zIndex = String(50 + $('#modalRoot').children.length);

  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    if (onClose) onClose();
    mask.remove();
  };
  mask._closeModal = close;
  closeBtn.onclick = close;
  mask.onclick = (e) => { if (e.target === mask) close(); };
  return { mask, body: bodyEl, close, foot };
}

function mkBtn(text, cls, onClick) {
  const b = el('button', 'btn' + (cls ? ' ' + cls : ''), esc(text));
  b.onclick = onClick;
  return b;
}

// ------------------------------------------------------------------ 报账人管理
function openProfileManager(forceCreate) {
  const wrap = el('div');

  function renderList() {
    const list = el('div', 'profile-list');
    if (!State.profiles.length) {
      list.innerHTML = '<div class="hint">还没有报账人。</div>';
      return list;
    }
    State.profiles.forEach((p) => {
      const row = el('div', 'profile-row');
      row.innerHTML = `<div class="profile-row-main">
        <div class="profile-row-title"><b>${esc(p.name)}</b><span class="arrow">→</span>${esc(p.reviewer)}
          ${p.is_default ? ' <span class="badge pass">默认</span>' : ''}</div>
        <div class="profile-row-extra">发票归属人</div>
      </div>`;
      const actions = el('div', 'profile-row-actions');
      actions.appendChild(mkBtn('编辑', 'small ghost', () => editProfileFlow(p, refreshProfileList)));
      if (!p.is_default) actions.appendChild(mkBtn('设为默认', 'small ghost', async () => {
        await Api.setDefaultProfile(p.id); await loadProfiles(); refreshProfileList();
      }));
      actions.appendChild(mkBtn('删除', 'small danger', async () => {
        try { await Api.deleteProfile(p.id); await loadProfiles(); refreshProfileList(); toast('已删除', 'ok'); }
        catch (e) { toast(e.message, 'err'); }
      }));
      row.appendChild(actions);
      list.appendChild(row);
    });
    return list;
  }
  function refreshProfileList() { wrap.replaceChild(renderList(), wrap.firstChild); }

  const form = el('div');
  form.innerHTML = `
    <h3 class="detail-section" style="margin:18px 0 10px"><span>新增报账人</span><span class="h3-line"></span></h3>
    <div class="form-grid">
      <div class="form-row"><label>姓名 *</label><input id="pfName" placeholder="必填"/></div>
      <div class="form-row"><label>审核人 *</label><input id="pfReviewer" placeholder="必填"/></div>
    </div>
    `;

  wrap.appendChild(renderList());
  wrap.appendChild(form);

  const addBtn = mkBtn('添加报账人', 'primary', async () => {
    const name = form.querySelector('#pfName').value.trim();
    const reviewer = form.querySelector('#pfReviewer').value.trim();
    if (!name || !reviewer) { toast('姓名与审核人必填', 'err'); return; }
    try {
      await Api.createProfile(name, reviewer, State.profiles.length === 0, {
      });
      await loadProfiles();
      refreshProfileList();
      ['pfName', 'pfReviewer'].forEach((id) => { form.querySelector('#' + id).value = ''; });
      toast('报账人已添加', 'ok');
    } catch (e) { toast(e.message, 'err'); }
  });

  const m = modal({
    title: '报账人管理',
    wide: true,
    body: wrap,
    footer: [addBtn, mkBtn('关闭', 'ghost', () => m.close())],
  });
}

// 编辑已有报账人（后端 update_profile 支持全部字段）
function editProfileFlow(p, onDone) {
  const body = el('div');
  body.innerHTML = `
    <div class="form-grid">
      <div class="form-row"><label>姓名 *</label><input id="epName" value="${esc(p.name)}"/></div>
      <div class="form-row"><label>审核人 *</label><input id="epReviewer" value="${esc(p.reviewer)}"/></div>
    </div>`;
  const m = modal({
    title: '编辑报账人', body,
    footer: [mkBtn('取消', 'ghost', () => m.close()), mkBtn('保存', 'primary', async () => {
      const name = body.querySelector('#epName').value.trim();
      const reviewer = body.querySelector('#epReviewer').value.trim();
      if (!name || !reviewer) { toast('姓名与审核人必填', 'err'); return; }
      try {
        await Api.updateProfile(p.id, {
          name, reviewer,
        });
        await loadProfiles();
        m.close(); if (onDone) onDone();
        toast('已保存', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    })],
  });
}

async function openSettings() {
  let paths, printStatus, appInfo, operatorPrefs, multiMode, paymentOcrMode;
  let defaultPaidMode, defaultEntryTitleMode, materialRequirementsMode, bindleNotesMode, bindleTagsMode;
  let autoUpdateMode, maintenance, verificationPrefs, ocrStatus;
  const themeMode = State.themeMode;
  try {
    paths = await Api.dataRoot();
    printStatus = await Api.printComponentStatus();
    ocrStatus = await Api.ocrComponentStatus();
    appInfo = await Api.appInfo();
    maintenance = await Api.storageMaintenanceStatus();
    const prefValues = await Promise.all([
      Api.appPreference(OPERATOR_PREF_KEYS.name, ''),
      Api.appPreference(OPERATOR_PREF_KEYS.student_id, ''),
      Api.appPreference(OPERATOR_PREF_KEYS.contact, ''),
      Api.appPreference(OPERATOR_PREF_KEYS.bank_name, ''),
      Api.appPreference(OPERATOR_PREF_KEYS.bank_card, ''),
      Api.appPreference(MULTI_CLAIMANT_KEY, State.multiClaimantMode ? '1' : '0'),
      Api.appPreference(PAYMENT_OCR_KEY, State.paymentOcrEnabled ? '1' : '0'),
      Api.appPreference(DEFAULT_PAID_TO_INVOICE_KEY, State.defaultPaidToInvoice ? '1' : '0'),
      Api.appPreference(DEFAULT_ENTRY_TITLE_KEY, State.defaultEntryTitle || ''),
      Api.materialRequirements(),
      Api.appPreference(BINDLE_INCLUDE_NOTES_KEY, '1'),
      Api.appPreference(BINDLE_INCLUDE_TAGS_KEY, '1'),
      Api.appPreference(AUTO_UPDATE_KEY, '0'),
      Api.invoiceVerificationPreferences(),
    ]);
    operatorPrefs = {
      name: prefValues[0],
      student_id: prefValues[1],
      contact: prefValues[2],
      bank_name: prefValues[3],
      bank_card: prefValues[4],
    };
    multiMode = prefValues[5] === '1';
    paymentOcrMode = prefValues[6] !== '0';
    defaultPaidMode = prefValues[7] !== '0';
    defaultEntryTitleMode = prefValues[8] || '';
    materialRequirementsMode = { ...DEFAULT_MATERIAL_REQUIREMENTS, ...(prefValues[9] || {}), invoice: true };
    bindleNotesMode = prefValues[10] !== '0';
    bindleTagsMode = prefValues[11] !== '0';
    autoUpdateMode = prefValues[12] === '1';
    verificationPrefs = prefValues[13];
    State.defaultEntryTitle = defaultEntryTitleMode;
    State.materialRequirements = materialRequirementsMode;
  } catch (e) { toast(e.message, 'err'); return; }
  const body = el('div');

  const profileCount = State.profiles.length;
  const defaultProfile = State.profiles.find((p) => p.is_default);
  const printBadge = printStatus.available
    ? '<span class="settings-ok">已安装</span>'
    : '<span class="settings-warn">未安装</span>';
  const ocrBadge = ocrStatus.available
    ? '<span class="settings-ok">已安装</span>'
    : '<span class="settings-warn">未安装</span>';
  const hasAvailableUpdate = (State.updateStatus?.updates || []).some(
    (item) => item.available
  );

  body.innerHTML = `
    <div class="settings-shell">
      <!-- 报账人 -->
      <div class="settings-block">
        <div class="settings-row" id="setProfiles">
          <div class="settings-row-copy">
            <b>报账人</b>
            <span>${profileCount ? `${profileCount} 个${defaultProfile ? ' · 默认 ' + esc(defaultProfile.name) : ''}` : '0 个'}</span>
          </div>
          <button class="btn small" id="setProfilesManage">管理</button>
        </div>
        <div class="settings-row">
          <div class="settings-row-copy">
            <b>代填模式</b>
            <span>新建/导入时选择报账人</span>
          </div>
          <label class="switch-line"><input type="checkbox" id="setMultiClaimant" ${multiMode ? 'checked' : ''}/><span>${multiMode ? '已开启' : '已关闭'}</span></label>
        </div>
      </div>

      <!-- 抬头与税号 -->
      <div class="settings-block">
        <div class="settings-block-title">抬头与税号</div>
        <div class="settings-row">
          <div class="settings-row-copy">
            <b>报账抬头</b>
            <span>发票识别与校验按这些抬头、税号进行，可添加其他学校或单位</span>
          </div>
        </div>
        <div id="setTitleProfiles" class="settings-titleprofile-list"></div>
        <div class="settings-row-actions">
          <button class="btn small ghost" id="setTpAdd">添加抬头</button>
        </div>
      </div>

      <!-- 偏好 -->
      <div class="settings-block">
        <div class="settings-block-title">偏好</div>
        <div class="settings-row">
          <div class="settings-row-copy">
            <b>启动默认抬头</b>
            <span>打开软件时默认聚焦哪个抬头分区</span>
          </div>
          <select id="setDefaultTitle" class="settings-select">
            <option value="">全部</option>
            ${(State.titleOptions.length ? State.titleOptions : configuredTitleNames()).map((title) =>
              `<option value="${esc(title)}">${esc(TITLE_SHORT[title] || title)}</option>`).join('')}
          </select>
        </div>
        <div class="settings-row">
          <div class="settings-row-copy">
            <b>默认列表密度</b>
            <span>条目列表的默认松紧</span>
          </div>
          <select id="setDefaultDensity" class="settings-select">
            <option value="comfortable">标准</option>
            <option value="compact">精简</option>
          </select>
        </div>
        <div class="settings-row settings-theme-row">
          <div class="settings-row-copy">
            <b>外观主题</b>
            <span>跟随系统时会随电脑的浅色、深色外观切换</span>
          </div>
          <div class="segmented compact theme-segmented" role="group" aria-label="外观主题">
            ${[
              ['system', '跟随系统'],
              ['light', '浅色'],
              ['dark', '深色'],
            ].map(([value, label]) => `<button type="button" class="seg theme-mode-btn${themeMode === value ? ' active' : ''}" data-theme-mode="${value}" aria-pressed="${themeMode === value ? 'true' : 'false'}">${label}</button>`).join('')}
          </div>
        </div>
        <div class="settings-row">
          <div class="settings-row-copy">
            <b>付款截图 OCR</b>
            <span>关闭后不识别截图金额，单独拖入截图时直接手动选择条目</span>
          </div>
          <label class="switch-line"><input type="checkbox" id="setPaymentOcr" ${paymentOcrMode ? 'checked' : ''}/><span>${paymentOcrMode ? '已开启' : '已关闭'}</span></label>
        </div>
        <div class="settings-row">
          <div class="settings-row-copy">
            <b>实付默认等于发票金额</b>
            <span>开启后，新建或批量导入条目时自动填写；关闭后留空待确认</span>
          </div>
          <label class="switch-line"><input type="checkbox" id="setDefaultPaidInvoice" ${defaultPaidMode ? 'checked' : ''}/><span>${defaultPaidMode ? '已开启' : '已关闭'}</span></label>
        </div>
        <div class="settings-row">
          <div class="settings-row-copy">
            <b>绑定包包含备注</b>
            <span>导出条目备注、附件备注及备注修改记录</span>
          </div>
          <label class="switch-line"><input type="checkbox" id="setBindleNotes" ${bindleNotesMode ? 'checked' : ''}/><span>${bindleNotesMode ? '已开启' : '已关闭'}</span></label>
        </div>
        <div class="settings-row">
          <div class="settings-row-copy">
            <b>绑定包包含标签</b>
            <span>关闭后，导出的绑定包不会带出条目标签</span>
          </div>
          <label class="switch-line"><input type="checkbox" id="setBindleTags" ${bindleTagsMode ? 'checked' : ''}/><span>${bindleTagsMode ? '已开启' : '已关闭'}</span></label>
        </div>
      </div>

      <!-- 扩展 -->
      <details class="settings-block">
        <summary class="settings-block-title">扩展</summary>
        <div class="settings-row">
          <div class="settings-row-copy">
            <b>新建默认抬头</b>
            <span>新建或批量导入时自动带入，可在当前弹窗中修改</span>
          </div>
          <select id="setDefaultEntryTitle" class="settings-select">
            ${titleChoiceOptions(defaultEntryTitleMode, '跟随发票识别')}
          </select>
        </div>
        <div class="settings-requirements-head">
          <div class="settings-row-copy">
            <b>材料要求</b>
            <span>只有标为“必需”的项目会影响条目的材料齐备状态</span>
          </div>
        </div>
        <div class="settings-requirement-list">
          <div class="settings-requirement-row">
            <div class="settings-row-copy"><b>发票</b><span>发票 PDF 或 XML</span></div>
            <span class="settings-fixed-required">必需</span>
          </div>
          ${[
            ['payment_screenshot', '付款截图', '付款凭证图片'],
            ['physical_image', '实物图', '物资照片'],
            ['inspection_pdf', '查验单', '发票查验单 PDF'],
            ['paid_amount', '实付金额', '实际支付金额'],
          ].map(([key, label, hint]) => `
            <div class="settings-requirement-row">
              <div class="settings-row-copy"><b>${label}</b><span>${hint}</span></div>
              <label class="switch-line"><input type="checkbox" data-material-requirement="${key}" ${materialRequirementsMode[key] ? 'checked' : ''}/><span>${materialRequirementsMode[key] ? '必需' : '可选'}</span></label>
            </div>`).join('')}
        </div>
      </details>

      <!-- 查验单归档 -->
      <div class="settings-block">
        <div class="settings-block-title">查验单</div>
        <div class="settings-row">
          <div class="settings-row-copy">
            <b>额外归档目录</b>
            <span>除下载、桌面和文档外，再监测一个常用保存位置</span>
            <code class="settings-inline-path" id="setVerificationWatchPath"
              data-tooltip-overflow="${esc(verificationPrefs.watch_directory || '')}">${esc(verificationPrefs.watch_directory || '未设置')}</code>
          </div>
          <div class="settings-row-controls">
            <button class="btn small ghost" id="setVerificationWatchPick">选择</button>
            <button class="btn small ghost" id="setVerificationWatchClear"
              ${verificationPrefs.watch_directory ? '' : 'disabled'}>清除</button>
          </div>
        </div>
        <div class="settings-row">
          <div class="settings-row-copy">
            <b>归档后清理原 PDF</b>
            <span>成功复制到条目后，将原文件移到系统废纸篓或回收站；需要时可以恢复</span>
          </div>
          <label class="switch-line"><input type="checkbox" id="setVerificationTrash"
            ${verificationPrefs.trash_source_after_archive ? 'checked' : ''}/><span>${verificationPrefs.trash_source_after_archive ? '已开启' : '已关闭'}</span></label>
        </div>
      </div>

      <!-- 数据位置 -->
      <div class="settings-block">
        <div class="settings-block-title">数据位置</div>
        <div class="settings-datapath">
          <code data-tooltip-overflow="${esc(paths.root)}">${esc(paths.root)}</code>
          <span class="settings-datapath-tag">${paths.is_default ? '系统默认位置' : '自定义位置'}</span>
        </div>
        <div class="settings-row-actions">
          <button class="btn small ghost" id="setOpenData">打开文件夹</button>
          <button class="btn small ghost" id="setOpenExports">打开导出目录 · ${fmtBytes(maintenance.exports_size || 0)}</button>
          <button class="btn small ghost" id="setCleanup" ${maintenance.files ? '' : 'disabled'}>清理临时文件${maintenance.size ? ` · ${fmtBytes(maintenance.size)}` : ''}</button>
        </div>
        <details class="settings-advanced" id="paymentInfoFold">
          <summary>收款信息</summary>
          <div class="form-grid settings-form-grid">
            <div class="form-row"><label>姓名</label><input id="opName" value="${esc(operatorPrefs.name)}"/></div>
            <div class="form-row"><label>学号</label><input id="opStudent" value="${esc(operatorPrefs.student_id)}"/></div>
            <div class="form-row"><label>电话</label><input id="opContact" value="${esc(operatorPrefs.contact)}"/></div>
            <div class="form-row"><label>开户行</label><input id="opBank" value="${esc(operatorPrefs.bank_name)}"/></div>
            <div class="form-row"><label>卡号</label><input id="opCard" value="${esc(operatorPrefs.bank_card)}"/></div>
          </div>
          <div class="settings-row-actions">
            <button class="btn small" id="setSaveOperator">保存身份</button>
          </div>
        </details>
        <details class="settings-advanced">
          <summary>高级数据维护</summary>
          <div class="settings-row-actions">
            <button class="btn small" id="setMigrate">迁移到新位置…</button>
            ${paths.is_default ? '' : '<button class="btn small ghost" id="setResetData">恢复默认位置</button>'}
          </div>
        </details>
      </div>

      <!-- 阿里云 OCR -->
      <div class="settings-block">
        <div class="settings-block-title">阿里云 OCR</div>
        <div class="settings-row is-actionable" id="setOcrManage">
          <div class="settings-row-copy">
            <b>识别组件 ${ocrBadge}</b>
            <span>补齐本地遗漏并提供云端结果比对；按量计费</span>
          </div>
          <button class="btn small ghost">管理</button>
        </div>
        <div class="settings-row ocr-key-row">
          <div class="settings-row-copy">
            <b>AccessKey</b>
            <span id="setOcrKeyHint">${ocrStatus.credentials_configured
              ? `已配置 ${esc(ocrStatus.access_key_id_masked)} · 本机累计调用 ${ocrStatus.total_calls} 次`
              : `未配置 · 建议使用只授权「文字识别 OCR」的 RAM 账号 Key`}</span>
          </div>
          <div class="ocr-key-controls">
            <input id="setOcrKeyId" placeholder="AccessKey ID" autocomplete="off"/>
            <input id="setOcrKeySecret" type="password" placeholder="AccessKey Secret" autocomplete="new-password"/>
            <button class="btn small" id="setOcrSaveKey">保存</button>
            <button class="btn small ghost" id="setOcrClearKey" ${ocrStatus.credentials_configured ? '' : 'disabled'}>清除</button>
          </div>
        </div>
        <div class="settings-row">
          <div class="settings-row-copy">
            <b>密钥说明</b>
            <span>密钥仅保存在本机数据里，不随导出、绑定包外传；调用只在点击「云识别」时发生 <button class="link-btn" id="setOcrConsole">阿里云 OCR 控制台</button></span>
          </div>
        </div>
      </div>

      <!-- 可选组件与更新 -->
      <div class="settings-block">
        <div class="settings-block-title">组件与更新</div>
        <div class="settings-row is-actionable" id="setComponentsUpdate">
          <div class="settings-row-copy">
            <b>软件与组件 ${hasAvailableUpdate ? '<span class="settings-warn">有可用更新</span>' : ''}</b>
            <span>tidoc v${esc(appInfo.version)} · 打印导出组件 ${printBadge} · OCR 识别组件 ${ocrBadge}</span>
          </div>
          <button class="btn small ghost">管理</button>
        </div>
        <div class="settings-row">
          <div class="settings-row-copy">
            <b>启动后检查更新</b>
          </div>
          <label class="switch-line"><input type="checkbox" id="setAutoUpdate" ${autoUpdateMode ? 'checked' : ''}/><span>${autoUpdateMode ? '已开启' : '已关闭'}</span></label>
        </div>
      </div>

      <!-- 关于 -->
      <div class="settings-block about">
        <img src="assets/tidoc-logo-128.png" alt="" id="setRepoLogo" title="打开 GitHub 仓库" />
        <div class="settings-about-copy">
          <b>${esc(appInfo.name)} <small>v${esc(appInfo.version)}</small></b>
          <div class="settings-credit"><button class="link-btn" id="setBitfsae">BITFSAE</button><span>出品</span></div>
          <div class="settings-about-actions">
            <button class="link-btn with-icon" id="setRepo">${wrapSvg(I.github, 14)}<span>GitHub</span></button>
            <button class="link-btn with-icon" id="setBilibili">${wrapSvg(I.bilibili, 14)}<span>视频说明</span></button>
            <button class="link-btn with-icon" id="setDocGuide">${wrapSvg(I.doc, 14)}<span>说明文档</span></button>
            <button class="link-btn" id="setGuide">使用提示</button>
          </div>
        </div>
      </div>
    </div>`;

  // 偏好回填
  body.querySelector('#setDefaultTitle').value = localStorage.getItem('tidoc.defaultTitle') || '';
  body.querySelector('#setDefaultDensity').value = localStorage.getItem('tidoc.defaultDensity') || 'comfortable';
  body.querySelector('#setDefaultTitle').onchange = (ev) => {
    localStorage.setItem('tidoc.defaultTitle', ev.target.value);
    State.activeTitle = ev.target.value;
    const titleSel = $('#filterTitle');
    if (titleSel) titleSel.value = State.activeTitle;
    State.selected.clear();
    refreshEntries();
    toast('已保存', 'ok');
  };
  body.querySelector('#setDefaultDensity').onchange = (ev) => {
    localStorage.setItem('tidoc.defaultDensity', ev.target.value);
    State.density = ev.target.value;
    $('#entryList').dataset.density = State.density;
    toast('已保存', 'ok');
  };
  const themeButtons = [...body.querySelectorAll('[data-theme-mode]')];
  const syncThemeButtons = (mode) => {
    themeButtons.forEach((button) => {
      const active = button.dataset.themeMode === mode;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  };
  themeButtons.forEach((button) => {
    button.onclick = async () => {
      const nextMode = normalizeThemeMode(button.dataset.themeMode);
      if (nextMode === State.themeMode) return;
      const previousMode = State.themeMode;
      themeButtons.forEach((item) => { item.disabled = true; });
      await animateThemeChange(nextMode, button, { persist: true });
      syncThemeButtons(nextMode);
      try {
        await Api.setAppPreference(THEME_KEY, nextMode);
        toast('外观主题已保存', 'ok');
      } catch (e) {
        applyTheme(previousMode, { persist: true });
        syncThemeButtons(previousMode);
        toast(e.message, 'err');
      } finally {
        themeButtons.forEach((item) => { item.disabled = false; });
      }
    };
  });
  body.querySelector('#setDefaultEntryTitle').onchange = async (ev) => {
    const value = ev.target.value;
    ev.target.disabled = true;
    try {
      await Api.setAppPreference(DEFAULT_ENTRY_TITLE_KEY, value);
      State.defaultEntryTitle = value;
      if (value) localStorage.setItem(DEFAULT_ENTRY_TITLE_KEY, value);
      else localStorage.removeItem(DEFAULT_ENTRY_TITLE_KEY);
      toast(value ? `新建默认抬头已设为「${value}」` : '新建默认抬头已改为跟随发票识别', 'ok');
    } catch (e) {
      ev.target.value = State.defaultEntryTitle || '';
      toast(e.message, 'err');
    } finally {
      ev.target.disabled = false;
    }
  };
  body.querySelectorAll('[data-material-requirement]').forEach((input) => {
    input.onchange = async () => {
      const key = input.dataset.materialRequirement;
      const enabled = input.checked;
      const label = input.nextElementSibling;
      input.disabled = true;
      try {
        const next = { ...State.materialRequirements, [key]: enabled, invoice: true };
        State.materialRequirements = await Api.setMaterialRequirements(next);
        label.textContent = enabled ? '必需' : '可选';
        await loadBatches();
        await refreshEntries();
        toast(`${key === 'paid_amount' ? '实付金额' : key === 'payment_screenshot' ? '付款截图' : key === 'physical_image' ? '实物图' : '查验单'}已设为${enabled ? '必需' : '可选'}`, 'ok');
      } catch (e) {
        input.checked = !enabled;
        toast(e.message, 'err');
      } finally {
        input.disabled = false;
      }
    };
  });
  // 抬头与税号配置：就地编辑，失焦/移除即保存，并同步各处抬头下拉。
  const tpList = body.querySelector('#setTitleProfiles');
  const titleProfileRowHtml = (profile = {}) => `
    <input data-tp-name placeholder="抬头名称" value="${esc(profile.name || '')}"/>
    <input data-tp-tax placeholder="税号（可选）" value="${esc(profile.tax_id || '')}"/>
    <button type="button" class="btn small ghost" data-tp-remove title="移除该抬头">移除</button>`;
  const renderTitleProfileRows = () => {
    tpList.innerHTML = '';
    State.titleProfiles.forEach((profile) => {
      tpList.appendChild(el('div', 'settings-titleprofile-row', titleProfileRowHtml(profile)));
    });
    if (!State.titleProfiles.length) {
      tpList.appendChild(el('div', 'settings-titleprofile-empty', '未配置抬头：只提示明细与金额问题，不按抬头校验。'));
    }
  };
  const refillSettingsTitleSelects = () => {
    const defSel = body.querySelector('#setDefaultTitle');
    const current = defSel.value;
    const titles = State.titleOptions.length ? State.titleOptions : configuredTitleNames();
    defSel.innerHTML = '<option value="">全部</option>' + titles.map((title) =>
      `<option value="${esc(title)}"${title === current ? ' selected' : ''}>${esc(TITLE_SHORT[title] || title)}</option>`).join('');
    if (current && !titles.includes(current)) {
      defSel.value = '';
      State.activeTitle = '';
    }
    const entrySel = body.querySelector('#setDefaultEntryTitle');
    if (entrySel) entrySel.innerHTML = titleChoiceOptions(State.defaultEntryTitle, '跟随发票识别');
  };
  const saveTitleProfiles = async () => {
    const profiles = [...tpList.querySelectorAll('.settings-titleprofile-row')].map((row) => ({
      name: row.querySelector('[data-tp-name]').value.trim(),
      tax_id: row.querySelector('[data-tp-tax]').value.trim(),
    })).filter((profile) => profile.name || profile.tax_id);
    try {
      const r = await Api.setTitleProfiles(profiles);
      State.titleProfiles = r.profiles || [];
      renderTitleProfileRows();
      await refreshTitleOptions();
      refillSettingsTitleSelects();
      toast('抬头与税号已保存', 'ok');
    } catch (e) { toast(e.message, 'err'); }
  };
  renderTitleProfileRows();
  tpList.addEventListener('click', (ev) => {
    const remove = ev.target.closest('[data-tp-remove]');
    if (remove) {
      remove.closest('.settings-titleprofile-row').remove();
      saveTitleProfiles();
    }
  });
  tpList.addEventListener('change', (ev) => {
    if (ev.target.matches('[data-tp-name], [data-tp-tax]')) saveTitleProfiles();
  });
  body.querySelector('#setTpAdd').onclick = () => {
    renderTitleProfileRows();
    const row = el('div', 'settings-titleprofile-row', titleProfileRowHtml());
    tpList.appendChild(row);
    row.querySelector('[data-tp-name]').focus();
  };
  body.querySelector('#setMultiClaimant').onchange = async (ev) => {
    const enabled = ev.target.checked;
    const value = enabled ? '1' : '0';
    const label = ev.target.nextElementSibling;
    ev.target.disabled = true;
    try {
      await Api.setAppPreference(MULTI_CLAIMANT_KEY, value);
      State.multiClaimantMode = enabled;
      localStorage.setItem(MULTI_CLAIMANT_KEY, value);
      label.textContent = enabled ? '已开启' : '已关闭';
      toast('已保存', 'ok');
    } catch (e) {
      ev.target.checked = !enabled;
      toast(e.message, 'err');
    } finally {
      ev.target.disabled = false;
    }
  };
  body.querySelector('#setPaymentOcr').onchange = async (ev) => {
    const enabled = ev.target.checked;
    const label = ev.target.nextElementSibling;
    ev.target.disabled = true;
    try {
      await Api.setAppPreference(PAYMENT_OCR_KEY, enabled ? '1' : '0');
      State.paymentOcrEnabled = enabled;
      label.textContent = enabled ? '已开启' : '已关闭';
      toast(enabled ? '已开启付款截图金额识别' : '已关闭付款截图金额识别', 'ok');
    } catch (e) {
      ev.target.checked = !enabled;
      toast(e.message, 'err');
    } finally {
      ev.target.disabled = false;
    }
  };
  body.querySelector('#setDefaultPaidInvoice').onchange = async (ev) => {
    const enabled = ev.target.checked;
    const label = ev.target.nextElementSibling;
    ev.target.disabled = true;
    try {
      await Api.setAppPreference(DEFAULT_PAID_TO_INVOICE_KEY, enabled ? '1' : '0');
      State.defaultPaidToInvoice = enabled;
      label.textContent = enabled ? '已开启' : '已关闭';
      toast(enabled ? '新条目将默认填写发票金额' : '新条目实付金额将默认留空', 'ok');
    } catch (e) {
      ev.target.checked = !enabled;
      toast(e.message, 'err');
    } finally {
      ev.target.disabled = false;
    }
  };
  const bindlePreferenceHandler = (key, enabledText, disabledText) => async (ev) => {
    const enabled = ev.target.checked;
    const label = ev.target.nextElementSibling;
    ev.target.disabled = true;
    try {
      await Api.setAppPreference(key, enabled ? '1' : '0');
      label.textContent = enabled ? '已开启' : '已关闭';
      toast(enabled ? enabledText : disabledText, 'ok');
    } catch (e) {
      ev.target.checked = !enabled;
      toast(e.message, 'err');
    } finally {
      ev.target.disabled = false;
    }
  };
  body.querySelector('#setBindleNotes').onchange = bindlePreferenceHandler(
    BINDLE_INCLUDE_NOTES_KEY,
    '绑定包将包含备注',
    '绑定包将不包含备注',
  );
  body.querySelector('#setBindleTags').onchange = bindlePreferenceHandler(
    BINDLE_INCLUDE_TAGS_KEY,
    '绑定包将包含标签',
    '绑定包将不包含标签',
  );
  const renderVerificationWatchDirectory = (path) => {
    const pathNode = body.querySelector('#setVerificationWatchPath');
    const clearBtn = body.querySelector('#setVerificationWatchClear');
    pathNode.textContent = path || '未设置';
    pathNode.dataset.tooltipOverflow = path || '';
    clearBtn.disabled = !path;
  };
  body.querySelector('#setVerificationWatchPick').onclick = async (ev) => {
    ev.target.disabled = true;
    try {
      const picked = await Api.pickFolder();
      if (!picked.path) return;
      const result = await Api.setInvoiceVerificationPreferences({
        watch_directory: picked.path,
      });
      State.verificationWatchDirectory = result.watch_directory || '';
      renderVerificationWatchDirectory(State.verificationWatchDirectory);
      toast('查验单归档目录已保存', 'ok');
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      ev.target.disabled = false;
    }
  };
  body.querySelector('#setVerificationWatchClear').onclick = async (ev) => {
    ev.target.disabled = true;
    try {
      const result = await Api.setInvoiceVerificationPreferences({
        watch_directory: '',
      });
      State.verificationWatchDirectory = result.watch_directory || '';
      renderVerificationWatchDirectory(State.verificationWatchDirectory);
      toast('已恢复默认监测目录', 'ok');
    } catch (e) {
      ev.target.disabled = false;
      toast(e.message, 'err');
    }
  };
  body.querySelector('#setVerificationTrash').onchange = async (ev) => {
    const enabled = ev.target.checked;
    const label = ev.target.nextElementSibling;
    ev.target.disabled = true;
    try {
      const result = await Api.setInvoiceVerificationPreferences({
        trash_source_after_archive: enabled,
      });
      State.verificationTrashSource = !!result.trash_source_after_archive;
      label.textContent = State.verificationTrashSource ? '已开启' : '已关闭';
      toast(
        State.verificationTrashSource
          ? '归档后会把原 PDF 移到废纸篓或回收站'
          : '归档后将保留原 PDF',
        'ok',
      );
    } catch (e) {
      ev.target.checked = !enabled;
      toast(e.message, 'err');
    } finally {
      ev.target.disabled = false;
    }
  };
  body.querySelector('#setAutoUpdate').onchange = async (ev) => {
    const enabled = ev.target.checked;
    const label = ev.target.nextElementSibling;
    ev.target.disabled = true;
    try {
      await Api.setAppPreference(AUTO_UPDATE_KEY, enabled ? '1' : '0');
      label.textContent = enabled ? '已开启' : '已关闭';
      if (enabled) {
        toast('已开启自动检查', 'ok');
        maybeAutoCheckUpdates(true);
      } else {
        setUpdateNotice(null);
        toast('已关闭自动检查', 'ok');
      }
    } catch (e) {
      ev.target.checked = !enabled;
      toast(e.message, 'err');
    } finally {
      ev.target.disabled = false;
    }
  };
  body.querySelector('#setSaveOperator').onclick = async () => {
    const values = {
      name: body.querySelector('#opName').value.trim(),
      student_id: body.querySelector('#opStudent').value.trim(),
      contact: body.querySelector('#opContact').value.trim(),
      bank_name: body.querySelector('#opBank').value.trim(),
      bank_card: body.querySelector('#opCard').value.trim(),
    };
    try {
      await Promise.all([
        Api.setAppPreference(OPERATOR_PREF_KEYS.name, values.name),
        Api.setAppPreference(OPERATOR_PREF_KEYS.student_id, values.student_id),
        Api.setAppPreference(OPERATOR_PREF_KEYS.contact, values.contact),
        Api.setAppPreference(OPERATOR_PREF_KEYS.bank_name, values.bank_name),
        Api.setAppPreference(OPERATOR_PREF_KEYS.bank_card, values.bank_card),
      ]);
      toast('已保存', 'ok');
    } catch (e) { toast(e.message, 'err'); }
  };

  body.querySelector('#setProfilesManage').onclick = () => { m.close(); openProfileManager(false); };
  body.querySelector('#setComponentsUpdate').onclick = () => { m.close(); openUpdateDialog(); };
  body.querySelector('#setOcrManage').onclick = () => { m.close(); openUpdateDialog(); };
  body.querySelector('#setOcrConsole').onclick = () => Api.openExternalUrl(OCR_CONSOLE_URL).catch((e) => toast(e.message, 'err'));
  body.querySelector('#setOcrSaveKey').onclick = async (ev) => {
    const keyId = body.querySelector('#setOcrKeyId').value.trim();
    const secret = body.querySelector('#setOcrKeySecret').value.trim();
    if (!keyId || !secret) { toast('更换密钥需完整填写 AccessKey ID 和 Secret', 'err'); return; }
    ev.target.disabled = true;
    try {
      const r = await Api.saveOcrCredentials(keyId, secret);
      State.ocrStatus = await Api.ocrComponentStatus();
      body.querySelector('#setOcrKeyHint').innerHTML =
        `已配置 ${esc(r.access_key_id_masked)} · 本机累计调用 ${State.ocrStatus.total_calls} 次`;
      body.querySelector('#setOcrKeyId').value = '';
      body.querySelector('#setOcrKeySecret').value = '';
      body.querySelector('#setOcrClearKey').disabled = false;
      refreshOcrStatus();
      toast('阿里云密钥已保存', 'ok');
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      ev.target.disabled = false;
    }
  };
  body.querySelector('#setOcrClearKey').onclick = async (ev) => {
    if (!confirm('清除已保存的阿里云密钥？清除后云识别不可用，直到重新填写。')) return;
    ev.target.disabled = true;
    try {
      await Api.clearOcrCredentials();
      State.ocrStatus = await Api.ocrComponentStatus();
      body.querySelector('#setOcrKeyHint').innerHTML =
        '未配置 · 建议使用只授权「文字识别 OCR」的 RAM 账号 Key';
      refreshOcrStatus();
      toast('已清除阿里云密钥', 'ok');
    } catch (e) {
      toast(e.message, 'err');
      ev.target.disabled = false;
    }
  };
  body.querySelector('#setGuide').onclick = () => openUsageGuide(false);
  body.querySelector('#setBitfsae').onclick = () => Api.openExternalUrl('https://www.bitfsae.com').catch((e) => toast(e.message, 'err'));
  body.querySelector('#setRepo').onclick = () => Api.openExternalUrl(appInfo.repository).catch((e) => toast(e.message, 'err'));
  body.querySelector('#setBilibili').onclick = () => Api.openExternalUrl(BILIBILI_GUIDE_URL).catch((e) => toast(e.message, 'err'));
  body.querySelector('#setDocGuide').onclick = () => Api.openExternalUrl(DOC_GUIDE_URL).catch((e) => toast(e.message, 'err'));
  body.querySelector('#setRepoLogo').onclick = () => Api.openExternalUrl(appInfo.repository).catch((e) => toast(e.message, 'err'));
  body.querySelector('#setOpenData').onclick = () => Api.openPath(paths.root).catch((e) => toast(e.message, 'err'));
  body.querySelector('#setOpenExports').onclick = () => Api.openPath(paths.exports).catch((e) => toast(e.message, 'err'));
  body.querySelector('#setMigrate').onclick = async () => {
    if (!confirm('迁移数据到新位置？请选择一个空文件夹。迁移过程中请勿关闭软件。')) return;
    try {
      const r = await Api.chooseAndMigrateDataRoot();
      if (r && r.changed) { m.close(); toast('数据已迁移到新位置', 'ok'); openSettings(); }
    } catch (e) { toast(e.message, 'err'); }
  };
  const resetBtn = body.querySelector('#setResetData');
  if (resetBtn) resetBtn.onclick = async () => {
    if (!confirm('把数据迁回系统默认位置？')) return;
    try {
      const r = await Api.resetDataRootToDefault();
      if (r && r.changed) { m.close(); toast('已恢复默认位置', 'ok'); openSettings(); }
    } catch (e) { toast(e.message, 'err'); }
  };
  body.querySelector('#setCleanup').onclick = async (ev) => {
    ev.target.disabled = true;
    try {
      const r = await Api.cleanupAppCache();
      ev.target.textContent = '暂无可清理文件';
      toast(r.files ? `已释放 ${fmtBytes(r.size)}` : '暂无可清理文件', 'ok');
    } catch (e) {
      ev.target.disabled = false;
      toast(e.message, 'err');
    }
  };

  const m = modal({
    title: '设置',
    body,
    wide: true,
    footer: [mkBtn('关闭', 'ghost', () => m.close())],
  });
}

// 更新对话框里的可选组件：安装/修复走同一套流程，只差名称与安装入口
const UPDATE_COMPONENTS = {
  print: {
    name: '打印导出组件',
    description: '负责材料 PDF 拼接、付款截图排版与编号，以及报账说明、验收单 Word；独立版本，只有组件本身变化时才需更新。',
    busy: '正在下载、校验并安装打印导出组件…',
    install: () => Api.installPrintComponent(),
  },
  ocr: {
    name: 'OCR 识别组件',
    description: '负责调用阿里云发票识别，补齐本地遗漏并提供结果比对；需要在设置的「阿里云 OCR」里填写密钥后使用；独立版本。',
    busy: '正在下载、校验并安装 OCR 识别组件…',
    install: () => Api.installOcrComponent(),
  },
};

async function openUpdateDialog() {
  const body = el('div', 'update-shell', `
    <div class="update-loading">
      <div class="update-progress"><span></span></div>
      <span>正在检查可用版本…</span>
    </div>`);
  let appInfo = { version: '', releases: 'https://github.com/totok22/tidoc/releases/latest' };
  try { appInfo = await Api.appInfo(); } catch (e) {}
  const m = modal({
    title: '软件更新',
    body,
    wide: true,
    footer: [mkBtn('关闭', 'ghost', () => m.close())],
  });
  const setBusy = (label) => {
    body.querySelectorAll('button').forEach((btn) => { btn.disabled = true; });
    const op = body.querySelector('#updateOperation');
    if (op) op.innerHTML = `<div class="update-progress"><span></span></div><div class="hint">${esc(label)}</div>`;
  };
  const openReleases = () => Api.openExternalUrl(appInfo.releases).catch((e) => toast(e.message, 'err'));
  const renderError = (message) => {
    body.innerHTML = `
      <div class="update-summary is-error">
        <span class="update-summary-icon">!</span>
        <div><b>暂时无法检查更新</b><span>${esc(message)}</span></div>
      </div>
      <div class="update-fallback"><button class="github-release-btn" data-open-release>${wrapSvg(I.github, 18)}<span>GitHub Releases</span></button></div>
      <div class="update-inline-actions"><button class="btn small" data-refresh-update>重新检查</button></div>`;
    body.querySelector('[data-open-release]').onclick = openReleases;
    body.querySelector('[data-refresh-update]').onclick = () => render();
  };
  const render = async (message = '') => {
    const status = await Api.checkUpdates();
    setUpdateNotice(status);
    const availableItems = (status.updates || []).filter((item) => item.available);
    const coreUpdate = availableItems.find((item) => item.component === 'core');
    const checkedAt = fmtCheckTime(status.checked_at);
    const installDoneHint = status.platform === 'macos'
      ? 'DMG 已打开。请将 tidoc 拖到“应用程序”，再退出并重新打开。'
      : '安装器已打开。完成安装后请退出并重新打开 tidoc。';
    const rows = (status.updates || []).map((u) => {
      const available = u.available;
      const assetSize = fmtBytes(u.asset?.size);
      const meta = [
        `当前 ${u.current_version ? 'v' + esc(u.current_version) : '未安装'}`,
        `最新 v${esc(u.latest_version || '未知')}`,
        assetSize,
      ].filter(Boolean).join(' · ');
      let state = u.manifest_missing
        ? '<span class="update-badge current">等待发布</span>'
        : available ? '<span class="update-badge available">可更新</span>' : '<span class="update-badge current">已是最新</span>';
      let action = '';
      const installable = u.manifest_missing ? null : UPDATE_COMPONENTS[u.component];
      if (installable) {
        const attr = `data-install-component="${esc(u.component)}"`;
        if (u.needs_repair) {
          state = '<span class="update-badge available">需要修复</span>';
          action = `<button class="btn small" ${attr}>修复组件</button>`;
        } else if (available) {
          state = `<span class="update-badge available">${u.current_version ? '可更新' : '可安装'}</span>`;
          action = `<button class="btn small" ${attr}>${u.current_version ? '更新组件' : '安装组件'}</button>`;
        } else {
          state = '<span class="update-badge current">已安装</span>';
          action = `<button class="btn small ghost" ${attr}>重新安装</button>`;
        }
      } else if (!available) {
        action = '';
      } else if (u.downloaded) {
        state = '<span class="update-badge pending">已下载</span>';
        action = '<button class="btn small" data-open-core>打开更新包</button>';
      } else {
        action = '<button class="btn small" data-download-core>下载并打开</button>';
      }
      const notes = Array.isArray(u.asset?.notes) ? u.asset.notes : (u.asset?.notes ? [u.asset.notes] : []);
      const responsibility = installable
        ? `<span class="update-component-description">${installable.description}</span>`
        : '';
      return `<div class="update-component">
        <div class="update-component-main">
          <div class="update-component-copy">
            <div class="update-component-title"><b>${esc(u.name || u.component)}</b>${state}</div>
            <span>${meta}</span>
            ${responsibility}
          </div>
          <div class="update-component-action">${action}</div>
        </div>
        ${available && notes.length ? `<ul class="update-notes">${notes.slice(0, 3).map((note) => `<li>${esc(note)}</li>`).join('')}</ul>` : ''}
      </div>`;
    }).join('');
    body.innerHTML = `
      <div class="update-summary ${availableItems.length ? 'has-update' : 'is-current'}">
        <span class="update-summary-icon">${availableItems.length ? '↓' : '✓'}</span>
        <div>
          <b>${availableItems.length ? (coreUpdate ? `tidoc v${esc(coreUpdate.latest_version)} 可用` : '有可用组件更新') : '已经是最新版本'}</b>
          <span>${availableItems.length ? `${availableItems.length} 项可更新` : `v${esc(status.current_core_version || appInfo.version)}`}</span>
        </div>
        <button class="btn small ghost" data-refresh-update>重新检查</button>
      </div>
      <div class="update-section-head"><b>程序与组件</b>${checkedAt ? `<span>检查于 ${esc(checkedAt)}</span>` : ''}</div>
      <div class="update-components">
        ${rows || '<div class="hint warn">暂时没有适用于本机的更新包。</div>'}
      </div>
      <div id="updateOperation">${message ? `<div class="hint ok">${esc(message)}</div>` : ''}</div>
      <div class="update-fallback"><button class="github-release-btn" data-open-release>${wrapSvg(I.github, 18)}<span>GitHub Releases</span></button></div>`;
    body.querySelector('[data-refresh-update]').onclick = async () => {
      setBusy('正在重新检查版本…');
      try { await render(); } catch (e) { renderError(e.message); }
    };
    body.querySelector('[data-open-release]').onclick = openReleases;
    const coreBtn = body.querySelector('[data-download-core]');
    if (coreBtn) coreBtn.onclick = async () => {
      setBusy('正在下载并校验更新包，完成后会自动打开…');
      let ok = false;
      try {
        const r = await Api.downloadCoreUpdate();
        toast('更新包已打开：' + baseName(r.file_path), 'ok');
        await render(installDoneHint);
        ok = true;
      } catch (e) { toast(e.message, 'err'); }
      finally { if (!ok) await render().catch((e) => renderError(e.message)); }
    };
    const openCoreBtn = body.querySelector('[data-open-core]');
    if (openCoreBtn) openCoreBtn.onclick = async () => {
      setBusy('正在打开已下载的更新包…');
      let ok = false;
      try {
        await Api.openDownloadedCoreUpdate();
        toast('更新包已打开', 'ok');
        await render(installDoneHint);
        ok = true;
      } catch (e) { toast(e.message, 'err'); }
      finally { if (!ok) await render().catch((e) => renderError(e.message)); }
    };
    body.querySelectorAll('[data-install-component]').forEach((btn) => {
      btn.onclick = async () => {
        const comp = UPDATE_COMPONENTS[btn.dataset.installComponent];
        if (!comp) return;
        setBusy(comp.busy);
        let ok = false;
        try {
          const r = await comp.install();
          toast(`${comp.name}已安装：v${r.version}`, 'ok');
          await render(`${comp.name}已安装：v${r.version}`);
          ok = true;
        } catch (e) { toast(e.message, 'err'); }
        finally { if (!ok) await render().catch((e) => renderError(e.message)); }
      };
    });
  };
  try {
    await render();
  } catch (e) {
    renderError(e.message);
  }
}

async function maybeShowFirstUseGuide() {
  if (!State.profiles.length || localStorage.getItem(USAGE_GUIDE_SEEN_KEY) || $('#modalRoot').lastChild) return;
  try {
    if (await Api.appPreference(USAGE_GUIDE_SEEN_KEY, '')) {
      localStorage.setItem(USAGE_GUIDE_SEEN_KEY, '1');
      return;
    }
  } catch (e) {}
  setTimeout(() => {
    if (!$('#modalRoot').lastChild && !localStorage.getItem(USAGE_GUIDE_SEEN_KEY)) openUsageGuide(true);
  }, 450);
}

function usageGuideStepsMarkup() {
  return `<div><b>1 · 导入发票</b><span>拖入或粘贴发票 PDF/XML；多张用“导入发票”。</span></div>
    <div><b>2 · 补齐材料</b><span>在卡片或详情添加付款截图、实物图和查验单；右键可打开已有文件。</span></div>
    <div><b>3 · 核对条目</b><span>从“待补材料”或“识别提醒”进入详情，确认实付、明细和备注。</span></div>
    <div><b>4 · 组织批次</b><span>勾选条目后装入批次；点击批次右侧“⋯”可编辑批次、填写批次备注、归档，已归档批次可从“已归档”查看并恢复。</span></div>
    <div><b>5 · 导出打印</b><span>选中条目后导出绑定包、汇总或打印材料。</span></div>
    <div><b>6 · 后续查找</b><span>用抬头、报账人、状态、日期、金额或关键词筛选。</span></div>`;
}

function openUsageGuide(firstRun) {
  const body = el('div', 'guide-steps');
  body.innerHTML = usageGuideStepsMarkup();
  let m;
  const close = () => {
    if (firstRun) {
      localStorage.setItem(USAGE_GUIDE_SEEN_KEY, '1');
      Api.setAppPreference(USAGE_GUIDE_SEEN_KEY, '1').catch(() => {});
    }
    m.close();
  };
  m = modal({
    title: firstRun ? '开始使用' : '使用提示',
    body,
    footer: [mkBtn('知道了', 'primary', close)],
    onClose: () => {
      if (firstRun && !localStorage.getItem(USAGE_GUIDE_SEEN_KEY)) {
        localStorage.setItem(USAGE_GUIDE_SEEN_KEY, '1');
        Api.setAppPreference(USAGE_GUIDE_SEEN_KEY, '1').catch(() => {});
      }
    },
  });
}

// ------------------------------------------------------------------ 新建条目
function openNewEntry() {
  if (!State.currentProfileId) { toast('请先创建报账人', 'err'); openProfileManager(true); return; }

  const picked = { xml: null, pdf: null, payments: [], physical: [], inspection: null };
  const body = el('div');

  function uploadTile(key, label, hint, ph, ico) {
    return `<div class="upload-tile" data-tile="${key}">
      <div class="ut-title">${ico} ${label}</div>
      <div class="ut-hint">${hint}</div>
      <button class="btn small" data-pick="${key}">${ph}</button>
      <div class="ut-name" id="ne-${key}-name" hidden></div>
    </div>`;
  }
  const utIco = (svg) => svg.replace('<svg ', '<svg width="18" height="18" ');

  body.innerHTML = `
    <div class="hint">推荐同时上传发票 PDF 和 XML，识别更准。材料不齐可先存草稿，之后补齐。</div>
    <div class="form-row" style="margin-top:14px">
      <label>抬头</label>
      <select id="neTitle">
        ${titleChoiceOptions(State.defaultEntryTitle)}
      </select>
    </div>
    ${claimantConfirmHtml()}
    <div class="upload-grid">
      ${uploadTile('pdf', '发票 PDF', '推荐上传', '选择文件', utIco(iconPdf()))}
      ${uploadTile('xml', '发票 XML', '让识别更准', '选择文件', utIco(iconXml()))}
      ${uploadTile('payment', '付款截图', '可多张 · 浅色背景', '选择图片', utIco(iconImage()))}
      ${uploadTile('physical', '实物图', '可多张 · 可选', '选择图片', utIco(iconImage()))}
      ${uploadTile('inspection', '查验单 PDF', '可选', '选择文件', utIco(iconInspect()))}
    </div>
    <div id="nePreview"></div>`;

  body.querySelectorAll('[data-pick]').forEach((btn) => {
    btn.onclick = async () => {
      const key = btn.dataset.pick;
      try {
        const multiple = key === 'payment' || key === 'physical';
        const res = await Api.pickFiles(multiple);
        const paths = res.paths || [];
        if (!paths.length) return;
        const nameEl = body.querySelector(`#ne-${key}-name`);
        const tile = body.querySelector(`[data-tile="${key}"]`);
        if (key === 'payment') {
          picked.payments = paths;
          nameEl.textContent = `${paths.length} 张付款截图`;
        } else if (key === 'physical') {
          picked.physical = paths;
          nameEl.textContent = `${paths.length} 张实物图`;
        } else if (key === 'inspection') {
          picked.inspection = paths[0];
          nameEl.textContent = baseName(paths[0]);
        } else {
          picked[key] = paths[0];
          nameEl.textContent = baseName(paths[0]);
        }
        nameEl.hidden = false;
        tile.classList.add('has-file');
        if (key === 'xml' || key === 'pdf') await preview();
      } catch (e) { toast(e.message, 'err'); }
    };
  });

  async function preview() {
    if (!picked.xml && !picked.pdf) return;
    const progress = taskProgress('正在识别发票信息…');
    try {
      const r = await Api.parseFiles(picked.xml, picked.pdf);
      const p = r.parsed, c = r.check;
      const prev = body.querySelector('#nePreview');
      prev.innerHTML = `
        <div class="detail-section" style="margin-top:18px">
          <h3>识别结果 <span class="badge ${c.status}">${CHECK_LABEL[c.status]}</span></h3>
          <div class="kv">
            <span class="k">发票号码</span><span class="v-mono">${esc(p.invoice_no || '—')}</span>
            <span class="k">发票日期</span><span>${esc(p.invoice_date || '—')}</span>
            <span class="k">销售方</span><span>${esc(p.seller || '—')}</span>
            <span class="k">购买方抬头</span><span>${esc(p.buyer_name || '—')}</span>
            <span class="k">价税合计</span><span><b style="font-family:var(--font-serif);font-size:15px">${fmtMoney(p.total)}</b></span>
            <span class="k">明细条数</span><span>${p.items.length}</span>
          </div>
          ${c.message ? `<p class="hint warn" style="margin-top:12px">${esc(c.message)}</p>` : ''}
        </div>`;
      const titleSel = body.querySelector('#neTitle');
      if (!titleSel.value && p.buyer_name) titleSel.value = p.buyer_name;
    } catch (e) { toast('识别失败：' + e.message, 'err'); }
    finally { progress.close(); }
  }

  const create = async () => {
    const progress = taskProgress('正在保存条目并识别材料…');
    try {
      await Api.createEntry({
        profileId: selectedClaimantId(body),
        title: body.querySelector('#neTitle').value,
        xmlPath: picked.xml, pdfPath: picked.pdf,
        paymentPaths: picked.payments, physicalPaths: picked.physical,
        inspectionPath: picked.inspection,
        status: 'draft',
      });
      m.close();
      await loadBatches();
      await refreshEntries();
      toast('已保存', 'ok');
    } catch (e) { toast(e.message, 'err'); }
    finally { progress.close(); }
  };

  const m = modal({
    title: '新建报账条目',
    subhead: '上传后自动识别，发票号、金额等无需手输；状态按材料齐全度自动判定',
    wide: true, body,
    footer: [
      mkBtn('取消', 'ghost', () => m.close()),
      mkBtn('保存', 'primary', () => create()),
    ],
  });
}

// ------------------------------------------------------------------ 批量导入
async function openBatchImport() {
  if (!State.currentProfileId) { toast('请先创建报账人', 'err'); openProfileManager(true); return; }
  const body = el('div');
  body.innerHTML = `
    <div class="import-choice-grid">
      <button class="import-choice" id="biFolder">
        <b>选择文件夹</b>
        <span>扫描其中的发票 PDF 和 XML</span>
      </button>
      <button class="import-choice" id="biFiles">
        <b>多选发票文件</b>
        <span>选择几张 PDF，可同时选 XML</span>
      </button>
    </div>`;
  const m = modal({
    title: '批量导入',
    body,
    footer: [mkBtn('取消', 'ghost', () => m.close())],
  });
  body.querySelector('#biFolder').onclick = () => { m.close(); openBatchImportFromFolder(); };
  body.querySelector('#biFiles').onclick = () => { m.close(); openBatchImportFromFiles(); };
}

async function openBatchImportFromFolder() {
  let picked;
  try { picked = await Api.pickFolder(); } catch (e) { toast(e.message, 'err'); return; }
  const folder = picked && picked.path;
  if (!folder) return;
  const progress = taskProgress('正在扫描文件夹中的发票和 XML…');
  try {
    const scan = await Api.scanFolder(folder);
    openBatchImportPreview(scan, folder);
  } catch (e) { toast('扫描失败：' + e.message, 'err'); }
  finally { progress.close(); }
}

async function openBatchImportFromFiles() {
  let picked;
  try { picked = await Api.pickFiles(true); } catch (e) { toast(e.message, 'err'); return; }
  const paths = (picked && picked.paths) || [];
  if (!paths.length) return;
  const progress = taskProgress(`正在扫描 ${paths.length} 个发票文件…`);
  try {
    const scan = await Api.scanFiles(paths);
    openBatchImportPreview(scan, `${paths.length} 个文件`);
  } catch (e) { toast('扫描失败：' + e.message, 'err'); }
  finally { progress.close(); }
}

function openBatchImportPreview(scan, sourceLabel, options = {}) {
  const groups = scan.groups.map((g) => ({
    key: g.key,
    label: g.label,
    selected: g.selected !== false,
    created: false,
    error: '',
    warnings: g.warnings || [],
    files: g.files.map((f) => ({ ...f })),
  }));
  const ungrouped = scan.ungrouped || [];
  const ignored = scan.ignored || [];
  const pendingMaterialInfos = options.pendingMaterialInfos || [];

  const body = el('div');
  const render = () => {
    const groupRows = groups.map((g, gi) => `
      <div class="bi-group${g.selected ? '' : ' off'}${g.error ? ' failed' : ''}${g.created ? ' created' : ''}">
        <div class="bi-group-head">
          <label class="bi-ignore"><input type="checkbox" data-bi-group="${gi}" ${g.selected ? 'checked' : ''} ${g.created ? 'disabled' : ''}/> ${g.created ? '已创建' : '导入'}</label>
          <b>组 ${esc(g.label)}</b>
          <span class="bi-count">${batchGroupSummary(g)}</span>
        </div>
        ${g.files.map((f) => `
          <div class="bi-file">
            <span class="attach-type">${esc(f.type_label || attachTypeLabel(f.type))}</span>
            <span class="bi-name" data-tooltip-overflow="${esc(f.name)}">${esc(f.name)}</span>
            ${f.warning ? `<span class="bi-warning">${esc(f.warning)}</span>` : ''}
          </div>`).join('')}
        ${(g.warnings || []).length ? `<div class="bi-warning">${g.warnings.map(esc).join('；')}</div>` : ''}
        ${g.error ? `<div class="bi-error"><b>未创建：</b>${esc(g.error)}</div>` : ''}
      </div>`).join('') || '<div class="hint">没有找到可导入的发票 PDF。批量导入要求每条至少有一个发票 PDF，XML 可以没有。</div>';

    const ungroupedRows = ungrouped.length ? `
      <div class="detail-section" style="margin-top:16px">
        <h3>未匹配 XML<span class="h3-line"></span></h3>
        <div class="hint" style="margin-bottom:10px">这些 XML 没有对应发票 PDF，不会单独创建条目。</div>
        <div class="bi-muted-list">${ungrouped.map((f) => `
          <div class="bi-file">
            <span class="attach-type">${esc(f.type_label || 'XML')}</span>
            <span class="bi-name" data-tooltip-overflow="${esc(f.name)}">${esc(f.name)}</span>
            <span class="bi-warning">${esc(f.warning || '')}</span>
          </div>`).join('')}</div>
      </div>` : '';

    const pendingRows = pendingMaterialInfos.length ? `
      <div class="detail-section" style="margin-top:16px">
        <h3>可自动绑定材料<span class="h3-line"></span></h3>
        <div class="hint" style="margin-bottom:10px">创建条目后，将按发票号自动绑定查验单；未匹配的材料会再让你选择条目。</div>
        <div class="bi-muted-list">${pendingMaterialInfos.map((f) => `
          <div class="bi-file">
            <span class="attach-type">${esc(f.type_label || attachTypeLabel(f.type))}</span>
            <span class="bi-name" data-tooltip-overflow="${esc(f.name)}">${esc(f.name)}</span>
            ${f.invoice_no ? `<span class="bi-warning">发票号 ${esc(f.invoice_no)}</span>` : ''}
            ${f.warning ? `<span class="bi-warning">${esc(f.warning)}</span>` : ''}
          </div>`).join('')}</div>
      </div>` : '';

    const ignoredRows = ignored.length ? `
      <div class="detail-section" style="margin-top:16px">
        <h3>未参与批量<span class="h3-line"></span></h3>
        <div class="hint" style="margin-bottom:10px">付款截图、查验单需要绑定到具体条目，请在条目详情里添加，或拖到主界面后选择条目。</div>
        <div class="bi-muted-list">${ignored.map((f) => `
          <div class="bi-file">
            <span class="attach-type">${esc(f.type_label || '跳过')}</span>
            <span class="bi-name" data-tooltip-overflow="${esc(f.name)}">${esc(f.name)}</span>
            <span class="bi-warning">${esc(f.warning || '')}</span>
          </div>`).join('')}</div>
      </div>` : '';

    body.innerHTML = `
      <div class="batch-import-summary">
        <div><span>条目</span><b>${scan.invoice_pdf_count || groups.length}</b></div>
        <div><span>XML</span><b>${scan.matched_xml_count || 0}</b></div>
        <div><span>跳过</span><b>${ignored.length + ungrouped.length}</b></div>
      </div>
      <div class="hint" style="margin-top:12px">从 <b>${esc(sourceLabel)}</b> 扫描到 <b>${scan.total_files}</b> 个候选文件。每个发票 PDF 创建一条；XML 只在能匹配到 PDF 时一起带入。</div>
      ${claimantConfirmHtml()}
      <div class="bi-groups" style="margin-top:14px">${groupRows}</div>
      ${pendingRows}
      ${ungroupedRows}
      ${ignoredRows}`;

    body.querySelectorAll('[data-bi-group]').forEach((cb) => {
      cb.onchange = () => {
        const group = groups[+cb.dataset.biGroup];
        group.selected = cb.checked;
        group.error = '';
        render();
      };
    });
  };
  render();

  const selectedImportPaths = () => groups
    .filter((g) => g.selected)
    .flatMap((g) => g.files.map((f) => f.path));
  const allPreviewPaths = () => options.cleanupPaths || [
    ...groups.flatMap((g) => g.files.map((f) => f.path)),
    ...ungrouped.map((f) => f.path),
    ...ignored.map((f) => f.path),
    ...pendingMaterialInfos.map((f) => f.path),
  ];
  let cleanupOnClose = allPreviewPaths;
  const createdEntries = [];
  let m;
  const finishCreatedEntries = async (progress) => {
    if (pendingMaterialInfos.length) progress.update('正在识别并匹配随附材料…');
    const bind = pendingMaterialInfos.length && createdEntries.length
      ? await autoBindMaterialInfos(pendingMaterialInfos, createdEntries)
      : { auto: [], manual: [] };
    const manualPaths = new Set((bind.manual || []).map((f) => f.path));
    const cleanupNow = allPreviewPaths().filter((p) => !manualPaths.has(p));
    cleanupOnClose = () => [];
    await cleanupDroppedPaths(cleanupNow);
    await loadBatches();
    await refreshEntries();
    m.close();
    if (bind.auto && bind.auto.length) {
      toast(`已创建 ${createdEntries.length} 条，并绑定 ${bind.auto.length} 份材料`, 'ok');
    } else {
      toast(`已创建 ${createdEntries.length} 条`, 'ok');
    }
  };
  m = modal({
    title: '确认导入',
    wide: true, body,
    onClose: () => cleanupDroppedPaths(cleanupOnClose()),
    footer: [
      mkBtn('取消', 'ghost', async () => {
        await cleanupDroppedPaths(allPreviewPaths());
        m.close();
      }),
      mkBtn('创建选中条目', 'primary', async () => {
        const payload = groups
          .filter((g) => g.selected && !g.created)
          .map((g) => ({ key: g.key, label: g.label, files: g.files.map((f) => ({ path: f.path, type: f.type })) }));
        if (!payload.length) {
          if (!createdEntries.length) { toast('没有可创建的分组', 'err'); return; }
          const progress = taskProgress('正在完成已创建条目的材料处理…');
          try { await finishCreatedEntries(progress); }
          catch (e) { toast(e.message, 'err'); }
          finally { progress.close(); }
          return;
        }
        const progress = taskProgress(`正在创建 ${payload.length} 个报账条目…`);
        try {
          const r = await Api.batchCreateEntries(selectedClaimantId(body), payload, State.defaultEntryTitle);
          createdEntries.push(...(r.created_entries || []));
          const failedByKey = new Map((r.failed || []).map((item) => [item.key || item.group, item]));
          const createdKeys = new Set((r.created_entries || []).map((item) => item.group));
          groups.forEach((group) => {
            const failure = failedByKey.get(group.key) || failedByKey.get(group.label);
            if (failure) {
              group.error = failure.error || '导入失败';
              group.selected = false;
            } else if (createdKeys.has(group.key) || payload.some((item) => item.key === group.key)) {
              group.created = true;
              group.selected = false;
              group.error = '';
            }
          });
          await loadBatches();
          await refreshEntries();
          if (r.failed && r.failed.length) {
            render();
            if (createdEntries.length) {
              const primary = m.foot.querySelector('.btn.primary');
              if (primary) primary.textContent = '完成已创建条目';
            }
            const detail = r.failed.length === 1
              ? `${r.created ? `已创建 ${r.created} 条；` : ''}未创建：${r.failed[0].error}`
              : `有 ${r.failed.length} 组未创建，请查看标红原因`;
            toast(detail, 'err');
            return;
          }
          await finishCreatedEntries(progress);
        } catch (e) { toast(e.message, 'err'); }
        finally { progress.close(); }
      }),
    ],
  });
}

function batchGroupSummary(g) {
  const count = (type) => g.files.filter((f) => f.type === type).length;
  const parts = [`${g.files.length} 个文件`, 'PDF'];
  if (count('invoice_xml')) parts.push('XML');
  return parts.join(' · ');
}

// ------------------------------------------------------------------ 条目详情
async function openEntryDetail(entryId, currentDetail = null) {
  let e;
  try { e = currentDetail || await Api.getEntry(entryId); } catch (err) { toast(err.message, 'err'); return; }
  if (!e) { toast('条目不存在', 'err'); return; }

  const body = el('div');
  const f = e.fields || {};
  const owner = State.profileById[e.profile_id];

  const itemsRows = (e.items || []).map((it) => {
    const cols = [
      { f: 'actual_name', v: it.actual_name || it.name, cls: '' },
      { f: 'unit', v: it.unit, cls: '' },
      { f: 'quantity', v: it.quantity, cls: 'num' },
      { f: 'unit_price', v: it.unit_price, cls: 'num' },
      { f: 'total', v: it.total, cls: 'num' },
    ];
    return `<tr data-item-id="${it.id}">${cols.map((c) =>
      `<td class="${c.cls}"><span class="cell-val">${c.f === 'quantity' ? fmtQuantity(c.v) : (c.f === 'total' || c.f === 'unit_price' ? fmtMoney(c.v) : esc(c.v || '\u2014'))}</span><input class="cell-input${c.cls === 'num' ? ' num' : ''}" data-item-field="${c.f}" value="${esc(c.v || '')}"/></td>`
    ).join('')}<td class="act"><button class="del-row" data-del-item="${it.id}" title="删除此行">${CLOSE_ICON}</button></td></tr>`;
  }).join('') || `<tr><td colspan="6" style="color:var(--ink-soft)">无明细</td></tr>`;

  // 附件按报账所需的三类分组展示：发票 / 付款截图 / 查验单；缺的类别显式提示
  const atts = e.attachments || [];
  const attGroup = (label, types, hint, requirementKey = types[0]) => {
    const list = atts.filter((a) => types.includes(a.type));
    const has = list.length > 0;
    const isInspection = types[0] === 'inspection_pdf';
    const required = requirementKey in State.materialRequirements
      ? State.materialRequirements[requirementKey] !== false
      : false;
    const rows = list.map((a) => `
      <div class="attach-item">
        <span class="attach-name" title="${esc(a.abs_path || a.stored_path)}">${esc(a.original_name)}</span>
        <div class="attach-actions">
          <select class="attach-type-select" data-att-type="${a.id}">
            ${ATTACHMENT_TYPE_OPTS.map(([v, l]) => `<option value="${v}"${v === a.type ? ' selected' : ''}>${l}</option>`).join('')}
          </select>
          <input class="attach-note" data-att-note="${a.id}" value="${esc(a.note || '')}" placeholder="附件备注"/>
          <button class="btn small ghost" data-open-att="${a.id}">打开</button>
          <button class="btn small ghost" data-reveal-att="${a.id}">位置</button>
          <button class="btn small ghost" data-replace-att="${a.id}">替换</button>
          <button class="btn small danger" data-del-att="${a.id}">删除</button>
        </div>
      </div>`).join('');
    return `
      <div class="att-group${has ? ' has' : ' missing'}" data-att-group="${types[0]}" data-att-requirement="${requirementKey}" data-att-required="${required ? '1' : '0'}" data-att-hint="${esc(hint)}">
        <div class="att-group-head">
          <span class="att-group-dot"></span>
          <span class="att-group-title">${label}</span>
          <span class="att-group-required">${required ? '必需' : '可选'}</span>
          <span class="att-group-status">${has ? `已上传 ${list.length}` : (required ? '未上传' : '可选 · 未上传')}</span>
          <span class="att-group-actions">
            ${isInspection ? '<button class="btn small att-group-add verify-online" data-online-verification>在线查验</button>' : ''}
            <button class="btn small att-group-add" data-add-att-type="${types[0]}" data-add-att-label="${label}">＋ ${isInspection ? '上传' : '添加'}</button>
          </span>
        </div>
        ${has ? `<div class="attach-list">${rows}</div>` : `<div class="att-group-hint">${hint}</div>`}
      </div>`;
  };
  const attachSection = [
    attGroup('发票', ['invoice_pdf', 'invoice_xml'], '上传发票 PDF 或 XML，用于识别发票信息。', 'invoice'),
    attGroup('付款截图', ['payment_screenshot'], '上传付款截图，作为实付凭证。', 'payment_screenshot'),
    (State.materialRequirements.physical_image || atts.some((a) => a.type === 'physical_image')
      ? attGroup('实物图', ['physical_image'], '上传实物照片，作为物资凭证。', 'physical_image') : ''),
    attGroup('查验单', ['inspection_pdf'], '上传发票查验单 PDF。', 'inspection_pdf'),
    (atts.some((a) => a.type === 'other')
      ? attGroup('其他', ['other'], '') : ''),
  ].join('');

  const history = (e.history || []).map((h) => `
    <div class="hitem">
      <span class="h-time">${esc(h.changed_at)}</span>
      <span class="h-field">${esc(FIELD_LABEL[h.field] || h.field)}</span>
      <span class="h-val">「${esc(h.old_value || '空')}」→「${esc(h.new_value || '空')}」</span>
    </div>`).join('')
    || '<div class="attach-item" style="color:var(--ink-soft)">暂无修改记录</div>';

  const completenessLine = (detail) => {
    const state = detail.completeness || { ready: false, missing: [] };
    return state.ready
      ? `<p class="hint ok-hint" style="margin-top:10px">材料齐全、实付已填、校验通过。</p>`
      : (state.missing?.length ? `<p class="hint" style="margin-top:10px">待补：${state.missing.map(esc).join('、')}。</p>` : '');
  };
  const compLine = completenessLine(e);

  body.innerHTML = `
    <div class="detail-top-grid">
      <div class="detail-section">
        <h3>关键信息 ${e.check_status && e.check_status !== 'pass' ? `<span class="badge ${e.check_status}">${CHECK_LABEL[e.check_status]}</span>` : ''}<span class="h3-line"></span></h3>
        <div class="kv compact">
          ${lockedKV('发票号码', 'invoice_no', e.invoice_no, true)}
          ${lockedKV('发票日期', 'invoice_date', e.invoice_date)}
          ${lockedKV('销售方', 'seller', e.seller)}
          ${lockedKV('价税合计', 'total', e.total, true, true)}
          ${lockedKV('购买方', 'buyer_name', e.buyer_name)}
          <span class="k">报账人</span>
          <span><select class="detail-profile-select" id="deProfile">${profileOptionsHtml(e.profile_id)}</select></span>
        </div>
        ${e.check_message ? `<p class="hint warn compact-hint">${esc(e.check_message)}</p>` : ''}
      </div>

      <div class="detail-section">
        <h3>需要手动确认<span class="h3-line"></span></h3>
        <div class="form-grid compact">
          ${editRow('实付金额', 'paid_amount', f.paid_amount)}
          ${editRow('实际物资名称', 'actual_item_name', f.actual_item_name)}
        </div>
        ${editRow('条目备注', 'notes', f.notes, true)}
        <div data-detail-completeness>${compLine}</div>
      </div>
    </div>

    <div class="detail-section">
      <h3>报账材料<span class="h3-line"></span></h3>
      <div class="material-drop" id="materialDrop">
        <b>拖拽材料到这里</b>
        <span>PDF 自动识别为发票或查验单，图片自动作为付款截图；也可用下方添加按钮。</span>
      </div>
      <div class="att-groups">${attachSection}</div>
    </div>

    <details class="detail-section minor-section">
      <summary>物品明细</summary>
      <table class="items-table">
        <thead><tr><th>名称</th><th>单位</th><th style="text-align:right">数量</th><th style="text-align:right">单价</th><th style="text-align:right">金额</th><th style="width:32px"></th></tr></thead>
        <tbody>${itemsRows}</tbody>
      </table>
      <div class="items-add-row"><button class="btn small" id="deAddItem">＋ 添加明细行</button></div>
    </details>

    ${(e.ocr_recognized || State.ocrStatus?.available) ? `<details class="detail-section minor-section${e.ocr_pending ? ' ocr-has-pending' : ''}" id="deOcrSection">
      <summary>阿里云识别${e.ocr_pending ? ' <span class="badge warning">待确认</span>' : ''}</summary>
      <div class="ocr-detail-body" id="deOcrBody"><div class="hint">正在读取识别结果…</div></div>
    </details>` : ''}

    <details class="detail-section minor-section">
      <summary>修改记录</summary>
      <div class="history-list">${history}</div>
    </details>`;

  function lockedKV(label, field, val, mono, money) {
    const display = money ? fmtMoney(val) : esc(val || '\u2014');
    return `<span class="k field-locked-label">${label}<span class="lock-icon">${wrapSvg(I.lock, 11)}</span></span>
      <span data-locked="${field}" class="${mono ? 'v-mono' : ''}">
        <span class="locked-val" title="点击修改">${display}</span>
        <input class="locked-input${mono ? ' v-mono' : ''}" value="${esc(val || '')}"/>
      </span>`;
  }

  function editRow(label, field, fv, full) {
    const modified = field !== 'notes' && fv && fv.modified;
    const val = fv ? fv.current : '';
    if (field === 'notes') {
      return `<div class="form-row"${full ? ' style="grid-column:1/-1"' : ''}>
        <label>${label}${modified ? '<span class="field-modified-mark">' + iconPencil(11) + '已人工修改</span>' : ''}</label>
        <textarea data-edit="${field}" rows="3" style="width:100%;font-family:inherit;font-size:13px;padding:10px;border-radius:9px;border:1px solid var(--line);resize:vertical">${esc(val)}</textarea>
      </div>`;
    }
    return `<div class="form-row"${full ? ' style="grid-column:1/-1"' : ''}>
      <label>${label}${modified ? '<span class="field-modified-mark">' + iconPencil(11) + '已人工修改</span>' : ''}</label>
      <input data-edit="${field}" value="${esc(val)}"/>
    </div>`;
  }

  setupMaterialDrop(body.querySelector('#materialDrop'), entryId, async () => {
    toast('材料已添加', 'ok');
    await reopenEntryDetail(mm, entryId, { affectsStatus: true });
  });

  // ---- editable fields (paid_amount, actual_item_name, notes)
  body.querySelector('#deProfile').onchange = async (ev) => {
    try {
      await Api.updateEntryProfile(entryId, ev.target.value, State.currentProfileId);
      toast('报账人已更新', 'ok');
      await reopenEntryDetail(mm, entryId, { relist: true });
    } catch (err) { toast(err.message, 'err'); }
  };

  // ---- editable fields (paid_amount, actual_item_name, notes)
  body.querySelectorAll('[data-edit]').forEach((inp) => {
    inp.onchange = async () => {
      try {
        const field = inp.dataset.edit;
        await Api.updateField(entryId, field, inp.value, State.currentProfileId);
        toast('已保存', 'ok');
        await syncEntryAfterChange(entryId, {
          searchable: field === 'actual_item_name' || field === 'notes',
          notes: field === 'notes',
          affectsStatus: field === 'paid_amount',
        });
      } catch (err) { toast(err.message, 'err'); }
    };
  });

  // ---- locked fields: click-to-edit inline
  body.querySelectorAll('[data-locked]').forEach((wrap) => {
    const field = wrap.dataset.locked;
    const valSpan = wrap.querySelector('.locked-val');
    const inp = wrap.querySelector('.locked-input');
    valSpan.onclick = () => {
      wrap.classList.add('locked-editing');
      inp.focus();
      inp.select();
    };
    const commit = async () => {
      wrap.classList.remove('locked-editing');
      const nv = inp.value.trim();
      const ov = e[field] || '';
      if (nv === ov) return;
      try {
        await Api.correctLocked(entryId, field, nv, State.currentProfileId);
        toast('已修改并记下', 'ok');
        await reopenEntryDetail(mm, entryId, { relist: true });
      } catch (err) { toast(err.message, 'err'); }
    };
    inp.onblur = commit;
    inp.onkeydown = (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); inp.blur(); }
      if (ev.key === 'Escape' && wrap.classList.contains('locked-editing')) {
        ev.preventDefault(); ev.stopPropagation();
        inp.value = e[field] || '';
        wrap.classList.remove('locked-editing');
      }
    };
  });

  // ---- items: click-to-edit inline cells
  body.querySelectorAll('.items-table td .cell-val').forEach((valSpan) => {
    valSpan.onclick = () => {
      const td = valSpan.parentElement;
      td.classList.add('editing');
      const inp = td.querySelector('.cell-input');
      inp.focus();
      inp.select();
    };
  });
  body.querySelectorAll('.items-table .cell-input').forEach((inp) => {
    const td = inp.parentElement;
    const tr = td.closest('tr');
    const itemId = parseInt(tr.dataset.itemId, 10);
    const field = inp.dataset.itemField;
    let origVal = inp.value;
    const commit = async () => {
      td.classList.remove('editing');
      const nv = inp.value.trim();
      if (nv === origVal) return;
      try {
        await Api.updateItem(itemId, { [field]: nv });
        origVal = nv;
        td.querySelector('.cell-val').textContent = nv || '\u2014';
        toast('已保存', 'ok');
        await syncEntryAfterChange(entryId, { searchable: true });
      } catch (err) { toast(err.message, 'err'); }
    };
    inp.onblur = commit;
    inp.onkeydown = (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); inp.blur(); }
      if (ev.key === 'Escape' && td.classList.contains('editing')) {
        ev.preventDefault(); ev.stopPropagation();
        inp.value = origVal;
        td.classList.remove('editing');
      }
      if (ev.key === 'Tab') {
        ev.preventDefault();
        inp.blur();
        // move to next/prev editable cell
        const allInputs = [...body.querySelectorAll('.items-table .cell-input')];
        const idx = allInputs.indexOf(inp);
        const next = allInputs[ev.shiftKey ? idx - 1 : idx + 1];
        if (next) {
          const nextTd = next.parentElement;
          nextTd.classList.add('editing');
          next.focus();
          next.select();
        }
      }
    };
  });

  // ---- items: delete row
  body.querySelectorAll('[data-del-item]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await Api.deleteItem(parseInt(btn.dataset.delItem, 10));
        const row = btn.closest('tr');
        const tbody = row?.parentElement;
        row?.remove();
        if (tbody && !tbody.querySelector('tr[data-item-id]')) {
          tbody.innerHTML = '<tr><td colspan="6" style="color:var(--ink-soft)">无明细</td></tr>';
        }
        await syncEntryAfterChange(entryId, { searchable: true });
        toast('已删除', 'ok');
      } catch (err) { toast(err.message, 'err'); }
    };
  });

  // ---- items: add row
  body.querySelector('#deAddItem').onclick = async () => {
    try {
      await Api.addItem(entryId, { name: '', actual_name: '', unit: '', quantity: '', unit_price: '', total: '' });
      toast('已添加', 'ok');
      await reopenEntryDetail(mm, entryId, { searchable: true });
    } catch (err) { toast(err.message, 'err'); }
  };

  // ---- 报账材料：按类别添加（预选类型）
  body.querySelectorAll('[data-add-att-type]').forEach((btn) => {
    btn.onclick = () => addAttachmentFlow(entryId, mm, btn.dataset.addAttType);
  });
  body.querySelectorAll('[data-online-verification]').forEach((btn) => {
    btn.onclick = () => {
      mm.close();
      onlineVerificationFlow(entryId);
    };
  });
  body.querySelectorAll('[data-open-att]').forEach((b) => {
    b.onclick = async () => {
      try { await Api.openAttachment(b.dataset.openAtt); }
      catch (err) { toast(err.message, 'err'); }
    };
  });
  body.querySelectorAll('[data-reveal-att]').forEach((b) => {
    b.onclick = async () => {
      try { await Api.revealAttachment(b.dataset.revealAtt); }
      catch (err) { toast(err.message, 'err'); }
    };
  });
  body.querySelectorAll('[data-replace-att]').forEach((b) => {
    b.onclick = async () => {
      const att = atts.find((a) => a.id === b.dataset.replaceAtt);
      if (!att) return;
      try {
        const type = att.type;
        const res = await Api.pickFiles(false);
        const path = (res.paths || [])[0];
        if (!path) return;
        const result = await Api.updateAttachment(att.id, { src_path: path, type });
        toast(result.cleanup_warning || '附件已替换', result.cleanup_warning ? 'err' : 'ok');
        await reopenEntryDetail(mm, entryId, { affectsStatus: true });
      } catch (err) { toast(err.message, 'err'); }
    };
  });
  body.querySelectorAll('[data-att-type]').forEach((sel) => {
    sel.onchange = async () => {
      try {
        const result = await Api.updateAttachment(sel.dataset.attType, { type: sel.value });
        const reset = result.paid_amount_reset;
        toast(
          reset?.reset ? `附件类型已更新，实付已恢复为 ${fmtMoney(reset.value)}` : '附件类型已更新',
          'ok',
        );
        await reopenEntryDetail(mm, entryId, { affectsStatus: true });
      } catch (err) { toast(err.message, 'err'); }
    };
  });
  body.querySelectorAll('[data-att-note]').forEach((inp) => {
    inp.onchange = async () => {
      try { await Api.updateAttachment(inp.dataset.attNote, { note: inp.value }); toast('附件备注已保存', 'ok'); }
      catch (err) { toast(err.message, 'err'); }
    };
  });
  body.querySelectorAll('[data-del-att]').forEach((b) => {
    b.onclick = async () => {
      try {
        const result = await Api.deleteAttachment(b.dataset.delAtt);
        const group = b.closest('.att-group');
        b.closest('.attach-item')?.remove();
        if (group) {
          const count = group.querySelectorAll('.attach-item').length;
          const status = group.querySelector('.att-group-status');
          if (status) status.textContent = count
            ? `已上传 ${count}`
            : (group.dataset.attRequired === '1' ? '未上传' : '可选 · 未上传');
          if (!count) {
            group.classList.remove('has');
            group.classList.add('missing');
            group.querySelector('.attach-list')?.remove();
            group.appendChild(el('div', 'att-group-hint', esc(group.dataset.attHint || '')));
          }
        }
        const detail = await Api.getEntry(entryId);
        await syncEntryAfterChange(entryId, { affectsStatus: true }, detail);
        const reset = result.paid_amount_reset;
        const message = reset?.reset
          ? `已删除最后一张付款截图，实付已恢复为 ${fmtMoney(reset.value)}`
          : '已删除';
        toast(
          result.cleanup_warning ? `${message}；${result.cleanup_warning}` : message,
          result.cleanup_warning ? 'err' : 'ok',
        );
      }
      catch (err) { toast(err.message, 'err'); }
    };
  });

  State.activeDetailEntryId = entryId;
  const mm = modal({
    title: e.seller || (e.invoice_no ? '发票 ' + e.invoice_no : '报账条目'),
    wide: true, body,
    onClose: () => {
      if (State.activeDetailEntryId === entryId) State.activeDetailEntryId = null;
    },
    footer: [
      mkBtn('删除条目', 'danger', async () => {
        if (!confirm('确认删除该条目及其附件？此操作不可撤销。')) return;
        try {
          const result = await Api.deleteEntry(entryId);
          mm.close();
          await loadBatches();
          await refreshEntries();
          toast(result.cleanup_warning || '已删除', result.cleanup_warning ? 'err' : 'ok');
        }
        catch (err) { toast(err.message, 'err'); }
      }),
      mkBtn('关闭', 'ghost', () => mm.close()),
    ],
  });

  // ---- 阿里云识别结果（有差异时默认展开）：必须在 modal 创建后再加载，
  // loadOcrDetail 的回调会引用 mm，提前调用会触发 TDZ 错误。
  const ocrSection = body.querySelector('#deOcrSection');
  if (ocrSection) {
    if (e.ocr_pending) ocrSection.open = true;
    loadOcrDetail(body.querySelector('#deOcrBody'), mm, entryId, e.items || []);
  }
}

const ATTACHMENT_TYPE_OPTS = [
  ['payment_screenshot', '付款截图'],
  ['physical_image', '实物图'],
  ['invoice_pdf', '发票 PDF'],
  ['invoice_xml', '发票 XML'],
  ['inspection_pdf', '查验单 PDF'],
  ['other', '其他'],
];

function classifyAttachmentByName(name) {
  const n = String(name || '').toLowerCase();
  if (n.endsWith('.tidoc')) return 'bindle_package';
  if (n.endsWith('.xml')) return 'invoice_xml';
  if (/\.(jpg|jpeg|png|webp|bmp|gif)$/i.test(n)) return 'payment_screenshot';
  if (n.endsWith('.pdf') && (name.includes('查验') || name.includes('验真'))) return 'inspection_pdf';
  if (n.endsWith('.pdf')) return 'invoice_pdf';
  return 'other';
}

function dragFilePath(file) {
  return file.path || file.webkitRelativePath || '';
}

function isInvoiceImportInfo(info) {
  return info && (info.type === 'invoice_pdf' || info.type === 'invoice_xml');
}

function isBindlePackageInfo(info) {
  return info && info.type === 'bindle_package';
}

function isLooseMaterialInfo(info) {
  return info && ['payment_screenshot', 'physical_image', 'inspection_pdf', 'other'].includes(info.type);
}

async function materialInfosForPaths(paths) {
  const infos = await Api.classifyMaterialFiles(paths || []);
  return (infos || []).map((info) => ({
    ...info,
    type: info.type || classifyAttachmentByName(info.path || info.name),
  }));
}

async function openInboundBindle(infos, cleanupPaths, progress = null) {
  const packages = (infos || []).filter(isBindlePackageInfo);
  if (!packages.length) return false;
  if (packages.length !== 1 || infos.length !== 1) {
    throw new Error('绑定包需要单独导入：一次请只拖入或粘贴一个 .tidoc 文件。');
  }
  if (!State.currentProfileId) throw new Error('请先创建报账人，再导入绑定包。');
  progress?.update('正在检查绑定包完整性…');
  const info = packages[0];
  const insp = await Api.inspectBindle(info.path);
  await openBindleImportPreview(info.path, insp, {
    onClose: () => cleanupDroppedPaths(cleanupPaths || [info.path]),
  });
  return true;
}

async function addDroppedMaterialFiles(entryId, files, progress = null) {
  if (!files.length) return false;
  progress?.update('正在读取拖入材料…');
  const paths = await droppedFilesToPaths(files);
  let cleanupDeferred = false;
  try {
    progress?.update(State.paymentOcrEnabled ? '正在识别文件类型和付款金额…' : '正在识别文件类型…');
    const infos = await materialInfosForPaths(paths);
    if (await openInboundBindle(infos, paths, progress)) {
      cleanupDeferred = true;
      return false;
    }
    progress?.update('正在校验并添加到当前条目…');
    await addMaterialInfosToEntry(entryId, infos);
  } finally {
    if (!cleanupDeferred) await cleanupDroppedPaths(paths);
  }
  return true;
}

async function addMaterialInfosToEntry(entryId, infos) {
  await validateDroppedMaterialsForEntry(entryId, infos);
  const paymentInfos = [];
  for (const info of infos) {
    const options = info.type === 'payment_screenshot'
      ? {
          apply_payment_ocr: false,
          skip_payment_ocr: true,
          payment_ocr_attempted: State.paymentOcrEnabled,
          recognized_payment_amount: info.paid_amount || '',
        }
      : null;
    const att = await Api.addAttachment(entryId, info.path, info.type, '', options);
    if (info.type === 'payment_screenshot') {
      paymentInfos.push({
        ...info,
        paid_amount: info.paid_amount || att.payment_ocr?.paid_amount || '',
      });
    }
  }
  const message = await settlePaymentAmountAfterAdd(entryId, paymentInfos);
  return { message };
}

async function validateDroppedMaterialsForEntry(entryId, infos) {
  const entry = await Api.getEntry(entryId);
  const wrongInvoices = [];
  const unconfirmedInvoices = [];
  for (const info of infos) {
    const path = info.path;
    const type = info.type || classifyAttachmentByName(path);
    if (type !== 'invoice_pdf' && type !== 'invoice_xml') continue;
    try {
      const result = type === 'invoice_xml'
        ? await Api.parseFiles(path, null)
        : await Api.parseFiles(null, path);
      const invoiceNo = result.parsed?.invoice_no || '';
      if (entry.invoice_no && invoiceNo && invoiceNo !== entry.invoice_no) {
        wrongInvoices.push(baseName(path));
      } else if (entry.invoice_no && !invoiceNo) {
        unconfirmedInvoices.push(baseName(path));
      }
    } catch (_) {
      if (entry.invoice_no || entry.has_invoice) unconfirmedInvoices.push(baseName(path));
    }
  }
  if (wrongInvoices.length) {
    throw new Error(`这张发票不属于当前条目：${wrongInvoices.join('、')}。发票 PDF/XML 请拖到主界面批量导入。`);
  }
  if (unconfirmedInvoices.length) {
    throw new Error(`无法确认发票是否属于当前条目：${unconfirmedInvoices.join('、')}。请用“替换”或从主界面导入。`);
  }
}

async function autoBindMaterialInfos(infos, extraEntries = [], options = {}) {
  const candidates = (extraEntries || []).map((entry) => entry.entry_id || entry.id || entry).filter(Boolean);
  const planned = await Api.suggestMaterialBindings(infos, candidates);
  const auto = [];
  const manual = [];

  for (const info of planned) {
    if (info.suggested_entry_id) auto.push({ info, entryId: info.suggested_entry_id });
    else manual.push(info);
  }

  const grouped = new Map();
  for (const item of auto) {
    if (!grouped.has(item.entryId)) grouped.set(item.entryId, []);
    grouped.get(item.entryId).push(item.info);
  }
  for (const [entryId, group] of grouped) {
    await addMaterialInfosToEntry(entryId, group);
  }
  if (manual.length) openAttachDroppedFiles(manual.map((info) => info.path), {
    infos: manual,
    cleanupPaths: options.cleanupPaths,
  });
  return { auto: auto.map((item) => item.info), manual };
}

async function handleLooseMaterialInfos(infos, cleanupPaths) {
  if (!infos.length) return false;
  // 独立拖入或粘贴的材料应按全库匹配，含已归档条目。只有与一批发票同时导入的
  // 材料才由 openBatchImportPreview 显式限定到本次新建条目。
  const bind = await autoBindMaterialInfos(infos, [], { cleanupPaths });
  const manualPaths = new Set(bind.manual.map((info) => info.path));
  await cleanupDroppedPaths((cleanupPaths || infos.map((info) => info.path)).filter((p) => !manualPaths.has(p)));
  if (bind.auto.length) {
    await refreshEntries();
    toast(`已自动绑定 ${bind.auto.length} 份材料`, 'ok');
  }
  return true;
}

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error || new Error('读取文件失败'));
    reader.readAsDataURL(file);
  });
}

async function droppedFilesToPaths(files) {
  const list = [...(files || [])];
  if (!list.length) return [];
  const directPaths = list.map(dragFilePath).filter(Boolean);
  if (directPaths.length === list.length) return directPaths;
  const payload = [];
  for (const file of list) {
    payload.push({ name: file.name || 'dropped-file', data_url: await readFileAsDataURL(file) });
  }
  const saved = await Api.saveDroppedFiles(payload);
  return saved.paths || [];
}

async function cleanupDroppedPaths(paths) {
  if (!paths || !paths.length) return;
  try { await Api.cleanupDroppedFiles(paths); }
  catch (_) { /* 清理失败不影响用户当前操作 */ }
}

function setupMaterialDrop(zone, entryId, onDone) {
  if (!zone) return;
  zone.ondragover = (ev) => {
    ev.preventDefault();
    zone.classList.add('dragging');
  };
  zone.ondragleave = () => zone.classList.remove('dragging');
  zone.ondrop = async (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    zone.classList.remove('dragging');
    const files = [...(ev.dataTransfer?.files || [])];
    const progress = taskProgress('正在读取拖入材料…');
    try {
      const added = await addDroppedMaterialFiles(entryId, files, progress);
      progress.close();
      if (added) await onDone();
    } catch (err) { toast(err.message, 'err'); }
    finally { progress.close(); }
  };
}

function setupGlobalDrop() {
  let hideTimer = null;
  const hasFiles = (dt) => dt && Array.from(dt.types || []).includes('Files');
  const keepOverlayVisible = () => {
    showDragOverlay();
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideDragOverlay, 220);
  };
  document.addEventListener('dragenter', (ev) => {
    if (!hasFiles(ev.dataTransfer)) return;
    keepOverlayVisible();
  });
  document.addEventListener('dragleave', (ev) => {
    if (ev.clientX <= 0 || ev.clientY <= 0 ||
        ev.clientX >= window.innerWidth || ev.clientY >= window.innerHeight) {
      clearTimeout(hideTimer);
      hideDragOverlay();
    }
  });
  document.addEventListener('dragover', (ev) => {
    if (!hasFiles(ev.dataTransfer)) return;
    ev.preventDefault();
    keepOverlayVisible();
  });
  document.addEventListener('drop', async (ev) => {
    if (!ev.dataTransfer?.files?.length) return;
    ev.preventDefault();
    clearTimeout(hideTimer);
    hideDragOverlay();
    if (ev.target instanceof Element && ev.target.closest('.entry-card, .material-drop, .modal-mask')) return;
    const progress = taskProgress('正在读取拖入文件…');
    let paths = [];
    let cleanupDeferred = false;
    try {
      paths = await droppedFilesToPaths([...ev.dataTransfer.files]);
      progress.update('正在识别文件类型和材料信息…');
      const infos = await materialInfosForPaths(paths);
      if (await openInboundBindle(infos, paths, progress)) {
        cleanupDeferred = true;
        return;
      }
      const invoiceInfos = infos.filter(isInvoiceImportInfo);
      const materialInfos = infos.filter(isLooseMaterialInfo);
      if (invoiceInfos.length) {
        progress.update('正在扫描发票并整理导入分组…');
        const scan = await Api.scanFiles(invoiceInfos.map((info) => info.path));
        openBatchImportPreview(scan, `${invoiceInfos.length} 个拖入文件`, {
          pendingMaterialInfos: materialInfos,
          cleanupPaths: paths,
        });
        return;
      }
      if (materialInfos.length) {
        progress.update(State.paymentOcrEnabled ? '正在识别金额并匹配对应条目…' : '正在准备手动绑定付款截图…');
        await handleLooseMaterialInfos(materialInfos, paths);
        return;
      }
      await cleanupDroppedPaths(paths);
      toast('请拖入发票 PDF、XML、付款截图、实物图或查验单', 'err');
    } catch (e) {
      if (!cleanupDeferred) await cleanupDroppedPaths(paths);
      toast(e.message || '处理拖入文件失败', 'err');
    } finally {
      progress.close();
    }
  });
  document.addEventListener('dragend', () => {
    clearTimeout(hideTimer);
    hideDragOverlay();
  });
  window.addEventListener('blur', () => {
    clearTimeout(hideTimer);
    hideDragOverlay();
  });
}

function filesFromClipboard(ev) {
  const dt = ev.clipboardData;
  if (!dt) return [];
  const out = [];
  const seen = new Set();
  for (const file of [...(dt.files || [])]) {
    const key = `${file.name}:${file.size}:${file.type}`;
    if (!seen.has(key)) { seen.add(key); out.push(file); }
  }
  for (const item of [...(dt.items || [])]) {
    if (item.kind !== 'file') continue;
    const file = item.getAsFile();
    if (!file) continue;
    const key = `${file.name}:${file.size}:${file.type}`;
    if (!seen.has(key)) { seen.add(key); out.push(file); }
  }
  return out;
}

function setupClipboardUpload() {
  document.addEventListener('paste', async (ev) => {
    const files = filesFromClipboard(ev);
    if (!files.length) return;
    ev.preventDefault();
    const activeEntryId = State.activeDetailEntryId;
    const progress = taskProgress('正在读取剪切板材料…');
    let paths = [];
    let cleanupDeferred = false;
    try {
      paths = await droppedFilesToPaths(files);
      progress.update('正在识别文件类型和材料信息…');
      const infos = await materialInfosForPaths(paths);
      if (await openInboundBindle(infos, paths, progress)) {
        cleanupDeferred = true;
        return;
      }
      if (activeEntryId) {
        progress.update(State.paymentOcrEnabled ? '正在添加材料并识别付款金额…' : '正在添加材料…');
        await addMaterialInfosToEntry(activeEntryId, infos);
        await cleanupDroppedPaths(paths);
        await refreshEntries();
        toast(`已从剪切板添加 ${infos.length} 份材料`, 'ok');
        return;
      }
      const invoiceInfos = infos.filter(isInvoiceImportInfo);
      const materialInfos = infos.filter(isLooseMaterialInfo);
      if (invoiceInfos.length) {
        progress.update('正在扫描发票并整理导入分组…');
        const scan = await Api.scanFiles(invoiceInfos.map((info) => info.path));
        openBatchImportPreview(scan, `剪切板 ${invoiceInfos.length} 个文件`, {
          pendingMaterialInfos: materialInfos,
          cleanupPaths: paths,
        });
      } else if (materialInfos.length) {
        progress.update(State.paymentOcrEnabled ? '正在识别金额并匹配对应条目…' : '正在准备手动绑定付款截图…');
        await handleLooseMaterialInfos(materialInfos, paths);
      } else {
        await cleanupDroppedPaths(paths);
        toast('剪切板里没有可导入的发票或材料', 'err');
      }
    } catch (e) {
      if (!cleanupDeferred) await cleanupDroppedPaths(paths);
      toast(e.message || '读取剪切板失败', 'err');
    } finally {
      progress.close();
    }
  });
}

function showDragOverlay() {
  document.body.classList.add('dragging-files');
  if ($('.drag-overlay')) return;
  const ov = el('div', 'drag-overlay', `
    <div class="drag-guide">
      <b>空白列表区：导入发票 PDF/XML 或 .tidoc 绑定包</b>
      <span>拖到条目卡片：绑定付款截图、实物图或查验单；绑定包会进入导入预览</span>
    </div>`);
  document.body.appendChild(ov);
}

function hideDragOverlay() {
  document.body.classList.remove('dragging-files');
  const ov = $('.drag-overlay');
  if (ov) ov.remove();
}

async function openAttachDroppedFiles(paths, options = {}) {
  const infos = options.infos || paths.map((path) => ({
    path,
    name: baseName(path),
    type: classifyAttachmentByName(path),
    type_label: attachTypeLabel(classifyAttachmentByName(path)),
  }));
  const cleanupPaths = options.cleanupPaths || paths;
  if (!State.currentProfileId) {
    await cleanupDroppedPaths(cleanupPaths);
    toast('请先创建报账人', 'err');
    return;
  }
  let entries = [];
  try { entries = await Api.listEntries({}); }
  catch (e) {
    await cleanupDroppedPaths(cleanupPaths);
    toast(e.message, 'err');
    return;
  }
  if (!entries.length) {
    await cleanupDroppedPaths(cleanupPaths);
    toast('还没有可绑定的条目', 'err');
    return;
  }

  const entryOptions = '<option value="">请选择条目</option>' +
    entries.map((e) => `<option value="${e.id}">${esc(dropEntryLabel(e))}</option>`).join('');
  const body = el('div');
  const rows = infos.map((info, idx) => `
    <div class="drop-bind-row">
      <div class="drop-bind-file">
        <span class="attach-name" title="${esc(info.path)}">${esc(info.name || baseName(info.path))}${info.invoice_no ? ` · ${esc(info.invoice_no)}` : ''}</span>
        ${info.binding_reason ? `<small>${esc(info.binding_reason)}</small>` : ''}
      </div>
      <select data-drop-entry="${idx}" aria-label="绑定到条目">${entryOptions}</select>
      <select data-drop-type="${idx}">
        ${ATTACHMENT_TYPE_OPTS.filter(([v]) => ['payment_screenshot', 'physical_image', 'inspection_pdf', 'other'].includes(v))
          .map(([v, l]) => `<option value="${v}"${v === info.type ? ' selected' : ''}>${l}</option>`).join('')}
      </select>
    </div>`).join('');
  body.innerHTML = `
    <div class="hint">这些材料无法唯一匹配，请逐份确认要绑定的条目。</div>
    <div class="drop-bind-list">${rows}</div>`;
  const m = modal({
    title: '绑定材料',
    body, wide: true,
    onClose: () => cleanupDroppedPaths(cleanupPaths),
    footer: [
      mkBtn('取消', 'ghost', async () => {
        await cleanupDroppedPaths(cleanupPaths);
        m.close();
      }),
      mkBtn('确认绑定', 'primary', async () => {
        let progress = null;
        try {
          const selectedInfos = infos.map((info, idx) => ({
            ...info,
            entryId: body.querySelector(`[data-drop-entry="${idx}"]`).value,
            type: body.querySelector(`[data-drop-type="${idx}"]`).value,
          }));
          if (selectedInfos.some((info) => !info.entryId)) {
            toast('请为每份材料选择条目', 'err');
            return;
          }
          progress = taskProgress(`正在绑定 ${selectedInfos.length} 份材料…`);
          const grouped = new Map();
          selectedInfos.forEach((info) => {
            if (!grouped.has(info.entryId)) grouped.set(info.entryId, []);
            grouped.get(info.entryId).push(info);
          });
          const messages = [];
          for (const [entryId, group] of grouped) {
            const result = await addMaterialInfosToEntry(entryId, group);
            if (result.message) messages.push(result.message);
          }
          await cleanupDroppedPaths(cleanupPaths);
          m.close();
          await refreshEntries();
          toast(messages.join('；') || `已绑定 ${selectedInfos.length} 份材料`, 'ok');
        } catch (e) { toast(e.message, 'err'); }
        finally { progress?.close(); }
      }),
    ],
  });
}

function dropEntryLabel(e) {
  const item = (e.fields?.actual_item_name?.current) || (e.items?.[0]?.actual_name) || e.seller || '未命名条目';
  return `${item} · ${e.invoice_no || '无发票号'} · ${fmtMoney(e.total)}`;
}

function attachTypeLabel(t) {
  return { invoice_pdf: '发票PDF', invoice_xml: '发票XML', payment_screenshot: '付款截图', physical_image: '实物图',
    inspection_pdf: '查验单', other: '其他' }[t] || t;
}
async function addAttachmentFlow(entryId, parentModal, presetType) {
  const body = el('div');
  body.innerHTML = `
    <div class="form-row"><label>附件类型</label>
      <select id="atType">
        ${ATTACHMENT_TYPE_OPTS.map(([v, l]) => `<option value="${v}"${v === presetType ? ' selected' : ''}>${l}</option>`).join('')}
      </select>
    </div>`;
  const m = modal({
    title: '添加附件', body,
    footer: [
      mkBtn('取消', 'ghost', () => m.close()),
      mkBtn('选择文件并添加', 'primary', async () => {
        let progress = null;
        try {
          const type = body.querySelector('#atType').value;
          const res = await Api.pickFiles(type === 'payment_screenshot' || type === 'physical_image');
          const paths = res.paths || [];
          if (!paths.length) return;
          progress = taskProgress(type === 'payment_screenshot'
            ? (State.paymentOcrEnabled ? '正在识别付款截图金额…' : '正在添加付款截图…')
            : '正在校验并添加附件…');
          if (type === 'payment_screenshot') {
            const infos = (await materialInfosForPaths(paths)).map((info) => ({ ...info, type }));
            progress.update('正在添加截图并处理实付金额…');
            const result = await addMaterialInfosToEntry(entryId, infos);
            m.close(); parentModal.close(); openEntryDetail(entryId); await refreshEntries();
            toast(result.message || '附件已添加', 'ok');
          } else {
            for (const p of paths) await Api.addAttachment(entryId, p, type);
            m.close(); parentModal.close(); openEntryDetail(entryId); await refreshEntries();
            toast('附件已添加', 'ok');
          }
        } catch (err) { toast(err.message, 'err'); }
        finally { progress?.close(); }
      }),
    ],
  });
}

// ------------------------------------------------------------------ 汇总 / 绑定包 / 批量
async function exportSummary(ids) {
  const list = ids || (State.selected.size ? [...State.selected] : State.entries.map((e) => e.id));
  if (!list.length) { toast('没有可汇总的条目', 'err'); return; }
  try {
    const s = await Api.buildSummary(list);
    const byTitle = Object.entries(s.by_title || {}).map(([t, n]) => `${TITLE_SHORT[t] || t}：${n} 条`).join('　·　');
    const rows = (s.entries || []).slice(0, 12).map((e, i) => `
      <tr>
        <td>${i + 1}</td><td>${esc(e.invoice_no || '未识别')}</td><td>${esc(e.seller || '未识别')}</td>
        <td class="num">${fmtMoney(e.total)}</td><td>${esc(e.status || '')}</td>
      </tr>`).join('');
    const m = modal({
      title: '汇总信息',
      wide: true,
      body: `<div class="summary-strip">
          <div><span>条目</span><b>${s.count}</b></div>
          <div><span>合计</span><b>${fmtMoney(s.total)}</b></div>
          <div><span>抬头</span><b>${esc(byTitle || '未分组')}</b></div>
        </div>
        <table class="items-table summary-table" style="margin-top:14px">
          <thead><tr><th>#</th><th>发票号</th><th>销售方</th><th style="text-align:right">金额</th><th>状态</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="5" style="color:var(--ink-soft)">没有可显示的条目</td></tr>'}</tbody>
        </table>
        ${(s.entries || []).length > 12 ? '<div class="hint" style="margin-top:10px">这里只预览前 12 条；需要完整表格请使用导出里的“总览 Excel”。</div>' : ''}`,
      footer: [mkBtn('关闭', 'ghost', () => m.close())],
    });
  } catch (e) { toast(e.message, 'err'); }
}

async function doExport(ids) {
  if (!ids || !ids.length) { toast('请先选择要导出的条目', 'err'); return; }
  const body = el('div');
  const defaultName = '报账导出-' + filenameTimestamp();
  body.innerHTML = `
    <div class="form-row">
      <label>导出名称</label>
      <input id="exName" value="${esc(defaultName)}"/>
    </div>
    <div class="export-options">
      <label class="export-option">
        <input type="checkbox" data-export="bindle" checked/>
        <span><b>绑定包</b><small>包含条目、附件、报账人和签名清单；备注与标签按设置导出。</small></span>
      </label>
      <label class="export-option">
        <input type="checkbox" data-export="excel"/>
        <span><b>总览 Excel</b><small>给负责人核对条数、金额、材料状态和备注。</small></span>
      </label>
      <label class="export-option">
        <input type="checkbox" data-export="archive"/>
        <span><b>规范命名附件包</b><small>按“序号_发票号_销售方_金额”分文件夹整理附件并压缩。</small></span>
      </label>
    </div>
    <div class="hint" style="margin-top:12px">当前选择 <b>${ids.length}</b> 条。导出的文件会放在设置里的“导出目录”。</div>`;
  const m = modal({
    title: '导出',
    wide: true,
    body,
    footer: [
      mkBtn('取消', 'ghost', () => m.close()),
      mkBtn('开始导出', 'primary', async () => {
        const name = body.querySelector('#exName').value.trim() || defaultName;
        const chosen = [...body.querySelectorAll('[data-export]:checked')].map((x) => x.dataset.export);
        if (!chosen.length) { toast('请选择至少一种导出内容', 'err'); return; }
        const progress = taskProgress(`正在准备 ${ids.length} 条报账数据…`);
        try {
          const outputs = [];
          if (chosen.includes('bindle')) {
            progress.update('正在生成绑定包…');
            outputs.push(await Api.exportBindle(ids, name + '-绑定包'));
          }
          if (chosen.includes('excel')) {
            progress.update('正在生成总览 Excel…');
            outputs.push(await Api.exportOverviewExcel(ids, name + '-总览'));
          }
          if (chosen.includes('archive')) {
            progress.update('正在整理并压缩附件…');
            outputs.push(await Api.exportAttachmentArchive(ids, name + '-附件'));
          }
          m.close();
          showExportResult(outputs);
          toast(`已导出 ${outputs.length} 个文件`, 'ok');
        } catch (e) { toast(e.message, 'err'); }
        finally { progress.close(); }
      }),
    ],
  });
}

function showExportResult(outputs) {
  const body = el('div');
  const rows = outputs.map((o) => `
    <div class="attach-item">
      <span class="attach-name" title="${esc(o.path)}">${esc(baseName(o.path))}</span>
      <button class="btn small ghost" data-open-export="${esc(o.path)}">打开</button>
      <button class="btn small ghost" data-open-export-dir="${esc(dirName(o.path))}">文件夹</button>
    </div>`).join('');
  body.innerHTML = `<div class="hint">已生成以下文件。</div><div class="attach-list" style="margin-top:12px">${rows}</div>`;
  body.querySelectorAll('[data-open-export]').forEach((btn) => {
    btn.onclick = async () => {
      try { await Api.openPath(btn.dataset.openExport); }
      catch (e) { toast(e.message, 'err'); }
    };
  });
  body.querySelectorAll('[data-open-export-dir]').forEach((btn) => {
    btn.onclick = async () => {
      try { await Api.openPath(btn.dataset.openExportDir); }
      catch (e) { toast(e.message, 'err'); }
    };
  });
  const m = modal({
    title: '导出完成',
    body,
    footer: [mkBtn('关闭', 'ghost', () => m.close())],
  });
}

async function openBindleImportPreview(path, insp, options = {}) {
  const [batchResult, tags] = await Promise.all([
    Api.listBatches(true),
    Api.listTags(),
  ]);
  const batches = Array.isArray(batchResult) ? batchResult : (batchResult?.batches || []);
  const fallback = State.profileById[State.currentProfileId];
  const entries = insp.entries || [];
  const legacyProfiles = new Map();
  entries.forEach((entry) => {
    const sourceId = entry.profile_id || '__fallback__';
    if (!legacyProfiles.has(sourceId)) {
      legacyProfiles.set(sourceId, {
        id: sourceId,
        sourceId,
        name: entry.profile_name || fallback?.name || '',
        reviewer: entry.reviewer || fallback?.reviewer || '',
        is_default: false,
      });
    }
  });
  const packageProfiles = (insp.profiles || []).length
    ? insp.profiles.map((p) => ({ ...p, sourceId: p.id || '__fallback__' }))
    : [...legacyProfiles.values()];
  if (!packageProfiles.length) {
    packageProfiles.push({
      id: '__fallback__', sourceId: '__fallback__',
      name: fallback?.name || '', reviewer: fallback?.reviewer || '',
      is_default: false,
    });
  }
  const counts = new Map(packageProfiles.map((p) => [p.sourceId, 0]));
  (insp.entries || []).forEach((entry) => {
    const key = entry.profile_id || '__fallback__';
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  const activeBatches = (batches || []).filter((batch) => !batch.archived);
  const attachmentCount = entries.reduce((sum, entry) => sum + (entry.attachments || []).length, 0);
  const body = el('div', 'bindle-import');
  const profileRows = packageProfiles.map((profile) => `
    <div class="bindle-profile-row" data-bind-profile="${esc(profile.sourceId)}">
      <input data-bind-profile-name aria-label="报账人" value="${esc(profile.name || '')}" placeholder="填写报账人"/>
      <input data-bind-profile-reviewer aria-label="审核人" value="${esc(profile.reviewer || '')}" placeholder="填写审核人"/>
      <span>${counts.get(profile.sourceId) || 0} 条</span>
    </div>`).join('');
  const entryRows = entries.slice(0, 100).map((entry) => {
    const profile = packageProfiles.find((item) => item.sourceId === (entry.profile_id || '__fallback__'));
    return `<div class="bindle-entry-row" data-bind-entry-profile="${esc(entry.profile_id || '__fallback__')}">
      <span class="mono">${esc(entry.invoice_no || '无发票号')}</span>
      <span class="bindle-entry-seller" data-tooltip-overflow="${esc(entry.seller || '')}">${esc(entry.seller || '未识别销售方')}</span>
      <span>${fmtMoney(entry.total)}</span>
      <span class="bindle-entry-owner">${esc(profile?.name || entry.profile_name || '未填写')} · ${esc(profile?.reviewer || entry.reviewer || '未填写')}</span>
    </div>`;
  }).join('');
  const existingTagOptions = (tags || []).map((tag) => `<option value="${esc(tag)}"></option>`).join('');
  const existingBatchOptions = activeBatches.map((batch) =>
    `<option value="${esc(batch.id)}">${esc(batch.name)} · ${batch.stats?.count || 0} 条</option>`
  ).join('');
  body.innerHTML = `
    <div class="bindle-import-bar">
      <strong data-tooltip-overflow="${esc(baseName(path))}">${esc(baseName(path))}</strong>
      <div class="bindle-import-meta">
        <span><b>${entries.length}</b> 条目</span>
        <span><b>${packageProfiles.length}</b> 报账人</span>
        <span><b>${attachmentCount}</b> 附件</span>
      </div>
    </div>
    ${insp.verified ? '' : `<div class="bindle-integrity-warning">
      <b>完整性校验未通过</b><span>${esc((insp.tampered || []).join('、'))}</span>
      <label><input type="checkbox" id="bindleAllowTampered"/> 我确认继续，导入后标记为严重问题</label>
    </div>`}
    <section class="bindle-import-section">
      <h3>身份对应</h3>
      <div class="bindle-profile-list">
        <div class="bindle-profile-labels"><span>报账人</span><span>审核人</span><span>条目</span></div>
        ${profileRows}
      </div>
    </section>
    <section class="bindle-import-section">
      <h3>导入设置</h3>
      <div class="bindle-import-options">
        <div class="form-row bindle-batch-option">
          <label>报账批次</label>
          <div class="bindle-segments" role="radiogroup" aria-label="报账批次">
            <label><input type="radio" name="bindleBatchMode" value="none" checked/><span>不加入</span></label>
            <label class="${activeBatches.length ? '' : 'disabled'}"><input type="radio" name="bindleBatchMode" value="existing" ${activeBatches.length ? '' : 'disabled'}/><span>已有批次</span></label>
            <label><input type="radio" name="bindleBatchMode" value="new"/><span>新建批次</span></label>
          </div>
          <select id="bindleBatchSelect" class="hidden" aria-label="选择已有批次">${existingBatchOptions}</select>
          <input id="bindleNewBatch" class="hidden" aria-label="新批次名称" placeholder="输入批次名称"/>
        </div>
        <div class="form-row">
          <label for="bindleTagInput">统一标签 <span class="optional">可选</span></label>
          <input id="bindleTagInput" list="bindleTagOptions" placeholder="选择或输入标签"/>
          <datalist id="bindleTagOptions">${existingTagOptions}</datalist>
        </div>
      </div>
    </section>
    <section class="bindle-import-section">
      <h3>条目</h3>
      <div class="bindle-entry-list">
        ${entryRows ? '<div class="bindle-entry-head"><span>发票号</span><span>销售方</span><span>金额</span><span>归属</span></div>' + entryRows : '<div class="bindle-empty">包内没有条目</div>'}
      </div>
      ${entries.length > 100 ? '<div class="bindle-list-note">显示前 100 条，不影响导入</div>' : ''}
    </section>`;

  const tagInput = body.querySelector('#bindleTagInput');
  const batchSelect = body.querySelector('#bindleBatchSelect');
  const newBatch = body.querySelector('#bindleNewBatch');
  const batchModes = [...body.querySelectorAll('[name="bindleBatchMode"]')];
  batchModes.forEach((input) => {
    input.onchange = () => {
      batchSelect.classList.toggle('hidden', input.value !== 'existing');
      newBatch.classList.toggle('hidden', input.value !== 'new');
      if (input.checked && input.value === 'new') newBatch.focus();
    };
  });
  body.querySelectorAll('[data-bind-profile]').forEach((row) => {
    const nameInput = row.querySelector('[data-bind-profile-name]');
    const reviewerInput = row.querySelector('[data-bind-profile-reviewer]');
    const syncOwner = () => {
      body.querySelectorAll('[data-bind-entry-profile]').forEach((entryRow) => {
        if (entryRow.dataset.bindEntryProfile !== row.dataset.bindProfile) return;
        entryRow.querySelector('.bindle-entry-owner').textContent =
          `${nameInput.value.trim() || '未填写'} · ${reviewerInput.value.trim() || '未填写'}`;
      });
    };
    nameInput.oninput = syncOwner;
    reviewerInput.oninput = syncOwner;
  });

  let m;
  m = modal({
    title: '导入绑定包',
    wide: true,
    body,
    onClose: options.onClose,
    footer: [
      mkBtn('取消', 'ghost', () => m.close()),
      mkBtn(entries.length ? `导入 ${entries.length} 条` : '确认导入', 'primary', async () => {
        const tampered = body.querySelector('#bindleAllowTampered');
        if (tampered && !tampered.checked) {
          toast('请确认完整性异常后再导入', 'err');
          return;
        }
        const profileOverrides = {};
        let invalidProfile = false;
        body.querySelectorAll('[data-bind-profile]').forEach((row) => {
          const name = row.querySelector('[data-bind-profile-name]').value.trim();
          const reviewer = row.querySelector('[data-bind-profile-reviewer]').value.trim();
          if (!name || !reviewer) invalidProfile = true;
          profileOverrides[row.dataset.bindProfile] = { name, reviewer };
        });
        if (invalidProfile) { toast('报账人与审核人都必须填写', 'err'); return; }
        const tag = tagInput.value.trim();
        const batchMode = body.querySelector('[name="bindleBatchMode"]:checked').value;
        const batchId = batchMode === 'existing' ? batchSelect.value : '';
        const batchName = batchMode === 'new' ? newBatch.value.trim() : '';
        if (batchMode === 'existing' && !batchId) { toast('请选择报账批次', 'err'); return; }
        if (batchMode === 'new' && !batchName) { toast('请填写新批次名称', 'err'); return; }
        const options = {
          profile_overrides: profileOverrides,
          tags: tag ? [tag] : [],
          batch_id: batchId,
          batch_name: batchName,
        };
        const progress = taskProgress('正在导入条目和附件…');
        try {
          const r = await Api.importBindle(path, State.currentProfileId, !!tampered, options);
          progress.update('正在刷新条目列表…');
          m.close();
          await loadProfiles();
          await loadBatches();
          await refreshEntries();
          await refreshTagOptions();
          toast(r.message + `（${r.imported} 条）`, r.tampered && r.tampered.length ? 'err' : 'ok');
        } catch (e) { toast(e.message, 'err'); }
        finally { progress.close(); }
      }),
    ],
  });
}

async function importBindleFlow(path) {
  if (!State.currentProfileId) { toast('请先创建报账人', 'err'); return; }
  const progress = taskProgress('正在检查绑定包完整性…');
  try {
    const insp = await Api.inspectBindle(path);
    progress.close();
    await openBindleImportPreview(path, insp);
  } finally {
    progress.close();
  }
}

async function doImport() {
  if (!State.currentProfileId) { toast('请先创建报账人', 'err'); return; }
  try {
    const res = await Api.pickFiles(false, ['绑定包 (*.tidoc)']);
    const paths = res.paths || [];
    if (!paths.length) return;
    await importBindleFlow(paths[0]);
  } catch (e) { toast(e.message, 'err'); }
}

// ------------------------------------------------------------------ 打印导出组件
async function openPrintDialog(ids) {
  if (!ids || !ids.length) { toast('请先选择要打印的条目', 'err'); return; }
  const focusedBatch = actualBatchId() ? (State.currentBatch || State.batches.find((item) => item.id === actualBatchId()) || null) : null;
  let status;
  try { status = await Api.printComponentStatus(); } catch (e) { toast(e.message, 'err'); return; }

  if (!status.available) {
    let m;
    const needsRepair = !!status.needs_repair;
    m = modal({
      title: needsRepair ? '打印导出组件需要修复' : '打印导出组件未安装',
      body: `<div class="hint warn">${needsRepair ? '组件文件缺失或损坏，请重新安装后继续。' : '安装打印导出组件后即可继续。'}</div>`,
      footer: [
        mkBtn('取消', 'ghost', () => m.close()),
        mkBtn(needsRepair ? '修复组件' : '安装组件', 'primary', () => { m.close(); openUpdateDialog(); }),
      ],
    });
    return;
  }

  const OUTPUTS = [
    ['make_entry_bundle_pdf', '按条目材料拼接 PDF'],
    ['make_reimburse_doc', '报账说明 Word'], ['make_acceptance_doc', '验收单 Word'],
  ];
  const LEGACY_OUTPUTS = [
    ['make_invoice_pdf', '发票拼接 PDF'], ['make_payment_pdf', '付款截图拼接 PDF'],
    ['make_inspection_pdf', '查验单拼接 PDF'],
  ];

  const body = el('div');
  body.innerHTML = `
    <div class="detail-section">
      <h3>生成内容<span class="h3-line"></span></h3>
      <div class="check-grid">
        ${OUTPUTS.map(([k, label]) => `<label class="chk"><input type="checkbox" data-out="${k}" checked/> ${label}</label>`).join('')}
      </div>
      <label class="print-annotation-option"><input type="checkbox" id="pAnnotate" checked/><span><b>叠加条目编号与页码</b><small>便于纸质审查时核对材料归属；不需要时可关闭。</small></span></label>
      <details class="print-legacy-outputs">
        <summary>按材料类型分别导出（可选）</summary>
        <div class="check-grid">
          ${LEGACY_OUTPUTS.map(([k, label]) => `<label class="chk"><input type="checkbox" data-out="${k}"/> ${label}</label>`).join('')}
        </div>
      </details>
    </div>
    <div class="form-grid">
      <div class="form-row"><label>文档日期</label><input id="pDate" placeholder="如 2026年7月5日"/></div>
      <div class="form-row"><label>存放地点</label><input id="pLoc" value="工训楼"/></div>
      <div class="form-row"><label>批次备注</label><input id="pNote" value="${esc(focusedBatch?.note || '')}" placeholder="可选"/></div>
    </div>`;

  const genBtn = mkBtn('生成打印件', 'primary', async () => {
    const options = {};
    body.querySelectorAll('[data-out]').forEach((c) => { options[c.dataset.out] = c.checked; });
    options.annotate = body.querySelector('#pAnnotate').checked;
    const date = body.querySelector('#pDate').value.trim();
    if (date) options.document_date = date;
    options.storage_location = body.querySelector('#pLoc').value.trim() || '工训楼';
    options.batch_note = body.querySelector('#pNote').value.trim();

    genBtn.disabled = true; genBtn.textContent = '生成中…';
    try {
      const stamp = new Date().toLocaleString('sv').replace(/[: ]/g, '-').replace('T', '_');
      const name = '打印件-' + stamp;
      const r = await Api.buildPrints(ids, options, name);
      m.close();
      showPrintResult(r.results);
    } catch (e) {
      toast(e.message, 'err');
      genBtn.disabled = false; genBtn.textContent = '生成打印件';
    }
  });

  const m = modal({
    title: '打印导出',
    wide: true, body,
    footer: [mkBtn('取消', 'ghost', () => m.close()), genBtn],
  });
}

function showPrintResult(results) {
  const OUT_LABEL = {
    entry_bundle_pdf: '按条目材料拼接 PDF',
    invoice_pdf: '发票拼接 PDF', payment_pdf: '付款截图拼接 PDF', inspection_pdf: '查验单拼接 PDF',
    reimburse_doc: '报账说明 Word', acceptance_doc: '验收单 Word',
  };
  const html = (results || []).map((g) => {
    const tcls = TITLE_CLASS[g.title] || '';
    const firstPath = Object.values(g.files || {})[0] || '';
    const files = Object.entries(g.files).map(([k, v]) =>
      `<div class="attach-item"><span class="attach-type">${OUT_LABEL[k] || k}</span><span style="flex:1" title="${esc(v)}">${esc(baseName(v))}</span></div>`).join('');
    return `<div class="detail-section">
      <h3>${tcls ? `<span class="title-chip ${tcls}">${esc(TITLE_SHORT[g.title] || g.title)}</span>` : esc(g.title || '未标注抬头')}<span class="h3-line"></span>${firstPath ? `<button class="btn small ghost" data-open-print-dir="${esc(dirName(firstPath))}">文件夹</button>` : ''}</h3>
      <div class="attach-list">${files || '<span style="color:var(--ink-soft)">无文件</span>'}</div>
    </div>`;
  }).join('');
  const m = modal({
    title: '打印件已生成',
    subhead: '已按抬头分文件夹保存到导出目录',
    wide: true,
    body: html || '<div class="hint">无生成结果。</div>',
    footer: [mkBtn('完成', 'primary', () => m.close())],
  });
  m.body.querySelectorAll('[data-open-print-dir]').forEach((btn) => {
    btn.onclick = async () => {
      try { await Api.openPath(btn.dataset.openPrintDir); }
      catch (e) { toast(e.message, 'err'); }
    };
  });
}

// ------------------------------------------------------------------ 阿里云 OCR
function updateOcrSuggestBar() {
  const bar = $('#ocrSuggestBar');
  if (!bar) return;
  const applicable = State.quickView === 'warning'
    ? State.entries.filter((entry) =>
        entry.attachment_types?.invoice_pdf && !entry.attachment_types?.invoice_xml)
    : [];
  if (!applicable.length || !ocrReady()) {
    bar.classList.add('hidden');
    bar.innerHTML = '';
    return;
  }
  bar.classList.remove('hidden');
  bar.innerHTML = `
    <span class="ocr-suggest-text">当前视图有 ${applicable.length} 条发票可用阿里云识别</span>
    <button class="btn small" id="ocrSuggestRun">识别全部</button>`;
  bar.querySelector('#ocrSuggestRun').onclick = () => {
    const ids = applicable.map((entry) => entry.id);
    State.selected = new Set(ids);
    State.lastSelectedId = null;
    renderEntries();
    openOcrDialog(ids);
  };
}

async function openOcrDialog(ids) {
  if (!ids || !ids.length) { toast('请先选择要用阿里云识别的条目', 'err'); return; }
  let status;
  try { status = await Api.ocrComponentStatus(); } catch (e) { toast(e.message, 'err'); return; }
  State.ocrStatus = status;
  if (!status.available) {
    let m;
    const needsRepair = !!status.needs_repair;
    m = modal({
      title: needsRepair ? 'OCR 识别组件需要修复' : 'OCR 识别组件未安装',
      body: `<div class="hint warn">${needsRepair ? '组件文件缺失或损坏，请重新安装后继续。' : '安装 OCR 识别组件后，即可用阿里云识别补齐发票明细。'}</div>`,
      footer: [
        mkBtn('取消', 'ghost', () => m.close()),
        mkBtn(needsRepair ? '修复组件' : '安装组件', 'primary', () => { m.close(); openUpdateDialog(); }),
      ],
    });
    return;
  }
  if (!status.credentials_configured) {
    let m;
    m = modal({
      title: '尚未配置阿里云密钥',
      body: `<div class="hint warn">请先在设置 → 阿里云 OCR 填写 AccessKey。每个账号每月有免费额度，超出后按量计费。</div>`,
      footer: [
        mkBtn('取消', 'ghost', () => m.close()),
        mkBtn('打开设置', 'primary', () => { m.close(); openSettings(); }),
      ],
    });
    return;
  }

  let preview;
  try { preview = await Api.ocrPreview(ids); } catch (e) { toast(e.message, 'err'); return; }
  const entries = preview.entries || [];
  const withPdf = entries.filter((it) => it.has_invoice_pdf);
  const xmlCount = withPdf.filter((it) => it.has_invoice_xml).length;
  const existingCount = withPdf.filter((it) => it.existing_current_result).length;
  const canSkipExisting = ids.length > 1 && existingCount > 0;
  const noPdfCount = entries.length - withPdf.length;

  const body = el('div');
  body.innerHTML = `
    <div class="ocr-confirm-copy">
      将识别 <b id="ocrInvoiceCount">0</b> 张发票，共调用 <b id="ocrCallCount">0</b> 次。${OCR_QUOTA_NOTE} <button class="link-btn" id="ocrQuotaLink">查看免费额度</button>
    </div>
    <div class="ocr-confirm-notes">
      ${canSkipExisting ? `<label class="chk"><input type="checkbox" id="ocrSkipExisting" checked/> 跳过当前发票已有识别结果的 ${existingCount} 条（避免重复计费）</label>` : ''}
      ${xmlCount ? `<label class="chk"><input type="checkbox" id="ocrIncludeXml"/> 包含已有 XML 数据的 ${xmlCount} 条（结果仅作比对，同样计费）</label>` : ''}
      ${!canSkipExisting && existingCount ? `<div class="hint">已有识别结果，再次识别会重复计费。</div>` : ''}
      ${noPdfCount ? `<div class="hint">另有 ${noPdfCount} 条没有发票 PDF，自动跳过。</div>` : ''}
    </div>`;

  const countEl = body.querySelector('#ocrCallCount');
  const invoiceCountEl = body.querySelector('#ocrInvoiceCount');
  body.querySelector('#ocrQuotaLink')?.addEventListener('click', () => {
    Api.openExternalUrl(OCR_CONSOLE_URL).catch((e) => toast(e.message, 'err'));
  });
  const chk = body.querySelector('#ocrIncludeXml');
  const skipChk = body.querySelector('#ocrSkipExisting');
  const eligibleEntries = () => {
    const includeXml = !!(chk && chk.checked);
    const skipExisting = !!(skipChk && skipChk.checked);
    return withPdf.filter((it) =>
      (includeXml || !it.has_invoice_xml)
      && (!skipExisting || !it.existing_current_result)
    );
  };
  const renderCount = () => {
    const eligible = eligibleEntries();
    invoiceCountEl.textContent = String(eligible.length);
    countEl.textContent = String(eligible.reduce((sum, item) => sum + Math.max(1, Number(item.page_count) || 1), 0));
  };
  if (chk) chk.onchange = renderCount;
  if (skipChk) skipChk.onchange = renderCount;
  renderCount();

  const runBtn = mkBtn('开始识别', 'primary', async () => {
    runBtn.disabled = true;
    runBtn.textContent = '识别中…';
    const includeXml = !!(chk && chk.checked);
    const skipExisting = !!(skipChk && skipChk.checked);
    const targets = eligibleEntries().map((it) => it.entry_id);
    if (!targets.length) { toast('没有可识别的发票', 'err'); runBtn.disabled = false; runBtn.textContent = '开始识别'; return; }

    const progress = taskProgress(`正在识别 0/${targets.length}…`);
    const rows = [];
    const skipped = [];
    let done = 0;
    let apiCalls = 0;
    for (const entryId of targets) {
      try {
        const r = await Api.runOcrRecognition(
          [entryId], { include_xml: includeXml, skip_existing: skipExisting }
        );
        apiCalls += Number(r.called || 0);
        const row = (r.results || [])[0];
        if (row) rows.push(row);
        else (r.skipped || []).forEach((s) => skipped.push(s));
      } catch (e) {
        rows.push({ entry_id: entryId, ok: false, error: e.message });
      }
      done += 1;
      progress.update(`正在识别 ${done}/${targets.length}…`);
    }
    progress.close();
    m.close();
    State.selected.clear();
    await refreshEntries();
    refreshOcrStatus();
    showOcrSummary(rows, skipped, apiCalls);
  });

  const m = modal({
    title: '阿里云识别',
    subhead: '比对阿里云识别结果，按需采用；不自动改已经确认的数据',
    body,
    footer: [mkBtn('取消', 'ghost', () => m.close()), runBtn],
  });
}

function showOcrSummary(rows, skipped, total) {
  const labelFor = (entryId) => {
    const entry = State.entries.find((it) => it.id === entryId);
    if (!entry) return '条目';
    return entry.invoice_no ? `发票 ${entry.invoice_no}` : (entry.seller || '未识别销售方');
  };
  const okCount = rows.filter((r) => r.ok).length;
  const pendingCount = rows.filter((r) => r.ok && r.pending_count).length;
  const failedCount = rows.filter((r) => !r.ok).length;
  const rowHtml = rows.map((r) => {
    if (!r.ok) {
      return `<div class="ocr-summary-row err">
        <span class="ocr-summary-icon">✗</span>
        <span class="ocr-summary-label">${esc(labelFor(r.entry_id))}</span>
        <span class="ocr-summary-note">${esc(r.error || '识别失败')}</span>
      </div>`;
    }
    const bits = [];
    if (r.applied_fields?.length) bits.push(`已补齐 ${r.applied_fields.map((f) => OCR_FIELD_LABEL[f] || f).join('、')}`);
    if (r.items_replaced) bits.push('明细已按识别结果修复');
    if (r.local_preferred_fields?.length) bits.push(
      `已保留软件识别的 ${r.local_preferred_fields.map((f) => OCR_FIELD_LABEL[f] || f).join('、')}`
    );
    if (r.pending_count) bits.push(`${r.pending_count} 项差异待确认`);
    if (!bits.length) bits.push('与当前数据一致');
    return `<button class="ocr-summary-row${r.pending_count ? ' warn' : ' ok'}" data-ocr-goto="${esc(r.entry_id)}">
      <span class="ocr-summary-icon">${r.pending_count ? '!' : '✓'}</span>
      <span class="ocr-summary-label">${esc(labelFor(r.entry_id))}</span>
      <span class="ocr-summary-note">${esc(bits.join(' · '))}</span>
    </button>`;
  }).join('');
  const skippedHtml = (skipped || []).map((s) => `
    <div class="ocr-summary-row muted">
      <span class="ocr-summary-icon">–</span>
      <span class="ocr-summary-label">${esc(labelFor(s.entry_id))}</span>
      <span class="ocr-summary-note">${esc(s.reason || '跳过')}</span>
    </div>`).join('');
  const m = modal({
    title: '阿里云识别完成',
    subhead: `调用 ${total} 次 · 发票成功 ${okCount}${pendingCount ? ` · 待确认 ${pendingCount}` : ''}${failedCount ? ` · 失败 ${failedCount}` : ''}`,
    wide: true,
    body: `<div class="ocr-summary-list">${rowHtml || '<div class="hint">没有识别结果。</div>'}</div>${skippedHtml ? `<div class="ocr-summary-list muted-list">${skippedHtml}</div>` : ''}`,
    footer: [mkBtn('完成', 'primary', () => m.close())],
  });
  m.body.querySelectorAll('[data-ocr-goto]').forEach((btn) => {
    btn.onclick = () => { m.close(); openEntryDetail(btn.dataset.ocrGoto); };
  });
}

async function runOcrFromDetail(mm, entryId) {
  const entry = State.entries.find((it) => it.id === entryId);
  const xmlNote = entry?.attachment_types?.invoice_xml
    ? '该条目已有 XML 权威数据，本次结果仅作比对。' : '';
  let pageCount = 1;
  try {
    const preview = await Api.ocrPreview([entryId]);
    pageCount = Math.max(1, Number(preview.entries?.[0]?.page_count) || 1);
  } catch (_) { /* 识别调用会返回实际错误 */ }
  const pageNote = pageCount > 1 ? `这张发票共 ${pageCount} 页，将调用 ${pageCount} 次。` : '将调用 1 次。';
  if (!confirm(`${pageNote}${xmlNote}按量计费，继续？`)) return;
  const progress = taskProgress('正在调用阿里云识别…');
  try {
    const r = await Api.runOcrRecognition([entryId], { include_xml: true });
    progress.close();
    const row = (r.results || [])[0];
    const skip = (r.skipped || [])[0];
    if (row && row.ok) {
      toast(row.pending_count ? `识别完成，${row.pending_count} 项差异待确认` : '识别完成', row.pending_count ? '' : 'ok');
      await reopenEntryDetail(mm, entryId, { relist: true });
    } else {
      toast((row && row.error) || (skip && skip.reason) || '识别失败', 'err');
    }
  } catch (e) {
    progress.close();
    toast(e.message, 'err');
  }
}

function ocrItemValueSame(field, left, right) {
  if (field === 'spec') return true;
  const a = String(left ?? '').trim();
  const b = String(right ?? '').trim();
  if (field === 'quantity' || field === 'total') {
    const na = Number(a);
    const nb = Number(b);
    if (a !== '' && b !== '' && Number.isFinite(na) && Number.isFinite(nb)) return Math.abs(na - nb) < 0.000001;
  }
  return a.normalize('NFKC').replace(/\s/g, '') === b.normalize('NFKC').replace(/\s/g, '');
}

function ocrItemCell(value, field, other) {
  const different = other !== undefined && !ocrItemValueSame(field, value, other);
  const shown = field === 'total' ? fmtMoney(value) : esc(value || '—');
  return `<td class="${field === 'quantity' || field === 'total' ? 'num ' : ''}${different ? 'ocr-cell-diff' : ''}"${different ? ' title="与另一侧识别结果不同"' : ''}>${shown}</td>`;
}

function ocrItemsMiniTable(items, compareItems) {
  const comparing = Array.isArray(compareItems);
  const rows = (items || []).map((it, index) => {
    const other = comparing ? (compareItems[index] || {}) : null;
    return `
    <tr>
      ${ocrItemCell(it.actual_name || it.name, 'name', comparing ? (other.actual_name || other.name || '') : undefined)}
      ${ocrItemCell(it.spec, 'spec', comparing ? (other.spec ?? '') : undefined)}
      ${ocrItemCell(it.unit, 'unit', comparing ? (other.unit ?? '') : undefined)}
      ${ocrItemCell(it.quantity, 'quantity', comparing ? (other.quantity ?? '') : undefined)}
      ${ocrItemCell(it.total, 'total', comparing ? (other.total ?? '') : undefined)}
    </tr>`;
  }).join('');
  return `<div class="ocr-items-scroll"><table class="ocr-items-table">
    <colgroup><col class="ocr-item-name"><col class="ocr-item-spec"><col class="ocr-item-unit"><col class="ocr-item-qty"><col class="ocr-item-money"></colgroup>
    <thead><tr>
    <th>名称</th><th>规格</th><th>单位</th><th style="text-align:right">数量</th><th style="text-align:right">金额</th>
  </tr></thead><tbody>${rows || '<tr><td colspan="5" style="color:var(--ink-soft)">无明细</td></tr>'}</tbody></table></div>`;
}

function ocrAppliedChangesHtml(changes) {
  const fields = changes?.fields || [];
  const itemChange = changes?.items || null;
  if (!fields.length && !itemChange) return '';
  const historicalCorrection = !!changes.inferred_from_legacy || fields.some((row) => row.action === 'autofix');
  const beforeLabel = historicalCorrection ? '变动前' : '补齐前';
  const afterLabel = historicalCorrection ? '变动后' : '补齐后';
  const fieldRows = fields.map((row) => {
    const moneyField = row.field === 'total';
    const before = row.before === '' ? '—' : (moneyField ? fmtMoney(row.before) : esc(row.before));
    const after = row.after === '' ? '—' : (moneyField ? fmtMoney(row.after) : esc(row.after));
    return `<tr><td>${esc(row.label || OCR_FIELD_LABEL[row.field] || row.field)}</td><td>${before}</td><td class="ocr-ocr-col">${after}</td></tr>`;
  }).join('');
  const fieldBlock = fieldRows ? `<table class="ocr-diff-table"><thead><tr>
    <th>字段</th><th>${beforeLabel}</th><th>${afterLabel}</th>
  </tr></thead><tbody>${fieldRows}</tbody></table>` : '';
  const itemsBlock = itemChange ? `<div class="ocr-compare">
    <div><div class="ocr-compare-title">${beforeLabel}明细</div>${ocrItemsMiniTable(itemChange.before)}</div>
    <div><div class="ocr-compare-title">${afterLabel}明细</div>${ocrItemsMiniTable(itemChange.after)}</div>
  </div>` : '';
  const count = fields.length + (itemChange ? 1 : 0);
  const legacyNote = changes.inferred_from_legacy
    ? '<div class="hint">早期识别记录未直接保存变动前明细，以下原值由原发票重新读取。</div>' : '';
  return `<details class="ocr-applied-log" open>
    <summary>${historicalCorrection ? '历史自动改动' : '本次自动补齐'} ${count} 项</summary>
    <div class="ocr-applied-log-body">${legacyNote}${fieldBlock}${itemsBlock}</div>
  </details>`;
}

const OCR_ACTION_LABEL = {
  same: '一致', ocr_empty: '阿里云为空', fill: '可补齐', autofix: '可修复',
  local_preferred: '保留软件值', pending: '有差异',
};

async function loadOcrDetail(container, mm, entryId, currentItems) {
  let view;
  try { view = await Api.getOcrResult(entryId); } catch (e) {
    container.innerHTML = `<div class="hint warn">${esc(e.message)}</div>`;
    return;
  }
  const plan = view.plan;
  const latest = view.latest;
  if (!latest || !plan) {
    const failed = view.latest_failed;
    container.innerHTML = `
      ${failed ? `<div class="hint warn">上次识别失败：${esc(failed.error || '')}</div>` : ''}
      <div class="hint">尚未进行阿里云识别。</div>
      <div class="ocr-detail-actions"><button class="btn small" id="deOcrRun">${failed ? '重新识别' : '识别此发票'}</button></div>`;
    container.querySelector('#deOcrRun').onclick = () => runOcrFromDetail(mm, entryId);
    return;
  }

  const closure = plan.closure_pass
    ? '<span class="settings-ok">通过</span>'
    : '<span class="settings-warn">未通过</span>';
  const fieldRows = (plan.field_rows || []).map((row) => {
    const isMoney = row.field === 'total';
    const local = row.local === '' ? '—' : (isMoney ? fmtMoney(row.local) : esc(row.local));
    const ocr = row.ocr === '' ? '—' : (isMoney ? fmtMoney(row.ocr) : esc(row.ocr));
    const adoptable = ['fill', 'autofix', 'local_preferred', 'pending'].includes(row.action);
    return `<tr class="ocr-field-row ${row.action}">
      <td>${esc(row.label)}</td>
      <td class="ocr-field-value">${local}</td>
      <td class="ocr-ocr-col">${ocr}</td>
      <td><div class="ocr-field-decision"><span class="ocr-status ${row.action}">${OCR_ACTION_LABEL[row.action] || row.action}</span>${adoptable ? `<button class="btn small ghost" data-adopt-field="${esc(row.field)}" ${row.ocr === '' ? 'disabled' : ''}>采用</button>` : ''}</div></td>
    </tr>`;
  }).join('');

  const itemsBlock = !plan.items_differ
    ? `<div class="hint ok-hint">明细与当前一致。</div>`
    : `
      <div class="ocr-compare">
        <div><div class="ocr-compare-title">当前明细</div>${ocrItemsMiniTable(currentItems, view.ocr_items)}</div>
        <div><div class="ocr-compare-title">阿里云明细</div>${ocrItemsMiniTable(view.ocr_items, currentItems)}</div>
      </div>
      ${plan.items_action === 'pending' ? '<div class="hint warn">明细存在差异，已保留软件识别结果，请核对后决定是否采用阿里云明细。</div>' : ''}
      ${plan.closure_pass ? '' : '<div class="hint warn">阿里云明细未通过金额闭合校验，采用前请逐行核对。</div>'}
      <div class="ocr-detail-actions"><button class="btn small" id="deOcrAdoptItems">采用阿里云明细</button></div>`;

  container.innerHTML = `
    <div class="ocr-detail-meta">
      <span>识别于 ${esc(latest.created_at)}</span>
      ${latest.file_name ? `<span class="sep">·</span><span data-tooltip-overflow="${esc(latest.file_name)}">${esc(latest.file_name)}</span>` : ''}
      <span class="sep">·</span><span>金额闭合 ${closure}</span>
      ${Number(latest.api_calls || 1) > 1 ? `<span class="sep">·</span><span>本次调用 ${Number(latest.api_calls)} 次</span>` : ''}
      <span class="sep">·</span><span>本机累计调用 ${view.call_count} 次</span>
    </div>
    ${view.stale ? '<div class="hint warn">发票文件在识别后被替换过，以下结果对应旧文件，建议重新识别。</div>' : ''}
    ${view.page_coverage_stale ? `<div class="hint warn">这份发票共 ${Number(view.page_count)} 页，当前结果只识别了 ${Number(latest.api_calls || 1)} 页，请重新识别以补齐全部页面。</div>` : ''}
    ${ocrAppliedChangesHtml(view.applied_changes)}
    ${plan.pending?.length ? `<div class="hint">待确认：${plan.pending.map((f) => OCR_FIELD_LABEL[f] || f).map(esc).join('、')}。</div>` : ''}
    <table class="ocr-diff-table ocr-field-table">
      <colgroup><col class="ocr-field-name"><col class="ocr-field-current"><col class="ocr-field-cloud"><col class="ocr-field-action"></colgroup>
      <thead><tr>
      <th>字段</th><th>当前值</th><th>阿里云值</th><th>处理</th>
    </tr></thead><tbody>${fieldRows}</tbody></table>
    ${itemsBlock}
    <div class="ocr-detail-actions"><button class="btn small ghost" id="deOcrRerun">重新识别</button></div>`;

  container.querySelector('#deOcrRerun').onclick = () => runOcrFromDetail(mm, entryId);
  container.querySelectorAll('[data-adopt-field]').forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const r = await Api.applyOcrField(entryId, btn.dataset.adoptField);
        toast(r.pending?.length ? `已采用；剩余待确认 ${r.pending.length} 项` : '已采用并记录', 'ok');
        await reopenEntryDetail(mm, entryId, { relist: true });
      } catch (e) {
        toast(e.message, 'err');
        btn.disabled = false;
      }
    };
  });
  const adoptItemsBtn = container.querySelector('#deOcrAdoptItems');
  if (adoptItemsBtn) adoptItemsBtn.onclick = async () => {
    if (!confirm('用阿里云明细替换当前明细？明细表内的手动修改会被覆盖；人工修改过的「实际物资名称」会保留。')) return;
    adoptItemsBtn.disabled = true;
    try {
      await Api.applyOcrItems(entryId);
      toast('明细已采用', 'ok');
      await reopenEntryDetail(mm, entryId, { relist: true });
    } catch (e) {
      toast(e.message, 'err');
      adoptItemsBtn.disabled = false;
    }
  };
}

async function batchDelete() {
  const ids = [...State.selected];
  if (!ids.length) return;
  if (!confirm(`确认删除所选 ${ids.length} 条？此操作不可撤销。`)) return;
  try {
    const result = await Api.deleteEntries(ids);
    State.selected.clear();
    await loadBatches();
    await refreshEntries();
    toast(result.cleanup_warning || '已删除', result.cleanup_warning ? 'err' : 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

async function batchReparse() {
  const ids = [...State.selected];
  if (!ids.length) return;
  let preview;
  try { preview = await Api.recognitionPreview(ids); }
  catch (e) { toast(e.message, 'err'); return; }

  const invoice = preview.invoice || {};
  const payment = preview.payment || {};
  const body = el('div');
  body.innerHTML = `
    <div class="recognition-choice-list">
      <label class="recognition-choice${invoice.total ? '' : ' disabled'}">
        <input type="checkbox" id="recognizeInvoice" ${invoice.pending ? 'checked' : ''} ${invoice.total ? '' : 'disabled'}/>
        <span><b>发票</b><small>${invoice.pending || 0} 条待识别${invoice.current ? `，${invoice.current} 条已处理` : ''}</small></span>
      </label>
      <label class="recognition-choice${payment.total && payment.enabled ? '' : ' disabled'}">
        <input type="checkbox" id="recognizePayment" ${payment.pending && payment.enabled ? 'checked' : ''} ${payment.total && payment.enabled ? '' : 'disabled'}/>
        <span><b>付款截图</b><small>${payment.enabled ? `${payment.pending || 0} 张待识别${payment.current ? `，${payment.current} 张已处理` : ''}` : '付款截图 OCR 已关闭，可在设置中开启'}</small></span>
      </label>
    </div>`;

  const runBtn = mkBtn('开始识别', 'primary', async () => {
    const kinds = [];
    if (body.querySelector('#recognizeInvoice')?.checked) kinds.push('invoice');
    if (body.querySelector('#recognizePayment')?.checked) kinds.push('payment');
    if (!kinds.length) { toast('请选择要重新识别的材料', 'err'); return; }
    runBtn.disabled = true;
    runBtn.textContent = '识别中…';
    const kindLabel = kinds.map((kind) => kind === 'invoice' ? '发票' : '付款截图').join('、');
    const progress = taskProgress(`正在识别 0/${ids.length} 条 · ${kindLabel}`);
    try {
      const result = {
        invoice: kinds.includes('invoice') ? {
          processed: 0, resolved: 0, remaining: 0, failed: [], results: [], skipped_current: 0,
        } : null,
        payment: kinds.includes('payment') ? {
          processed: 0, recognized: 0, unrecognized: 0, failed: 0, skipped_current: 0,
        } : null,
      };
      for (let index = 0; index < ids.length; index++) {
        progress.update(`正在识别 ${index + 1}/${ids.length} 条 · ${kindLabel}`);
        await new Promise((resolve) => setTimeout(resolve, 0));
        const part = await Api.rerecognizeMaterials([ids[index]], kinds);
        if (result.invoice && part.invoice) {
          ['processed', 'resolved', 'remaining', 'skipped_current'].forEach((key) => {
            result.invoice[key] += Number(part.invoice[key] || 0);
          });
          result.invoice.failed.push(...(part.invoice.failed || []));
          result.invoice.results.push(...(part.invoice.results || []));
        }
        if (result.payment && part.payment) {
          ['processed', 'recognized', 'unrecognized', 'failed', 'skipped_current'].forEach((key) => {
            result.payment[key] += Number(part.payment[key] || 0);
          });
        }
        progress.update(`已完成 ${index + 1}/${ids.length} 条 · ${kindLabel}`);
      }
      m.close();
      State.selected.clear();
      await refreshEntries();
      const parts = [];
      if (result.invoice) {
        if (result.invoice.processed) parts.push(`发票 ${result.invoice.processed} 条`);
        if (result.invoice.skipped_current) parts.push(`发票跳过 ${result.invoice.skipped_current} 条`);
      }
      if (result.payment) {
        if (result.payment.recognized) parts.push(`付款截图识别 ${result.payment.recognized} 张`);
        if (result.payment.unrecognized) parts.push(`${result.payment.unrecognized} 张未识别到金额`);
        if (result.payment.skipped_current) parts.push(`付款截图跳过 ${result.payment.skipped_current} 张`);
      }
      const failed = (result.invoice?.failed?.length || 0) + (result.payment?.failed || 0);
      toast(parts.join('，') || '当前规则已处理，无需重复识别', failed ? 'err' : 'ok');
    } catch (e) {
      toast(e.message, 'err');
      runBtn.disabled = false;
      runBtn.textContent = '开始识别';
    } finally {
      progress.close();
    }
  });
  const m = modal({
    title: '重新识别',
    subhead: `已选择 ${ids.length} 条，可分别处理发票和付款截图`,
    body,
    footer: [mkBtn('取消', 'ghost', () => m.close()), runBtn],
  });
}

window.addEventListener('error', (e) => {
  const msg = e.error ? e.error.message : e.message;
  if (msg && !/TSMMenuKey|NSSoftLinking/i.test(msg)) toast('JS 错误：' + msg, 'err');
});
window.addEventListener('unhandledrejection', (e) => {
  const msg = e.reason && (e.reason.message || String(e.reason));
  if (msg) toast(msg, 'err');
});

// ------------------------------------------------------------------ 启动
window.addEventListener('DOMContentLoaded', init);

/* qr-ticket.js — shared operator workflow; loaded before app.js. */
// ------------------------------------------------------------------ 二维码取票
// 商家给的取票二维码（图片或截图）→ 从税务平台取回发票 XML / PDF → 与拖入发票文件走同一条导入流程。
// 只对「拖到主界面空白处」和「不在条目详情里粘贴」的图片做判断；落在卡片或详情上的图片仍是材料。
// 判断分两步：先让后端只识别二维码（不联网、很快），确认是取票二维码才联网取票；
// 不是取票二维码的图片原样回到付款截图 / 实物图的处理，不打扰用户。
const QR_IMAGE_RE = /\.(png|jpe?g|webp|bmp)$/i;
const QR_CONCURRENCY = 2;
const QR_WAIT_NOTICE_SECONDS = 6;
const QR_TIP_KEY = 'tidoc.hint.qrTicket.dismissed';
const QR_STAGE_LABEL = { decode: '识别二维码', link: '校验取票链接', download: '下载文件', content: '校验文件内容' };
// 预检时出现这些错误，说明图片里确实是税务平台的取票二维码，只是软件读不懂它；这类图片要告诉用户，不能当材料。
const QR_TICKET_LIKE_CODES = new Set(['platform_changed']);

function isQrImagePath(path) {
  return QR_IMAGE_RE.test(String(path || ''));
}

// 按顺序返回结果；最多同时跑 limit 个，stopped() 为真后不再启动新的。worker 自己处理异常。
async function mapWithLimit(items, limit, worker, stopped = () => false) {
  const results = new Array(items.length);
  let next = 0;
  const lanes = Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, async () => {
    while (next < items.length && !stopped()) {
      const index = next++;
      results[index] = await worker(items[index], index);
    }
  });
  await Promise.all(lanes);
  return results;
}

// 预检：只识别二维码，不联网。tickets 是取票二维码；rest 是应回到原有流程的路径（含非图片文件）。
async function detectQrTicketImages(paths, { api = Api, concurrency = QR_CONCURRENCY } = {}) {
  const list = [...(paths || [])];
  const candidates = list.filter(isQrImagePath);
  const verdicts = new Map();
  await mapWithLimit(candidates, concurrency, async (path) => {
    try {
      const data = await api.fetchInvoiceByQr(path, { preview_only: true });
      verdicts.set(path, { kind: 'ticket', invoice_no: data?.invoice_no || '', host: data?.host || '' });
    } catch (error) {
      verdicts.set(path, { kind: 'other', code: error?.code || '', message: error?.message || '', stage: error?.stage || '' });
    }
  });
  const found = { tickets: [], failures: [], rest: [], duplicates: [], faceQr: 0, unsupported: 0 };
  const seen = new Set();
  for (const path of list) {
    const verdict = verdicts.get(path);
    if (!verdict) { found.rest.push(path); continue; }
    const name = baseName(path);
    if (verdict.kind === 'ticket') {
      const key = verdict.invoice_no || path;
      if (seen.has(key)) { found.duplicates.push(path); continue; }
      seen.add(key);
      found.tickets.push({ path, name, invoice_no: verdict.invoice_no, host: verdict.host });
    } else if (QR_TICKET_LIKE_CODES.has(verdict.code)) {
      found.failures.push({ path, name, invoice_no: '', error: verdict.message || '取票二维码无法识别', code: verdict.code, stage: verdict.stage, open_url: '' });
    } else {
      if (verdict.code === 'invoice_face_qr') found.faceQr += 1;
      else if (verdict.code === 'image_unsupported') found.unsupported += 1;
      found.rest.push(path);
    }
  }
  return found;
}

// 联网取票：同时最多 concurrency 张，一张失败不影响其余；isCancelled() 为真后不再启动新的。
async function fetchQrTickets(tickets, { api = Api, concurrency = QR_CONCURRENCY, isCancelled = () => false, onSettle = () => {} } = {}) {
  return mapWithLimit(tickets, concurrency, async (ticket) => {
    let outcome;
    try {
      const data = await api.fetchInvoiceByQr(ticket.path, ticket.request_id ? { request_id: ticket.request_id } : null);
      outcome = { ticket, ok: true, data: data || {} };
    } catch (error) {
      outcome = {
        ticket, ok: false, error: error?.message || '取票失败', code: error?.code || '',
        stage: error?.stage || '', detail: error?.detail || '', open_url: error?.open_url || '',
      };
    }
    onSettle(outcome);
    return outcome;
  }, isCancelled);
}

function qrProgressText(done, total, seconds) {
  const notes = [];
  if (total > 1) notes.push(`已完成 ${done}/${total}`);
  if (seconds >= QR_WAIT_NOTICE_SECONDS) notes.push(`已等待 ${seconds} 秒`);
  return `正在从税务平台取票…${notes.length ? `（${notes.join('，')}）` : ''}`;
}

// 没有当成取票二维码、却值得让用户知道的两种图片。其余不是二维码的图片不提示。
function qrFallbackNotice({ faceQr = 0, unsupported = 0 } = {}) {
  const notes = [];
  if (faceQr) notes.push(`有 ${faceQr} 张图片是发票票面上的二维码（只含发票信息，没有取票地址），已按普通材料处理。取票请用商家给的取票二维码。`);
  if (unsupported) notes.push(`有 ${unsupported} 张图片是 HEIC 格式，识别不了二维码，已按普通材料处理。要取票请转存为 JPG / PNG，或截图后再试。`);
  return notes.join('');
}

// 把拖入 / 粘贴的路径里的取票二维码图片换成从税务平台取回的发票文件，其余路径原样保留。
// 返回 { paths, report, cancelled }：paths 可直接接着走原有的识别与导入；report 记录成功、警告和失败。
async function expandQrTicketImages(paths, progress, {
  api = Api, notify = toast, cleanup = cleanupDroppedPaths, now = () => Date.now(),
} = {}) {
  const list = [...(paths || [])];
  if (!list.some(isQrImagePath)) return { paths: list, report: null, cancelled: false, fetchedFileCount: 0 };
  progress?.update('正在检查图片里是否有取票二维码…');
  const found = await detectQrTicketImages(list, { api });
  const notice = qrFallbackNotice(found);
  if (notice) notify(notice, '', 7000);
  const consumed = list.filter((path) => !found.rest.includes(path));
  if (!found.tickets.length && !found.failures.length) {
    return { paths: found.rest, report: null, cancelled: false, fetchedFileCount: 0 };
  }

  const report = { fetched: [], failed: [...found.failures], duplicates: found.duplicates.length };
  const stagedFiles = [];
  let cancelled = false;
  try {
    if (found.tickets.length) {
      const batchKey = `qr-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      found.tickets.forEach((ticket, index) => { ticket.request_id = `${batchKey}-${index}`; });
      const total = found.tickets.length;
      const settled = [];
      let done = 0;
      let stop = () => {};
      const cancelSignal = new Promise((resolve) => { stop = resolve; });
      const startedAt = now();
      const show = () => progress?.update(qrProgressText(done, total, Math.floor((now() - startedAt) / 1000)));
      progress?.cancelable?.(() => {
        cancelled = true;
        for (const ticket of found.tickets) api.cancelInvoiceQr?.(ticket.request_id)?.catch(() => {});
        stop('cancelled');
      });
      show();
      const ticker = setInterval(show, 1000);
      let outcomes = [];
      try {
        const run = fetchQrTickets(found.tickets, {
          api,
          isCancelled: () => cancelled,
          onSettle: (outcome) => {
            done += 1;
            // 取消后才返回的结果没人再等，取回的文件直接丢弃。
            if (cancelled) { if (outcome.ok) cleanup(outcome.data.paths || []); return; }
            settled.push(outcome);
            show();
          },
        });
        const winner = await Promise.race([run, cancelSignal]);
        if (!cancelled) outcomes = winner;
      } finally {
        clearInterval(ticker);
        progress?.cancelable?.(null);
      }
      if (cancelled) {
        await cleanup(settled.filter((outcome) => outcome.ok).flatMap((outcome) => outcome.data.paths || []));
        notify('已取消取票，正在停止下载', '', 3200);
      } else {
        for (const outcome of outcomes) {
          const { ticket } = outcome;
          const filePaths = outcome.ok ? outcome.data.paths || [] : [];
          if (outcome.ok && filePaths.length) {
            stagedFiles.push(...filePaths);
            report.fetched.push({
              name: ticket.name, invoice_no: outcome.data.invoice_no || ticket.invoice_no,
              warnings: outcome.data.warnings || [], kinds: (outcome.data.files || []).map((file) => file.kind),
            });
          } else {
            report.failed.push({
              name: ticket.name, invoice_no: ticket.invoice_no,
              error: outcome.ok ? '没有取回发票文件，请稍后再试。' : outcome.error,
              code: outcome.code || '', stage: outcome.stage || '', detail: outcome.detail || '', open_url: outcome.open_url || '',
            });
          }
        }
      }
    }
  } catch (error) {
    await cleanup(stagedFiles);
    throw error;
  } finally {
    // 二维码图片本身只是取票的凭据，用完即删（不会动用户原来的文件，它们只是中转区里的副本）。
    await cleanup(consumed);
  }
  return { paths: [...found.rest, ...stagedFiles], report, cancelled, fetchedFileCount: stagedFiles.length };
}

function qrTicketLabel(item) {
  return `${esc(item.name || '图片')}${item.invoice_no ? ` · 发票号码 ${esc(item.invoice_no)}` : ''}`;
}

// 取票结果：取回几张、哪些只拿到一部分文件、哪些没成功以及原因。standalone 用于单独的失败窗口，不再重复标题。
function qrReportHtml(report, { standalone = false } = {}) {
  const fetched = report?.fetched || [];
  const failed = report?.failed || [];
  if (!fetched.length && !failed.length) return '';
  const warned = fetched.filter((item) => (item.warnings || []).length);
  const duplicates = report.duplicates ? `<small>另有 ${report.duplicates} 张图片的发票号码重复，已合并。</small>` : '';
  const warnRows = warned.map((item) => `
    <div class="qr-report-row warn">
      <b>${qrTicketLabel(item)}</b>
      <span>${item.warnings.map(esc).join('；')}</span>
    </div>`).join('');
  const failRows = failed.map((item, index) => {
    const tip = [QR_STAGE_LABEL[item.stage], item.detail].filter(Boolean).join('：');
    const guide = item.open_url
      ? `<div class="qr-report-actions"><button type="button" class="btn small" data-qr-retry="${index}">重试</button><button type="button" class="btn small" data-qr-open="${index}">在浏览器中打开</button><small>下载 PDF / XML 后，把文件拖进软件即可。</small></div>`
      : item.code === 'platform_changed'
        ? '<small>软件没能读出取票链接，可以用手机扫描这张二维码，在浏览器里下载发票文件后拖进软件。</small>' : '';
    return `
    <div class="qr-report-row err">
      <b>${qrTicketLabel(item)}</b>
      <span${tip ? ` title="${esc(tip)}"` : ''}>${esc(item.error)}</span>
      ${guide}
    </div>`;
  }).join('');
  if (!failed.length && !warned.length) {
    return `<div class="hint ok qr-report-head">已从税务平台取回 ${fetched.length} 张发票的文件。${duplicates}</div>`;
  }
  const head = standalone ? '' : `<div class="qr-report-head">二维码取票：${fetched.length ? `取回 ${fetched.length} 张` : ''}${fetched.length && failed.length ? '，' : ''}${failed.length ? `${failed.length} 张没有成功` : ''}${duplicates}</div>`;
  return `<div class="qr-report${failed.length ? ' has-error' : ''}">${head}${failRows}${warnRows}</div>`;
}

function bindQrReport(root, report) {
  root.addEventListener('click', (event) => {
    const retry = event.target?.closest?.('[data-qr-retry]');
    if (retry?.dataset?.qrRetry !== undefined) {
      const item = (report.failed || [])[Number(retry.dataset.qrRetry)];
      if (item?.open_url && !retry.disabled) retryQrTicket(item, retry);
      return;
    }
    const button = event.target?.closest?.('[data-qr-open]');
    if (!button) return;
    const item = (report.failed || [])[Number(button.dataset.qrOpen)];
    if (item?.open_url) Api.openExternalUrl(item.open_url).catch((e) => toast(e.message, 'err'));
  });
}

async function retryQrTicket(item, button) {
  button.disabled = true;
  button.textContent = '正在重试…';
  button.setAttribute('aria-busy', 'true');
  let paths = [];
  try {
    const data = await Api.fetchInvoiceByQr('', { retry_url: item.open_url });
    paths = data.paths || [];
    if (!paths.length) throw new Error('没有取回发票文件，请稍后再试。');
    const scan = await Api.scanFiles(paths);
    openBatchImportPreview(scan, '二维码取票重试', {cleanupPaths: paths});
    paths = []; // The import preview now owns these staged files.
    button.textContent = '已取回';
  } catch (error) {
    await cleanupDroppedPaths(paths);
    button.disabled = false;
    button.textContent = '重试';
    toast(error.message || '取票失败，请稍后重试', 'err');
  } finally {
    button.removeAttribute('aria-busy');
  }
}

function openQrFailureDialog(report) {
  const body = el('div', 'qr-report-dialog', qrReportHtml(report, { standalone: true }));
  bindQrReport(body, report);
  const m = modal({
    title: '二维码取票没有成功',
    body,
    footer: [mkBtn('关闭', 'primary', () => m.close())],
  });
  return m;
}

// 没有进入导入窗口时（全部失败，或同批的其余文件走了别的流程），把取票失败单独告诉用户。
function reportQrOutcome(qr) {
  if (qr?.report?.failed?.length) openQrFailureDialog(qr.report);
}

function qrSourceLabel(fallback, qr, invoiceCount) {
  const fetched = qr?.report?.fetched?.length || 0;
  if (!fetched) return fallback;
  return (qr.fetchedFileCount || 0) >= invoiceCount ? `${fetched} 张取票二维码` : `${fallback}（含 ${fetched} 张取票二维码）`;
}

// 「也可以把取票二维码图片拖进来」：低调的一行说明，关掉后不再出现。
function mountQrTip(slot) {
  if (!slot) return;
  slot.replaceChildren();
  if (State.qrTipDismissed) return;
  const tip = el('div', 'qr-tip', `<span>也可以把商家给的取票二维码图片拖进来，或截图后粘贴。</span>
    <button type="button" class="qr-tip-close" aria-label="不再显示这条说明" title="不再显示">${CLOSE_ICON}</button>`);
  tip.querySelector('button').onclick = () => {
    dismissQrTip();
    $$('.qr-tip').forEach((node) => node.remove());
  };
  slot.appendChild(tip);
}

function dismissQrTip() {
  State.qrTipDismissed = true;
  try { localStorage.setItem(QR_TIP_KEY, '1'); } catch (e) { /* 浏览器存储不可用时只在本次运行内生效 */ }
  Api.setAppPreference(QR_TIP_KEY, '1').catch(() => {});
}

async function loadQrTipPreference() {
  let dismissed = false;
  try { dismissed = localStorage.getItem(QR_TIP_KEY) === '1'; } catch (e) {}
  if (!dismissed) {
    try { dismissed = !!(await Api.appPreference(QR_TIP_KEY, '')); } catch (e) {}
    if (dismissed) { try { localStorage.setItem(QR_TIP_KEY, '1'); } catch (e) {} }
  }
  State.qrTipDismissed = dismissed;
}

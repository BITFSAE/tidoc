/* hub-ui.js — 共享批次；自动同步按批次开启，默认手动同步。 */
const HubUI = (() => {
  const REVIEW_LABELS = {
    submitted: '已提交',
    needs_fix: '待修改',
    updated: '已修改待复核',
    locked: '已锁定',
    withdrawn: '已撤回',
  };
  const SPACE_STATUS_LABELS = {
    active: '开放中',
    closed: '已截止',
    archived: '已归档',
  };

  let state = {
    connected: false,
    default_url: 'https://www.bitfsae.com/tidoc-hub',
    server_url: '',
    user_id: '',
    name: '',
    reviewer: '',
    role: '',
    last_space_id: '',
  };
  let batchLinks = {};
  let syncTimer = null;
  let syncInFlight = false;
  let syncGeneration = 0;
  let manualSyncPending = false;
  let sessionEpoch = 0;
  let syncIdentity = '';
  let expandedReportBatchId = '';
  const cachedReports = new Map();
  const reportLoading = new Set();
  const SYNC_LABELS = {
    manual: '手动同步', idle: '待同步', syncing: '同步中', synced: '已同步', stopped: '已停止',
    conflict: '待核对', partial: '部分未上传', error: '同步失败', paused: '已暂停', offline: '等待连接',
  };
  const REPORT_STATUS_META = {
    error: { label: '失败', tone: 'error' },
    pull_conflict: { label: '待拉取核对', tone: 'warn' },
    conflict: { label: '版本冲突', tone: 'warn' },
    locked: { label: '已锁定', tone: 'warn' },
    duplicate: { label: '重复发票', tone: 'error' },
    other_space: { label: '关联其他批次', tone: 'warn' },
    space_closed: { label: '批次已截止', tone: 'muted' },
    stopped: { label: '已停止', tone: 'warn' },
    pending: { label: '未处理', tone: 'muted' },
    uploading: { label: '上传中', tone: 'ok' },
    pushed: { label: '已上传', tone: 'ok' },
    pulled_new: { label: '已新增', tone: 'ok' },
    pulled_merged: { label: '已合并', tone: 'ok' },
    status_updated: { label: '审核更新', tone: 'ok' },
    withdrawn: { label: '已撤回', tone: 'muted' },
  };

  function isConnected() {
    return !!state.connected;
  }

  function isAdmin() {
    return state.connected && state.role === 'admin';
  }

  function applyVisibility() {
    const topBtn = $('#hubBtn');
    const batchBtn = $('#batchHubPushBtn');
    if (topBtn) topBtn.classList.toggle('hidden', !state.connected);
    if (batchBtn) batchBtn.classList.toggle('hidden', !state.connected);
  }

  async function refreshState() {
    try {
      const next = await Api.hubSettings();
      if (next && typeof next === 'object') state = { ...state, ...next };
    } catch (e) {
      // 读取本地配置失败时按未连接处理，不干扰启动。
    }
    applyVisibility();
    const identity = state.connected ? `${state.server_url}|${state.user_id}` : '';
    if (identity !== syncIdentity) {
      sessionEpoch++;
      syncIdentity = identity;
      syncGeneration = 0;
      batchLinks = {};
      cachedReports.clear();
      expandedReportBatchId = '';
      manualSyncPending = false;
      clearTimeout(syncTimer);
      syncTimer = null;
    }
    if (!state.connected) {
      sessionEpoch++;
      batchLinks = {};
      cachedReports.clear();
      expandedReportBatchId = '';
      manualSyncPending = false;
      clearTimeout(syncTimer);
      syncTimer = null;
    } else if (typeof Api.hubBatchLinks === 'function') {
      try {
        const result = await Api.hubBatchLinks();
        batchLinks = result.batches || {};
        scheduleSync();
      } catch (e) {
        batchLinks = {};
        clearTimeout(syncTimer);
        syncTimer = null;
      }
    }
    renderBatchBar();
    return state;
  }

  function batchInfo(batch) {
    if (!state.connected) return null;
    return batchLinks[batch.id] || batch.shared_batch || null;
  }

  function batchIndicatorHtml(batch) {
    const shared = batchInfo(batch);
    if (!shared) return '';
    const status = shared.state || (shared.auto_sync ? 'idle' : 'manual');
    const label = SYNC_LABELS[status] || '待同步';
    const attention = ['syncing', 'stopped', 'conflict', 'partial', 'error', 'offline', 'paused'].includes(status);
    const countSuffix = status === 'conflict' && Number(shared.conflict_count || 0) > 0
      ? ` ${Number(shared.conflict_count)}`
      : (status === 'partial' && Number(shared.other_space_count || 0) > 0 ? ` ${Number(shared.other_space_count)}` : '');
    const title = `共享批次 · ${shared.space_name || batch.name} · ${label}${shared.message ? `：${shared.message}` : ''}`;
    return `<span class="batch-shared-mark is-${esc(status)}" data-hub-batch-mark="${esc(batch.id)}" title="${esc(title)}" aria-label="${esc(title)}">${uiIcon('cloud', 12)}${attention ? `<span>${esc(label)}${esc(countSuffix)}</span>` : ''}</span>`;
  }

  function formatSyncTime(iso) {
    if (!iso) return '';
    const s = String(iso);
    const m = s.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
    if (!m) return '';
    const now = new Date();
    const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    return m[1] === today ? m[2] : `${m[1].slice(5)} ${m[2]}`;
  }

  function activeBatchObject() {
    const bid = typeof actualBatchId === 'function'
      ? actualBatchId()
      : (State.batchFilter && !String(State.batchFilter).startsWith('__') ? State.batchFilter : '');
    if (!bid) return null;
    return (State.batches || []).find((item) => item.id === bid) || (State.currentBatch?.id === bid ? State.currentBatch : null);
  }

  function formatReportMoney(val) {
    if (val === '' || val === null || val === undefined) return '';
    return typeof fmtMoney === 'function' ? fmtMoney(val) : String(val);
  }

  async function loadBatchReport(batchId) {
    if (!batchId || typeof Api.hubBatchSyncReport !== 'function' || reportLoading.has(batchId)) return;
    reportLoading.add(batchId);
    try {
      const res = await Api.hubBatchSyncReport(batchId);
      if (res && res.report) {
        cachedReports.set(batchId, res.report);
        if (res.shared && batchLinks[batchId]) batchLinks[batchId] = res.shared;
      }
    } catch (e) {
      cachedReports.set(batchId, { error: true, message: e?.message || '读取同步明细失败' });
    } finally {
      reportLoading.delete(batchId);
      renderBatchBar();
    }
  }

  function renderBatchBar() {
    const bar = typeof $ === 'function' ? $('#hubBatchBar') : null;
    if (!bar) return;
    const ds = bar.dataset || (bar.dataset = {});
    const batch = activeBatchObject();
    const shared = batch ? batchInfo(batch) : null;
    if (!batch || !shared) {
      ds.renderKey = '';
      bar.classList.add('hidden');
      bar.innerHTML = '';
      return;
    }
    const status = shared.state || (shared.auto_sync ? 'idle' : 'manual');
    const label = SYNC_LABELS[status] || '待同步';
    const toneClass = status === 'syncing'
      ? 'is-syncing'
      : (status === 'error' ? 'is-error' : (['conflict', 'partial', 'stopped'].includes(status) ? 'is-warn' : 'is-calm'));
    bar.className = `hub-batch-bar ${toneClass}`;

    const modeText = (status === 'paused' || status === 'manual') ? '' : (shared.auto_sync ? '自动同步' : '手动同步');
    const timeText = status === 'syncing' ? '' : formatSyncTime(shared.last_sync);
    const spaceLabel = shared.space_name && shared.space_name !== batch.name ? `共享「${shared.space_name}」` : '';
    const metaParts = [spaceLabel, modeText, timeText ? `上次 ${timeText}` : ''].filter(Boolean);
    let rawMsg = (shared.message || '').trim();
    if (status === 'stopped' && rawMsg.startsWith('已停止')) {
      const inner = rawMsg.match(/^已停止（(.+)）$/);
      rawMsg = inner ? inner[1] : rawMsg.replace(/^已停止[，,：:\s]*/, '');
    } else if (status === 'paused' && rawMsg === '批次已归档，同步已暂停') {
      rawMsg = '批次已归档';
    }
    const msgText = (status === 'synced' && rawMsg === '已是最新') || rawMsg === label ? '' : rawMsg;
    const isExpanded = expandedReportBatchId === batch.id;
    const issueCount = Number(shared.issue_count || 0) || (Number(shared.conflict_count || 0) + Number(shared.other_space_count || 0));
    const showDetailBtn = issueCount > 0 || !!shared.last_sync || ['syncing', 'stopped', 'error'].includes(status);
    const cachedReport = isExpanded ? cachedReports.get(batch.id) : null;
    const isLoadingReport = isExpanded && !cachedReport && reportLoading.has(batch.id);
    const renderKey = JSON.stringify([
      batch.id, status, msgText, metaParts,
      !!shared.can_stop, !!shared.can_resume, !!shared.stopping,
      issueCount, isExpanded, isLoadingReport, cachedReport || null,
    ]);
    if (ds.renderKey === renderKey && bar.innerHTML) return;
    ds.renderKey = renderKey;

    let primaryActions = '';
    if (shared.can_stop) {
      primaryActions += `<button type="button" class="btn small ghost" data-hub-bar-stop ${shared.stopping ? 'disabled' : ''}>${shared.stopping ? '停止中…' : '停止'}</button>`;
    } else if (shared.can_resume) {
      primaryActions += '<button type="button" class="btn small primary" data-hub-bar-resume>继续同步</button>';
    } else if (status !== 'paused') {
      primaryActions += `<button type="button" class="btn small ghost" data-hub-bar-sync>${status === 'error' ? '重试同步' : '立即同步'}</button>`;
    }
    if (status === 'conflict' || Number(shared.conflict_count || 0) > 0) {
      primaryActions += '<button type="button" class="btn small ghost" data-hub-bar-changes>核对变化</button>';
    }
    if (showDetailBtn) {
      const detailLabel = isExpanded ? '收起明细' : (issueCount > 0 ? `查看明细 (${issueCount})` : '同步明细');
      primaryActions += `<button type="button" class="btn small ghost" data-hub-bar-detail aria-expanded="${isExpanded ? 'true' : 'false'}">${esc(detailLabel)}</button>`;
    }
    primaryActions += '<button type="button" class="btn small ghost" data-hub-bar-settings>共享设置</button>';

    let detailHtml = '';
    if (isExpanded) {
      const report = cachedReport;
      if (!report && typeof Api.hubBatchSyncReport === 'function') {
        if (!reportLoading.has(batch.id)) loadBatchReport(batch.id);
        detailHtml = '<div class="hub-batch-detail"><span class="hub-batch-detail-empty">正在读取明细…</span></div>';
      } else if (report && report.error) {
        detailHtml = `<div class="hub-batch-detail"><span class="hub-batch-detail-empty">${esc(report.message || '读取同步明细失败')} <button type="button" class="link-btn" data-hub-report-retry>重试</button></span></div>`;
      } else if (report) {
        const counts = report.counts || {};
        const items = report.items || [];
        const chips = items.length ? [
          counts.pushed ? `<span class="hub-batch-detail-chip is-ok">已上传 ${counts.pushed}</span>` : '',
          counts.pulled ? `<span class="hub-batch-detail-chip is-ok">拉取 ${counts.pulled}</span>` : '',
          counts.status_updated ? `<span class="hub-batch-detail-chip is-ok">审核状态 ${counts.status_updated}</span>` : '',
          counts.unchanged ? `<span class="hub-batch-detail-chip">已是最新 ${counts.unchanged}</span>` : '',
          counts.conflict ? `<span class="hub-batch-detail-chip is-warn">冲突 ${counts.conflict}</span>` : '',
          counts.locked ? `<span class="hub-batch-detail-chip is-warn">已锁定 ${counts.locked}</span>` : '',
          counts.duplicate ? `<span class="hub-batch-detail-chip is-error">重复发票 ${counts.duplicate}</span>` : '',
          counts.other_space ? `<span class="hub-batch-detail-chip is-warn">关联其他批次 ${counts.other_space}</span>` : '',
          counts.stopped ? `<span class="hub-batch-detail-chip is-warn">已停止 ${counts.stopped}</span>` : '',
          counts.pending ? `<span class="hub-batch-detail-chip">未处理 ${counts.pending}</span>` : '',
          counts.error ? `<span class="hub-batch-detail-chip is-error">失败 ${counts.error}</span>` : '',
        ].filter(Boolean).join('') : '';
        const rowsHtml = items.length
          ? `<div class="hub-batch-detail-list">${items.map((item) => {
              const meta = REPORT_STATUS_META[item.status] || { label: item.status || '提示', tone: 'muted' };
              const titleText = item.seller || item.title || '未命名条目';
              const subParts = [item.invoice_no, formatReportMoney(item.total)].filter(Boolean).join(' · ');
              let rowActions = '';
              if (item.entry_id && typeof openEntryDetail === 'function') {
                rowActions += `<button type="button" class="link-btn" data-hub-report-open="${esc(item.entry_id)}">查看条目</button>`;
              }
              if (item.status === 'pull_conflict') {
                rowActions += '<button type="button" class="link-btn" data-hub-report-changes>核对合并</button>';
              } else if (item.status === 'other_space') {
                rowActions += '<button type="button" class="link-btn" data-hub-report-settings>解除旧关联</button>';
              } else if (['conflict', 'error', 'stopped', 'pending'].includes(item.status) && item.entry_id) {
                rowActions += `<button type="button" class="link-btn" data-hub-report-push="${esc(item.entry_id)}">单独重传</button>`;
              }
              return `<div class="hub-batch-detail-row">
                <div class="hub-batch-detail-entry">
                  <b title="${esc(titleText)}">${esc(titleText)}</b>
                  ${subParts ? `<span title="${esc(subParts)}">${esc(subParts)}</span>` : ''}
                </div>
                <div class="hub-batch-detail-reason">
                  <span class="badge hub-report-${esc(meta.tone)}">${esc(meta.label)}</span>
                  <span class="hub-batch-detail-reason-text">${esc(item.message || '')}</span>
                </div>
                <div class="hub-batch-detail-row-actions">${rowActions}</div>
              </div>`;
            }).join('')}</div>`
          : `<div class="hub-batch-detail-empty">${report.unchanged_count ? `共 ${report.unchanged_count} 条，均已是最新` : '暂无变动记录'}</div>`;
        detailHtml = `<div class="hub-batch-detail">${chips ? `<div class="hub-batch-detail-summary">${chips}</div>` : ''}${rowsHtml}</div>`;
      }
    }

    bar.innerHTML = `<div class="hub-batch-bar-row">
      <div class="hub-batch-bar-status">
        <span class="hub-batch-pill is-${esc(status)}">${uiIcon('cloud', 11)}<span>${esc(label)}</span></span>
        ${msgText ? `<span class="hub-batch-bar-msg">${esc(msgText)}</span>` : ''}
        ${metaParts.length ? `<span class="hub-batch-bar-meta">${esc(metaParts.join(' · '))}</span>` : ''}
      </div>
      <div class="hub-batch-bar-actions">${primaryActions}</div>
    </div>${detailHtml}`;

    bar.querySelector('[data-hub-bar-stop]')?.addEventListener('click', () => stopBatchSync(batch));
    bar.querySelector('[data-hub-bar-resume]')?.addEventListener('click', () => syncBatch(batch));
    bar.querySelector('[data-hub-bar-sync]')?.addEventListener('click', () => syncBatch(batch));
    bar.querySelector('[data-hub-bar-changes]')?.addEventListener('click', () => openBatchChanges(batch));
    bar.querySelector('[data-hub-bar-settings]')?.addEventListener('click', () => openBatchSharing(batch));
    bar.querySelector('[data-hub-bar-detail]')?.addEventListener('click', () => {
      expandedReportBatchId = expandedReportBatchId === batch.id ? '' : batch.id;
      renderBatchBar();
      if (expandedReportBatchId === batch.id) loadBatchReport(batch.id);
    });
    bar.querySelector('[data-hub-report-retry]')?.addEventListener('click', () => {
      cachedReports.delete(batch.id);
      renderBatchBar();
      loadBatchReport(batch.id);
    });
    bar.querySelectorAll('[data-hub-report-open]').forEach((btn) => {
      btn.addEventListener('click', () => {
        if (typeof openEntryDetail === 'function') openEntryDetail(btn.dataset.hubReportOpen);
      });
    });
    bar.querySelectorAll('[data-hub-report-changes]').forEach((btn) => {
      btn.addEventListener('click', () => openBatchChanges(batch));
    });
    bar.querySelectorAll('[data-hub-report-settings]').forEach((btn) => {
      btn.addEventListener('click', () => openBatchSharing(batch));
    });
    bar.querySelectorAll('[data-hub-report-push]').forEach((btn) => {
      btn.addEventListener('click', () => openPushDialog([btn.dataset.hubReportPush], {
        preferredSpaceId: shared.space_id || '',
        onDone: async () => { await refreshState(); await loadBatchReport(batch.id); },
      }));
    });
  }

  function syncFromBatches(batches) {
    if (!state.connected || !Array.isArray(batches)) return;
    batches.forEach((batch) => {
      if (!batch?.id) return;
      if (batch.shared_batch) {
        if (!batchLinks[batch.id]?.checking) batchLinks[batch.id] = batch.shared_batch;
      } else {
        delete batchLinks[batch.id];
      }
    });
    renderBatchBar();
  }

  function paintBatchStates() {
    if (typeof document.querySelectorAll === 'function') {
      document.querySelectorAll('[data-hub-batch-mark]').forEach((node) => {
        const batch = State.batches.find((item) => item.id === node.dataset.hubBatchMark);
        if (!batch) return;
        const html = batchIndicatorHtml(batch);
        if (node.outerHTML !== html) node.outerHTML = html;
      });
    }
    renderBatchBar();
  }

  function scheduleSync(delay = 2000) {
    if (syncTimer !== null || !state.connected) return;
    const needed = manualSyncPending || Object.values(batchLinks).some((link) => link.auto_sync || link.state === 'syncing');
    if (!needed) return;
    syncTimer = setTimeout(() => { syncTimer = null; pollSync(); }, delay);
  }

  async function pollSync() {
    if (!state.connected || typeof Api.hubSyncTick !== 'function') return;
    if (syncInFlight) { scheduleSync(); return; }
    const epoch = sessionEpoch;
    syncInFlight = true;
    let delay = 2000;
    try {
      const prevExpanded = expandedReportBatchId ? batchLinks[expandedReportBatchId] : null;
      const result = await Api.hubSyncTick();
      if (epoch !== sessionEpoch || !state.connected) return;
      batchLinks = result.batches || {};
      manualSyncPending = Object.values(batchLinks).some((link) => link.state === 'syncing');
      if (expandedReportBatchId) {
        const curExpanded = batchLinks[expandedReportBatchId];
        if (!curExpanded) {
          expandedReportBatchId = '';
        } else if (
          !prevExpanded
          || prevExpanded.state !== curExpanded.state
          || prevExpanded.last_sync !== curExpanded.last_sync
          || prevExpanded.message !== curExpanded.message
          || (prevExpanded.progress?.done ?? -1) !== (curExpanded.progress?.done ?? -1)
        ) {
          loadBatchReport(expandedReportBatchId);
        }
      }
      paintBatchStates();
      if (Number(result.generation || 0) !== syncGeneration) {
        syncGeneration = Number(result.generation || 0);
        await refreshEntries();
      }
    } catch (e) {
      delay = 10000;
    } finally {
      syncInFlight = false;
      if (epoch === sessionEpoch) scheduleSync(delay);
    }
  }

  async function syncBatch(batch) {
    const epoch = sessionEpoch;
    try {
      const res = await Api.hubSyncBatch(batch.id);
      if (epoch !== sessionEpoch || !state.connected) return;
      if (res && res.shared) batchLinks[batch.id] = res.shared;
      else if (batchLinks[batch.id]) batchLinks[batch.id] = { ...batchLinks[batch.id], state: 'syncing', can_stop: true, can_resume: false };
      manualSyncPending = true;
      paintBatchStates();
      await pollSync();
      scheduleSync(500);
    } catch (e) { toast(e.message, 'err'); }
  }

  async function stopBatchSync(batch) {
    if (typeof Api.hubStopBatchSync !== 'function') return;
    try {
      const res = await Api.hubStopBatchSync(batch.id);
      if (res && res.shared) batchLinks[batch.id] = res.shared;
      manualSyncPending = Object.values(batchLinks).some((link) => link.state === 'syncing');
      paintBatchStates();
      if (expandedReportBatchId === batch.id) await loadBatchReport(batch.id);
      await pollSync();
    } catch (e) { toast(e.message, 'err'); }
  }

  async function openBatchReport(batch) {
    if (!batch?.id) return;
    expandedReportBatchId = batch.id;
    const curId = typeof actualBatchId === 'function' ? actualBatchId() : State.batchFilter;
    if (curId !== batch.id && typeof focusBatch === 'function') {
      await focusBatch(batch.id);
    } else {
      renderBatchBar();
    }
    await loadBatchReport(batch.id);
  }

  async function openBatchChanges(batch) {
    const shared = batchInfo(batch);
    if (!shared) return;
    if (!Number(shared.conflict_count || 0) && Number(shared.other_space_count || 0) > 0) {
      await openBatchSharing(batch);
      return;
    }
    await openPullPreview({ id: shared.space_id, name: shared.space_name || batch.name }, {
      batchId: batch.id,
      onDone: async () => { await syncBatch(batch); },
    });
  }

  async function openBatchSharing(batch = null, { space = null } = {}) {
    const shared = batch ? batchInfo(batch) : null;
    const body = el('div', 'hub-batch-settings');
    let dialog;
    if (shared) {
      const otherCount = Number(shared.other_space_count || 0);
      const otherNames = Array.isArray(shared.other_space_names) && shared.other_space_names.length
        ? `（${shared.other_space_names.slice(0, 2).join('、')}${shared.other_space_names.length > 2 ? '等' : ''}）`
        : '';
      body.innerHTML = `<div class="hub-batch-summary"><b>${esc(shared.space_name || batch.name)}</b><span class="hub-inline-note" role="status">${esc(shared.message || SYNC_LABELS[shared.state] || '手动同步')}</span></div>
        <label class="hub-inline-check"><input id="hubBatchAutoSync" type="checkbox" ${shared.auto_sync ? 'checked' : ''}/>自动同步</label>
        <p class="hub-inline-note">打开软件时自动同步；冲突需核对。</p>
        ${otherCount > 0 ? `<div class="hub-batch-other-space">
          <span class="hub-inline-note">有 ${otherCount} 条仍关联其他共享批次${esc(otherNames)}，不会自动上传到当前批次。</span>
          <button type="button" class="btn small ghost" id="hubDetachOtherSpaceBtn">解除旧关联并同步</button>
        </div>` : ''}`;
      const toggle = body.querySelector('#hubBatchAutoSync');
      toggle.onchange = async () => {
        const enabled = toggle.checked;
        toggle.disabled = true;
        try {
          await Api.hubSetBatchAutoSync(batch.id, enabled);
          await refreshState();
          paintBatchStates();
          body.querySelector('[role="status"]').textContent = enabled ? '自动同步已开启' : '已切换为手动同步';
        } catch (e) { toggle.checked = !enabled; toast(e.message, 'err'); }
        finally { toggle.disabled = false; }
      };
      const detachBtn = body.querySelector('#hubDetachOtherSpaceBtn');
      if (detachBtn && typeof Api.hubDetachOtherSpaceEntries === 'function') {
        detachBtn.onclick = async () => {
          detachBtn.disabled = true;
          try {
            await Api.hubDetachOtherSpaceEntries(batch.id);
            dialog.close();
            await refreshState();
            await syncBatch(batch);
          } catch (e) { detachBtn.disabled = false; toast(e.message, 'err'); }
        };
      }
      const unlink = mkBtn('取消关联', 'ghost danger push-left', async () => {
        if (!confirm('取消关联后停止同步，本机和共享批次中的条目都会保留。继续吗？')) return;
        unlink.disabled = true;
        try {
          await Api.hubUnlinkBatch(batch.id);
          dialog.close();
          await refreshState();
          await refreshEntries();
        } catch (e) { unlink.disabled = false; toast(e.message, 'err'); }
      });
      dialog = modal({ title: '共享设置', compact: true, body, footer: [unlink, mkBtn('完成', 'primary', () => dialog.close())] });
      return;
    }
    let spaces;
    const progress = taskProgress('正在读取共享批次…');
    try {
      spaces = space ? [space] : (await Api.hubListSpaces(false)).spaces || [];
    } catch (e) { toast(e.message, 'err'); return; }
    finally { progress.close(); }
    const choices = spaces.filter((item) => item.status !== 'archived');
    body.innerHTML = `<div class="form-row"><label for="hubBatchSpace">共享批次</label><select id="hubBatchSpace">
      ${isAdmin() && batch && !space ? '<option value="">新建同名共享批次</option>' : ''}
      ${choices.map((item) => `<option value="${esc(item.id)}">${esc(item.name)}${item.status === 'closed' ? '（已截止）' : ''}</option>`).join('')}
      </select></div>
      <label class="hub-inline-check"><input type="checkbox" id="hubBatchAutoSync"/>自动同步</label>
      <p class="hub-inline-note">${isAdmin() ? '打开软件时自动同步；管理员可汇总全部成员条目。' : '打开软件时自动同步；队员仅同步本人条目。'}</p>`;
    const confirmBtn = mkBtn(batch ? '共享并同步' : '加入并同步', 'primary', async () => {
      confirmBtn.disabled = true;
      try {
        const result = await Api.hubLinkBatch(batch?.id || '', body.querySelector('#hubBatchSpace').value, body.querySelector('#hubBatchAutoSync').checked);
        dialog.close();
        await refreshState();
        await loadBatches();
        if (State.batchFilter !== result.batch_id) await focusBatch(result.batch_id);
        else await refreshEntries();
        await syncBatch({ id: result.batch_id });
      } catch (e) { confirmBtn.disabled = false; toast(e.message, 'err'); }
    });
    confirmBtn.disabled = !choices.length && !(isAdmin() && batch && !space);
    if (confirmBtn.disabled) body.appendChild(el('p', 'hub-inline-note', '暂无共享批次，请联系管理员创建。'));
    dialog = modal({ title: batch ? `共享「${batch.name}」` : '加入共享批次', compact: true, body,
      footer: [mkBtn('取消', 'ghost', () => dialog.close()), confirmBtn] });
  }

  function summaryText() {
    if (!state.connected) return '未连接（纯离线模式）';
    const roleLabel = state.role === 'admin' ? '管理员' : '队员';
    return `已连接 · ${state.name || '未命名'}（${roleLabel}）`;
  }

  function matchesCurrentHub(hub) {
    return !!(state.connected && hub && hub.remote_entry_id && (!hub.server_url || hub.server_url === state.server_url));
  }

  function badgeHtml(entry) {
    const hub = entry?.hub;
    if (!matchesCurrentHub(hub)) return '';
    const st = hub.review_state || '';
    const space = hub.space_name ? `「${hub.space_name}」` : '共享区';
    if (st === 'needs_fix') {
      const tip = hub.review_note ? `${space} 审核退回：${hub.review_note}` : `${space} 审核退回修改`;
      return `<span class="badge hub-needs-fix" data-tooltip="${esc(tip)}">待修改</span>`;
    }
    if (st === 'updated') {
      return `<span class="badge hub-updated" data-tooltip="${esc(`${space} 已重新提交，等待汇总人复核`)}">待复核</span>`;
    }
    if (st === 'locked') {
      return `<span class="badge hub-locked" data-tooltip="${esc(`${space} 已被审核锁定`)}">已锁定</span>`;
    }
    if (st === 'withdrawn') {
      return `<span class="badge hub-withdrawn" data-tooltip="${esc(`${space} 已撤回`)}">已撤回</span>`;
    }
    return '';
  }

  function cardNoteHtml(entry) {
    const hub = entry?.hub;
    if (!matchesCurrentHub(hub) || hub.review_state !== 'needs_fix' || !hub.review_note) return '';
    return `<div class="hub-review-note" data-tooltip-overflow="${esc(hub.review_note)}">审核意见：${esc(hub.review_note)}</div>`;
  }

  function detailBannerHtml(entry) {
    const hub = entry?.hub;
    if (!matchesCurrentHub(hub)) {
      if (!state.connected) return '';
      return `<div class="hub-detail-bar is-unlinked">
        <span>未上传到共享区</span>
        <button type="button" class="btn small ghost" data-hub-detail-push>上传到共享区</button>
      </div>`;
    }
    const label = REVIEW_LABELS[hub.review_state] || hub.review_state || '已同步';
    const canPush = state.connected && (hub.review_state !== 'locked' || isAdmin());
    const canWithdraw = state.connected && hub.review_state !== 'withdrawn' && (hub.review_state !== 'locked' || isAdmin());
    return `<div class="hub-detail-bar is-${esc(hub.review_state || 'submitted')}">
      <div class="hub-detail-copy">
        <b>共享区 · ${esc(hub.space_name || '已关联')}</b>
        <span class="badge hub-${esc(hub.review_state || 'submitted')}">${esc(label)}</span>
        ${hub.owner_name ? `<span class="hub-detail-meta">提交人 ${esc(hub.owner_name)}</span>` : ''}
        ${hub.review_note ? `<div class="hub-detail-note">审核意见：${esc(hub.review_note)}</div>` : ''}
      </div>
      <div class="hub-detail-actions">
        ${canPush ? '<button type="button" class="btn small ghost" data-hub-detail-push>上传更新</button>' : ''}
        ${isAdmin() ? '<button type="button" class="btn small ghost" data-hub-detail-review>审核状态</button>' : ''}
        ${canWithdraw ? '<button type="button" class="btn small ghost" data-hub-detail-withdraw>撤回</button>' : ''}
      </div>
    </div>`;
  }

  function bindDetailBanner(body, entry, onChanged) {
    const pushBtn = body.querySelector('[data-hub-detail-push]');
    if (pushBtn) {
      pushBtn.onclick = () => openPushDialog([entry.id], {
        preferredSpaceId: entry.hub?.space_id || '',
        onDone: onChanged,
      });
    }
    const reviewBtn = body.querySelector('[data-hub-detail-review]');
    if (reviewBtn) {
      reviewBtn.onclick = () => openReviewDialog(entry, { onDone: onChanged });
    }
    const withdrawBtn = body.querySelector('[data-hub-detail-withdraw]');
    if (withdrawBtn) {
      withdrawBtn.onclick = () => withdrawEntryFlow(entry, { onDone: onChanged });
    }
  }

  async function openPullPreview(space, { onDone, batchId = '' } = {}) {
    if (!State.currentProfileId) { toast('请先创建报账人', 'err'); return; }
    const progress = taskProgress(`正在读取共享区「${space.name}」…`);
    try {
      const preview = await Api.hubPreviewPull(space.id, State.scheme?.id);
      progress.close();
      const withdrawn = preview.withdrawn_local || [];
      const withdrawnBanner = withdrawn.length
        ? `<div class="bindle-integrity-warning hub-withdrawn-banner">
            <b>有 ${withdrawn.length} 条本机已拉取条目在共享区被撤回</b>
            <span>${esc(withdrawn.map((w) => `${w.owner_name || '成员'} · ${w.invoice_no || w.seller || '未命名'}`).join('、'))}（本机记录会保留，不会被自动删除）。</span>
          </div>`
        : '';
      if (!preview.entries?.length && !withdrawn.length) {
        toast(`共享区「${space.name}」目前没有可拉取的条目`, 'ok');
        return;
      }
      if (!preview.entries?.length && withdrawn.length) {
        const wBody = el('div', '', `
          <div class="bindle-integrity-warning hub-withdrawn-banner">
            <b>有 ${withdrawn.length} 条本机已拉取条目在共享区被撤回</b>
            <span>以下条目已被提交人撤回（本机记录会保留，不会被自动删除）：</span>
          </div>
          <ul class="diagnostic-list">${withdrawn.map((w) =>
            `<li><b>${esc(w.owner_name || '成员')}</b> · ${esc(w.invoice_no || w.seller || '未命名')}</li>`
          ).join('')}</ul>`);
        let wm;
        const ackBtn = mkBtn('确认知晓并更新状态', 'primary', async () => {
          ackBtn.disabled = true;
          try {
            const r = await Api.hubApplyPull(space.id, State.currentProfileId, { selected_entry_indexes: [] });
            wm.close();
            await refreshEntries();
            onDone?.(r);
            toast('已更新本机撤回状态标记', 'ok');
          } catch (err) {
            toast(err.message, 'err');
            ackBtn.disabled = false;
          }
        });
        wm = modal({
          title: `共享区撤回提醒 · ${space.name}`,
          body: wBody,
          footer: [mkBtn('关闭', 'ghost', () => wm.close()), ackBtn],
        });
        return;
      }
      await openBindleImportPreview(`hub:${space.id}`, preview, {
        title: `从共享区拉取 · ${space.name}`,
        sourceLabel: `共享区 · ${space.name}`,
        suggestedBatchName: space.name,
        defaults: batchId ? { batchMode: 'existing', batchId } : undefined,
        comparisonLabel: '共享区',
        sourceBanner: withdrawnBanner,
        entryReviewLabel: (entry) => REVIEW_LABELS[entry.hub_meta?.review_state] || '审核意见已更新',
        confirmLabel: '同步到本机',
        progressText: '正在下载缺失材料并同步到本机…',
        busyText: '正在同步…',
        reopenWithScheme: (scheme) => Api.hubPreviewPull(space.id, scheme),
        applyImport: (_tampered, opts) => Api.hubApplyPull(space.id, State.currentProfileId, opts),
        onImported: (r) => { onDone?.(r); },
      });
    } catch (e) {
      progress.close();
      toast(e.message, 'err');
    }
  }

  async function openSpaceEntries(space) {
    const body = el('div', '', `
      <div class="form-row">
        <label for="hubEntrySearch">搜索条目</label>
        <input id="hubEntrySearch" type="search" placeholder="发票号码、销售方、抬头或提交人" disabled>
      </div>
      <div class="hub-entry-meta" role="status"></div>
      <div class="hub-entry-results"></div>`);
    const search = body.querySelector('input');
    const status = body.querySelector('[role="status"]');
    const results = body.querySelector('.hub-entry-results');
    let entries = [];
    const render = () => {
      const query = search.value.trim().toLowerCase();
      const rows = entries.filter((entry) => [entry.invoice_no, entry.seller, entry.title, entry.owner_name]
        .some((value) => String(value || '').toLowerCase().includes(query)));
      status.textContent = !entries.length ? '这个共享批次暂无可查看的条目。'
        : query ? `显示 ${rows.length} / ${entries.length} 条` : `共 ${entries.length} 条`;
      results.innerHTML = rows.length ? `
        <table class="items-table hub-entry-table" aria-label="共享条目">
          <thead><tr><th scope="col">发票</th><th scope="col">提交人</th><th scope="col">金额</th><th scope="col">状态</th></tr></thead>
          <tbody>${rows.map((entry) => `
            <tr>
              <td><b>${esc(entry.seller || entry.title || '未命名条目')}</b>
                ${entry.invoice_no ? `<div class="hub-entry-meta">${esc(entry.invoice_no)}</div>` : ''}
              </td>
              <td>${esc(entry.owner_name || '—')}</td>
              <td class="num">${esc(fmtMoney(entry.total))}</td>
              <td><span class="badge hub-${esc(entry.review_state?.replace('_', '-'))}">${esc(REVIEW_LABELS[entry.review_state] || '—')}</span>
                ${entry.review_note ? `<div class="hub-entry-meta">${esc(entry.review_note)}</div>` : ''}
              </td>
            </tr>`).join('')}</tbody>
        </table>` : (entries.length ? '<p class="hub-inline-note">没有匹配的条目。</p>' : '');
    };
    const load = async () => {
      refreshBtn.disabled = true;
      search.disabled = true;
      status.textContent = '正在读取共享条目…';
      results.innerHTML = '';
      try {
        const res = await Api.hubListSpaceEntries(space.id);
        entries = res.entries || [];
        search.disabled = !entries.length;
        render();
      } catch (e) {
        status.textContent = '读取共享条目失败。点击「刷新」重试。';
        results.textContent = e.message;
      } finally {
        refreshBtn.disabled = false;
      }
    };
    search.oninput = render;
    const refreshBtn = mkBtn('刷新', '', load);
    const m = modal({
      title: `共享条目 · ${space.name}`,
      wide: true,
      body,
      footer: [mkBtn('返回共享批次', 'ghost', () => m.close()), refreshBtn],
    });
    await load();
  }

  async function openWorkspace() {
    if (!state.connected) {
      await openSettings();
      return;
    }
    const body = el('div', 'hub-workspace');
    if (typeof Api.hubBatchLinks === 'function') batchLinks = (await Api.hubBatchLinks()).batches || {};
    let includeArchived = false;
    let spaces = [];
    let modalHandle = null;

    const render = () => {
      const roleLabel = isAdmin() ? '管理员（可查看全部成员条目）' : '队员（仅同步自己的条目）';
      const rowsHtml = spaces.length
        ? spaces.map((sp) => {
          const linkedBatchId = Object.keys(batchLinks).find((id) => batchLinks[id].space_id === sp.id);
          const stLabel = SPACE_STATUS_LABELS[sp.status] || sp.status;
          const badges = [
            `<span class="hub-stat">共 ${Number(sp.entry_count || 0)} 条</span>`,
            Number(sp.needs_fix_count) ? `<span class="badge hub-needs-fix">待修改 ${Number(sp.needs_fix_count)}</span>` : '',
            Number(sp.updated_count) ? `<span class="badge hub-updated">待复核 ${Number(sp.updated_count)}</span>` : '',
            Number(sp.locked_count) ? `<span class="badge hub-locked">已锁定 ${Number(sp.locked_count)}</span>` : '',
          ].filter(Boolean).join('');
          return `<div class="hub-space-row" data-space-id="${esc(sp.id)}">
            <div class="hub-space-info">
              <div class="hub-space-head">
                <b>${esc(sp.name)}</b>
                <span class="hub-space-status is-${esc(sp.status)}">${esc(stLabel)}</span>
              </div>
              ${sp.description ? `<div class="hub-space-desc">${esc(sp.description)}</div>` : ''}
              <div class="hub-space-stats">${badges}</div>
            </div>
            <div class="hub-space-actions">
              <button type="button" class="btn small" data-hub-view="${esc(sp.id)}">查看条目</button>
              <button type="button" class="btn small primary" data-hub-batch="${esc(sp.id)}" data-local-batch="${esc(linkedBatchId || '')}">${linkedBatchId ? '打开批次' : '加入本机'}</button>
            </div>
          </div>`;
        }).join('')
        : '<div class="hub-inline-note">暂无共享批次。' + (isAdmin() ? '可在下方新建，或右键本地批次共享。' : '请联系负责人创建。') + '</div>';

      body.innerHTML = `
        <div class="hub-identity-bar">
          <div>
            <b>${esc(state.name || '已连接')}</b>
            <span class="hub-inline-note">${esc(roleLabel)} · ${esc(state.server_url)}</span>
          </div>
          <div class="hub-identity-actions">
            <label class="hub-inline-check"><input type="checkbox" id="hubShowArchived" ${includeArchived ? 'checked' : ''}/> 显示已归档</label>
            <button type="button" class="btn small ghost" id="hubRefreshSpaces">刷新</button>
            <button type="button" class="btn small ghost" id="hubOpenSettings">设置</button>
          </div>
        </div>
        <div class="hub-space-list">${rowsHtml}</div>
        ${isAdmin() ? `<div class="hub-quick-create">
          <input id="hubNewSpaceName" aria-label="共享批次名称" placeholder="共享批次名称"/>
          <input id="hubNewSpaceDesc" aria-label="共享批次说明" placeholder="说明（可选）"/>
          <button type="button" class="btn small" id="hubCreateSpaceBtn">新建批次</button>
        </div>` : ''}`;

      body.querySelector('#hubShowArchived').onchange = (ev) => {
        includeArchived = ev.target.checked;
        loadSpaces();
      };
      body.querySelector('#hubRefreshSpaces').onclick = () => loadSpaces();
      body.querySelector('#hubOpenSettings').onclick = () => openSettings();
      body.querySelectorAll('[data-hub-view]').forEach((btn) => {
        btn.onclick = () => {
          const space = spaces.find((item) => item.id === btn.dataset.hubView);
          if (space) openSpaceEntries(space);
        };
      });
      body.querySelectorAll('[data-hub-batch]').forEach((btn) => {
        btn.onclick = async () => {
          const sp = spaces.find((item) => item.id === btn.dataset.hubBatch);
          if (!sp) return;
          modalHandle?.close();
          if (btn.dataset.localBatch) {
            if (State.batchFilter !== btn.dataset.localBatch) await focusBatch(btn.dataset.localBatch);
          } else await openBatchSharing(null, { space: sp });
        };
      });
      const createBtn = body.querySelector('#hubCreateSpaceBtn');
      if (createBtn) {
        createBtn.onclick = async () => {
          const name = body.querySelector('#hubNewSpaceName').value.trim();
          const desc = body.querySelector('#hubNewSpaceDesc').value.trim();
          if (!name) { toast('请填写共享批次名称', 'err'); return; }
          createBtn.disabled = true;
          try {
            await Api.hubCreateSpace(name, desc);
            toast(`已创建共享批次「${name}」`, 'ok');
            await loadSpaces();
          } catch (e) { toast(e.message, 'err'); }
          finally { createBtn.disabled = false; }
        };
      }
    };

    const loadSpaces = async () => {
      body.innerHTML = '<div class="hint">正在读取共享批次…</div>';
      try {
        const res = await Api.hubListSpaces(includeArchived);
        spaces = res.spaces || [];
        if (res.hub) { state = { ...state, ...res.hub }; applyVisibility(); }
        render();
      } catch (e) {
        body.innerHTML = `<div class="diagnostic-section is-blocked"><b>读取失败</b><div>${esc(e.message)}</div></div>`;
      }
    };

    modalHandle = modal({
      title: '共享批次',
      wide: true,
      body,
      footer: [mkBtn('关闭', 'ghost', () => modalHandle.close())],
    });
    await loadSpaces();
  }

  async function openPushDialog(entryIds, { preferredSpaceId = '', onDone } = {}) {
    const ids = [...new Set((entryIds || []).filter(Boolean))];
    if (!ids.length) { toast('请先选择要上传的条目', 'err'); return; }
    if (!state.connected) {
      toast('尚未连接共享区，请先在设置中配置', 'err');
      await openSettings();
      return;
    }
    const progress = taskProgress('正在读取共享批次…');
    let spaces = [];
    try {
      const res = await Api.hubListSpaces(false);
      // 管理员允许向已截止空间推送补充/审核更新；普通队员仅可上传至开放中的空间。
      spaces = (res.spaces || []).filter((sp) => (isAdmin() ? sp.status !== 'archived' : sp.status === 'active'));
    } catch (e) {
      progress.close();
      toast(e.message, 'err');
      return;
    }
    progress.close();
    if (!spaces.length) {
      toast('暂无可上传的共享批次，请联系负责人新建', 'err');
      return;
    }

    const defaultSpaceId = spaces.some((s) => s.id === preferredSpaceId)
      ? preferredSpaceId
      : (spaces.some((s) => s.id === state.last_space_id) ? state.last_space_id : spaces[0].id);

    const labels = new Map(State.entries.map((entry) => [
      entry.id, [entry.seller, entry.invoice_no].filter(Boolean).join(' · ') || entry.title,
    ]));
    const body = el('div', 'hub-push-dialog');
    body.innerHTML = `
      <div class="form-row">
        <label for="hubPushSpace">上传到哪个共享批次</label>
        <select id="hubPushSpace">
          ${spaces.map((sp) => `<option value="${esc(sp.id)}" ${sp.id === defaultSpaceId ? 'selected' : ''}>${esc(sp.name)}${sp.status === 'closed' ? '（已截止）' : ''}</option>`).join('')}
        </select>
      </div>
      <p class="hub-inline-note">已选 <b>${ids.length}</b> 条 · 相同材料自动跳过，收款账号与密钥仅留本机</p>
      <div id="hubPushStatus" role="status"></div>
      <div id="hubPushResult"></div>`;

    const select = body.querySelector('#hubPushSpace');
    const status = body.querySelector('#hubPushStatus');
    const box = body.querySelector('#hubPushResult');
    const outcomes = new Map();
    const groups = {
      pushed: '已上传',
      unchanged: '无需上传',
      failed: '失败',
      conflicts: '版本冲突',
      duplicates: '重复发票',
      locked: '已锁定',
    };
    let uploaded = 0;
    let unchanged = 0;
    let busy = false;
    let stopRequested = false;
    let space;
    const cancelBtn = mkBtn('取消', 'ghost', () => {
      if (!busy) {
        m.close();
        return;
      }
      stopRequested = true;
      cancelBtn.disabled = true;
      status.textContent = '正在完成当前条目，随后停止。';
    });
    const pushBtn = mkBtn('开始上传', 'primary', async () => {
      if (busy) return;
      space ||= spaces.find((s) => s.id === select.value);
      if (!space) return;
      const pending = ids.filter((id) => !outcomes.has(id) || outcomes.get(id).group === 'failed');
      if (!pending.length) return;

      busy = true;
      stopRequested = false;
      select.disabled = true;
      closeBtn.disabled = true;
      pushBtn.disabled = true;
      pushBtn.textContent = '上传中…';
      cancelBtn.textContent = '停止上传';
      box.innerHTML = '';
      status.textContent = '正在检查共享区中已有的条目…';
      let prepared, prepareError = '';
      try {
        prepared = await Api.hubPreparePush(space.id, pending);
      } catch (e) {
        prepareError = e.message;
      }
      // 停止请求截断后续提交，进行中的条目照常完成。
      for (const [index, id] of pending.entries()) {
        if (stopRequested || !prepared) break;
        status.textContent = `正在处理本轮第 ${index + 1} / ${pending.length} 条 · 已上传 ${uploaded} / ${ids.length} 条${unchanged ? `，跳过 ${unchanged} 条` : ''}`;
        try {
          const res = await Api.hubPushEntries(space.id, [id], space.name, prepared.plan_id);
          const group = Object.keys(groups).find((key) => res[key]?.length);
          if (group === 'pushed') uploaded++;
          if (group === 'unchanged') unchanged++;
          outcomes.set(id, group
            ? { ...res[group][0], group }
            : { group: 'failed', error: '未收到这条记录的上传结果，请重试。' });
        } catch (e) {
          outcomes.set(id, { group: 'failed', error: e.message });
          break;
        }
      }
      const unprocessed = ids.length - outcomes.size;
      const counts = { failed: 0, conflicts: 0, duplicates: 0, locked: 0 };
      for (const { group } of outcomes.values()) {
        if (group !== 'pushed' && group !== 'unchanged') counts[group]++;
      }
      const summary = [
        uploaded || !unchanged ? `已上传 ${uploaded} 条` : '',
        unchanged ? `无需上传 ${unchanged} 条` : '',
        ...Object.keys(counts).filter((key) => counts[key]).map((key) => `${groups[key]} ${counts[key]} 条`),
        unprocessed ? `未处理 ${unprocessed} 条` : '',
      ].filter(Boolean).join('，');

      cancelBtn.disabled = true;
      status.textContent = '';
      if (uploaded + unchanged === ids.length) {
        m.close();
        toast(summary, 'ok');
      } else {
        const issueItems = ids.map((id, i) => {
          const item = outcomes.get(id);
          if (!item || item.group === 'pushed' || item.group === 'unchanged') return '';
          const label = labels.get(id) || `第 ${i + 1} 条`;
          return `<li><b>${esc(label)} · ${groups[item.group]}</b><div>${esc(item.error)}</div></li>`;
        }).join('');
        box.innerHTML = `<div class="diagnostic-section is-warning hub-push-result">
          <b role="status">${stopRequested && unprocessed ? '已停止。' : ''}${esc(summary)}。</b>
          ${prepareError ? `<div>${esc(prepareError)}</div>` : ''}
          ${issueItems ? `<ul class="diagnostic-list">${issueItems}</ul>` : ''}
          ${counts.conflicts ? '<button type="button" class="btn small" id="hubConflictPullNow">立即拉取合并</button>' : ''}
        </div>`;
        box.querySelector('#hubConflictPullNow')?.addEventListener('click', () => openPullPreview(space));
        const remaining = unprocessed + counts.failed;
        pushBtn.classList.toggle('hidden', !remaining);
        pushBtn.disabled = !remaining;
        pushBtn.textContent = unprocessed ? `继续上传（${remaining} 条）` : `重试失败条目（${remaining} 条）`;
        cancelBtn.textContent = '关闭';
        cancelBtn.disabled = false;
        closeBtn.disabled = false;
        busy = false;
        (remaining ? pushBtn : cancelBtn).focus();
      }
      try { await refreshEntries(); }
      catch (e) { toast(e.message, 'err'); }
      if (uploaded + unchanged === ids.length) onDone?.();
    });

    const m = modal({
      title: '上传到共享区',
      guard: () => !busy,
      body,
      footer: [cancelBtn, pushBtn],
    });
    const closeBtn = m.mask.querySelector('.modal-close');
  }

  async function openReviewDialog(entry, { onDone } = {}) {
    if (!isAdmin()) { toast('仅管理员可以修改审核状态', 'err'); return; }
    const hub = entry?.hub;
    if (!hub || !hub.remote_entry_id) { toast('该条目尚未关联共享区', 'err'); return; }

    const openCheckTexts = (entry.check_lines || [])
      .filter((line) => !line.reviewed && line.text)
      .map((line) => line.text);

    const body = el('div', 'hub-review-dialog');
    body.innerHTML = `
      <div class="form-row">
        <label for="hubReviewState">审核状态</label>
        <select id="hubReviewState">
          <option value="needs_fix" ${hub.review_state === 'needs_fix' || hub.review_state === 'submitted' ? 'selected' : ''}>退回修改（队员拉取后可见提醒并重传）</option>
          <option value="submitted" ${hub.review_state === 'updated' ? 'selected' : ''}>已提交（待继续审核）</option>
          <option value="locked" ${hub.review_state === 'locked' ? 'selected' : ''}>审核锁定（归档保护，防止队员再改）</option>
        </select>
      </div>
      <div class="form-row">
        <div class="hub-review-label-row">
          <label for="hubReviewNote">退回原因 / 审核说明</label>
          ${openCheckTexts.length ? '<button type="button" class="link-btn" id="hubFillCheckHints">填入本条识别提醒</button>' : ''}
        </div>
        <textarea id="hubReviewNote" rows="3" placeholder="例如：付款截图为订单页，请更换为微信/支付宝支付账单截图">${esc(hub.review_note || '')}</textarea>
      </div>
      <label class="hub-inline-check" style="margin-top:8px">
        <input type="checkbox" id="hubReviewPushLocal" checked/>
        同时将本机修改与补充材料（如查验单、识别结论）推回共享区
      </label>`;

    body.querySelector('#hubFillCheckHints')?.addEventListener('click', () => {
      const ta = body.querySelector('#hubReviewNote');
      const hintText = openCheckTexts.join('；');
      ta.value = ta.value.trim() ? `${ta.value.trim()}\n${hintText}` : hintText;
      ta.focus();
    });

    let busy = false;
    let m;
    const saveBtn = mkBtn('提交审核状态', 'primary', async () => {
      if (busy) return;
      const reviewState = body.querySelector('#hubReviewState').value;
      const reviewNote = body.querySelector('#hubReviewNote').value.trim();
      const pushLocal = body.querySelector('#hubReviewPushLocal').checked;
      busy = true;
      window.TidocUI?.setBusy(saveBtn, true, '提交中…');
      try {
        const res = await Api.hubReviewEntry(entry.id, reviewState, reviewNote, pushLocal);
        m.close();
        await refreshEntries();
        onDone?.(res);
        toast(res.message || '已更新审核状态', 'ok');
      } catch (e) {
        toast(e.message, 'err');
      } finally {
        busy = false;
        if (m.mask?.isConnected) window.TidocUI?.setBusy(saveBtn, false);
      }
    });

    m = modal({
      title: '共享区审核状态',
      guard: () => !busy,
      body,
      footer: [mkBtn('取消', 'ghost', () => { if (!busy) m.close(); }), saveBtn],
    });
  }

  async function withdrawEntryFlow(entry, { onDone } = {}) {
    const hub = entry?.hub;
    if (!hub || !hub.remote_entry_id) { toast('该条目尚未关联共享区', 'err'); return; }
    if (!confirm('确定要从共享区撤回该条目吗？撤回后本机数据不会删除，该发票可重新提交。')) return;
    try {
      const res = await Api.hubWithdrawEntry(entry.id);
      await refreshEntries();
      onDone?.(res);
      toast(res.message || '已撤回', 'ok');
    } catch (e) {
      toast(e.message, 'err');
    }
  }

  async function openSettings() {
    await refreshState();
    const defaultProfile = State.profiles.find((p) => p.is_default) || State.profiles[0] || {};
    const body = el('div', 'hub-settings');
    let m;

    const renderDisconnected = () => {
      body.innerHTML = `
        <div class="bindle-segments" role="radiogroup" aria-label="连接方式">
          <label><input type="radio" name="hubJoinMode" value="invite" checked/><span>邀请码加入</span></label>
          <label><input type="radio" name="hubJoinMode" value="token"/><span>管理员令牌</span></label>
        </div>
        <div class="form-row">
          <label for="hubServerUrl">服务器地址</label>
          <input id="hubServerUrl" value="${esc(state.server_url || state.default_url)}" placeholder="https://www.bitfsae.com/tidoc-hub"/>
        </div>
        <div id="hubInviteFields">
          <div class="form-row">
            <label for="hubInviteCode">邀请码</label>
            <input id="hubInviteCode" placeholder="向管理员获取一次性/团队邀请码" autocomplete="off"/>
          </div>
          <div class="form-grid compact">
            <div class="form-row">
              <label for="hubJoinName">本人姓名</label>
              <input id="hubJoinName" value="${esc(defaultProfile.name || '')}" placeholder="必填"/>
            </div>
            <div class="form-row">
              <label for="hubJoinReviewer">对应审核人</label>
              <input id="hubJoinReviewer" value="${esc(defaultProfile.reviewer || '')}" placeholder="可选"/>
            </div>
          </div>
        </div>
        <div id="hubTokenFields" class="hidden">
          <div class="form-row">
            <label for="hubAccessToken">访问令牌</label>
            <input id="hubAccessToken" type="password" placeholder="th_..." autocomplete="off"/>
          </div>
        </div>
        <div class="hub-settings-actions">
          <span class="hub-inline-note">收款账号与密钥仅存本机</span>
          <button type="button" class="btn primary" id="hubConnectBtn">连接共享区</button>
        </div>`;

      const modeRadios = body.querySelectorAll('[name="hubJoinMode"]');
      const syncMode = () => {
        const mode = body.querySelector('[name="hubJoinMode"]:checked').value;
        body.querySelector('#hubInviteFields').classList.toggle('hidden', mode !== 'invite');
        body.querySelector('#hubTokenFields').classList.toggle('hidden', mode !== 'token');
      };
      modeRadios.forEach((r) => { r.onchange = syncMode; });

      const connectBtn = body.querySelector('#hubConnectBtn');
      connectBtn.onclick = async () => {
        const mode = body.querySelector('[name="hubJoinMode"]:checked').value;
        const url = body.querySelector('#hubServerUrl').value.trim();
        if (!url) { toast('请填写服务器地址', 'err'); return; }
        connectBtn.disabled = true;
        connectBtn.textContent = '连接中…';
        try {
          let res;
          if (mode === 'token') {
            const tok = body.querySelector('#hubAccessToken').value.trim();
            if (!tok) { toast('请填写访问令牌', 'err'); return; }
            res = await Api.hubConnectToken(url, tok);
          } else {
            const code = body.querySelector('#hubInviteCode').value.trim();
            const name = body.querySelector('#hubJoinName').value.trim();
            const reviewer = body.querySelector('#hubJoinReviewer').value.trim();
            if (!code) { toast('请填写邀请码', 'err'); return; }
            if (!name) { toast('请填写本人姓名', 'err'); return; }
            res = await Api.hubJoin(url, code, name, reviewer);
          }
          if (res.hub) state = { ...state, ...res.hub };
          applyVisibility();
          await refreshState();
          await refreshEntries();
          toast(res.message || '已连接共享区', 'ok');
          renderConnected();
        } catch (e) {
          toast(e.message, 'err');
        } finally {
          connectBtn.disabled = false;
          connectBtn.textContent = '连接共享区';
        }
      };
    };

    const renderConnected = async () => {
      const roleLabel = isAdmin() ? '管理员' : '队员';
      body.innerHTML = `
        <div class="settings-block">
          <div class="settings-row">
            <div class="settings-row-copy">
              <b>${esc(state.name || '已连接')}（${esc(roleLabel)}）</b>
              <span>${esc(state.server_url)} · 令牌 ${esc(state.token_masked || '已保存')}</span>
            </div>
            <div class="hub-inline-btns">
              <button type="button" class="btn small ghost" id="hubVerifyBtn">验证连接</button>
              <button type="button" class="btn small danger" id="hubDisconnectBtn">断开</button>
            </div>
          </div>
        </div>
        ${isAdmin() ? '<div id="hubAdminPanel" class="hub-admin-panel"><div class="hint">正在加载管理信息…</div></div>' : ''}`;

      body.querySelector('#hubVerifyBtn').onclick = async (ev) => {
        ev.target.disabled = true;
        try {
          const res = await Api.hubRefreshMe();
          if (res.hub) state = { ...state, ...res.hub };
          applyVisibility();
          toast('连接正常', 'ok');
          renderConnected();
        } catch (e) { toast(e.message, 'err'); }
        finally { ev.target.disabled = false; }
      };
      body.querySelector('#hubDisconnectBtn').onclick = async () => {
        if (!confirm('断开后将清除本机保存的共享区令牌，已拉取到本地的条目不受影响。确定断开吗？')) return;
        const res = await Api.hubDisconnect();
        if (res.hub) state = { ...state, ...res.hub };
        applyVisibility();
        await refreshState();
        await refreshEntries();
        toast(res.message || '已断开', 'ok');
        renderDisconnected();
      };

      if (isAdmin()) {
        await renderAdminPanel(body.querySelector('#hubAdminPanel'));
      }
    };

    const renderAdminPanel = async (container) => {
      if (!container) return;
      try {
        const [spRes, invRes, usrRes] = await Promise.all([
          Api.hubListSpaces(true),
          Api.hubListInvites(),
          Api.hubListUsers(),
        ]);
        const spaces = spRes.spaces || [];
        const invites = invRes.invites || [];
        const users = usrRes.users || [];

        container.innerHTML = `
          <details class="detail-section minor-section" open>
            <summary>共享批次（${spaces.length}）</summary>
            <div class="hub-quick-create" style="margin-bottom:8px">
              <input id="hubAdmSpaceName" placeholder="新建共享批次名称"/>
              <input id="hubAdmSpaceDesc" placeholder="说明（可选）"/>
              <button type="button" class="btn small" id="hubAdmCreateSpace">新建</button>
            </div>
            <div class="hub-admin-list">
              ${spaces.map((sp) => `<div class="hub-admin-row">
                <div><b>${esc(sp.name)}</b> <span class="hub-inline-note">${esc(SPACE_STATUS_LABELS[sp.status] || sp.status)} · ${Number(sp.entry_count || 0)} 条</span></div>
                <div class="hub-inline-btns">
                  ${sp.status === 'active'
                    ? `<button type="button" class="btn small ghost" data-space-status="${esc(sp.id)}" data-next="closed">截止</button>`
                    : `<button type="button" class="btn small ghost" data-space-status="${esc(sp.id)}" data-next="active">重新开放</button>`}
                  ${sp.status !== 'archived'
                    ? `<button type="button" class="btn small ghost" data-space-status="${esc(sp.id)}" data-next="archived">归档</button>` : ''}
                </div>
              </div>`).join('') || '<div class="hub-inline-note">暂无共享批次</div>'}
            </div>
          </details>
          <details class="detail-section minor-section">
            <summary>邀请码（${invites.length}）</summary>
            <div class="hub-quick-create" style="margin-bottom:8px">
              <select id="hubAdmInvRole">
                <option value="member">队员</option>
                <option value="admin">管理员</option>
              </select>
              <input id="hubAdmInvUses" type="number" min="1" max="200" value="20" style="width:72px" title="最大使用次数"/>
              <input id="hubAdmInvNote" placeholder="备注（如：2026赛季队员）"/>
              <button type="button" class="btn small" id="hubAdmCreateInv">生成邀请码</button>
            </div>
            <div class="hub-admin-list">
              ${invites.map((inv) => `<div class="hub-admin-row">
                <div><code class="mono">${esc(inv.code)}</code> <span class="hub-inline-note">${inv.role === 'admin' ? '管理员' : '队员'} · 已用 ${Number(inv.uses || 0)}/${Number(inv.max_uses || 0)}${inv.revoked ? ' · 已撤销' : ''}${inv.note ? ` · ${esc(inv.note)}` : ''}</span></div>
                <div class="hub-inline-btns">
                  <button type="button" class="btn small ghost" data-copy-invite="${esc(inv.code)}">复制</button>
                  ${inv.revoked ? '' : `<button type="button" class="btn small danger" data-revoke-invite="${esc(inv.code)}">撤销</button>`}
                </div>
              </div>`).join('') || '<div class="hub-inline-note">暂无邀请码</div>'}
            </div>
          </details>
          <details class="detail-section minor-section">
            <summary>已加入成员（${users.length}）</summary>
            <div class="hub-admin-list">
              ${users.map((u) => `<div class="hub-admin-row">
                <div><b>${esc(u.name)}</b> <span class="hub-inline-note">${u.role === 'admin' ? '管理员' : '队员'}${u.disabled ? ' · 已停用' : ''}${u.device_name ? ` · ${esc(u.device_name)}` : ''}</span></div>
                <div class="hub-inline-btns">
                  ${u.id !== state.user_id ? `<button type="button" class="btn small ghost" data-toggle-user="${esc(u.id)}" data-disabled="${u.disabled ? '0' : '1'}">${u.disabled ? '启用' : '停用'}</button>` : '<span class="hub-inline-note">当前账号</span>'}
                </div>
              </div>`).join('')}
            </div>
          </details>`;

        container.querySelector('#hubAdmCreateSpace').onclick = async () => {
          const name = container.querySelector('#hubAdmSpaceName').value.trim();
          const desc = container.querySelector('#hubAdmSpaceDesc').value.trim();
          if (!name) { toast('请填写共享批次名称', 'err'); return; }
          try {
            await Api.hubCreateSpace(name, desc);
            toast(`已创建共享批次「${name}」`, 'ok');
            await renderAdminPanel(container);
          } catch (e) { toast(e.message, 'err'); }
        };
        container.querySelectorAll('[data-space-status]').forEach((btn) => {
          btn.onclick = async () => {
            try {
              await Api.hubUpdateSpace(btn.dataset.spaceStatus, { status: btn.dataset.next });
              await renderAdminPanel(container);
            } catch (e) { toast(e.message, 'err'); }
          };
        });
        container.querySelector('#hubAdmCreateInv').onclick = async () => {
          const role = container.querySelector('#hubAdmInvRole').value;
          const maxUses = Number(container.querySelector('#hubAdmInvUses').value || 20);
          const note = container.querySelector('#hubAdmInvNote').value.trim();
          try {
            const res = await Api.hubCreateInvite(role, maxUses, 30, note);
            if (navigator.clipboard?.writeText && res.invite?.code) {
              navigator.clipboard.writeText(res.invite.code).catch(() => {});
            }
            toast(`已生成邀请码 ${res.invite?.code || ''}（已尝试复制到剪贴板）`, 'ok');
            await renderAdminPanel(container);
          } catch (e) { toast(e.message, 'err'); }
        };
        container.querySelectorAll('[data-copy-invite]').forEach((btn) => {
          btn.onclick = () => {
            navigator.clipboard?.writeText(btn.dataset.copyInvite)
              .then(() => toast('已复制邀请码', 'ok'))
              .catch(() => toast(`邀请码：${btn.dataset.copyInvite}`, 'ok'));
          };
        });
        container.querySelectorAll('[data-revoke-invite]').forEach((btn) => {
          btn.onclick = async () => {
            try {
              await Api.hubRevokeInvite(btn.dataset.revokeInvite);
              toast('已撤销邀请码', 'ok');
              await renderAdminPanel(container);
            } catch (e) { toast(e.message, 'err'); }
          };
        });
        container.querySelectorAll('[data-toggle-user]').forEach((btn) => {
          btn.onclick = async () => {
            try {
              await Api.hubUpdateUser(btn.dataset.toggleUser, { disabled: btn.dataset.disabled === '1' });
              toast('已更新成员状态', 'ok');
              await renderAdminPanel(container);
            } catch (e) { toast(e.message, 'err'); }
          };
        });
      } catch (e) {
        container.innerHTML = `<div class="diagnostic-section is-blocked"><b>加载管理数据失败</b><div>${esc(e.message)}</div></div>`;
      }
    };

    m = modal({
      title: '协同共享区',
      key: 'hub-settings',
      wide: true,
      body,
      footer: [mkBtn('关闭', 'ghost', () => m.close())],
    });
    if (state.connected) await renderConnected();
    else renderDisconnected();
  }

  return {
    isConnected,
    batchInfo,
    batchIndicatorHtml,
    renderBatchBar,
    syncFromBatches,
    openBatchSharing,
    syncBatch,
    stopBatchSync,
    openBatchReport,
    openBatchChanges,
    isAdmin,
    matchesCurrentHub,
    refreshState,
    summaryText,
    badgeHtml,
    cardNoteHtml,
    detailBannerHtml,
    bindDetailBanner,
    openSettings,
    openWorkspace,
    openPullPreview,
    openPushDialog,
    openReviewDialog,
    withdrawEntryFlow,
  };
})();

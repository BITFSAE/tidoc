/* hub-ui.js — 团队协同共享区界面；所有网络操作均由用户显式触发。 */
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
    return state;
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

  async function openPullPreview(space, { onDone } = {}) {
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
        sourceBanner: withdrawnBanner,
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

  async function openWorkspace() {
    if (!state.connected) {
      await openSettings();
      return;
    }
    const body = el('div', 'hub-workspace');
    let includeArchived = false;
    let spaces = [];
    let modalHandle = null;

    const render = () => {
      const roleLabel = isAdmin() ? '管理员（可查看全部成员条目）' : '队员（仅同步自己的条目）';
      const rowsHtml = spaces.length
        ? spaces.map((sp) => {
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
              <button type="button" class="btn small primary" data-hub-pull="${esc(sp.id)}">拉取合并</button>
            </div>
          </div>`;
        }).join('')
        : '<div class="hint">暂无共享空间。' + (isAdmin() ? '点下方「新建空间」创建一个报账周期。' : '请联系负责人创建空间。') + '</div>';

      body.innerHTML = `
        <div class="hub-identity-bar">
          <div>
            <b>${esc(state.name || '已连接')}</b>
            <span class="hint">${esc(roleLabel)} · ${esc(state.server_url)}</span>
          </div>
          <div class="hub-identity-actions">
            <label class="hub-inline-check"><input type="checkbox" id="hubShowArchived" ${includeArchived ? 'checked' : ''}/> 显示已归档</label>
            <button type="button" class="btn small ghost" id="hubRefreshSpaces">刷新</button>
            <button type="button" class="btn small ghost" id="hubOpenSettings">设置</button>
          </div>
        </div>
        <div class="hub-space-list">${rowsHtml}</div>
        ${isAdmin() ? `<div class="hub-quick-create">
          <input id="hubNewSpaceName" placeholder="新空间名称（如：2026年11月报账）"/>
          <input id="hubNewSpaceDesc" placeholder="说明（可选）"/>
          <button type="button" class="btn small" id="hubCreateSpaceBtn">新建空间</button>
        </div>` : ''}`;

      body.querySelector('#hubShowArchived').onchange = (ev) => {
        includeArchived = ev.target.checked;
        loadSpaces();
      };
      body.querySelector('#hubRefreshSpaces').onclick = () => loadSpaces();
      body.querySelector('#hubOpenSettings').onclick = () => openSettings();
      body.querySelectorAll('[data-hub-pull]').forEach((btn) => {
        btn.onclick = () => {
          const sp = spaces.find((item) => item.id === btn.dataset.hubPull);
          if (!sp) return;
          modalHandle?.close();
          openPullPreview(sp);
        };
      });
      const createBtn = body.querySelector('#hubCreateSpaceBtn');
      if (createBtn) {
        createBtn.onclick = async () => {
          const name = body.querySelector('#hubNewSpaceName').value.trim();
          const desc = body.querySelector('#hubNewSpaceDesc').value.trim();
          if (!name) { toast('请填写空间名称', 'err'); return; }
          createBtn.disabled = true;
          try {
            await Api.hubCreateSpace(name, desc);
            toast(`已创建空间「${name}」`, 'ok');
            await loadSpaces();
          } catch (e) { toast(e.message, 'err'); }
          finally { createBtn.disabled = false; }
        };
      }
    };

    const loadSpaces = async () => {
      body.innerHTML = '<div class="hint">正在读取共享区空间列表…</div>';
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
      title: '团队协同共享区',
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
    const progress = taskProgress('正在读取共享空间…');
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
      toast('共享区暂无可上传的空间，请联系负责人新建空间', 'err');
      return;
    }

    const defaultSpaceId = spaces.some((s) => s.id === preferredSpaceId)
      ? preferredSpaceId
      : (spaces.some((s) => s.id === state.last_space_id) ? state.last_space_id : spaces[0].id);

    const body = el('div', 'hub-push-dialog');
    body.innerHTML = `
      <div class="form-row">
        <label for="hubPushSpace">上传到哪个空间</label>
        <select id="hubPushSpace">
          ${spaces.map((sp) => `<option value="${esc(sp.id)}" ${sp.id === defaultSpaceId ? 'selected' : ''}>${esc(sp.name)}${sp.status === 'closed' ? '（已截止）' : ''}（当前 ${Number(sp.entry_count || 0)} 条）</option>`).join('')}
        </select>
      </div>
      <p class="hint">即将上传 <b>${ids.length}</b> 条报账条目及其材料（自动按文件摘要去重，仅上传服务器尚未保存的材料；本机收款账号和密钥不会上传）。</p>
      <div id="hubPushResult"></div>`;

    let busy = false;
    let m;
    const pushBtn = mkBtn('开始上传', 'primary', async () => {
      if (busy) return;
      const select = body.querySelector('#hubPushSpace');
      const spaceId = select.value;
      const space = spaces.find((s) => s.id === spaceId);
      busy = true;
      window.TidocUI?.setBusy(pushBtn, true, '上传中…');
      const p = taskProgress(`正在上传 ${ids.length} 条到「${space?.name || ''}」…`);
      try {
        const res = await Api.hubPushEntries(spaceId, ids, space?.name || '');
        p.close();
        await refreshEntries();
        const hasIssues = (res.conflicts?.length || 0) + (res.duplicates?.length || 0) + (res.locked?.length || 0) + (res.failed?.length || 0);
        if (!hasIssues) {
          m.close();
          onDone?.(res);
          toast(res.message || '上传成功', 'ok');
          return;
        }
        const issueItems = [
          ...(res.conflicts || []).map((x) => `<li><b>版本冲突</b>：${esc(x.error)}</li>`),
          ...(res.duplicates || []).map((x) => `<li><b>重复发票</b>：${esc(x.error)}</li>`),
          ...(res.locked || []).map((x) => `<li><b>已锁定</b>：${esc(x.error)}</li>`),
          ...(res.failed || []).map((x) => `<li><b>失败</b>：${esc(x.error)}</li>`),
        ].join('');
        const box = body.querySelector('#hubPushResult');
        box.innerHTML = `<div class="diagnostic-section is-warning" style="margin-top:10px">
          <b>${esc(res.message)}</b>
          <ul class="diagnostic-list">${issueItems}</ul>
          ${res.conflicts?.length && space ? '<button type="button" class="btn small primary" id="hubConflictPullNow" style="margin-top:6px">立即拉取合并</button>' : ''}
        </div>`;
        box.querySelector('#hubConflictPullNow')?.addEventListener('click', () => {
          m.close();
          openPullPreview(space);
        });
        toast(res.message, 'err');
      } catch (e) {
        p.close();
        toast(e.message, 'err');
      } finally {
        busy = false;
        if (m.mask?.isConnected) window.TidocUI?.setBusy(pushBtn, false);
      }
    });

    m = modal({
      title: '上传到共享区',
      guard: () => !busy,
      body,
      footer: [mkBtn('取消', 'ghost', () => { if (!busy) m.close(); }), pushBtn],
    });
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
        <p class="hint">连接团队共享区后，队员可直接上传报账条目，审核人统一拉取、退回修改或回推补充材料。未操作时不产生后台联网；个人收款信息与本机密钥绝不会上传。</p>
        <div class="bindle-segments" role="radiogroup" aria-label="连接方式" style="margin:10px 0">
          <label><input type="radio" name="hubJoinMode" value="invite" checked/><span>凭邀请码加入</span></label>
          <label><input type="radio" name="hubJoinMode" value="token"/><span>凭管理员令牌接入</span></label>
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
            <label for="hubAccessToken">访问令牌（init-admin 生成）</label>
            <input id="hubAccessToken" type="password" placeholder="th_..." autocomplete="off"/>
          </div>
        </div>
        <div class="hub-settings-actions">
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
        ${isAdmin() ? '<div id="hubAdminPanel" class="hub-admin-panel"><div class="hint">正在加载管理信息…</div></div>' : '<p class="hint">仅在你点击「共享区」或「上传到共享区」时联网。需要更改角色或停用设备请联系管理员。</p>'}`;

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
            <summary>共享空间（${spaces.length}）</summary>
            <div class="hub-quick-create" style="margin-bottom:8px">
              <input id="hubAdmSpaceName" placeholder="新建空间名称（如：2026年12月报账）"/>
              <input id="hubAdmSpaceDesc" placeholder="说明（可选）"/>
              <button type="button" class="btn small" id="hubAdmCreateSpace">新建</button>
            </div>
            <div class="hub-admin-list">
              ${spaces.map((sp) => `<div class="hub-admin-row">
                <div><b>${esc(sp.name)}</b> <span class="hint">${esc(SPACE_STATUS_LABELS[sp.status] || sp.status)} · ${Number(sp.entry_count || 0)} 条</span></div>
                <div class="hub-inline-btns">
                  ${sp.status === 'active'
                    ? `<button type="button" class="btn small ghost" data-space-status="${esc(sp.id)}" data-next="closed">截止</button>`
                    : `<button type="button" class="btn small ghost" data-space-status="${esc(sp.id)}" data-next="active">重新开放</button>`}
                  ${sp.status !== 'archived'
                    ? `<button type="button" class="btn small ghost" data-space-status="${esc(sp.id)}" data-next="archived">归档</button>` : ''}
                </div>
              </div>`).join('') || '<div class="hint">暂无空间</div>'}
            </div>
          </details>
          <details class="detail-section minor-section" open>
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
                <div><code class="mono">${esc(inv.code)}</code> <span class="hint">${inv.role === 'admin' ? '管理员' : '队员'} · 已用 ${Number(inv.uses || 0)}/${Number(inv.max_uses || 0)}${inv.revoked ? ' · 已撤销' : ''}${inv.note ? ` · ${esc(inv.note)}` : ''}</span></div>
                <div class="hub-inline-btns">
                  <button type="button" class="btn small ghost" data-copy-invite="${esc(inv.code)}">复制</button>
                  ${inv.revoked ? '' : `<button type="button" class="btn small danger" data-revoke-invite="${esc(inv.code)}">撤销</button>`}
                </div>
              </div>`).join('') || '<div class="hint">暂无邀请码</div>'}
            </div>
          </details>
          <details class="detail-section minor-section">
            <summary>已加入成员（${users.length}）</summary>
            <div class="hub-admin-list">
              ${users.map((u) => `<div class="hub-admin-row">
                <div><b>${esc(u.name)}</b> <span class="hint">${u.role === 'admin' ? '管理员' : '队员'}${u.disabled ? ' · 已停用' : ''}${u.device_name ? ` · ${esc(u.device_name)}` : ''}</span></div>
                <div class="hub-inline-btns">
                  ${u.id !== state.user_id ? `<button type="button" class="btn small ghost" data-toggle-user="${esc(u.id)}" data-disabled="${u.disabled ? '0' : '1'}">${u.disabled ? '启用' : '停用'}</button>` : '<span class="hint">当前账号</span>'}
                </div>
              </div>`).join('')}
            </div>
          </details>`;

        container.querySelector('#hubAdmCreateSpace').onclick = async () => {
          const name = container.querySelector('#hubAdmSpaceName').value.trim();
          const desc = container.querySelector('#hubAdmSpaceDesc').value.trim();
          if (!name) { toast('请填写空间名称', 'err'); return; }
          try {
            await Api.hubCreateSpace(name, desc);
            toast(`已创建空间「${name}」`, 'ok');
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

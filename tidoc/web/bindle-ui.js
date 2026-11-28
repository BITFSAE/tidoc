/* bindle-ui.js — shared operator workflow; loaded before app.js. */
const BindlePreview = {
  profiles(insp, fallback) {
    const declared = new Map((insp.profiles || []).map((profile) => [profile.id || '__fallback__', profile]));
    const referenced = new Map();
    for (const entry of insp.entries || []) {
      const sourceId = entry.profile_id || '__fallback__';
      if (!referenced.has(sourceId)) referenced.set(sourceId, {
        id: sourceId,
        name: entry.profile_name || fallback?.name || '',
        reviewer: entry.reviewer || fallback?.reviewer || '',
        ...declared.get(sourceId),
        sourceId,
      });
    }
    return [...new Set([...declared.keys(), ...referenced.keys()])]
      .filter((sourceId) => referenced.has(sourceId)).map((sourceId) => referenced.get(sourceId));
  },
  isExisting: (entry) => entry.import_action === 'merge' || entry.import_action === 'unchanged',
  batchNames: (entry) => (entry.existing_batches || [])
    .map((batch) => `${batch.name}${batch.archived ? '（已归档）' : ''}`).join('、') || '未进批次',
  target(entry, value, name, mode, batchId, batchName, moveExisting) {
    if (value === 'keep' || (value === 'default' && this.isExisting(entry) && !moveExisting)) {
      return { mode: 'keep', label: this.batchNames(entry) };
    }
    if (value.startsWith('batch:')) return { mode: 'existing', id: value.slice(6) };
    if (value === 'new') return { mode: 'new', label: name.trim() || '待填写批次名' };
    if (value === 'none' || mode === 'none') return { mode: 'none', label: '未进批次' };
    return mode === 'existing' ? { mode: 'existing', id: batchId }
      : { mode: 'new', label: batchName.trim() || '待填写批次名' };
  },
};

const sortBindleBatches = (list) => [
  ...(list || []).filter((batch) => !batch.archived),
  ...(list || []).filter((batch) => batch.archived),
];
const bindleBatchOptionLabel = (batch) =>
  `${batch.name} · ${batch.stats?.count || 0} 条${batch.archived ? ' · 已归档' : ''}`;

async function openBindleImportPreview(path, insp, options = {}) {
  const [batchResult, tags] = await Promise.all([
    Api.listBatches(true),
    Api.listTags(),
  ]);
  const batches = Array.isArray(batchResult) ? batchResult : (batchResult?.batches || []);
  const fallback = State.profileById[State.currentProfileId];
  const entries = insp.entries || [];
  const packageProfiles = BindlePreview.profiles(insp, fallback);
  const counts = new Map(packageProfiles.map((p) => [p.sourceId, 0]));
  entries.forEach((entry) => {
    const key = entry.profile_id || '__fallback__';
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  const activeBatches = (batches || []).filter((batch) => !batch.archived);
  // 已归档批次也可以作为目标（例如补交材料），排在在办批次之后并标明，默认仍选在办批次。
  const targetBatches = sortBindleBatches(batches);
  const attachmentCount = entries.reduce((sum, entry) => sum + (entry.attachments || []).length, 0);
  const body = el('div', 'bindle-import');
  const profileRows = packageProfiles.map((profile) => `
    <div class="bindle-profile-row" data-bind-profile="${esc(profile.sourceId)}">
      <input type="checkbox" data-bind-profile-enabled aria-label="导入 ${esc(profile.name || '该报账人')}" checked/>
      <input data-bind-profile-name aria-label="报账人" value="${esc(profile.name || '')}" placeholder="填写报账人"/>
      <input data-bind-profile-reviewer aria-label="审核人" value="${esc(profile.reviewer || '')}" placeholder="填写审核人"/>
      <span data-bind-profile-count>${counts.get(profile.sourceId) || 0} 条</span>
    </div>`).join('');
  const perEntryBatchOptions = targetBatches.map((batch) =>
    `<option value="batch:${esc(batch.id)}">${esc(bindleBatchOptionLabel(batch))}</option>`
  ).join('');
  const entryRows = entries.map((entry, entryIndex) => {
    const profile = packageProfiles.find((item) => item.sourceId === (entry.profile_id || '__fallback__'));
    const isExisting = BindlePreview.isExisting(entry);
    const action = entry.import_action === 'merge' ? '已有·待补充' : isExisting ? '已有·无变化' : '新增';
    const actionDetail = entry.import_action === 'merge'
      ? (entry.merge_preview?.labels || []).join('、')
      : entry.import_action === 'unchanged'
        ? ''
        : `材料 ${(entry.attachments || []).length} 份`;
    const owner = isExisting && entry.existing_profile?.id ? entry.existing_profile : profile;
    const ownerText = `${owner?.name || entry.profile_name || '未填写'} · ${owner?.reviewer || entry.reviewer || '未填写'}`;
    return `<div class="bindle-entry-row" data-bind-entry-profile="${esc(entry.profile_id || '__fallback__')}" data-bind-entry-index="${entryIndex}" data-bind-action="${esc(entry.import_action || 'new')}">
      <input type="checkbox" data-bind-entry-enabled aria-label="导入发票 ${esc(entry.invoice_no || entryIndex + 1)}" checked/>
      <div class="bindle-entry-invoice"><span class="bindle-entry-seller" data-tooltip-overflow="${esc(entry.seller || '')}">${esc(entry.seller || '未识别销售方')}</span><small class="mono" data-tooltip-overflow="${esc(entry.invoice_no || '')}">${esc(entry.invoice_no || '无发票号')}</small></div>
      <span class="bindle-entry-amount">${fmtMoney(entry.total)}</span>
      <span class="bindle-entry-owner" title="${isExisting ? '保留本机报账人和审核人' : '新条目的报账人和审核人'}" data-tooltip-overflow="${esc(ownerText)}">${esc(ownerText)}</span>
      <div class="bindle-entry-result">
        <div class="bindle-entry-line"><span class="bindle-entry-action ${esc(entry.import_action || 'new')}">${esc(action)}</span><small data-tooltip-overflow="${esc(actionDetail)}">${esc(actionDetail)}</small></div>
        <div class="bindle-entry-line"><small class="bindle-entry-location"></small><span class="bindle-row-tools">${isExisting ? '<button type="button" class="link-btn" data-bind-entry-view title="查看本机已有条目">查看</button>' : ''}<button type="button" class="link-btn" data-bind-entry-adjust aria-label="单独设置该条目的批次" aria-expanded="false" aria-controls="bindleEntryOverride${entryIndex}">设置</button></span></div>
      </div>
      <div class="bindle-entry-override hidden" id="bindleEntryOverride${entryIndex}" data-bind-entry-override>
        <span>本条导入到</span>
        <select data-bind-entry-batch aria-label="设置该条目的目标批次">
          <option value="default">按上方统一设置</option>
          ${isExisting ? '<option value="keep">保留原批次</option>' : ''}
          <option value="none">不加入批次</option>
          ${perEntryBatchOptions}
          <option value="new">新建批次…</option>
        </select>
        <input class="hidden" data-bind-entry-new-batch aria-label="该条目的新批次名称" placeholder="输入新批次名称"/>
        <button type="button" class="link-btn" data-bind-entry-reset>恢复统一设置</button>
      </div>
    </div>`;
  }).join('');
  const existingTagOptions = (tags || []).map((tag) => `<option value="${esc(tag)}"></option>`).join('');
  const existingBatchOptions = targetBatches.map((batch) =>
    `<option value="${esc(batch.id)}">${esc(bindleBatchOptionLabel(batch))}</option>`
  ).join('');
  const defaultBatchTarget = targetBatches[0]?.id || '';
  const preset = options.defaults || null;
  const presetBatchValid = preset?.batchMode === 'existing'
    && targetBatches.some((batch) => batch.id === preset.batchId);
  const defaultBatchMode = preset?.batchMode === 'none' ? 'none'
    : preset?.batchMode === 'new' ? 'new'
      : presetBatchValid ? 'existing'
        : (activeBatches.length ? 'existing' : 'new');
  const initialBatchTarget = presetBatchValid ? preset.batchId : defaultBatchTarget;
  const suggestedBatchName = (preset?.batchMode === 'new' && String(preset.batchName || '').trim())
    || baseName(path).replace(/\.tidoc$/i, '').trim();
  const initialMoveExisting = !!(preset?.moveExisting && defaultBatchMode !== 'none');
  const initialTag = String(preset?.tag || '').trim();
  body.innerHTML = `
    <div class="bindle-import-bar">
      <strong data-tooltip-overflow="${esc(baseName(path))}">${esc(baseName(path))}</strong>
      <div class="bindle-import-meta">
        <span><b>${entries.length}</b> 条目</span>
        <span><b>${packageProfiles.length}</b> 报账人</span>
        <span><b>${attachmentCount}</b> 份材料</span>
      </div>
    </div>
    ${insp.verified ? '' : `<div class="bindle-integrity-warning">
      <b>完整性校验未通过</b><span>${esc((insp.tampered || []).join('、'))}</span>
      <label><input type="checkbox" id="bindleAllowTampered"/> 我确认继续，导入后标记为严重问题</label>
    </div>`}
    <div class="bindle-review hidden" id="bindleReview" role="status" aria-live="polite"></div>
    <details class="bindle-import-section bindle-profiles" data-bindle-review-target ${packageProfiles.length ? '' : 'hidden'}>
      <summary>报账人 <span id="bindleProfileSummary"></span></summary>
      ${entries.some((entry) => !BindlePreview.isExisting(entry)) ? '<p class="bindle-profile-note">仅新增条目可修改姓名</p>' : ''}
      <div class="bindle-profile-list">
        <div class="bindle-profile-labels"><input type="checkbox" id="bindleSelectAll" aria-label="全选报账人" title="全选报账人" checked/><span>报账人</span><span>审核人</span><span>条目</span></div>
        ${profileRows}
      </div>
    </details>
    <section class="bindle-import-section" data-bindle-review-target>
      <div class="bindle-import-options">
        <div class="form-row bindle-batch-option">
          <label>报账批次</label>
          <div class="bindle-batch-fields">
            <div class="bindle-segments" role="radiogroup" aria-label="报账批次">
              <label><input type="radio" name="bindleBatchMode" value="none" ${defaultBatchMode === 'none' ? 'checked' : ''}/><span>不加入</span></label>
              <label class="${targetBatches.length ? '' : 'disabled'}"><input type="radio" name="bindleBatchMode" value="existing" ${defaultBatchMode === 'existing' ? 'checked' : ''} ${targetBatches.length ? '' : 'disabled'}/><span>已有批次</span></label>
              <label><input type="radio" name="bindleBatchMode" value="new" ${defaultBatchMode === 'new' ? 'checked' : ''}/><span>新建批次</span></label>
            </div>
            <select id="bindleBatchSelect" class="${defaultBatchMode === 'existing' ? '' : 'hidden'}" aria-label="选择已有批次">${existingBatchOptions}</select>
            <input id="bindleNewBatch" class="${defaultBatchMode === 'new' ? '' : 'hidden'}" aria-label="新批次名称" placeholder="输入新批次名称" value="${esc(suggestedBatchName)}"/>
          </div>
          ${entries.some(BindlePreview.isExisting) ? `<label class="bindle-move-option" ${defaultBatchMode === 'none' ? 'hidden ' : ''}title="只处理本次勾选的条目，包含无变化的条目；单条设置优先"><input type="checkbox" id="bindleMoveExisting" ${initialMoveExisting ? 'checked' : ''}/> 已有条目也移入此批次</label>` : ''}
        </div>
        <div class="form-row bindle-tag-option">
          <label for="bindleTagInput">统一标签</label>
          <input id="bindleTagInput" list="bindleTagOptions" value="${esc(initialTag)}" placeholder="选择或输入标签（可选）"/>
          <datalist id="bindleTagOptions">${existingTagOptions}</datalist>
        </div>
      </div>
    </section>
    <section class="bindle-import-section bindle-entry-section">
      <div class="bindle-section-head">
        <div class="bindle-filters" role="group" aria-label="按本机情况筛选">${[
          ['all', '全部', entries.length],
          ['new', '新增', entries.filter((e) => !BindlePreview.isExisting(e)).length],
          ['merge', '已有·待补充', entries.filter((e) => e.import_action === 'merge').length],
          ['unchanged', '已有·无变化', entries.filter((e) => e.import_action === 'unchanged').length],
        ].map(([key, label, count]) => `<button type="button" data-bind-filter="${key}" aria-pressed="${key === 'all'}">${label} <b>${count}</b></button>`).join('')}</div>
        <input type="search" id="bindleSearch" aria-label="搜索包内条目" placeholder="搜索发票、销售方、报账人或批次"/>
      </div>
      <div class="bindle-selection-bar"><label><input type="checkbox" id="bindleEntrySelectAll"/> 全选当前结果</label><span class="bindle-selection-count" id="bindleSelectionCount" role="status"></span><div class="bindle-entry-tools"><button type="button" class="link-btn" id="bindleSelectVisible">仅选当前结果</button><button type="button" class="link-btn" id="bindleClearSelection">清空选择</button></div></div>
      <div class="bindle-entry-list">
        <div class="bindle-entry-head"><span></span><span>销售方 / 发票号</span><span class="bindle-entry-amount">金额</span><span>报账人 · 审核人</span><span>本机情况 / 批次去向</span></div>${entryRows}
        <div class="bindle-empty hidden" id="bindleEmpty">${entries.length ? '没有符合条件的条目' : '包内没有条目'}</div>
      </div>
    </section>`;

  const identities = insp.profile_mapping_preview || {};
  for (const row of body.querySelectorAll('[data-bind-profile]')) {
    const identity = identities[row.dataset.bindProfile];
    if (!identity || identity.status !== 'ambiguous') continue;
    body.querySelector('.bindle-profiles').open = true;
    const select = el('select'); select.dataset.bindProfileMapping = row.dataset.bindProfile;
    select.setAttribute('aria-label','选择同名报账人'); select.innerHTML = '<option value="">请选择本机同名报账人</option>' + identity.candidates.map(p=>`<option value="${esc(p.id)}">${esc(p.name)} · ${esc(p.reviewer||'无审核人')}</option>`).join(''); row.append(select);
  }
  const sourceScheme = insp.target_scheme_id || null;
  let reopening=false, importing=false;
  const onImported = options.onImported;
  const schemeSelector = el('div', 'bindle-scheme-option'); schemeSelector.innerHTML = AdapterUI.chooser(sourceScheme || State.scheme?.id);
  if (schemeSelector.firstChild) {
    body.querySelector('.bindle-import-options').classList.add('has-scheme');
    body.querySelector('.bindle-import-options').prepend(schemeSelector);
    $('[data-import-scheme]',schemeSelector).onchange = async event => {
      if (importing) return;
      importing = true;
      try {
        const updated = await Api.inspectBindle(path, event.target.value);
        await openBindleImportPreview(path, updated, {...options, replace: m.mask});
        reopening = true;
        m.close();
      } catch (e) {
        event.target.value = sourceScheme || State.scheme?.id;
        toast(e.message, 'err');
      } finally { importing = false; }
    };
  }
  const targetScheme = await Api.schemeDetails(sourceScheme);
  for (const [index, entry] of entries.entries()) {
    const row = body.querySelector(`[data-bind-entry-index="${index}"]`);
    if (!row) continue;
    const mapping = entry.role_mapping_preview;
    if (mapping?.requires_explicit_mapping?.length) {
      const panel=el('details','bindle-entry-material-map'); panel.innerHTML='<summary>材料角色对应</summary><p class="hint">保留外部材料时不会计入本机方案的必需材料。明确选择对应角色后才参与计算。</p>';
      let roles=targetScheme.definition.materials;
      if(entry.existing_entry_id){const existing=await Api.getEntry(entry.existing_entry_id);roles=existing.material_roles||roles;}
      for(const role of mapping.requires_explicit_mapping){const label=el('label','form-row');const roleName=el('span');roleName.textContent=attachTypeLabel(role);label.append(roleName);const select=el('select');select.dataset.bindRoleSource=role;select.innerHTML='<option value="">保留为外部材料</option>'+roles.filter(r=>r.id!=='invoice').map(r=>`<option value="${esc(r.id)}">${esc(r.label)}</option>`).join('');label.append(select);panel.append(label);}row.append(panel);
    }
    const notes=[];
    if(entry.adapter_compatibility?.missing_capabilities?.length)notes.push('来源方案有未支持的规则，请保留原资料并核对本机要求。');
    if(entry.extension_merge_preview?.skipped)notes.push('部分附加信息无法对应，保留本机值及外部历史信息。');
    if(notes.length){const hint=el('p','hint bindle-entry-note');hint.textContent=notes.join(' ');row.append(hint);}
  }

  const tagInput = body.querySelector('#bindleTagInput');
  const batchSelect = body.querySelector('#bindleBatchSelect');
  const newBatch = body.querySelector('#bindleNewBatch');
  const moveExisting = body.querySelector('#bindleMoveExisting');
  const profileRowElements = [...body.querySelectorAll('[data-bind-profile]')];
  const entryRowElements = [...body.querySelectorAll('[data-bind-entry-index]')];
  const entryFor = (row) => entries[Number(row.dataset.bindEntryIndex)];
  batchSelect.value = initialBatchTarget;
  const batchMode = () => body.querySelector('[name="bindleBatchMode"]:checked').value;
  const targetFor = (row) => {
    const target = BindlePreview.target(entryFor(row), row.querySelector('[data-bind-entry-batch]').value,
      row.querySelector('[data-bind-entry-new-batch]').value, batchMode(), batchSelect.value,
      newBatch.value, !!moveExisting?.checked && batchMode() !== 'none');
    if (target.mode === 'existing') {
      const batch = targetBatches.find((item) => item.id === target.id);
      target.label = batch ? `${batch.name}${batch.archived ? '（已归档）' : ''}` : '待选择批次';
      target.archived = !!batch?.archived;
    } else if (target.mode === 'keep') {
      target.archived = (entryFor(row).existing_batches || []).some((batch) => batch.archived);
    } else if (target.mode === 'new') {
      target.label += '（新建）';
    }
    const currentBatches = entryFor(row).existing_batches || [];
    target.unchanged = BindlePreview.isExisting(entryFor(row)) && (
      (target.mode === 'existing' && currentBatches.length === 1 && currentBatches[0].id === target.id)
      || (target.mode === 'none' && !currentBatches.length)
    );
    return target;
  };
  const renderTargets = () => {
    entryRowElements.forEach((row) => {
      const entry = entryFor(row), target = targetFor(row);
      const select = row.querySelector('[data-bind-entry-batch]');
      row.querySelector('[data-bind-entry-new-batch]').classList.toggle('hidden', select.value !== 'new');
      row.classList.toggle('has-bindle-override', select.value !== 'default');
      const summary = row.querySelector('.bindle-entry-location');
      summary.textContent = target.mode === 'keep' || target.unchanged ? `保留 · ${target.label}`
        : BindlePreview.isExisting(entry) ? `${BindlePreview.batchNames(entry)} → ${target.label}`
          : `导入到 · ${target.label}`;
      summary.dataset.tooltipOverflow = summary.textContent;
    });
  };
  const selectedEntryRows = () => entryRowElements.filter(
    (row) => row.querySelector('[data-bind-entry-enabled]').checked
  );
  const visibleEntryRows = () => entryRowElements.filter((row) => !row.hidden);
  let activeFilter = 'all';
  let importButton;
  let reviewing = false;
  const syncBindleSelection = () => {
    const selectedRows = selectedEntryRows();
    entryRowElements.forEach((row) => {
      row.classList.toggle('off', !row.querySelector('[data-bind-entry-enabled]').checked);
    });
    profileRowElements.forEach((row) => {
      const matchingRows = entryRowElements.filter(
        (entryRow) => entryRow.dataset.bindEntryProfile === row.dataset.bindProfile
      );
      const selected = matchingRows.filter((entryRow) => entryRow.querySelector('[data-bind-entry-enabled]').checked);
      const enabledInput = row.querySelector('[data-bind-profile-enabled]');
      enabledInput.checked = selected.length > 0 && selected.length === matchingRows.length;
      enabledInput.indeterminate = selected.length > 0 && selected.length < matchingRows.length;
      row.classList.toggle('off', !selected.length);
      row.querySelector('[data-bind-profile-count]').textContent = selected.length && selected.length < matchingRows.length
        ? `${selected.length} / ${matchingRows.length}` : `${matchingRows.length} 条`;
      const hasNew = selected.some((entryRow) => !BindlePreview.isExisting(entryFor(entryRow)));
      row.querySelector('[data-bind-profile-name]').disabled = !hasNew;
      row.querySelector('[data-bind-profile-reviewer]').disabled = !hasNew;
    });
    const all = body.querySelector('#bindleSelectAll');
    all.checked = selectedRows.length > 0 && selectedRows.length === entries.length;
    all.indeterminate = selectedRows.length > 0 && selectedRows.length < entries.length;
    const visible = visibleEntryRows();
    const selectedVisible = visible.filter((row) => row.querySelector('[data-bind-entry-enabled]').checked).length;
    const entryAll = body.querySelector('#bindleEntrySelectAll');
    entryAll.disabled = !visible.length;
    entryAll.checked = selectedVisible > 0 && selectedVisible === visible.length;
    entryAll.indeterminate = selectedVisible > 0 && selectedVisible < visible.length;
    const hiddenSelected = selectedRows.length - selectedVisible;
    body.querySelector('#bindleSelectionCount').textContent = `已选 ${selectedRows.length} / ${entries.length}${hiddenSelected ? `（筛选外 ${hiddenSelected} 条）` : ''}`;
    body.querySelector('#bindleSelectVisible').disabled = !visible.length;
    body.querySelector('#bindleClearSelection').disabled = !selectedRows.length;
    const selectedProfiles = profileRowElements.filter((row) => !row.classList.contains('off')).length;
    body.querySelector('#bindleProfileSummary').textContent = selectedRows.length === entries.length
      ? `全部 ${packageProfiles.length} 位` : `已选 ${selectedProfiles} / ${packageProfiles.length} 位`;
    all.disabled = !entries.length;
    if (importButton) {
      importButton.textContent = !selectedRows.length ? '请选择条目'
        : reviewing ? `确认导入 ${selectedRows.length} 条` : `导入 ${selectedRows.length} 条`;
      importButton.disabled = !selectedRows.length;
    }
    if (reviewing) renderReview();
  };
  const filterEntries = () => {
    const query = body.querySelector('#bindleSearch').value.trim().toLocaleLowerCase();
    entryRowElements.forEach((row) => {
      const entry = entryFor(row);
      const profile = packageProfiles.find((item) => item.sourceId === row.dataset.bindEntryProfile);
      const haystack = [entry.invoice_no, entry.seller, entry.title, profile?.name, profile?.reviewer,
        row.querySelector('.bindle-entry-owner').textContent, BindlePreview.batchNames(entry)].join(' ').toLocaleLowerCase();
      row.hidden = (activeFilter !== 'all' && row.dataset.bindAction !== activeFilter) || !haystack.includes(query);
    });
    body.querySelectorAll('[data-bind-filter]').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset.bindFilter === activeFilter));
    });
    body.querySelector('#bindleEmpty').classList.toggle('hidden', visibleEntryRows().length > 0);
    syncBindleSelection();
  };
  body.querySelectorAll('[data-bind-filter]').forEach((button) => {
    button.onclick = () => { activeFilter = button.dataset.bindFilter; filterEntries(); };
  });
  body.querySelector('#bindleSearch').oninput = filterEntries;
  body.querySelector('#bindleSelectAll').onchange = (event) => {
    entryRowElements.forEach((row) => { row.querySelector('[data-bind-entry-enabled]').checked = event.target.checked; });
    syncBindleSelection();
  };
  body.querySelector('#bindleEntrySelectAll').onchange = (event) => {
    visibleEntryRows().forEach((row) => { row.querySelector('[data-bind-entry-enabled]').checked = event.target.checked; });
    syncBindleSelection();
  };
  body.querySelector('#bindleSelectVisible').onclick = () => {
    entryRowElements.forEach((row) => { row.querySelector('[data-bind-entry-enabled]').checked = !row.hidden; });
    syncBindleSelection();
  };
  body.querySelector('#bindleClearSelection').onclick = () => {
    entryRowElements.forEach((row) => { row.querySelector('[data-bind-entry-enabled]').checked = false; });
    syncBindleSelection();
  };
  profileRowElements.forEach((row) => {
    const enabledInput = row.querySelector('[data-bind-profile-enabled]');
    const nameInput = row.querySelector('[data-bind-profile-name]');
    const reviewerInput = row.querySelector('[data-bind-profile-reviewer]');
    const syncOwner = () => {
      entryRowElements.forEach((entryRow) => {
        if (entryRow.dataset.bindEntryProfile !== row.dataset.bindProfile || BindlePreview.isExisting(entryFor(entryRow))) return;
        const owner = entryRow.querySelector('.bindle-entry-owner');
        owner.textContent = `${nameInput.value.trim() || '未填写'} · ${reviewerInput.value.trim() || '未填写'}`;
        owner.dataset.tooltipOverflow = owner.textContent;
      });
      filterEntries();
    };
    nameInput.oninput = syncOwner;
    reviewerInput.oninput = syncOwner;
    enabledInput.onchange = () => {
      entryRowElements.forEach((entryRow) => {
        if (entryRow.dataset.bindEntryProfile === row.dataset.bindProfile) {
          entryRow.querySelector('[data-bind-entry-enabled]').checked = enabledInput.checked;
        }
      });
      syncBindleSelection();
    };
  });
  body.querySelectorAll('[name="bindleBatchMode"]').forEach((input) => {
    input.onchange = () => {
      const mode = batchMode();
      batchSelect.classList.toggle('hidden', mode !== 'existing');
      newBatch.classList.toggle('hidden', mode !== 'new');
      if (moveExisting) {
        moveExisting.closest('label').hidden = mode === 'none';
        if (mode === 'none') moveExisting.checked = false;
      }
      if (mode === 'existing') enhanceNativeSelects(batchSelect);
      if (mode === 'new') newBatch.focus();
      renderTargets();
    };
  });
  entryRowElements.forEach((row) => {
    row.querySelector('[data-bind-entry-enabled]').onchange = syncBindleSelection;
    const view = row.querySelector('[data-bind-entry-view]');
    if (view) view.onclick = () => openEntryDetail(entryFor(row).existing_entry_id).catch((error) => toast(error.message, 'err'));
    const adjustButton = row.querySelector('[data-bind-entry-adjust]');
    const editor = row.querySelector('[data-bind-entry-override]');
    const select = row.querySelector('[data-bind-entry-batch]');
    const newBatchInput = row.querySelector('[data-bind-entry-new-batch]');
    adjustButton.onclick = () => {
      const opening = editor.classList.contains('hidden');
      // 收起面板时如果焦点还在里面，收起后把焦点放回该行的「设置」，不要落回页面。
      const focusedRow = entryRowElements.find((otherRow) =>
        otherRow.querySelector('[data-bind-entry-override]').contains(document.activeElement));
      entryRowElements.forEach((otherRow) => {
        const expanded = otherRow === row && opening;
        otherRow.querySelector('[data-bind-entry-override]').classList.toggle('hidden', !expanded);
        otherRow.classList.toggle('editing-bindle-override', expanded);
        const button = otherRow.querySelector('[data-bind-entry-adjust]');
        button.textContent = expanded ? '收起' : '设置';
        button.setAttribute('aria-expanded', String(expanded));
      });
      if (focusedRow && focusedRow.querySelector('[data-bind-entry-override]').classList.contains('hidden')) {
        focusedRow.querySelector('[data-bind-entry-adjust]').focus();
      }
      if (opening) enhanceNativeSelects(select);
    };
    select.onchange = () => {
      renderTargets();
      if (select.value === 'new') newBatchInput.focus();
    };
    row.querySelector('[data-bind-entry-reset]').onclick = () => {
      select.value = 'default';
      newBatchInput.value = '';
      renderTargets();
      if (reviewing) renderReview();
    };
  });

  // 校验输入并整理导入参数；有问题时提示并返回 null。
  const collectImport = () => {
    const rows = selectedEntryRows();
    const selected = new Set(rows.map((row) => row.dataset.bindEntryProfile));
    const selectedIndexes = new Set(rows.map((row) => Number(row.dataset.bindEntryIndex)));
    const selectedCount = rows.length;
    if (!selectedCount) { toast('请选择要导入的条目', 'err'); return null; }
    const tampered = body.querySelector('#bindleAllowTampered');
    if (tampered && !tampered.checked) {
      toast('请确认完整性异常后再导入', 'err');
      return null;
    }
    const profileOverrides = {};
    let invalidProfile = false;
    body.querySelectorAll('[data-bind-profile]').forEach((row) => {
      if (!selected.has(row.dataset.bindProfile)) return;
      const name = row.querySelector('[data-bind-profile-name]').value.trim();
      const reviewer = row.querySelector('[data-bind-profile-reviewer]').value.trim();
      if (!name && rows.some((entryRow) => entryRow.dataset.bindEntryProfile === row.dataset.bindProfile && !BindlePreview.isExisting(entryFor(entryRow)))) invalidProfile = true;
      profileOverrides[row.dataset.bindProfile] = { name, reviewer };
    });
    if (invalidProfile) {
      body.querySelector('.bindle-profiles').open = true;
      toast('请填写报账人姓名', 'err'); return null;
    }
    const missingMapping = [...body.querySelectorAll('[data-bind-profile-mapping]')].find((input) =>
      !input.value && rows.some((row) => row.dataset.bindEntryProfile === input.dataset.bindProfileMapping
        && !BindlePreview.isExisting(entryFor(row)))
    );
    if (missingMapping) {
      body.querySelector('.bindle-profiles').open = true;
      toast('请选择对应的本机报账人', 'err'); return null;
    }
    const tag = tagInput.value.trim();
    const selectedBatchMode = batchMode();
    const batchId = selectedBatchMode === 'existing' ? batchSelect.value : '';
    const batchName = selectedBatchMode === 'new' ? newBatch.value.trim() : '';
    const usesGlobalBatch = rows.some((row) => row.querySelector('[data-bind-entry-batch]').value === 'default' && (!BindlePreview.isExisting(entryFor(row)) || moveExisting?.checked));
    if (usesGlobalBatch && selectedBatchMode === 'existing' && !batchId) { toast('请选择报账批次', 'err'); return null; }
    if (usesGlobalBatch && selectedBatchMode === 'new' && !batchName) { toast('请填写新批次名称', 'err'); return null; }
    const entryBatchOverrides = {};
    let invalidEntryBatch = false;
    rows.forEach((row) => {
      const entryIndex = row.dataset.bindEntryIndex;
      const entryBatch = row.querySelector('[data-bind-entry-batch]');
      const mode = entryBatch.value;
      if (mode === 'none' || mode === 'keep') {
        entryBatchOverrides[entryIndex] = { mode };
      } else if (mode.startsWith('batch:')) {
        entryBatchOverrides[entryIndex] = {
          mode: 'existing', batch_id: mode.slice('batch:'.length),
        };
      } else if (mode === 'new') {
        const individualBatchName = row.querySelector('[data-bind-entry-new-batch]').value.trim();
        if (!individualBatchName) invalidEntryBatch = true;
        entryBatchOverrides[entryIndex] = { mode: 'new', batch_name: individualBatchName };
      }
    });
    if (invalidEntryBatch) { toast('请填写单条设置的新批次名称', 'err'); return null; }
    const options = {
      profile_overrides: profileOverrides,
      tags: tag ? [tag] : [],
      batch_id: usesGlobalBatch ? batchId : '',
      batch_name: usesGlobalBatch ? batchName : '',
      move_existing_entries: usesGlobalBatch && !!moveExisting?.checked && selectedBatchMode !== 'none',
      selected_profile_ids: [...selected],
      selected_entry_indexes: [...selectedIndexes],
      entry_batch_overrides: entryBatchOverrides,
      scheme_id: sourceScheme || $('[data-import-scheme]',body)?.value || State.scheme?.id,
      profile_mappings: Object.fromEntries($$('[data-bind-profile-mapping]',body).filter(input=>input.value).map(input=>[input.dataset.bindProfileMapping,input.value])),
      role_mappings: Object.fromEntries(rows.map(row=>[row.dataset.bindEntryIndex,Object.fromEntries($$('[data-bind-role-source]',row).filter(input=>input.value).map(input=>[input.dataset.bindRoleSource,input.value]))])),
    };
    return { tampered, options };
  };

  const reviewBox = body.querySelector('#bindleReview');
  const renderReview = () => {
    const rows = selectedEntryRows();
    const counts = { new: 0, merge: 0, unchanged: 0 };
    const destinations = new Map();
    let keep = 0, intoArchived = 0;
    rows.forEach((row) => {
      counts[row.dataset.bindAction] += 1;
      const target = targetFor(row);
      if (target.mode === 'keep' || target.unchanged) keep += 1;
      else destinations.set(target.label, (destinations.get(target.label) || 0) + 1);
      if (target.archived) intoArchived += 1;
    });
    const actions = [['new', '新增'], ['merge', '已有待补充'], ['unchanged', '已有无变化']]
      .filter(([key]) => counts[key]).map(([key, label]) => `${label} ${counts[key]} 条`).join('，');
    const locations = [keep ? `保留原批次 ${keep} 条` : '', ...[...destinations].map(([label, count]) => `${label === '未进批次' ? '未进批次' : `进入「${label}」`} ${count} 条`)].filter(Boolean);
    reviewBox.innerHTML = `<div class="bindle-review-head"><b>导入前请核对</b><span>${esc(actions)}。</span></div>
      <p class="bindle-review-note">${esc(locations.join('；'))}。</p>
      ${intoArchived ? `<p class="bindle-review-warn">${intoArchived} 条在已归档批次中，导入后到「已归档」查看。</p>` : ''}`;
  };
  const startReview = () => {
    reviewing = true;
    body.classList.add('reviewing');
    reviewBox.classList.remove('hidden');
    syncBindleSelection();
    reviewBox.scrollIntoView({ block: 'start', behavior: 'smooth' });
  };
  const syncPreview = () => { renderTargets(); if (reviewing) renderReview(); };
  body.addEventListener('input', syncPreview);
  body.addEventListener('change', syncPreview);

  let m;
  m = modal({
    title: '导入绑定包',
    replace: options.replace,
    guard: () => !importing,
    wide: true,
    body,
    onClose: () => { if(!reopening&&options.onClose)options.onClose(); },
    footer: [
      mkBtn('取消', 'ghost', () => { if (!importing) m.close(); }),
      (importButton = mkBtn('导入', 'primary', async () => {
        if (importing) return;
        const collected = collectImport();
        if (!collected) return;
        if (!reviewing) { startReview(); return; }
        const { tampered, options } = collected;
        importing = true;
        const progress = taskProgress('正在导入条目和材料…');
        window.TidocUI?.setBusy(importButton, true, '正在导入…');
        try {
          const r = await Api.importBindle(path, State.currentProfileId, !!tampered, options);
          onImported?.(r);
          progress.update('正在刷新条目列表…');
          m.close();
          await loadProfiles();
          await loadBatches();
          await refreshEntries();
          await refreshTagOptions();
          toast(r.message, r.tampered && r.tampered.length ? 'err' : 'ok');
        } catch (e) { toast(e.message, 'err'); }
        finally {
          importing = false;
          progress.close();
          // 窗口还开着（失败或只是停留）时恢复按钮；已关闭就不必再动它。
          if (m.mask?.isConnected) { window.TidocUI?.setBusy(importButton, false); syncBindleSelection(); }
        }
      })),
    ],
  });
  renderTargets();
  filterEntries();
}

async function importBindleFlow(path) {
  if (!State.currentProfileId) { toast('请先创建报账人', 'err'); return; }
  const progress = taskProgress('正在检查绑定包完整性…');
  try {
    const insp = await Api.inspectBindle(path, State.scheme?.id);
    progress.close();
    await openBindleImportPreview(path, insp);
  } finally {
    progress.close();
  }
}

const BindleBatch = {
  reviewReasons(insp) {
    if (!insp) return ['无法读取绑定包'];
    const reasons = [];
    if (!insp.verified) reasons.push('完整性校验未通过');
    if (!insp.entries?.length) reasons.push('包内没有条目');
    if (Object.values(insp.profile_mapping_preview || {}).some((p) => p?.status === 'ambiguous')) {
      reasons.push('存在同名报账人，需确认对应身份');
    }
    if ((insp.entries || []).some((e) => e.role_mapping_preview?.requires_explicit_mapping?.length)) {
      reasons.push('含需手动对应的自定义材料');
    }
    if ((insp.entries || []).some((e) => e.adapter_compatibility?.missing_capabilities?.length)) {
      reasons.push('来源方案有本机未支持的规则');
    }
    if ((insp.entries || []).some((e) => e.extension_merge_preview?.skipped)) {
      reasons.push('部分附加信息无法自动合并');
    }
    return reasons;
  },
  entryKeys(entry) {
    const keys = [];
    const invoiceNo = String(entry?.invoice_no || '').trim();
    if (invoiceNo) keys.push('no:' + invoiceNo);
    for (const att of entry?.attachments || []) {
      if (att.type !== 'invoice_pdf' && att.type !== 'invoice_xml') continue;
      const digest = String(att.sha256 || '').trim();
      if (digest) keys.push('sha:' + digest);
    }
    return keys;
  },
  summarize(insp) {
    const entries = insp?.entries || [];
    let newCount = 0, mergeCount = 0, unchangedCount = 0, attachmentCount = 0, totalAmount = 0, hasAmount = false;
    const profiles = new Set();
    const declared = new Map((insp?.profiles || []).map((p) => [p.id || '__fallback__', p]));
    for (const entry of entries) {
      if (entry.import_action === 'merge') mergeCount += 1;
      else if (entry.import_action === 'unchanged') unchangedCount += 1;
      else newCount += 1;
      attachmentCount += (entry.attachments || []).length;
      const rawTotal = entry.total;
      if (rawTotal !== '' && rawTotal != null && Number.isFinite(Number(rawTotal))) {
        totalAmount += Number(rawTotal);
        hasAmount = true;
      }
      const ownerName = (BindlePreview.isExisting(entry) && entry.existing_profile?.name)
        || entry.profile_name
        || declared.get(entry.profile_id || '__fallback__')?.name
        || '';
      if (String(ownerName).trim()) profiles.add(String(ownerName).trim());
    }
    return {
      entryCount: entries.length,
      newCount,
      mergeCount,
      unchangedCount,
      attachmentCount,
      totalAmount,
      hasAmount,
      profiles: [...profiles],
      allUnchanged: entries.length > 0 && newCount === 0 && mergeCount === 0 && unchangedCount === entries.length,
    };
  },
  folderHint(path) {
    const normalized = String(path || '').replace(/\\/g, '/');
    const parts = normalized.split('/').filter(Boolean);
    if (parts.length < 2) return '';
    const parent = parts[parts.length - 2];
    const grand = parts[parts.length - 3] || '';
    if (grand === 'dropped' && /^[0-9a-f]{8}$/i.test(parent)) return '';
    if (/^[a-z]:$/i.test(parent)) return '';
    return parent;
  },
  disambiguatePaths(paths) {
    const counts = new Map();
    for (const path of paths) {
      const name = baseName(path);
      counts.set(name, (counts.get(name) || 0) + 1);
    }
    return paths.map((path) => {
      const name = baseName(path);
      return {
        name,
        dir: (counts.get(name) || 0) > 1 ? this.folderHint(path) : '',
      };
    });
  },
  analyzeRows(rows) {
    const firstByDigest = new Map();
    const seenInvoices = new Set();
    for (const row of rows) {
      row.duplicateOf = '';
      row.duplicateImported = false;
      row.overlapCount = 0;
      if (row.state === 'error' || !row.insp) continue;
      const digest = String(row.insp.archive_digest || '').trim();
      if (row.state === 'done') {
        if (digest && !firstByDigest.has(digest)) {
          firstByDigest.set(digest, { name: row.displayName || baseName(row.path), imported: true });
        }
        continue;
      }
      if (digest) {
        if (firstByDigest.has(digest)) {
          const match = firstByDigest.get(digest);
          row.duplicateOf = match.name;
          row.duplicateImported = match.imported;
          if (row.state === 'ready' || row.state === 'unchanged' || row.state === 'duplicate') {
            row.state = 'duplicate';
            if (!row.userToggled) row.checked = false;
          }
          continue;
        }
        firstByDigest.set(digest, { name: row.displayName || baseName(row.path), imported: false });
      }
      if (row.state === 'duplicate') {
        row.state = row.summary?.allUnchanged ? 'unchanged' : 'ready';
        if (!row.userToggled) row.checked = row.state === 'ready';
      }
      const entries = row.insp.entries || [];
      let overlap = 0;
      for (const entry of entries) {
        const keys = this.entryKeys(entry);
        if (keys.some((key) => seenInvoices.has(key))) overlap += 1;
      }
      row.overlapCount = overlap;
      if (row.checked && row.state !== 'review' && row.state !== 'empty') {
        for (const entry of entries) {
          for (const key of this.entryKeys(entry)) seenInvoices.add(key);
        }
      }
    }
  },
};

function openBindleBatch(initialPaths, onCloseOrOptions) {
  const opts = typeof onCloseOrOptions === 'function'
    ? { onClose: onCloseOrOptions }
    : (onCloseOrOptions || {});
  const cleanupSet = new Set(opts.cleanupPaths || []);
  const releaseAllStaged = () => {
    const list = [...cleanupSet];
    cleanupSet.clear();
    if (list.length && typeof cleanupDroppedPaths === 'function') {
      cleanupDroppedPaths(list);
    }
    opts.onClose?.();
  };
  if (!State.currentProfileId) {
    releaseAllStaged();
    toast('请先创建报账人', 'err');
    return null;
  }

  let batches = [...(State.batches || [])];
  let tags = [...(State.allTags || [])];

  const body = el('div', 'bindle-import bindle-batch');
  body.innerHTML = `
    <div class="bindle-import-bar">
      <strong id="bindleBatchHeadline">共 0 个绑定包</strong>
      <div class="bindle-import-meta" id="bindleBatchMeta"></div>
    </div>
    <div class="bindle-review hidden" id="bindleBatchReport" role="status" aria-live="polite"></div>
    <section class="bindle-import-section">
      <div class="bindle-import-options" id="bindleBatchOptions">
        <div class="form-row bindle-batch-option">
          <label>报账批次</label>
          <div class="bindle-batch-fields">
            <div class="bindle-segments" role="radiogroup" aria-label="报账批次">
              <label><input type="radio" name="bindleBatchDestMode" value="none" checked/><span>不加入</span></label>
              <label id="bindleBatchExistingSeg"><input type="radio" name="bindleBatchDestMode" value="existing"/><span>已有批次</span></label>
              <label><input type="radio" name="bindleBatchDestMode" value="new"/><span>新建批次</span></label>
            </div>
            <select id="bindleBatchSelect" class="hidden" aria-label="选择已有批次"></select>
            <input id="bindleBatchNewName" class="hidden" aria-label="新批次名称" placeholder="输入新批次名称"/>
            <span class="bindle-batch-none-hint" id="bindleBatchNoneHint">新增条目暂不进批次，已有条目保留原批次</span>
          </div>
          <label class="bindle-move-option" id="bindleBatchMoveRow" hidden title="只处理勾选包中的已有条目，包含无变化的条目"><input type="checkbox" id="bindleBatchMoveExisting"/> 已有条目也移入此批次</label>
          <p class="bindle-batch-archived-warn hidden" id="bindleBatchArchivedWarn">所选批次已归档，导入后请到「已归档」查看。</p>
        </div>
        <div class="form-row bindle-tag-option">
          <label for="bindleBatchTag">统一标签</label>
          <input id="bindleBatchTag" list="bindleBatchTagOptions" placeholder="选择或输入标签（可选）"/>
          <datalist id="bindleBatchTagOptions"></datalist>
        </div>
      </div>
    </section>
    <section class="bindle-import-section bindle-batch-section">
      <div class="bindle-selection-bar">
        <label><input type="checkbox" id="bindleBatchSelectAll"/> 全选</label>
        <span class="bindle-selection-count" id="bindleBatchSelectionCount" role="status"></span>
        <div class="bindle-entry-tools">
          <button type="button" class="link-btn" id="bindleBatchAdd">添加绑定包…</button>
          <button type="button" class="link-btn hidden" id="bindleBatchToggleDone">隐藏已导入</button>
          <button type="button" class="link-btn" id="bindleBatchClear">清空选择</button>
        </div>
      </div>
      <div class="bindle-batch-list" id="bindleBatchList">
        <div class="bindle-batch-head">
          <span></span>
          <span>绑定包 / 内容</span>
          <span class="bindle-entry-amount">金额</span>
          <span>本机情况 / 状态</span>
          <span class="bindle-batch-head-act">操作</span>
        </div>
        <div class="bindle-batch-rows" id="bindleBatchRows"></div>
        <div class="bindle-empty hidden" id="bindleBatchEmpty">暂无绑定包，可点击「添加绑定包…」或直接拖入 .tidoc 文件。</div>
      </div>
    </section>`;

  const optionsGrid = body.querySelector('#bindleBatchOptions');
  let selectedScheme = State.scheme?.id || '';
  const schemeHtml = typeof AdapterUI?.chooser === 'function' ? AdapterUI.chooser(selectedScheme) : '';
  let schemeSelect = null;
  if (schemeHtml) {
    const schemeWrap = el('div', 'bindle-scheme-option');
    schemeWrap.innerHTML = schemeHtml;
    if (schemeWrap.firstChild) {
      optionsGrid.classList.add('has-scheme');
      optionsGrid.prepend(schemeWrap);
      schemeSelect = schemeWrap.querySelector('[data-import-scheme]');
      if (schemeSelect) {
        selectedScheme = schemeSelect.value || selectedScheme;
      }
    }
  }

  const headlineEl = body.querySelector('#bindleBatchHeadline');
  const metaEl = body.querySelector('#bindleBatchMeta');
  const reportEl = body.querySelector('#bindleBatchReport');
  const batchSelect = body.querySelector('#bindleBatchSelect');
  const newBatchInput = body.querySelector('#bindleBatchNewName');
  const noneHintEl = body.querySelector('#bindleBatchNoneHint');
  const existingSeg = body.querySelector('#bindleBatchExistingSeg');
  const existingRadio = existingSeg.querySelector('input');
  const moveRow = body.querySelector('#bindleBatchMoveRow');
  const moveExistingInput = body.querySelector('#bindleBatchMoveExisting');
  const archivedWarnEl = body.querySelector('#bindleBatchArchivedWarn');
  const tagInput = body.querySelector('#bindleBatchTag');
  const tagOptionsEl = body.querySelector('#bindleBatchTagOptions');
  const selectAllInput = body.querySelector('#bindleBatchSelectAll');
  const selectionCountEl = body.querySelector('#bindleBatchSelectionCount');
  const addBtn = body.querySelector('#bindleBatchAdd');
  const toggleDoneBtn = body.querySelector('#bindleBatchToggleDone');
  const clearBtn = body.querySelector('#bindleBatchClear');
  const listEl = body.querySelector('#bindleBatchList');
  const rowsHost = body.querySelector('#bindleBatchRows');
  const emptyEl = body.querySelector('#bindleBatchEmpty');

  const rows = [];
  let closed = false;
  let importing = false;
  let previewOpen = false;
  let inspecting = false;
  let inspectToken = 0;
  let cancelRequested = false;
  let hideDone = false;
  let runProgress = null; // { current, total }
  let lastReport = null;
  // 新建批次在同一次批量导入（含后续重试）中跨多个包复用同一个新建批次 ID。
  let createdNewBatch = { name: '', id: '' };

  const footNote = el('span', 'modal-foot-note');
  const closeBtn = mkBtn('关闭', 'ghost', () => {
    if (importing) {
      cancelRequested = true;
      closeBtn.disabled = true;
      closeBtn.textContent = '正在停止…';
      return;
    }
    if (!previewOpen) dialog.close();
  });
  const runBtn = mkBtn('导入所选', 'primary', () => {
    if (allDone()) {
      dialog.close();
      return;
    }
    return importSelected();
  });

  const isBusy = () => importing || previewOpen;
  const drainPendingLaunch = () => {
    if (typeof PendingLaunchFiles !== 'undefined' && PendingLaunchFiles.length && typeof handleSecondaryLaunch === 'function') {
      setTimeout(() => handleSecondaryLaunch(), 0);
    }
  };
  const destMode = () => body.querySelector('[name="bindleBatchDestMode"]:checked')?.value || 'none';
  const selectableRow = (row) => ['ready', 'unchanged', 'duplicate', 'failed'].includes(row.state);
  const allDone = () => rows.length > 0 && rows.every((row) => row.state === 'done');

  function populateBatches(nextBatches) {
    batches = sortBindleBatches(nextBatches);
    const prevValue = batchSelect.value;
    batchSelect.innerHTML = batches.map((b) =>
      `<option value="${esc(b.id)}">${esc(bindleBatchOptionLabel(b))}</option>`
    ).join('');
    if (prevValue && batches.some((b) => b.id === prevValue)) {
      batchSelect.value = prevValue;
    } else if (batches.length) {
      batchSelect.value = batches[0].id;
    }
    const hasBatches = batches.length > 0;
    existingRadio.disabled = !hasBatches || isBusy();
    existingSeg.classList.toggle('disabled', !hasBatches);
    if (!hasBatches && destMode() === 'existing') {
      const noneRadio = body.querySelector('[name="bindleBatchDestMode"][value="none"]');
      if (noneRadio) noneRadio.checked = true;
    }
    if (typeof enhanceNativeSelects === 'function') enhanceNativeSelects(batchSelect);
    syncBatchControls();
  }

  function populateTags(nextTags) {
    tags = [...(nextTags || [])];
    tagOptionsEl.innerHTML = tags.map((tag) => `<option value="${esc(tag)}"></option>`).join('');
  }

  function syncBatchControls() {
    const mode = destMode();
    batchSelect.classList.toggle('hidden', mode !== 'existing');
    newBatchInput.classList.toggle('hidden', mode !== 'new');
    noneHintEl.classList.toggle('hidden', mode !== 'none');
    moveRow.hidden = mode === 'none';
    if (mode === 'none') moveExistingInput.checked = false;
    const selectedBatch = mode === 'existing' ? batches.find((b) => b.id === batchSelect.value) : null;
    archivedWarnEl.classList.toggle('hidden', !selectedBatch?.archived);
  }

  function syncDisambiguation() {
    const info = BindleBatch.disambiguatePaths(rows.map((r) => r.path));
    rows.forEach((row, idx) => {
      row.displayName = info[idx].name;
      row.displayDir = info[idx].dir;
    });
  }

  const STATE_META = {
    checking: { label: '检查中', cls: 'is-muted' },
    ready: { label: '可导入', cls: 'is-ready' },
    unchanged: { label: '已有·无变化', cls: 'is-muted' },
    duplicate: { label: '重复包', cls: 'is-warn' },
    review: { label: '需核对', cls: 'is-warn' },
    empty: { label: '无条目', cls: 'is-muted' },
    error: { label: '无法读取', cls: 'is-error' },
    importing: { label: '导入中', cls: 'is-working' },
    done: { label: '已导入', cls: 'is-done' },
    failed: { label: '未导入', cls: 'is-error' },
  };

  function rowSummaryText(row) {
    if (row.state === 'checking') return '正在核对完整性与本机条目…';
    if (row.state === 'error') return row.displayDir ? `位于 ${row.displayDir}` : '';
    const s = row.summary;
    if (!s) return '';
    if (!s.entryCount) return '0 条';
    const parts = [`${s.entryCount} 条`];
    if (s.profiles.length) {
      const shown = s.profiles.slice(0, 2).join('、') + (s.profiles.length > 2 ? ` 等 ${s.profiles.length} 人` : '');
      parts.push(shown);
    }
    if (s.attachmentCount) parts.push(`${s.attachmentCount} 份材料`);
    return parts.join(' · ');
  }

  function rowDetailText(row) {
    if (row.state === 'checking') return '读取中…';
    if (row.state === 'importing') return row.progressText || '正在导入…';
    if (row.state === 'done') return row.resultMessage || '已导入';
    if (row.state === 'failed') return row.errorMessage || '导入失败';
    if (row.state === 'error') return row.errorMessage || '无法读取该绑定包';
    if (row.state === 'empty') return '包内没有条目，无需导入';
    if (row.state === 'review') return (row.reviewReasons || []).join('；') || '请打开单包预览核对';
    if (row.state === 'duplicate') {
      return row.duplicateImported
        ? `与已导入的「${row.duplicateOf}」内容完全相同，已跳过`
        : `与「${row.duplicateOf}」内容完全相同，已跳过`;
    }
    if (row.state === 'unchanged') {
      const canAct = (destMode() !== 'none' && moveExistingInput.checked) || Boolean(tagInput.value.trim());
      return canAct
        ? '本机已有全部条目；勾选后可统一调整批次或添加标签'
        : '本机已包含全部条目和材料，无需重复导入';
    }
    const parts = [];
    const s = row.summary;
    if (s) {
      const diff = [
        s.newCount ? `新增 ${s.newCount} 条` : '',
        s.mergeCount ? `补充 ${s.mergeCount} 条` : '',
        s.unchangedCount ? `无变化 ${s.unchangedCount} 条` : '',
      ].filter(Boolean).join('，');
      if (diff) parts.push(diff);
    }
    if (row.overlapCount > 0) {
      parts.push(`含 ${row.overlapCount} 张与前面包重复的发票，按顺序合并`);
    }
    return parts.join('；') || '待导入';
  }

  function renderReport() {
    if (!lastReport) {
      reportEl.classList.add('hidden');
      reportEl.innerHTML = '';
      return;
    }
    reportEl.classList.remove('hidden');
    const summaryParts = [];
    if (lastReport.importedPackages) {
      const entryBits = [];
      if (lastReport.entriesNew) entryBits.push(`新增 ${lastReport.entriesNew} 条`);
      if (lastReport.entriesUpdated) entryBits.push(`补充 ${lastReport.entriesUpdated} 条`);
      summaryParts.push(`已导入 ${lastReport.importedPackages} 个包${entryBits.length ? `（${entryBits.join('、')}）` : ''}`);
    }
    const remainingReview = rows.filter((r) => r.state === 'review').length;
    const remainingFailed = rows.filter((r) => r.state === 'failed').length;
    if (remainingReview) summaryParts.push(`${remainingReview} 个需单包预览`);
    if (remainingFailed) summaryParts.push(`${remainingFailed} 个未导入`);
    if (lastReport.stopped && lastReport.remainingSelected) {
      summaryParts.push(`剩余 ${lastReport.remainingSelected} 个已暂停`);
    }
    reportEl.innerHTML = `
      <div class="bindle-review-head">
        <b>${esc(lastReport.stopped ? '已停止批量导入' : '本轮导入完成')}</b>
        <span>${esc(summaryParts.join('；') || '未执行导入')}。</span>
      </div>
      ${lastReport.intoArchived ? '<p class="bindle-review-warn">已导入到归档批次，关闭后请到「已归档」查看。</p>' : ''}`;
  }

  function renderRowDom(row) {
    const dom = row.dom;
    const busy = isBusy();
    const canCheck = selectableRow(row) && !busy && !inspecting;
    const badge = STATE_META[row.state] || STATE_META.ready;
    dom.root.hidden = hideDone && row.state === 'done';
    dom.root.className = 'bindle-batch-row'
      + (row.state === 'done' ? ' is-done' : '')
      + (row.state === 'review' ? ' is-review' : '')
      + (row.state === 'error' || row.state === 'failed' ? ' is-error' : '')
      + (row.state === 'importing' ? ' is-working' : '')
      + (!row.checked && selectableRow(row) ? ' off' : '');
    dom.check.checked = !!row.checked;
    dom.check.disabled = !canCheck;
    dom.name.textContent = row.displayName || baseName(row.path);
    dom.name.title = row.path;
    dom.dir.textContent = row.displayDir ? row.displayDir : '';
    dom.dir.hidden = !row.displayDir;
    const metaText = rowSummaryText(row);
    dom.meta.textContent = metaText;
    dom.meta.dataset.tooltipOverflow = metaText;
    dom.amount.textContent = row.summary?.hasAmount ? fmtMoney(row.summary.totalAmount) : '—';
    dom.badge.className = 'bindle-batch-badge ' + badge.cls;
    dom.badge.textContent = badge.label;
    const detailText = rowDetailText(row);
    dom.detail.textContent = detailText;
    dom.detail.dataset.tooltipOverflow = detailText;

    const showRetryCheck = row.state === 'error';
    const showPreview = !showRetryCheck && row.state !== 'done' && row.state !== 'empty';
    dom.preview.hidden = !showPreview;
    dom.preview.disabled = busy || row.state === 'checking' || row.state === 'importing';
    dom.preview.classList.toggle('is-attention', row.state === 'review');
    dom.retryCheck.hidden = !showRetryCheck;
    dom.retryCheck.disabled = busy || inspecting;
    dom.remove.hidden = busy || row.state === 'done' || row.state === 'importing';
    dom.remove.disabled = busy;
  }

  function sync() {
    if (closed) return;
    syncDisambiguation();
    BindleBatch.analyzeRows(rows);
    syncBatchControls();

    const busy = isBusy();
    const selectable = rows.filter(selectableRow);
    const selected = selectable.filter((r) => r.checked);
    const doneCount = rows.filter((r) => r.state === 'done').length;
    const reviewCount = rows.filter((r) => r.state === 'review').length;
    const errorCount = rows.filter((r) => r.state === 'error' || r.state === 'failed').length;
    const checkingCount = rows.filter((r) => r.state === 'checking').length;

    // 顶栏汇总：展示整个列表的条目、新增、待补充和总金额。
    headlineEl.textContent = `共 ${rows.length} 个绑定包`;
    if (checkingCount > 0) {
      const inspectedCount = rows.length - checkingCount;
      metaEl.innerHTML = `<span class="bindle-batch-scanning">正在检查 ${inspectedCount} / ${rows.length}…</span>`;
    } else {
      let totalEntries = 0, totalNew = 0, totalMerge = 0, totalAmount = 0, hasAmount = false;
      for (const row of rows) {
        if (!row.summary) continue;
        totalEntries += row.summary.entryCount;
        totalNew += row.summary.newCount;
        totalMerge += row.summary.mergeCount;
        if (row.summary.hasAmount) {
          totalAmount += row.summary.totalAmount;
          hasAmount = true;
        }
      }
      metaEl.innerHTML = [
        `<span><b>${totalEntries}</b> 条目</span>`,
        totalNew ? `<span><b>${totalNew}</b> 新增</span>` : '',
        totalMerge ? `<span><b>${totalMerge}</b> 待补充</span>` : '',
        hasAmount ? `<span>合计 <b>${esc(fmtMoney(totalAmount))}</b></span>` : '',
      ].filter(Boolean).join('');
    }

    rows.forEach(renderRowDom);
    emptyEl.classList.toggle('hidden', rows.length > 0);

    // 选择工具栏
    selectAllInput.disabled = busy || inspecting || !selectable.length;
    selectAllInput.checked = selectable.length > 0 && selected.length === selectable.length;
    selectAllInput.indeterminate = selected.length > 0 && selected.length < selectable.length;

    const selNew = selected.reduce((sum, r) => sum + (r.summary?.newCount || 0), 0);
    const selMerge = selected.reduce((sum, r) => sum + (r.summary?.mergeCount || 0), 0);
    const estParts = [
      selNew ? `新增 ${selNew} 条` : '',
      selMerge ? `补充 ${selMerge} 条` : '',
    ].filter(Boolean);
    selectionCountEl.textContent = !rows.length
      ? '尚未添加绑定包'
      : `已选 ${selected.length} / ${rows.length} 个包${estParts.length ? ` · 预计${estParts.join('、')}` : ''}`;

    addBtn.disabled = busy;
    clearBtn.disabled = busy || inspecting || !selected.length;
    const canToggleDone = doneCount > 0 && doneCount < rows.length;
    toggleDoneBtn.classList.toggle('hidden', !canToggleDone);
    if (!canToggleDone) hideDone = false;
    toggleDoneBtn.textContent = hideDone ? `显示已导入（${doneCount}）` : `隐藏已导入（${doneCount}）`;

    // 表单控件禁用态
    body.querySelectorAll('[name="bindleBatchDestMode"]').forEach((input) => {
      input.disabled = busy || (input.value === 'existing' && !batches.length);
    });
    batchSelect.disabled = busy;
    newBatchInput.disabled = busy;
    moveExistingInput.disabled = busy;
    tagInput.disabled = busy;
    if (schemeSelect) schemeSelect.disabled = busy || inspecting;

    // 底部提示与按钮
    renderReport();
    if (importing && runProgress) {
      footNote.textContent = cancelRequested
        ? `等待当前包完成（第 ${runProgress.current} / ${runProgress.total} 个）…`
        : `正在处理第 ${runProgress.current} / ${runProgress.total} 个绑定包…`;
    } else if (checkingCount > 0) {
      footNote.textContent = '正在检查绑定包完整性与本机条目…';
    } else if (allDone()) {
      footNote.textContent = `全部 ${doneCount} 个绑定包已导入完成`;
    } else {
      const notes = [];
      if (doneCount) notes.push(`已导入 ${doneCount} 个`);
      if (reviewCount) notes.push(`${reviewCount} 个需单包预览`);
      if (errorCount) notes.push(`${errorCount} 个异常`);
      if (!notes.length && rows.length) {
        notes.push('已有条目默认保留原批次，个别包可点「单包预览」调整');
      }
      footNote.textContent = notes.join(' · ');
    }

    if (importing) {
      closeBtn.hidden = false;
      closeBtn.disabled = cancelRequested;
      closeBtn.textContent = cancelRequested ? '正在停止…' : '停止';
      runBtn.disabled = true;
      runBtn.classList.add('is-busy');
      runBtn.textContent = runProgress
        ? `正在导入（${runProgress.current}/${runProgress.total}）…`
        : '正在导入…';
    } else if (allDone()) {
      closeBtn.hidden = true;
      runBtn.classList.remove('is-busy');
      runBtn.disabled = false;
      runBtn.textContent = '完成';
    } else {
      closeBtn.hidden = false;
      closeBtn.disabled = previewOpen;
      closeBtn.textContent = '关闭';
      runBtn.classList.remove('is-busy');
      if (checkingCount > 0) {
        runBtn.disabled = true;
        runBtn.textContent = '正在检查…';
      } else if (!selected.length) {
        runBtn.disabled = true;
        runBtn.textContent = '请选择绑定包';
      } else {
        const allFailed = selected.every((r) => r.state === 'failed');
        runBtn.disabled = previewOpen;
        runBtn.textContent = (allFailed ? '重试未导入（' : '导入所选（') + selected.length + '）';
      }
    }
  }

  function applyInspection(row, insp, { preserveChecked = false } = {}) {
    row.insp = insp;
    row.summary = BindleBatch.summarize(insp);
    row.errorMessage = '';
    const reasons = BindleBatch.reviewReasons(insp);
    row.reviewReasons = reasons;
    if (!insp.entries?.length) {
      row.state = 'empty';
      row.checked = false;
    } else if (reasons.length) {
      row.state = 'review';
      row.checked = false;
    } else if (row.summary.allUnchanged) {
      row.state = 'unchanged';
      if (!preserveChecked || !row.userToggled) row.checked = false;
    } else {
      row.state = 'ready';
      if (!preserveChecked) row.checked = !row.userToggled ? true : row.checked;
    }
  }

  async function inspectRows(targets, { preserveChecked = false } = {}) {
    const targetSet = new Set(targets || []);
    const queue = rows.filter((r) => (targetSet.has(r) || r.state === 'checking') && r.state !== 'done' && r.state !== 'importing');
    if (!queue.length) return;
    const token = ++inspectToken;
    inspecting = true;
    for (const row of queue) {
      if (!preserveChecked) row.state = 'checking';
    }
    sync();
    try {
      for (const row of queue) {
        if (closed || token !== inspectToken || !rows.includes(row) || row.state === 'done') return;
        try {
          const insp = await Api.inspectBindle(row.path, selectedScheme || State.scheme?.id);
          if (closed || token !== inspectToken || !rows.includes(row) || row.state === 'done') return;
          applyInspection(row, insp, { preserveChecked });
        } catch (e) {
          if (closed || token !== inspectToken || !rows.includes(row)) return;
          row.insp = null;
          row.summary = null;
          row.reviewReasons = [];
          row.state = 'error';
          row.checked = false;
          row.errorMessage = e.message || '无法读取绑定包';
        }
        sync();
      }
    } finally {
      if (token === inspectToken) {
        inspecting = false;
        sync();
      }
    }
  }

  async function refreshLookups() {
    try {
      const [batchRes, tagRes] = await Promise.all([
        typeof Api.listBatches === 'function' ? Api.listBatches(true) : Promise.resolve(batches),
        typeof Api.listTags === 'function' ? Api.listTags() : Promise.resolve(tags),
      ]);
      if (closed) return;
      const list = Array.isArray(batchRes) ? batchRes : (batchRes?.batches || batches);
      populateBatches(list);
      if (Array.isArray(tagRes)) populateTags(tagRes);
      sync();
    } catch (_) { /* 保留已有批次和标签缓存 */ }
  }

  function currentPreviewDefaults() {
    const mode = destMode();
    return {
      batchMode: mode,
      batchId: mode === 'existing' ? batchSelect.value : '',
      batchName: mode === 'new' ? newBatchInput.value.trim() : '',
      moveExisting: mode !== 'none' && moveExistingInput.checked,
      tag: tagInput.value.trim(),
    };
  }

  function createRow(path) {
    const root = el('div', 'bindle-batch-row');
    const check = el('input');
    check.type = 'checkbox';
    check.setAttribute('aria-label', `选择绑定包 ${baseName(path)}`);
    const fileCol = el('div', 'bindle-batch-file');
    const titleLine = el('div', 'bindle-batch-title-line');
    const name = el('strong', 'bindle-batch-name');
    const dir = el('small', 'bindle-batch-dir');
    dir.hidden = true;
    titleLine.append(name, dir);
    const meta = el('small', 'bindle-batch-meta');
    fileCol.append(titleLine, meta);
    const amount = el('span', 'bindle-entry-amount bindle-batch-amount', '—');
    const statusCol = el('div', 'bindle-batch-status');
    const badge = el('span', 'bindle-batch-badge is-muted', '检查中');
    const detail = el('small', 'bindle-batch-detail', '读取中…');
    statusCol.append(badge, detail);
    const actionsCol = el('div', 'bindle-batch-actions');
    const row = {
      path,
      displayName: baseName(path),
      displayDir: '',
      state: 'checking',
      checked: false,
      userToggled: false,
      insp: null,
      summary: null,
      reviewReasons: [],
      duplicateOf: '',
      overlapCount: 0,
      progressText: '',
      resultMessage: '',
      errorMessage: '',
      dom: null,
    };
    const preview = mkBtn('单包预览', 'small ghost', async () => {
      if (isBusy() || row.state === 'done' || row.state === 'checking') return;
      previewOpen = true;
      sync();
      let importedInPreview = false;
      try {
        const insp = await Api.inspectBindle(row.path, selectedScheme || State.scheme?.id);
        if (closed) return;
        await openBindleImportPreview(row.path, insp, {
          defaults: currentPreviewDefaults(),
          onImported: (result) => {
            importedInPreview = true;
            row.state = 'done';
            row.checked = false;
            row.resultMessage = result.message || '已导入';
            if (lastReport) {
              lastReport.importedPackages += 1;
              lastReport.entriesNew += Number(result.imported || 0);
              lastReport.entriesUpdated += Number(result.updated || 0);
            }
          },
          onClose: () => {
            previewOpen = false;
            if (closed) return;
            sync();
            if (importedInPreview) {
              refreshLookups();
              const remaining = rows.filter((r) => r.state !== 'done' && r.state !== 'failed' && r.state !== 'error');
              if (remaining.length) inspectRows(remaining, { preserveChecked: true });
            }
            drainPendingLaunch();
          },
        });
      } catch (e) {
        previewOpen = false;
        row.state = 'error';
        row.checked = false;
        row.errorMessage = e.message || '无法打开预览';
        sync();
      }
    });
    const retryCheck = mkBtn('重试检查', 'small ghost', () => {
      if (isBusy() || inspecting) return;
      inspectRows([row]);
    });
    retryCheck.hidden = true;
    const remove = el('button', 'bindle-batch-remove');
    remove.type = 'button';
    remove.setAttribute('aria-label', `移出 ${baseName(path)}`);
    remove.title = '从列表移除';
    remove.innerHTML = window.TidocIcons ? window.TidocIcons.icon('close', 13) : '×';
    remove.onclick = () => {
      if (isBusy() || row.state === 'done') return;
      const idx = rows.indexOf(row);
      if (idx >= 0) rows.splice(idx, 1);
      root.remove();
      if (cleanupSet.has(row.path) && !rows.some((r) => r.path === row.path)) {
        cleanupSet.delete(row.path);
        if (typeof cleanupDroppedPaths === 'function') cleanupDroppedPaths([row.path]);
      }
      sync();
    };
    check.onchange = () => {
      row.checked = check.checked;
      row.userToggled = true;
      sync();
    };
    actionsCol.append(preview, retryCheck, remove);
    root.append(check, fileCol, amount, statusCol, actionsCol);
    row.dom = { root, check, name, dir, meta, amount, badge, detail, preview, retryCheck, remove };
    rowsHost.append(root);
    return row;
  }

  function addPaths(paths, stagedPaths = []) {
    if (closed || isBusy()) return 0;
    for (const staged of stagedPaths || []) cleanupSet.add(staged);
    const existing = new Set(rows.map((r) => r.path));
    const added = [];
    let skipped = 0;
    for (const raw of paths || []) {
      const path = String(raw || '').trim();
      if (!path) continue;
      if (existing.has(path)) {
        skipped += 1;
        continue;
      }
      existing.add(path);
      const row = createRow(path);
      rows.push(row);
      added.push(row);
    }
    if (skipped && !added.length) {
      toast('所选绑定包已在列表中', 'ok');
    } else if (skipped) {
      toast(`已添加 ${added.length} 个绑定包，跳过 ${skipped} 个重复路径`, 'ok');
    }
    sync();
    if (added.length) inspectRows(added);
    return added.length;
  }

  async function importSelected() {
    if (isBusy() || inspecting) return;
    const queue = rows.filter((row) => selectableRow(row) && row.checked);
    if (!queue.length) return;

    const mode = destMode();
    const chosenBatchId = mode === 'existing' ? batchSelect.value : '';
    const chosenBatchName = mode === 'new' ? newBatchInput.value.trim() : '';
    if (mode === 'existing' && !chosenBatchId) {
      toast('请选择报账批次', 'err');
      batchSelect.focus();
      return;
    }
    if (mode === 'new' && !chosenBatchName) {
      toast('请填写新批次名称', 'err');
      newBatchInput.focus();
      return;
    }
    const moveExisting = mode !== 'none' && moveExistingInput.checked;
    const tag = tagInput.value.trim();
    const scheme = selectedScheme || State.scheme?.id || '';
    const selectedBatchObj = mode === 'existing' ? batches.find((b) => b.id === chosenBatchId) : null;

    importing = true;
    cancelRequested = false;
    lastReport = null;
    runProgress = { current: 0, total: queue.length };
    let importedPackages = 0;
    let entriesNew = 0;
    let entriesUpdated = 0;
    let intoArchived = false;
    let processedCount = 0;
    sync();

    try {
      for (let i = 0; i < queue.length; i += 1) {
        if (cancelRequested || closed) break;
        const row = queue[i];
        runProgress = { current: i + 1, total: queue.length };
        row.state = 'importing';
        row.progressText = '正在核对…';
        sync();
        try {
          const insp = await Api.inspectBindle(row.path, scheme);
          if (closed) break;
          row.insp = insp;
          row.summary = BindleBatch.summarize(insp);
          const reasons = BindleBatch.reviewReasons(insp);
          row.reviewReasons = reasons;
          if (!insp.entries?.length) {
            row.state = 'empty';
            row.checked = false;
            processedCount += 1;
            sync();
            continue;
          }
          if (reasons.length) {
            row.state = 'review';
            row.checked = false;
            processedCount += 1;
            sync();
            continue;
          }
          row.progressText = '正在写入条目和材料…';
          sync();

          const importOptions = {
            scheme_id: scheme,
            tags: tag ? [tag] : [],
            move_existing_entries: moveExisting,
          };
          if (mode === 'existing') {
            importOptions.batch_id = chosenBatchId;
          } else if (mode === 'new') {
            if (createdNewBatch.id && createdNewBatch.name === chosenBatchName) {
              importOptions.batch_id = createdNewBatch.id;
            } else {
              importOptions.batch_name = chosenBatchName;
            }
          }

          const result = await Api.importBindle(row.path, State.currentProfileId, false, importOptions);
          if (result.tampered?.length) {
            row.state = 'review';
            row.reviewReasons = ['完整性校验未通过'];
            row.checked = false;
            processedCount += 1;
            sync();
            continue;
          }
          if (mode === 'new' && result.batch_id && createdNewBatch.name !== chosenBatchName) {
            createdNewBatch = { name: chosenBatchName, id: result.batch_id };
          }
          row.state = 'done';
          row.checked = false;
          row.resultMessage = result.message || '已导入';
          importedPackages += 1;
          entriesNew += Number(result.imported || 0);
          entriesUpdated += Number(result.updated || 0);
          if (selectedBatchObj?.archived && (result.batch_id || result.imported || (moveExisting && (result.updated || result.skipped?.length)))) {
            intoArchived = true;
          }
          processedCount += 1;
          sync();
        } catch (e) {
          row.state = 'failed';
          row.checked = true;
          row.errorMessage = e.message || '导入失败';
          processedCount += 1;
          sync();
        }
      }
      lastReport = {
        stopped: cancelRequested,
        importedPackages,
        entriesNew,
        entriesUpdated,
        intoArchived,
        remainingSelected: Math.max(0, queue.length - processedCount),
      };
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      importing = false;
      cancelRequested = false;
      runProgress = null;
      if (importedPackages > 0) {
        try {
          await loadProfiles();
          await loadBatches();
          await refreshEntries();
          await refreshTagOptions();
        } catch (e) { toast(e.message, 'err'); }
      }
      if (!closed) {
        if (importedPackages > 0) {
          await refreshLookups();
          const remaining = rows.filter((r) => ['ready', 'unchanged', 'duplicate', 'review'].includes(r.state));
          if (remaining.length) await inspectRows(remaining, { preserveChecked: true });
        }
        sync();
        drainPendingLaunch();
      }
    }
  }

  // 事件绑定
  body.querySelectorAll('[name="bindleBatchDestMode"]').forEach((input) => {
    input.onchange = () => {
      syncBatchControls();
      if (destMode() === 'existing' && typeof enhanceNativeSelects === 'function') enhanceNativeSelects(batchSelect);
      if (destMode() === 'new') newBatchInput.focus();
      sync();
    };
  });
  batchSelect.onchange = () => sync();
  newBatchInput.oninput = () => {
    if (newBatchInput.value.trim() !== createdNewBatch.name) {
      createdNewBatch = { name: '', id: '' };
    }
  };
  moveExistingInput.onchange = () => sync();
  tagInput.oninput = () => sync();
  if (schemeSelect) {
    schemeSelect.onchange = () => {
      selectedScheme = schemeSelect.value || '';
      const targets = rows.filter((r) => r.state !== 'done' && r.state !== 'importing');
      inspectRows(targets);
    };
  }
  selectAllInput.onchange = () => {
    for (const row of rows) {
      if (!selectableRow(row)) continue;
      row.checked = selectAllInput.checked;
      row.userToggled = true;
    }
    sync();
  };
  clearBtn.onclick = () => {
    for (const row of rows) {
      if (!selectableRow(row)) continue;
      row.checked = false;
      row.userToggled = true;
    }
    sync();
  };
  toggleDoneBtn.onclick = () => {
    hideDone = !hideDone;
    sync();
  };
  addBtn.onclick = async () => {
    if (isBusy()) return;
    try {
      const res = await Api.pickFiles(true, ['绑定包 (*.tidoc)']);
      if (res?.paths?.length) addPaths(res.paths);
    } catch (e) { toast(e.message, 'err'); }
  };

  // 支持直接拖入更多 .tidoc 到已打开的批量导入窗口
  const hasDraggedFiles = (dt) => dt && Array.from(dt.types || []).includes('Files');
  body.addEventListener('dragover', (ev) => {
    if (!hasDraggedFiles(ev.dataTransfer)) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (!isBusy()) listEl.classList.add('is-dragover');
  });
  body.addEventListener('dragleave', (ev) => {
    if (!body.contains(ev.relatedTarget)) listEl.classList.remove('is-dragover');
  });
  body.addEventListener('drop', async (ev) => {
    if (!ev.dataTransfer?.files?.length) return;
    ev.preventDefault();
    ev.stopPropagation();
    listEl.classList.remove('is-dragover');
    if (isBusy()) {
      toast('正在处理中，请稍后再添加绑定包', 'err');
      return;
    }
    const files = [...ev.dataTransfer.files];
    const progress = taskProgress('正在读取拖入的绑定包…');
    let staged = [];
    try {
      staged = await droppedFilesToPaths(files, { dropped: true });
      const valid = staged.filter((p) => String(p || '').toLowerCase().endsWith('.tidoc'));
      if (valid.length !== staged.length) {
        await cleanupDroppedPaths(staged);
        toast('批量导入窗口仅支持添加 .tidoc 绑定包', 'err');
        return;
      }
      addPaths(valid, valid);
    } catch (e) {
      if (staged.length) await cleanupDroppedPaths(staged);
      toast(e.message || '读取拖入文件失败', 'err');
    } finally {
      progress.close();
    }
  });

  populateBatches(batches);
  populateTags(tags);
  for (const path of new Set((initialPaths || []).filter(Boolean))) {
    rows.push(createRow(path));
  }
  sync();

  const dialog = modal({
    title: '批量导入绑定包',
    key: 'bindle-batch',
    wide: true,
    body,
    footer: [footNote, closeBtn, runBtn],
    guard: () => !isBusy(),
    onClose: () => {
      closed = true;
      inspectToken += 1;
      if (window.ActiveBindleBatch?.mask === dialog.mask) {
        window.ActiveBindleBatch = null;
      }
      releaseAllStaged();
    },
  });
  window.ActiveBindleBatch = { mask: dialog.mask, isBusy, addPaths };
  refreshLookups();
  inspectRows(rows);
  return dialog;
}

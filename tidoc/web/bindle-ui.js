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
  const archivedBatches = (batches || []).filter((batch) => batch.archived);
  const targetBatches = [...activeBatches, ...archivedBatches];
  const batchOptionLabel = (batch) => `${batch.name} · ${batch.stats?.count || 0} 条${batch.archived ? ' · 已归档' : ''}`;
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
    `<option value="batch:${esc(batch.id)}">${esc(batchOptionLabel(batch))}</option>`
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
    `<option value="${esc(batch.id)}">${esc(batchOptionLabel(batch))}</option>`
  ).join('');
  const defaultBatchTarget = targetBatches[0]?.id || '';
  const defaultBatchMode = activeBatches.length ? 'existing' : 'new';
  const suggestedBatchName = baseName(path).replace(/\.tidoc$/i, '').trim();
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
              <label><input type="radio" name="bindleBatchMode" value="none"/><span>不加入</span></label>
              <label class="${targetBatches.length ? '' : 'disabled'}"><input type="radio" name="bindleBatchMode" value="existing" ${defaultBatchMode === 'existing' ? 'checked' : ''} ${targetBatches.length ? '' : 'disabled'}/><span>已有批次</span></label>
              <label><input type="radio" name="bindleBatchMode" value="new" ${defaultBatchMode === 'new' ? 'checked' : ''}/><span>新建批次</span></label>
            </div>
            <select id="bindleBatchSelect" class="${defaultBatchMode === 'existing' ? '' : 'hidden'}" aria-label="选择已有批次">${existingBatchOptions}</select>
            <input id="bindleNewBatch" class="${defaultBatchMode === 'new' ? '' : 'hidden'}" aria-label="新批次名称" placeholder="输入新批次名称" value="${esc(suggestedBatchName)}"/>
          </div>
          ${entries.some(BindlePreview.isExisting) ? '<label class="bindle-move-option" title="只处理本次勾选的条目，包含无变化的条目；单条设置优先"><input type="checkbox" id="bindleMoveExisting"/> 已有条目也移入此批次</label>' : ''}
        </div>
        <div class="form-row bindle-tag-option">
          <label for="bindleTagInput">统一标签</label>
          <input id="bindleTagInput" list="bindleTagOptions" placeholder="选择或输入标签（可选）"/>
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
  let reopening=false;
  const schemeSelector = el('div', 'bindle-scheme-option'); schemeSelector.innerHTML = AdapterUI.chooser(sourceScheme || State.scheme?.id);
  if (schemeSelector.firstChild) {
    body.querySelector('.bindle-import-options').classList.add('has-scheme');
    body.querySelector('.bindle-import-options').prepend(schemeSelector);
    $('[data-import-scheme]',schemeSelector).onchange = async event => {
      try { const updated=await Api.inspectBindle(path,event.target.value); reopening=true; m.close(); await openBindleImportPreview(path,updated,options); } catch(e) { reopening=false; toast(e.message,'err'); }
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
  batchSelect.value = defaultBatchTarget;
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
      entryRowElements.forEach((otherRow) => {
        const expanded = otherRow === row && opening;
        otherRow.querySelector('[data-bind-entry-override]').classList.toggle('hidden', !expanded);
        otherRow.classList.toggle('editing-bindle-override', expanded);
        const button = otherRow.querySelector('[data-bind-entry-adjust]');
        button.textContent = expanded ? '收起' : '设置';
        button.setAttribute('aria-expanded', String(expanded));
      });
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
    wide: true,
    body,
    onClose: () => { if(!reopening&&options.onClose)options.onClose(); },
    footer: [
      mkBtn('取消', 'ghost', () => m.close()),
      (importButton = mkBtn('导入', 'primary', async () => {
        const collected = collectImport();
        if (!collected) return;
        if (!reviewing) { startReview(); return; }
        const { tampered, options } = collected;
        const progress = taskProgress('正在导入条目和材料…');
        try {
          const r = await Api.importBindle(path, State.currentProfileId, !!tampered, options);
          progress.update('正在刷新条目列表…');
          m.close();
          await loadProfiles();
          await loadBatches();
          await refreshEntries();
          await refreshTagOptions();
          toast(r.message, r.tampered && r.tampered.length ? 'err' : 'ok');
        } catch (e) { toast(e.message, 'err'); }
        finally { progress.close(); }
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

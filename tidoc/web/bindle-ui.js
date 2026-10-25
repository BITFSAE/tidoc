/* bindle-ui.js — shared operator workflow; loaded before app.js. */
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
  const currentIdentity = State.profileById[State.currentProfileId];
  const matchingProfiles = packageProfiles.filter((profile) => (
    currentIdentity
    && profile.name === currentIdentity.name
    && profile.reviewer === currentIdentity.reviewer
  ));
  const initiallySelected = new Set(
    (matchingProfiles.length ? matchingProfiles : packageProfiles).map((profile) => profile.sourceId)
  );
  const activeBatches = (batches || []).filter((batch) => !batch.archived);
  // 已归档批次也可以作为目标（例如补交材料），排在在办批次之后并标明，默认仍选在办批次。
  const archivedBatches = (batches || []).filter((batch) => batch.archived);
  const targetBatches = [...activeBatches, ...archivedBatches];
  const batchOptionLabel = (batch) => `${batch.name} · ${batch.stats?.count || 0} 条${batch.archived ? ' · 已归档' : ''}`;
  const attachmentCount = entries.reduce((sum, entry) => sum + (entry.attachments || []).length, 0);
  const body = el('div', 'bindle-import');
  const profileRows = packageProfiles.map((profile) => `
    <div class="bindle-profile-row${initiallySelected.has(profile.sourceId) ? '' : ' off'}" data-bind-profile="${esc(profile.sourceId)}">
      <input type="checkbox" data-bind-profile-enabled aria-label="导入 ${esc(profile.name || '该报账人')}" ${initiallySelected.has(profile.sourceId) ? 'checked' : ''}/>
      <input data-bind-profile-name aria-label="报账人" value="${esc(profile.name || '')}" placeholder="填写报账人"/>
      <input data-bind-profile-reviewer aria-label="审核人" value="${esc(profile.reviewer || '')}" placeholder="填写审核人"/>
      <span>${counts.get(profile.sourceId) || 0} 条</span>
    </div>`).join('');
  const perEntryBatchOptions = targetBatches.map((batch) =>
    `<option value="batch:${esc(batch.id)}">${esc(batchOptionLabel(batch))}</option>`
  ).join('');
  const entryRows = entries.map((entry, entryIndex) => {
    const profile = packageProfiles.find((item) => item.sourceId === (entry.profile_id || '__fallback__'));
    const isExisting = entry.import_action === 'merge' || entry.import_action === 'unchanged';
    const action = entry.import_action === 'merge' ? '补充' : isExisting ? '已有' : '新增';
    const actionDetail = entry.import_action === 'merge'
      ? (entry.merge_preview?.labels || []).join('、')
      : entry.import_action === 'unchanged'
        ? '包内材料和信息已齐全'
        : `包内材料 ${(entry.attachments || []).length} 份`;
    const existingBatchNames = (entry.existing_batches || []).map((batch) => batch.name).filter(Boolean);
    const currentLocation = isExisting
      ? `所在批次 · ${existingBatchNames.length ? existingBatchNames.join('、') : '未进批次'}`
      : '本机无此条目';
    const ownerText = `${profile?.name || entry.profile_name || '未填写'} · ${profile?.reviewer || entry.reviewer || '未填写'}`;
    return `<div class="bindle-entry-row${initiallySelected.has(entry.profile_id || '__fallback__') ? '' : ' off'}" data-bind-entry-profile="${esc(entry.profile_id || '__fallback__')}" data-bind-entry-index="${entryIndex}">
      <input type="checkbox" data-bind-entry-enabled aria-label="导入发票 ${esc(entry.invoice_no || entryIndex + 1)}" ${initiallySelected.has(entry.profile_id || '__fallback__') ? 'checked' : ''}/>
      <span class="mono">${esc(entry.invoice_no || '无发票号')}</span>
      <span class="bindle-entry-seller" data-tooltip-overflow="${esc(entry.seller || '')}">${esc(entry.seller || '未识别销售方')}</span>
      <span class="bindle-entry-amount">${fmtMoney(entry.total)}</span>
      <span class="bindle-entry-owner" data-tooltip-overflow="${esc(ownerText)}">${esc(ownerText)}</span>
      <div class="bindle-entry-result">
        <div class="bindle-entry-line"><span class="bindle-entry-action ${esc(entry.import_action || 'new')}">${esc(action)}</span><small data-tooltip-overflow="${esc(actionDetail)}">${esc(actionDetail)}</small><button type="button" class="bindle-entry-adjust" data-bind-entry-adjust aria-expanded="false" aria-controls="bindleEntryOverride${entryIndex}">单独设置</button></div>
        <small class="bindle-entry-location" data-tooltip-overflow="${esc(currentLocation)}">${esc(currentLocation)}</small>
        <small class="bindle-entry-target-summary"></small>
      </div>
      <div class="bindle-entry-override hidden" id="bindleEntryOverride${entryIndex}" data-bind-entry-override>
        <span>本条导入到</span>
        <select data-bind-entry-batch aria-label="设置该条目的目标批次">
          <option value="default">按上方统一设置</option>
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
  const defaultBatchMode = activeBatches.length ? 'existing' : 'new';
  const suggestedBatchName = baseName(path).replace(/\.tidoc$/i, '').trim();
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
    <div class="bindle-review hidden" id="bindleReview" role="status" aria-live="polite"></div>
    <section class="bindle-import-section" data-bindle-review-target>
      <div class="bindle-section-head"><h3>选择报账人</h3><label><input type="checkbox" id="bindleSelectAll" ${initiallySelected.size === packageProfiles.length ? 'checked' : ''}/> 全选</label></div>
      <div class="bindle-profile-list">
        <div class="bindle-profile-labels"><span></span><span>报账人</span><span>审核人</span><span>条目</span></div>
        ${profileRows}
      </div>
    </section>
    <section class="bindle-import-section" data-bindle-review-target>
      <h3>导入设置</h3>
      <div class="bindle-import-options">
        <div class="form-row bindle-batch-option">
          <label>报账批次</label>
          <div class="bindle-segments" role="radiogroup" aria-label="报账批次">
            <label><input type="radio" name="bindleBatchMode" value="none"/><span>不加入</span></label>
            <label class="${targetBatches.length ? '' : 'disabled'}"><input type="radio" name="bindleBatchMode" value="existing" ${defaultBatchMode === 'existing' ? 'checked' : ''} ${targetBatches.length ? '' : 'disabled'}/><span>已有批次</span></label>
            <label><input type="radio" name="bindleBatchMode" value="new" ${defaultBatchMode === 'new' ? 'checked' : ''}/><span>新建批次</span></label>
          </div>
          <select id="bindleBatchSelect" class="${defaultBatchMode === 'existing' ? '' : 'hidden'}" aria-label="选择已有批次">${existingBatchOptions}</select>
          <input id="bindleNewBatch" class="${defaultBatchMode === 'new' ? '' : 'hidden'}" aria-label="新批次名称" placeholder="输入批次名称" value="${esc(suggestedBatchName)}"/>
        </div>
        <div class="form-row">
          <label for="bindleTagInput">统一标签 <span class="optional">可选</span></label>
          <input id="bindleTagInput" list="bindleTagOptions" placeholder="选择或输入标签"/>
          <datalist id="bindleTagOptions">${existingTagOptions}</datalist>
        </div>
      </div>
    </section>
    <section class="bindle-import-section bindle-entry-section">
      <div class="bindle-section-head">
        <h3>条目 <span class="bindle-selection-count" id="bindleSelectionCount"></span></h3>
        <div class="bindle-entry-tools">
          <label><input type="checkbox" id="bindleEntrySelectAll"/> 全选条目</label>
          <button type="button" class="link-btn" id="bindleCustomizeEntries" aria-pressed="false">逐条调整</button>
        </div>
      </div>
      <div class="bindle-entry-list">
        ${entryRows ? '<div class="bindle-entry-head"><span></span><span>发票号</span><span>销售方</span><span class="bindle-entry-amount">金额</span><span>报账人 · 审核人</span><span>本机情况</span></div>' + entryRows : '<div class="bindle-empty">包内没有条目</div>'}
      </div>
    </section>`;

  const identities = insp.profile_mapping_preview || {};
  for (const row of body.querySelectorAll('[data-bind-profile]')) {
    const identity = identities[row.dataset.bindProfile];
    if (!identity || identity.status !== 'ambiguous') continue;
    const select = el('select'); select.dataset.bindProfileMapping = row.dataset.bindProfile;
    select.setAttribute('aria-label','选择同名报账人'); select.innerHTML = '<option value="">请选择本机同名报账人</option>' + identity.candidates.map(p=>`<option value="${esc(p.id)}">${esc(p.name)} · ${esc(p.reviewer||'无审核人')}</option>`).join(''); row.append(select);
  }
  const sourceScheme = insp.target_scheme_id || null;
  let reopening=false;
  const schemeSelector = el('div'); schemeSelector.innerHTML = AdapterUI.chooser(sourceScheme || State.scheme?.id);
  if (schemeSelector.firstChild) {
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
  const batchModes = [...body.querySelectorAll('[name="bindleBatchMode"]')];
  batchModes.forEach((input) => {
    input.onchange = () => {
      batchSelect.classList.toggle('hidden', input.value !== 'existing');
      newBatch.classList.toggle('hidden', input.value !== 'new');
      if (input.checked && input.value === 'existing') enhanceNativeSelects(batchSelect);
      if (input.checked && input.value === 'new') newBatch.focus();
    };
  });
  const profileRowElements = [...body.querySelectorAll('[data-bind-profile]')];
  const entryRowElements = [...body.querySelectorAll('[data-bind-entry-index]')];
  profileRowElements.forEach((row) => {
    const enabledInput = row.querySelector('[data-bind-profile-enabled]');
    const nameInput = row.querySelector('[data-bind-profile-name]');
    const reviewerInput = row.querySelector('[data-bind-profile-reviewer]');
    const syncOwner = () => {
      body.querySelectorAll('[data-bind-entry-profile]').forEach((entryRow) => {
        if (entryRow.dataset.bindEntryProfile !== row.dataset.bindProfile) return;
        const owner = entryRow.querySelector('.bindle-entry-owner');
        owner.textContent = `${nameInput.value.trim() || '未填写'} · ${reviewerInput.value.trim() || '未填写'}`;
        owner.dataset.tooltipOverflow = owner.textContent;
      });
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

  const selectedEntryRows = () => entryRowElements.filter(
    (row) => row.querySelector('[data-bind-entry-enabled]').checked
  );
  const selectedEntryIndexes = () => new Set(
    selectedEntryRows().map((row) => Number(row.dataset.bindEntryIndex))
  );
  const selectedSourceIds = () => new Set(
    selectedEntryRows().map((row) => row.dataset.bindEntryProfile)
  );
  const selectedEntryCount = () => selectedEntryRows().length;
  let importButton;
  // 第一次点导入只进入核对：框出报账人和导入设置，列出将发生的变化，再点一次才真正导入。
  let reviewing = false;
  const syncBindleSelection = () => {
    const selectedRows = selectedEntryRows();
    const selectedIndexes = new Set(selectedRows.map((row) => row.dataset.bindEntryIndex));
    entryRowElements.forEach((row) => {
      row.classList.toggle('off', !selectedIndexes.has(row.dataset.bindEntryIndex));
    });
    profileRowElements.forEach((row) => {
      const matchingRows = entryRowElements.filter(
        (entryRow) => entryRow.dataset.bindEntryProfile === row.dataset.bindProfile
      );
      const selectedCount = matchingRows.filter(
        (entryRow) => entryRow.querySelector('[data-bind-entry-enabled]').checked
      ).length;
      const enabledInput = row.querySelector('[data-bind-profile-enabled]');
      enabledInput.checked = selectedCount > 0 && selectedCount === matchingRows.length;
      enabledInput.indeterminate = selectedCount > 0 && selectedCount < matchingRows.length;
      row.classList.toggle('off', selectedCount === 0);
      row.querySelector('[data-bind-profile-name]').disabled = selectedCount === 0;
      row.querySelector('[data-bind-profile-reviewer]').disabled = selectedCount === 0;
    });
    const all = body.querySelector('#bindleSelectAll');
    const selectedProfiles = profileRowElements.filter(
      (row) => row.querySelector('[data-bind-profile-enabled]').checked
    ).length;
    const partialProfiles = profileRowElements.some(
      (row) => row.querySelector('[data-bind-profile-enabled]').indeterminate
    );
    all.checked = selectedProfiles === packageProfiles.length;
    all.indeterminate = partialProfiles || (selectedProfiles > 0 && selectedProfiles < packageProfiles.length);
    const entryAll = body.querySelector('#bindleEntrySelectAll');
    entryAll.checked = selectedRows.length > 0 && selectedRows.length === entryRowElements.length;
    entryAll.indeterminate = selectedRows.length > 0 && selectedRows.length < entryRowElements.length;
    body.querySelector('#bindleSelectionCount').textContent = `· 已选 ${selectedRows.length} / ${entries.length}`;
    if (importButton) {
      importButton.textContent = !selectedRows.length ? '请选择条目'
        : reviewing ? `确认导入 ${selectedRows.length} 条` : `导入 ${selectedRows.length} 条`;
      importButton.disabled = !selectedRows.length;
    }
    if (reviewing) renderReview();
  };
  body.querySelector('#bindleSelectAll').onchange = (ev) => {
    entryRowElements.forEach((row) => {
      row.querySelector('[data-bind-entry-enabled]').checked = ev.target.checked;
    });
    syncBindleSelection();
  };
  body.querySelector('#bindleEntrySelectAll').onchange = (ev) => {
    entryRowElements.forEach((row) => {
      row.querySelector('[data-bind-entry-enabled]').checked = ev.target.checked;
    });
    syncBindleSelection();
  };
  entryRowElements.forEach((row) => {
    row.querySelector('[data-bind-entry-enabled]').onchange = syncBindleSelection;
    const adjustButton = row.querySelector('[data-bind-entry-adjust]');
    const editor = row.querySelector('[data-bind-entry-override]');
    const select = row.querySelector('[data-bind-entry-batch]');
    const newBatchInput = row.querySelector('[data-bind-entry-new-batch]');
    const summary = row.querySelector('.bindle-entry-target-summary');
    const syncOverride = () => {
      const isNew = select.value === 'new';
      newBatchInput.classList.toggle('hidden', !isNew);
      let label = '';
      if (select.value === 'none') label = '目标 · 未进批次';
      else if (select.value.startsWith('batch:')) label = `目标 · ${select.selectedOptions[0]?.textContent || '已有批次'}`;
      else if (isNew) label = `目标 · ${newBatchInput.value.trim() || '待填写新批次'}`;
      summary.textContent = label;
      row.classList.toggle('has-bindle-override', !!label);
    };
    adjustButton.onclick = () => {
      const opening = editor.classList.contains('hidden');
      entryRowElements.forEach((otherRow) => {
        if (otherRow === row) return;
        otherRow.querySelector('[data-bind-entry-override]').classList.add('hidden');
        otherRow.classList.remove('editing-bindle-override');
        const otherButton = otherRow.querySelector('[data-bind-entry-adjust]');
        otherButton.textContent = '单独设置';
        otherButton.setAttribute('aria-expanded', 'false');
      });
      editor.classList.toggle('hidden', !opening);
      row.classList.toggle('editing-bindle-override', opening);
      adjustButton.textContent = opening ? '收起' : '单独设置';
      adjustButton.setAttribute('aria-expanded', String(opening));
      if (opening) enhanceNativeSelects(select);
    };
    select.onchange = () => {
      syncOverride();
      if (select.value === 'new') newBatchInput.focus();
    };
    newBatchInput.oninput = syncOverride;
    row.querySelector('[data-bind-entry-reset]').onclick = () => {
      select.value = 'default';
      newBatchInput.value = '';
      syncOverride();
    };
    syncOverride();
  });
  const entrySection = body.querySelector('.bindle-entry-section');
  const customizeEntries = body.querySelector('#bindleCustomizeEntries');
  customizeEntries.onclick = () => {
    const customizing = !entrySection.classList.contains('customizing');
    entrySection.classList.toggle('customizing', customizing);
    customizeEntries.setAttribute('aria-pressed', String(customizing));
    customizeEntries.textContent = customizing ? '完成调整' : '逐条调整';
    if (!customizing) {
      entryRowElements.forEach((row) => {
        row.querySelector('[data-bind-entry-override]').classList.add('hidden');
        row.classList.remove('editing-bindle-override');
        const adjustButton = row.querySelector('[data-bind-entry-adjust]');
        adjustButton.textContent = '单独设置';
        adjustButton.setAttribute('aria-expanded', 'false');
      });
    }
  };

  // 校验输入并整理导入参数；有问题时提示并返回 null。
  const collectImport = () => {
    const selected = selectedSourceIds();
    const selectedIndexes = selectedEntryIndexes();
    const selectedCount = selectedEntryCount();
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
      if (!name) invalidProfile = true;
      profileOverrides[row.dataset.bindProfile] = { name, reviewer };
    });
    if (invalidProfile) { toast('请填写报账人姓名', 'err'); return null; }
    const tag = tagInput.value.trim();
    const batchMode = body.querySelector('[name="bindleBatchMode"]:checked').value;
    const batchId = batchMode === 'existing' ? batchSelect.value : '';
    const batchName = batchMode === 'new' ? newBatch.value.trim() : '';
    if (batchMode === 'existing' && !batchId) { toast('请选择报账批次', 'err'); return null; }
    if (batchMode === 'new' && !batchName) { toast('请填写新批次名称', 'err'); return null; }
    const entryBatchOverrides = {};
    let invalidEntryBatch = false;
    selectedEntryRows().forEach((row) => {
      const entryIndex = row.dataset.bindEntryIndex;
      const entryBatch = row.querySelector('[data-bind-entry-batch]');
      const mode = entryBatch.value;
      if (mode === 'none') {
        entryBatchOverrides[entryIndex] = { mode: 'none' };
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
    if (invalidEntryBatch) { toast('请填写逐条设置的新批次名称', 'err'); return null; }
    const options = {
      profile_overrides: profileOverrides,
      tags: tag ? [tag] : [],
      batch_id: batchId,
      batch_name: batchName,
      selected_profile_ids: [...selected],
      selected_entry_indexes: [...selectedIndexes],
      entry_batch_overrides: entryBatchOverrides,
      scheme_id: sourceScheme || $('[data-import-scheme]',body)?.value || State.scheme?.id,
      profile_mappings: Object.fromEntries($$('[data-bind-profile-mapping]',body).filter(input=>input.value).map(input=>[input.dataset.bindProfileMapping,input.value])),
      role_mappings: Object.fromEntries(selectedEntryRows().map(row=>[row.dataset.bindEntryIndex,Object.fromEntries($$('[data-bind-role-source]',row).filter(input=>input.value).map(input=>[input.dataset.bindRoleSource,input.value]))])),
    };
    return { tampered, options };
  };

  const reviewBox = body.querySelector('#bindleReview');
  // 只写框出的设置里看不出来的内容：条目构成，以及会进入已归档批次、留在未进批次等后果。
  const renderReview = () => {
    const rows = selectedEntryRows();
    const actions = { new: 0, merge: 0, unchanged: 0 };
    rows.forEach((row) => {
      const action = entries[Number(row.dataset.bindEntryIndex)].import_action || 'new';
      actions[action] = (actions[action] || 0) + 1;
    });
    const actionText = [['new', '新增'], ['merge', '补充'], ['unchanged', '已有']]
      .filter(([key]) => actions[key]).map(([key, label]) => `${label} ${actions[key]}`).join(' · ');
    const mode = body.querySelector('[name="bindleBatchMode"]:checked')?.value;
    const archivedIds = new Set(archivedBatches.map((batch) => batch.id));
    // 与导入逻辑一致：统一设置只作用于新增和补充的条目，「已有·无变化」的条目留在原批次，逐条设置才移动。
    let individual = 0, withoutBatch = 0, intoArchived = 0, keepPlace = 0;
    rows.forEach((row) => {
      const value = row.querySelector('[data-bind-entry-batch]').value;
      if (value !== 'default') individual += 1;
      else if (entries[Number(row.dataset.bindEntryIndex)].import_action === 'unchanged') { keepPlace += 1; return; }
      if (value === 'none' || (value === 'default' && mode === 'none')) withoutBatch += 1;
      const target = value.startsWith('batch:') ? value.slice('batch:'.length)
        : value === 'default' && mode === 'existing' ? batchSelect.value : '';
      if (archivedIds.has(target)) intoArchived += 1;
    });
    const lines = [
      intoArchived && ['warn', `${intoArchived} 条将进入已归档批次，不在「在办」列表显示，到「已归档」里查看。`],
      withoutBatch && ['warn', `${withoutBatch} 条将留在「未进批次」。`],
      individual && ['', `${individual} 条按逐条设置的批次导入。`],
      keepPlace && ['', `${keepPlace} 条已有且无变化，留在原批次。`],
    ].filter(Boolean);
    reviewBox.innerHTML = `
      <div class="bindle-review-head"><b>导入前请核对</b><span>将导入 ${rows.length} 条${actionText ? `（${esc(actionText)}）` : ''}。框出的报账人和导入设置会作用到这些条目，导入后要逐条改回。</span></div>
      ${lines.map(([tone, text]) => `<p class="${tone ? 'bindle-review-warn' : 'bindle-review-note'}">${esc(text)}</p>`).join('')}`;
  };
  const startReview = () => {
    reviewing = true;
    body.classList.add('reviewing');
    reviewBox.classList.remove('hidden');
    syncBindleSelection();
    reviewBox.scrollIntoView({ block: 'start', behavior: 'smooth' });
  };
  body.addEventListener('input', () => { if (reviewing) renderReview(); });
  body.addEventListener('change', () => { if (reviewing) renderReview(); });

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
        const progress = taskProgress('正在导入条目和附件…');
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
  syncBindleSelection();
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

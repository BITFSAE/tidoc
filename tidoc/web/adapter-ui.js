/* Scheme controls share the existing invoice work surface and compact dialogs. */
const AdapterUI = (() => {
  let schemes = [], current = null, activeSettings = null;
  function settings() { return current?.definition?.effective_settings || {}; }
  function reviewerRequired() { return settings()['profile.reviewer_required'] === true; }
  function reviewerControl(id, value='') {
    const presentation=settings()['profile.reviewer_presentation']||'visible';
    const row=`<div class="form-row"${presentation==='hidden'&&!value?' hidden':''}><label for="${esc(id)}">审核人${reviewerRequired()?' *':'（可选）'}</label><input id="${esc(id)}" value="${esc(value)}"/></div>`;
    return presentation==='advanced'||(presentation==='hidden'&&value)?`<details class="schema-advanced"><summary>${presentation==='hidden'?'已有审核信息':'审核信息'}</summary>${row}</details>`:row;
  }
  async function operation(message, action) {
    const id = typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : Date.now().toString(16) + Math.random().toString(16).slice(2);
    const body = el('div'); const status = el('p', 'hint'); status.textContent = message; status.setAttribute('role', 'status'); body.append(status);
    let settled = false, polling = false;
    const cancel = mkBtn('取消操作', 'ghost', async () => { cancel.disabled = true; status.textContent = '正在取消，等待当前步骤完成…'; await Api.cancelAdapterOperation(id); });
    const dialog = modal({title:message,body,footer:[cancel],onClose:()=>{if(!settled)Api.cancelAdapterOperation(id).catch(()=>{});}});
    body.setAttribute('aria-busy', 'true');
    const timer = setInterval(async () => {
      if (polling || settled) return; polling = true;
      try { const state = await Api.adapterOperationStatus(id); if (state&&!cancel.disabled) status.textContent = (state.message || message) + (state.total ? ` ${state.completed}/${state.total}` : ''); } catch (_) {} finally { polling = false; }
    }, 400);
    try { return await action(id); }
    finally { settled = true; clearInterval(timer); dialog.close(); }
  }
  // 抬头没有指定颜色（neutral，方案里无处设置）时按顺序分配不同的颜色，避免所有抬头的卡片色条都一样；
  // 方案明确指定的颜色保持不变，且不会被分给其他抬头。
  const TITLE_PALETTE = ['blue', 'green', 'purple', 'teal', 'amber', 'red'];
  function titleColors(titles) {
    const explicit = new Set(titles.map(t => t.color).filter(c => c && c !== 'neutral'));
    const free = TITLE_PALETTE.filter(c => !explicit.has(c));
    let next = 0;
    return titles.map(t => (t.color && t.color !== 'neutral') ? t.color : (free.length ? free[next++ % free.length] : 'neutral'));
  }
  async function refresh() {
    schemes = await Api.listSchemes(); current = schemes.find(s => s.is_default) || schemes[0] || null;
    State.schemes = schemes; State.scheme = current;
    State.titleProfiles = current?.definition?.scheme?.titles || [];
    for (const key of Object.keys(TITLE_CLASS)) delete TITLE_CLASS[key];
    for (const key of Object.keys(TITLE_SHORT)) delete TITLE_SHORT[key];
    const colors = titleColors(State.titleProfiles);
    State.titleProfiles.forEach((title, index) => { TITLE_CLASS[title.name] = 'title-' + colors[index]; TITLE_SHORT[title.name] = title.short_name || title.name; });
    State.defaultPaidToInvoice = settings()['entry.default_paid_to_invoice'];
    State.defaultEntryTitle = State.titleProfiles.find(t => t.id === settings()['entry.default_title_id'])?.name || '';
    State.materialRequirements = await Api.materialRequirements();
    State.paymentOcrEnabled = settings()['assist.payment_ocr'] !== 'manual';
    applyOcrUiVisibility();
  }
  async function setup() {
    const status = await Api.adapterSetupState();
    if (status.needs_legacy_preferences) {
      const preferences = {};
      for (const key of status.legacy_preference_keys || []) { const value = localStorage.getItem(key); if (value !== null) preferences[key] = value; }
      await Api.completeAdapterSetup(status.default_scheme_id, preferences);
    }
    await refresh();
    if (!status.needs_selection) return;
    await new Promise(resolve => {
      const body = el('div'); body.innerHTML = '<p class="hint">选择日常使用的报账方案。之后可在设置中切换，已有材料保留原规则。</p>';
      let finished = false, m;
      const choose = async id => { try { await Api.completeAdapterSetup(id); await refresh(); finished = true; m.close(); resolve(); } catch (error) { toast(error.message, 'err'); } };
      for (const scheme of schemes.filter(s => ['org.tidoc.generic','org.bitfsae.reimbursement'].includes(s.package_id))) {
        const choice = mkBtn('', 'adapter-choice', () => choose(scheme.id));
        const name = el('b'); name.textContent = scheme.name;
        const about = el('span', 'adapter-choice-about'); about.textContent = scheme.definition.manifest.description || '';
        choice.replaceChildren(name, about); body.append(choice);
      }
      body.append(mkBtn('导入报账方案', 'ghost', async () => { const installed = await importPackage(); if (installed) await choose(installed.id || installed.scheme_id); }));
      m = modal({ title: '选择报账方案', body, footer: [], onClose: () => { if (!finished) setTimeout(async () => { await setup(); resolve(); }, 0); } });
    });
  }
  async function initializeViewPreference() {
    const preference=await Api.appPreference(MULTI_CLAIMANT_KEY,'');
    if(preference===''){
      const value=settings()['profile.default_view']==='delegate'?'1':'0';
      await Api.setAppPreference(MULTI_CLAIMANT_KEY,value);
      localStorage.setItem(MULTI_CLAIMANT_KEY,value);
    }
  }
  async function importPackage(path = null) {
    let preview;
    try { preview = await operation('校验报账方案', id => path ? Api.inspectAdapter(path,id) : Api.chooseAdapterFile(id)); }
    catch (error) { toast(error.message, 'err'); return null; }
    if (!preview) return null;
    return new Promise(resolve => {
      const body = el('div'); const manifest = preview.manifest || {};
      body.innerHTML = `<p><b>${esc(manifest.name)}</b> · ${esc(manifest.package_version)}</p><p class="scheme-desc">${esc(manifest.description || '')}</p><p class="scheme-desc">作者：${esc(manifest.author || '未填写')}</p><div class="adapter-diagnostics">${diagnosticsMarkup(preview.diagnostics || [])}</div><label class="form-row">安装方式<select data-install-mode><option value="install">安装为新方案</option><option value="copy">另存本地副本</option>${schemes.some(s => s.package_id === manifest.package_id) ? '<option value="update">更新已有方案</option>' : ''}</select></label><label class="form-row">已有方案<select data-update-scheme>${schemes.filter(s => s.package_id === manifest.package_id).map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('')}</select></label>`;
      const changes = el('div'); body.append(changes);
      const conflictArea = el('div'); body.append(conflictArea);
      const resolutions = {};
      let submit, m;
      const syncSubmit = () => {
        const updating = $('[data-install-mode]',body).value === 'update';
        const unresolved = updating && (preview.conflicts?.[$('[data-update-scheme]',body).value] || []).some((c) => !resolutions[c.id]);
        if (submit) { submit.disabled = unresolved; submit.title = unresolved ? '请先为每项冲突选择处理方式' : ''; }
      };
      const redraw = () => {
        const updating = $('[data-install-mode]',body).value === 'update';
        const selected = $('[data-update-scheme]',body); selected.closest('.form-row').hidden = !updating;
        changes.replaceChildren(); conflictArea.replaceChildren(); for(const key of Object.keys(resolutions))delete resolutions[key];
        syncSubmit();
        if (!updating) return;
        for (const change of preview.changes?.[selected.value] || []) {
          const item = change.after || change.before || {};
          const names = {fields:'附加信息',materials:'材料要求',rules:'条件要求',outputs:'输出',titles:'报账抬头',organization:'单位信息',settings:'方案设置'};
          const row = el('p', 'field-help'); row.textContent = `${names[change.kind] || '方案内容'}：${item.label || item.name || change.id || ''} ${ {added:'新增',removed:'移除',changed:'变更'}[change.change] || '变更'}`; changes.append(row);
        }
        for (const conflict of preview.conflicts?.[selected.value] || []) {
          const row = el('label', 'form-row'); row.append(document.createTextNode(conflict.message));
          const input = el('select'); input.innerHTML = '<option value="">请选择处理方式</option><option value="incoming">采用新方案，保留历史信息</option>' + (conflict.kind === 'setting' ? '' : '<option value="history">保存为历史信息</option>');
          if(conflict.kind==='local_override')input.innerHTML='<option value="">请选择处理方式</option><option value="incoming">采用新方案</option><option value="local">保留本地内容</option>';
          input.onchange = () => { resolutions[conflict.id] = input.value; syncSubmit(); }; row.append(input); conflictArea.append(row);
        }
        if (m) enhanceNativeSelects(conflictArea);
      };
      $('[data-install-mode]',body).onchange = redraw; $('[data-update-scheme]',body).onchange = redraw; redraw();
      let installed = null;
      submit = mkBtn('安装方案', 'primary', async () => {
        submit.disabled = true; submit.textContent = '安装中…';
        try { installed = await operation('安装报账方案',id=>Api.installAdapter(preview.preview_id, { mode: $('[data-install-mode]',body).value, scheme_id: $('[data-update-scheme]',body).value || null, resolutions },id)); await refresh(); m.close(); toast('报账方案已安装', 'ok'); }
        catch (error) { showErrors(body,error); submit.textContent = '安装方案'; syncSubmit(); }
      });
      syncSubmit();
      m = modal({title:'导入报账方案',key:'scheme-import',body,footer:[mkBtn('取消','ghost',()=>m.close()),submit],onClose:()=>resolve(installed)});
    });
  }
  function showErrors(body,error) {
    let node = $('.schema-errors',body); if (!node) { node = el('div','schema-errors'); node.tabIndex = -1; node.setAttribute('role','alert'); body.prepend(node); }
    node.classList.remove('hidden'); node.textContent = error.message || String(error); node.focus();
  }
  function diagnosticsMarkup(items) { return items.map(d => `<p class="hint ${['required','blocked'].includes(d.severity) ? 'warn' : ''}">${esc(d.message || d.code)}</p>`).join(''); }
  const labels = { 'entry.default_title_id':'默认抬头', 'entry.default_paid_to_invoice':'实付初值取发票总额', 'entry.suggested_tags':'建议标签', 'profile.reviewer_required':'审核人必填', 'profile.reviewer_presentation':'审核人显示方式', 'profile.default_view':'初始报账人视图', 'payee.personnel_number_label':'人员编号名称', 'print.default_outputs':'默认输出', 'print.numbering':'材料编号', 'print.image_layout':'图片布局', 'print.content_order':'材料排序', 'print.amount_basis':'金额口径', 'print.payee_mode':'收款方式', 'print.sort_by':'条目排序', 'assist.cloud_ocr_visible':'显示云识别入口', 'assist.verification_visible':'显示查验入口', 'assist.payment_ocr':'付款截图金额识别（实验性）', 'transfer.include_notes':'绑定包包含备注', 'transfer.include_tags':'绑定包包含标签' };
  const choiceLabels={visible:'显示',advanced:'高级选项',hidden:'隐藏',self:'本人报账',delegate:'代填',invoice:'发票金额',paid:'实付金额',single:'统一收款',by_claimant:'分别收款',none:'不含收款信息',entry:'按条目',role:'按材料',selection:'当前选择顺序',invoice_date:'发票日期',invoice_no:'发票号',claimant:'报账人',seller:'销售方',created_at:'创建时间',local:'本地识别',cloud:'云识别',manual:'手动填写',a4_portrait_1:'A4 纵向，每页 1 张',a4_portrait_2:'A4 纵向，每页 2 张',a4_portrait_4:'A4 纵向，每页 4 张',a4_landscape_1:'A4 横向，每页 1 张',a4_landscape_2:'A4 横向，每页 2 张',a4_landscape_4:'A4 横向，每页 4 张'};
  function outputSettingsForm(definition,catalog,keys,values={}) {
    const fields=keys.map(key=>{const spec=catalog[key];const policy=definition.scheme.settings[key]||{};const fixed='fixed' in policy||policy.editable===false;return {id:key,label:labels[key],type:'select',editable:!fixed,presentation:'visible',options:(spec.type==='boolean'?['true','false']:spec.enum||[]).map(value=>({value,label:value==='true'?'开启':value==='false'?'关闭':choiceLabels[value]||value})),help:fixed?'由方案固定。':'留空沿用方案或批次设置。'};});
    const shown={...values};for(const key of keys){const policy=definition.scheme.settings[key]||{};if('fixed' in policy||policy.editable===false)shown[key]=policy.fixed??definition.effective_settings?.[key]??policy.default;}
    const form=SchemaForm.create({fields,values:Object.fromEntries(Object.entries(shown).map(([key,value])=>[key,typeof value==='boolean'?String(value):value]))});
    return {...form,values(){return Object.fromEntries(Object.entries(form.values()).filter(([,value])=>value!==null).map(([key,value])=>[key,catalog[key].type==='boolean'?value==='true':value]));}};
  }
  // ---- 报账方案管理页 -------------------------------------------------------
  // 页面沿用设置页的 settings-block / settings-row 语言：分组卡片，左侧说明、右侧控件。
  // 表单只提交被修改的项，且与方案包默认值相同时清除覆盖，避免一次保存把所有设置固化成本地覆盖。
  const SETTING_GROUPS = [
    { title: '条目录入', keys: ['entry.default_title_id', 'entry.default_paid_to_invoice', 'entry.suggested_tags'] },
    { title: '报账人与收款', keys: ['profile.default_view', 'profile.reviewer_required', 'profile.reviewer_presentation', 'payee.personnel_number_label'] },
    { title: '打印与导出', keys: ['print.default_outputs', 'print.amount_basis', 'print.payee_mode', 'print.sort_by', 'print.content_order', 'print.numbering', 'print.image_layout'] },
    { title: '识别与查验', keys: ['assist.payment_ocr', 'assist.cloud_ocr_visible', 'assist.verification_visible'] },
    { title: '绑定包', keys: ['transfer.include_notes', 'transfer.include_tags'] },
  ];
  const settingHelp = {
    'entry.default_title_id': '新建或批量导入条目时自动带入；留空则跟随发票识别。新添加的抬头保存后才能选择。',
    'entry.default_paid_to_invoice': '新建或导入时用发票金额填写实付；关闭后留空，等待确认。',
    'entry.suggested_tags': '录入时可一键添加的常用标签，用顿号或逗号分隔。',
    'profile.default_view': '新建、导入条目时是默认本人报账，还是先选择报账人（代填）。',
    'profile.reviewer_required': '开启后，没有填写审核人的条目不会被判为材料齐备。',
    'profile.reviewer_presentation': '审核人输入框在条目表单里的位置。',
    'payee.personnel_number_label': '收款信息里该字段的叫法，例如学号、工号。',
    'print.default_outputs': '打印导出时默认勾选的内容，导出前仍可调整。',
    'print.amount_basis': '导出文档中的金额按发票金额还是实付金额填写。',
    'print.payee_mode': '导出文档里的收款信息：统一收款、按报账人分别收款，或不含收款信息。',
    'print.sort_by': '导出时条目的排列顺序。',
    'print.content_order': '材料 PDF 按条目逐条排列，还是按材料类型归并。',
    'print.numbering': '在合并后的材料 PDF 页面上标注编号。',
    'print.image_layout': '图片类材料在材料 PDF 里的纸张方向与每页张数。',
    'assist.payment_ocr': '默认手动填写实付金额，不读取截图。本地识别使用系统自带 OCR，效果有限，识别结果仅供参考，请核对后再使用。',
    'assist.cloud_ocr_visible': '在条目里显示云识别入口。',
    'assist.verification_visible': '在条目里显示发票查验入口。',
    'transfer.include_notes': '导出绑定包时带上条目备注、材料备注及备注修改记录。',
    'transfer.include_tags': '关闭后，导出的绑定包不带条目标签。',
  };
  const outputTypeLabels = { docx: 'Word 文档', pdf_bundle: '材料 PDF', xlsx: 'Excel 表格', attachment_zip: '附件整理包' };
  const accountTypeLabels = { personal_bank: '个人银行账户', corporate_bank: '单位银行账户', none: '不使用账户' };
  const formatTime = (value) => String(value || '').replace('T', ' ').slice(0, 16);
  const normalized = (value) => Array.isArray(value)
    ? JSON.stringify([...value].sort())
    : JSON.stringify(value === '' || value === undefined ? null : value);
  const sameValue = (a, b) => normalized(a) === normalized(b);

  const confirmAction = (options) => confirmDialog(options);

  // 每种设置类型一个控件：统一提供 get / set / setDisabled，变化时调用 notify。
  function settingControl(key, spec, scheme, notify) {
    const value = spec.value;
    if (key === 'entry.default_title_id' || spec.type === 'select') {
      const options = key === 'entry.default_title_id'
        ? [{ value: '', label: '跟随发票识别' }, ...scheme.definition.scheme.titles.map((t) => ({ value: t.id, label: t.short_name || t.name }))]
        : (spec.enum || []).map((item) => ({ value: item, label: choiceLabels[item] || item }));
      const select = el('select', 'settings-select');
      select.innerHTML = options.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('');
      select.value = value ?? '';
      select.setAttribute('aria-label', labels[key] || key);
      select.onchange = notify;
      return {
        node: select,
        get: () => (select.value === '' ? null : select.value),
        set: (next) => { select.value = next ?? ''; },
        setDisabled: (disabled) => { select.disabled = disabled; },
      };
    }
    if (spec.type === 'boolean') {
      const label = el('label', 'switch-line'), input = el('input'), text = el('span');
      input.type = 'checkbox';
      input.checked = value === true;
      input.setAttribute('aria-label', labels[key] || key);
      const paint = () => { text.textContent = input.checked ? '已开启' : '已关闭'; };
      input.onchange = () => { paint(); notify(); };
      paint();
      label.append(input, text);
      return {
        node: label,
        get: () => input.checked,
        set: (next) => { input.checked = next === true; paint(); },
        setDisabled: (disabled) => { input.disabled = disabled; },
      };
    }
    if (key === 'print.default_outputs' || (spec.type === 'multiselect' && key !== 'entry.suggested_tags')) {
      const options = key === 'print.default_outputs'
        ? scheme.definition.outputs.map((o) => ({ value: o.id, label: o.label }))
        : (spec.enum || []).map((item) => ({ value: item, label: choiceLabels[item] || item }));
      const group = el('div', 'chip-group'), inputs = [];
      group.setAttribute('role', 'group');
      group.setAttribute('aria-label', labels[key] || key);
      for (const option of options) {
        const chip = el('label', 'chip-check'), input = el('input');
        input.type = 'checkbox';
        input.value = option.value;
        input.checked = (value || []).includes(option.value);
        chip.classList.toggle('is-on', input.checked);
        input.onchange = () => { chip.classList.toggle('is-on', input.checked); notify(); };
        chip.append(input, document.createTextNode(option.label));
        group.append(chip);
        inputs.push(input);
      }
      return {
        node: group,
        stacked: true,
        get: () => inputs.filter((i) => i.checked).map((i) => i.value),
        set: (next) => { for (const i of inputs) { i.checked = (next || []).includes(i.value); i.parentElement.classList.toggle('is-on', i.checked); } },
        setDisabled: (disabled) => { for (const i of inputs) { i.disabled = disabled; i.parentElement.classList.toggle('is-disabled', disabled); } },
      };
    }
    const input = el('input', key === 'entry.suggested_tags' ? 'settings-input wide' : 'settings-input');
    const isTags = key === 'entry.suggested_tags';
    input.type = 'text';
    input.setAttribute('aria-label', labels[key] || key);
    if (isTags) input.placeholder = '例如：差旅、办公、已报销';
    else if (spec.default) input.placeholder = String(spec.default);
    input.value = isTags ? (value || []).join('、') : (value ?? '');
    input.oninput = notify;
    return {
      node: input,
      stacked: isTags,
      get: () => {
        if (!isTags) return input.value.trim() || null;
        return [...new Set(input.value.split(/[、,，;；\n]+/).map((s) => s.trim()).filter(Boolean))];
      },
      set: (next) => { input.value = isTags ? (next || []).join('、') : (next ?? ''); },
      setDisabled: (disabled) => { input.disabled = disabled; },
    };
  }

  // 一行设置：左侧说明（含「已自定义 / 恢复默认」），右侧控件；变化时标出未保存，并与方案包基线比较。
  // makeControl(notify) 返回 { node, get, set, setDisabled, stacked? }。
  function buildRow({ key, label, help, locked, baseline, initial, makeControl, onChange }) {
    const row = el('div', 'settings-row' + (locked ? ' is-locked' : ''));
    const copy = el('div', 'settings-row-copy');
    const title = el('b');
    const custom = el('small', 'setting-flag is-changed', '已自定义');
    const reset = el('button', 'link-btn');
    reset.type = 'button';
    reset.textContent = '恢复默认';
    title.append(document.createTextNode(label));
    if (locked) title.append(el('small', 'setting-flag', '方案固定'));
    title.append(custom, reset);
    const helpNode = el('span');
    helpNode.textContent = help + (locked ? (help ? ' ' : '') + '此项由当前方案固定，不能修改。' : '');
    copy.append(title, helpNode);
    const holder = el('div', 'settings-row-control');
    const control = makeControl(() => { paint(); onChange(); });
    holder.append(control.node);
    if (control.stacked) row.classList.add('is-stacked');
    row.append(copy, holder);
    control.setDisabled(locked);
    function paint() {
      const value = control.get();
      row.classList.toggle('is-dirty', !sameValue(value, initial));
      const differs = !locked && !sameValue(value, baseline);
      custom.hidden = !differs;
      reset.hidden = !differs;
    }
    reset.onclick = () => { control.set(baseline); paint(); onChange(); };
    paint();
    return {
      key, node: row, locked, baseline, initial,
      get: control.get,
      set: (next) => { control.set(next); paint(); },
      isDirty: () => !sameValue(control.get(), initial),
    };
  }

  function settingRow(key, spec, scheme, onChange) {
    const policy = scheme.definition.scheme.settings?.[key] || {};
    const locked = 'fixed' in policy || policy.editable === false;
    // 输出已经声明了收款方式、方案又没有为它设规则时，「不含收款信息」对这些输出不起作用：
    // 设置里显示实际的收款方式，也不提供这个无效选项。
    let declaredPayeeMode = null;
    if (key === 'print.payee_mode' && supportsPayee(scheme.definition) && !scheme.definition.scheme.settings?.[key]) {
      declaredPayeeMode = scheme.definition.outputs.find(needsPayee).payee_mode;
      const unset = !spec.value || spec.value === 'none';
      spec = { ...spec, value: unset ? declaredPayeeMode : spec.value, default: declaredPayeeMode, enum: (spec.enum || []).filter((item) => item !== 'none') };
    }
    const baseline = locked ? spec.value : (scheme.settings_baseline?.[key] ?? spec.default ?? null);
    return buildRow({
      key, locked, onChange,
      label: labels[key] || spec.label || key,
      help: settingHelp[key] || '',
      // 基线 = 方案包自身给出的值（后端按包解析，含 import_defaults / default_outputs）；旧后端缺字段时退回注册默认值。
      baseline: declaredPayeeMode && (baseline === 'none' || baseline == null) ? declaredPayeeMode : baseline,
      initial: spec.value,
      makeControl: (notify) => settingControl(key, spec, scheme, notify),
    });
  }

  // ---- 抬头与材料要求：它们是方案本身的内容，和普通设置一起编辑、一起保存，一次保存只生成一个新版本。
  const TITLES_KEY = 'scheme:titles';
  const REQUIREMENT_PREFIX = 'scheme:requirement:';
  const REQUIREMENT_ROWS = [
    ['payment_screenshot', '付款截图', '付款凭证图片。'],
    ['physical_image', '实物图', '物资照片。'],
    ['inspection_pdf', '查验单', '发票查验单 PDF。'],
    ['paid_amount', '实付金额', '实际支付金额；没有填写时条目不算材料齐备。'],
  ];
  const titleList = (titles) => (titles || []).map((t) => ({ name: t.name || '', tax_id: t.tax_id || '' }));

  function titlesControl(initial, notify) {
    const wrap = el('div', 'settings-titleprofiles');
    const list = el('div', 'settings-titleprofile-list');
    const empty = el('div', 'settings-titleprofile-empty', '未配置抬头：只提示明细与金额问题，不按抬头校验。');
    const addButton = mkBtn('添加抬头', 'small ghost', () => { const row = addRow({}); row.querySelector('[data-tp-name]').focus(); notify(); });
    const refreshEmpty = () => { empty.hidden = list.children.length > 0; };
    function addRow(title) {
      const row = el('div', 'settings-titleprofile-row');
      row.innerHTML = '<input data-tp-name placeholder="抬头名称" aria-label="抬头名称"/>'
        + '<input data-tp-tax placeholder="税号（可选）" aria-label="税号"/>'
        + '<button type="button" class="btn small ghost" data-tp-remove>移除</button>';
      row.querySelector('[data-tp-name]').value = title.name || '';
      row.querySelector('[data-tp-tax]').value = title.tax_id || '';
      row.addEventListener('input', notify);
      row.querySelector('[data-tp-remove]').onclick = () => { row.remove(); refreshEmpty(); notify(); };
      list.append(row);
      refreshEmpty();
      return row;
    }
    const fill = (titles) => { list.replaceChildren(); titleList(titles).forEach((t) => addRow(t)); refreshEmpty(); };
    fill(initial);
    wrap.append(empty, list, addButton);
    return {
      node: wrap,
      stacked: true,
      get: () => [...list.children].map((row) => ({
        name: row.querySelector('[data-tp-name]').value.trim(),
        tax_id: row.querySelector('[data-tp-tax]').value.trim(),
      })).filter((t) => t.name || t.tax_id),
      set: fill,
      setDisabled: (disabled) => { for (const input of wrap.querySelectorAll('input,button')) input.disabled = disabled; },
    };
  }

  function requirementControl(initial, notify) {
    const label = el('label', 'switch-line'), input = el('input'), text = el('span');
    input.type = 'checkbox';
    input.checked = initial === true;
    const paint = () => { text.textContent = input.checked ? '必需' : '可选'; };
    input.onchange = () => { paint(); notify(); };
    paint();
    label.append(input, text);
    return {
      node: label,
      get: () => input.checked,
      set: (next) => { input.checked = next === true; paint(); },
      setDisabled: (disabled) => { input.disabled = disabled; },
    };
  }

  function titlesRow(scheme, onChange) {
    return buildRow({
      key: TITLES_KEY, locked: false, onChange,
      label: '报账抬头',
      help: '发票识别与校验按这些抬头、税号进行，可添加其他学校或单位。新添加的抬头保存后，才能在「条目录入」里选为默认抬头。',
      baseline: titleList(scheme.titles_baseline),
      initial: titleList(scheme.definition.scheme.titles),
      makeControl: (notify) => titlesControl(scheme.definition.scheme.titles, notify),
    });
  }

  // 方案里定义了的内置材料（发票固定必需）可以开关；方案自己规定的其他材料只读列出。
  function requirementRows(scheme, onChange) {
    const current = scheme.requirements || {};
    const baseline = scheme.requirements_baseline || {};
    return REQUIREMENT_ROWS.filter(([key]) => key in current).map(([key, label, help]) => buildRow({
      key: REQUIREMENT_PREFIX + key, locked: false, onChange, label, help,
      baseline: baseline[key] === true,
      initial: current[key] === true,
      makeControl: (notify) => requirementControl(current[key] === true, notify),
    }));
  }

  function fixedMaterialRows(scheme) {
    const builtin = ['invoice', 'payment_screenshot', 'physical_image', 'inspection_pdf'];
    const rows = [['invoice', '发票', '发票 PDF 或 XML；始终必需。', '必需']];
    for (const role of scheme.definition.materials || []) {
      if (builtin.includes(role.id) || role.id === 'other' && !(role.min_count > 0)) continue;
      const need = role.min_count > 0 ? `必需，至少 ${role.min_count} 份` : '可选';
      rows.push([role.id, role.label, '由方案规定，不能在这里修改。', need + (role.max_count != null ? `，最多 ${role.max_count} 份` : '')]);
    }
    return rows.map(([, label, help, state]) => {
      const row = el('div', 'settings-row is-locked');
      const copy = el('div', 'settings-row-copy');
      copy.append(el('b', null, esc(label)), el('span', null, esc(help)));
      row.append(copy, el('span', 'settings-fixed-required', esc(state)));
      return row;
    });
  }

  function schemeContents(scheme) {
    const definition = scheme.definition;
    // 抬头和材料要求已经可以在上面编辑；这里只留不能在界面修改的输出与条件规则。
    const sections = [
      ['输出', definition.outputs.map((o) => `${esc(o.label)} <span>${esc(outputTypeLabels[o.type] || o.type)}</span>`)],
      ['条件规则', definition.rules.map((r) => esc(r.message))],
    ].filter(([, items]) => items.length);
    if (!sections.length) return null;
    const block = el('details', 'settings-block scheme-contents');
    block.innerHTML = '<summary class="settings-block-title">其他方案内容（只读）</summary>'
      + sections.map(([title, items]) => `<div class="scheme-contents-section"><h4>${title}</h4><ul>${items.map((item) => `<li>${item}</li>`).join('')}</ul></div>`).join('');
    return block;
  }

  function openSettings() { return openOnce('scheme-page', () => withLoading('正在打开报账方案…', openSchemePage)); }
  async function openSchemePage() {
    await refresh();
    const body = el('div');
    const note = el('span', 'modal-foot-note');
    let dialog = null;
    let view = { dirtyCount: () => 0, resetAll() {}, save: async () => {} };
    const sync = () => {
      const count = view.dirtyCount();
      note.textContent = count ? `有 ${count} 项未保存的更改` : '';
      note.classList.toggle('is-dirty', count > 0);
      save.disabled = count === 0;
      save.textContent = count ? `保存 ${count} 项更改` : '保存更改';
    };
    const save = mkBtn('保存更改', 'primary', () => view.save());
    const resetAll = mkBtn('全部恢复默认', 'ghost', () => view.resetAll());
    const confirmLeave = async () => {
      const count = view.dirtyCount();
      return !count || confirmAction({ title: '放弃未保存的更改？', message: `有 ${count} 项设置还没有保存，离开后这些修改会丢失。`, confirmText: '放弃更改', cancelText: '继续编辑', danger: true });
    };
    const requestClose = () => dialog.requestClose();

    const render = async (schemeId) => {
      const scheme = await Api.schemeDetails(schemeId || current.id);
      activeSettings = scheme;
      const manifest = scheme.definition.manifest;
      const descriptions = scheme.settings_descriptions || scheme.settings_catalog || {};
      const rows = new Map();
      const page = el('div', 'settings-shell scheme-page');

      const summary = el('section', 'settings-block scheme-summary');
      const chips = [
        [`版本 ${scheme.package_version}`, `包标识：${scheme.package_id}`],
        [`${scheme.definition.scheme.titles.length} 个抬头`],
        [`${scheme.definition.materials.length} 类材料`],
        [`${scheme.definition.outputs.length} 项输出`],
        ...(manifest.author ? [[`作者 ${manifest.author}`]] : []),
        [`配置版本 ${String(scheme.revision_id || '').slice(0, 8)}`, '保存设置后生成的内部版本号；已有条目沿用创建时的版本。'],
      ];
      summary.innerHTML = `
        <div class="scheme-summary-top">
          <div class="scheme-picker">
            <label for="adapterScheme">当前方案</label>
            <select id="adapterScheme">${schemes.map((s) => `<option value="${esc(s.id)}"${s.id === scheme.id ? ' selected' : ''}>${esc(s.name)}${s.is_default ? ' · 默认' : ''}</option>`).join('')}</select>
          </div>
        </div>
        <p class="scheme-desc">${esc(manifest.description || '该方案没有填写说明。')}</p>
        <div class="scheme-meta">${chips.map(([text, tip]) => `<span class="scheme-chip"${tip ? ` title="${esc(tip)}"` : ''}>${esc(text)}</span>`).join('')}</div>
        <div class="scheme-actions"></div>`;
      const picker = $('#adapterScheme', summary);
      picker.onchange = async () => {
        if (!(await confirmLeave())) { picker.value = scheme.id; return; }
        render(picker.value).then(() => { dialog.body.scrollTop = 0; }).catch((e) => showErrors(body, e));
      };
      $('.scheme-summary-top', summary).append(mkBtn('导入方案…', 'small ghost', async () => {
        if (!(await confirmLeave())) return;
        const installed = await importPackage();
        await render(installed?.id || scheme.id);
      }));

      const actions = $('.scheme-actions', summary);
      const guarded = (action) => async () => {
        if (!(await confirmLeave())) return;
        try { await action(); } catch (e) { showErrors(body, e); }
      };
      if (!scheme.is_default) {
        actions.append(mkBtn('设为默认', 'small primary', guarded(async () => {
          await Api.setDefaultScheme(scheme.id);
          await refresh();
          await render(scheme.id);
          await refreshTitleOptions();
          toast('已设为默认，后续新建条目使用此方案', 'ok');
        })));
      }
      if (usesPayee(scheme.definition)) actions.append(mkBtn('收款信息', 'small ghost', () => payees(scheme.id)));
      actions.append(
        mkBtn('复制方案', 'small ghost', () => copy(scheme, async (copied) => { await refresh(); await render(copied?.id || scheme.id); })),
        mkBtn('导出方案', 'small ghost', () => exportScheme(scheme)),
        mkBtn('历史版本', 'small ghost', () => revisionHistory(scheme, confirmLeave, async () => { await refresh(); await render(scheme.id); })),
      );
      const schemeFields = await Api.getFormDescription('scheme', scheme.id, scheme.id).catch(() => null);
      if (schemeFields?.fields?.length) actions.append(mkBtn('方案附加信息', 'small ghost', () => editFields('scheme', scheme.id, scheme.id)));
      if (!scheme.is_default) {
        actions.append(mkBtn('停用', 'small ghost danger push-right', guarded(async () => {
          const ok = await confirmAction({ title: `停用「${scheme.name}」？`, message: '停用后，该方案不再出现在方案列表和新建条目的选择中；已使用它的条目继续沿用原有规则。', confirmText: '停用方案', danger: true });
          if (!ok) return;
          await Api.disableScheme(scheme.id);
          await refresh();
          await render(current.id);
          toast('方案已停用', 'ok');
        })));
      }
      page.append(summary);

      // 抬头与材料要求放在最前面：它们是方案的基础定义，下面的「新建默认抬头」等设置依赖抬头列表。
      const titlesBlock = el('section', 'settings-block');
      titlesBlock.append(el('div', 'settings-block-title', '抬头与税号'));
      const titlesEntry = titlesRow(scheme, () => sync());
      rows.set(TITLES_KEY, titlesEntry);
      titlesBlock.append(titlesEntry.node);
      page.append(titlesBlock);
      const materialsBlock = el('section', 'settings-block');
      materialsBlock.append(el('div', 'settings-block-title', '材料要求'));
      const fixedRows = fixedMaterialRows(scheme);
      materialsBlock.append(fixedRows[0]);
      for (const entry of requirementRows(scheme, () => sync())) {
        rows.set(entry.key, entry);
        materialsBlock.append(entry.node);
      }
      materialsBlock.append(...fixedRows.slice(1));
      page.append(materialsBlock);

      // 分组渲染：方案声明 hidden 的设置不显示，advanced 的收进「高级设置」，未归组的设置不丢失。
      const policies = scheme.definition.scheme.settings || {};
      // 方案没有对应功能（收款、材料 PDF、抬头）时，相关设置不显示。
      const visibleKeys = Object.keys(descriptions).filter((key) => policies[key]?.presentation !== 'hidden' && settingApplies(key, scheme.definition));
      const grouped = new Set(SETTING_GROUPS.flatMap((g) => g.keys));
      const advancedKeys = visibleKeys.filter((key) => policies[key]?.presentation === 'advanced');
      const mainKeys = (keys) => keys.filter((key) => visibleKeys.includes(key) && !advancedKeys.includes(key));
      const groups = [
        ...SETTING_GROUPS.map((g) => ({ title: g.title === '报账人与收款' && !supportsPayee(scheme.definition) ? '报账人' : g.title, keys: mainKeys(g.keys) })),
        { title: '其他设置', keys: mainKeys(visibleKeys.filter((key) => !grouped.has(key))) },
      ];
      const onChange = () => { sync(); };
      const buildBlock = (keys, container) => {
        for (const key of keys) rows.set(key, settingRow(key, descriptions[key], scheme, onChange));
        container.append(...keys.map((key) => rows.get(key).node));
      };
      for (const group of groups.filter((g) => g.keys.length)) {
        const block = el('section', 'settings-block');
        block.append(el('div', 'settings-block-title', esc(group.title)));
        buildBlock(group.keys, block);
        page.append(block);
      }
      if (advancedKeys.length) {
        const block = el('details', 'settings-block');
        block.append(el('summary', 'settings-block-title', '高级设置'));
        buildBlock(advancedKeys, block);
        if (advancedKeys.some((key) => rows.get(key).isDirty())) block.open = true;
        page.append(block);
      }
      const contents = schemeContents(scheme);
      if (contents) page.append(contents);
      const stopped = (await Api.listSchemes(true)).filter((s) => s.disabled);
      if (stopped.length) {
        const block = el('details', 'settings-block');
        block.append(el('summary', 'settings-block-title', `已停用的方案（${stopped.length}）`));
        for (const item of stopped) {
          const row = el('div', 'settings-row'), copy = el('div', 'settings-row-copy'), name = el('b'), detail = el('span');
          name.textContent = item.name;
          detail.textContent = `版本 ${item.package_version} · 不出现在新建条目的选择中，已使用它的条目不受影响。`;
          copy.append(name, detail);
          row.append(copy, mkBtn('重新启用', 'small ghost', guarded(async () => {
            await Api.enableScheme(item.id);
            await refresh();
            await render(item.id);
            toast('方案已重新启用', 'ok');
          })));
          block.append(row);
        }
        page.append(block);
      }

      view = {
        dirtyCount: () => [...rows.values()].filter((row) => row.isDirty()).length,
        resetAll() {
          for (const row of rows.values()) if (!row.locked) row.set(row.baseline);
          sync();
        },
        async save() {
          const dirty = [...rows.values()].filter((row) => row.isDirty());
          if (!dirty.length) return;
          save.disabled = true;
          try {
            // 与方案包默认值相同的项走 clear（清除本地覆盖）；其余提交具体值，空值（默认抬头选"无"）也是具体值。
            const values = {}, clear = [], changes = {};
            const requirements = {};
            for (const row of dirty) {
              if (row.key === TITLES_KEY) { changes.titles = row.get(); continue; }
              if (row.key.startsWith(REQUIREMENT_PREFIX)) { requirements[row.key.slice(REQUIREMENT_PREFIX.length)] = row.get(); continue; }
              const value = row.get();
              if (sameValue(value, row.baseline)) clear.push(row.key); else values[row.key] = value;
            }
            if (Object.keys(requirements).length) changes.material_requirements = requirements;
            const updated = await Api.updateScheme(scheme.id, scheme.revision_id, { ...changes, settings: values, clear });
            await refresh();
            await render(scheme.id);
            // 抬头变了：工具栏的抬头筛选、卡片色条和分组标题都要跟着更新。
            if (changes.titles) { await refreshTitleOptions(); renderEntries(); }
            toast(updated.current_revision_id === scheme.revision_id ? '设置没有变化' : '已保存。新建条目使用新设置，已有条目沿用原规则。', 'ok');
          } catch (e) {
            showErrors(body, e);
          } finally {
            sync();
          }
        },
      };
      body.replaceChildren(page);
      if (dialog) enhanceNativeSelects(body);
      sync();
    };

    await render();
    // 关闭按钮、点遮罩与 Esc 都由 modal() 统一走 guard：有未保存的更改时先确认。
    dialog = modal({
      title: '报账方案', wide: true, body, key: 'scheme-page', guard: confirmLeave,
      footer: [resetAll, note, mkBtn('关闭', 'ghost', requestClose), save],
    });
    sync();
  }
  async function copy(scheme, onCopied = null) {
    const body = el('div'); body.innerHTML = '<label class="form-row">副本名称<input data-copy-name/></label><p class="field-help">副本沿用当前的设置，收款信息和个人填写值不会复制。已有条目仍使用原方案。</p>';
    const input = $('[data-copy-name]', body); input.value = scheme.name + ' 副本';
    let m;
    const submit = async () => { try { const copied = await Api.copyScheme(scheme.id, input.value); await refresh(); m.close(); if (onCopied) await onCopied(copied); toast('已复制方案', 'ok'); } catch (e) { showErrors(body, e); } };
    input.onkeydown = (e) => { if (e.key === 'Enter') submit(); };
    m = modal({ title: '复制报账方案', key: 'scheme-copy', compact: true, body, footer: [mkBtn('取消', 'ghost', () => m.close()), mkBtn('复制', 'primary', submit)] });
    input.focus(); input.select();
  }
  async function exportScheme(scheme) {
    const body=el('div');body.innerHTML=`<p class="hint">公共包包含 ${scheme.definition.scheme.titles.length} 个抬头、${scheme.definition.materials.length} 类材料和 ${scheme.definition.outputs.length} 项输出。收款对象、账号、个人填写值及历史记录不包含在公共包中。</p><label class="form-row">包标识<input data-package-id value="${esc(scheme.package_id)}"/></label><label class="form-row">名称<input data-package-name value="${esc(scheme.definition.manifest.name)}"/></label><label class="form-row">作者<input data-package-author value="${esc(scheme.definition.manifest.author||'')}"/></label><label class="form-row">说明<textarea data-package-description>${esc(scheme.definition.manifest.description||'')}</textarea></label>`;let m;
    m=modal({title:'导出报账方案',key:'scheme-export',body,footer:[mkBtn('取消','ghost',()=>m.close()),mkBtn('导出','primary',async()=>{try{const r=await Api.exportAdapter(scheme.id,{package_id:$('[data-package-id]',body).value,name:$('[data-package-name]',body).value,author:$('[data-package-author]',body).value,description:$('[data-package-description]',body).value});if(!r.path){showErrors(body,new Error(r.message||'请填写修改后的包标识'));return;}m.close();await Api.openPath(r.path);toast('方案已导出','ok');}catch(e){showErrors(body,e);}})]});
  }
  async function editFields(scope,ownerId,schemeId=null,revisionId=null,onSaved=null) {
    try {
      const description=await Api.getFormDescription(scope,ownerId,schemeId,revisionId);
      const form=SchemaForm.create(description);let m,visibilityRequest=0;
      form.root.addEventListener('change',async()=>{
        const request=++visibilityRequest;
        try{const next=await Api.previewFormDescription(scope,ownerId,form.values(),description.scheme_id,description.revision_id);if(request===visibilityRequest)form.refreshVisibility(next);}catch(e){if(request===visibilityRequest)form.errors(e);}
      });
      if(description.history_values?.length){const historical=el('details');historical.innerHTML='<summary>历史信息</summary>';for(const item of description.history_values){const row=el('p','hint');row.textContent=(item.label||item.field_id)+': '+JSON.stringify(item.value);historical.append(row);}form.root.append(historical);}
      const save=mkBtn('保存','primary',async()=>{save.disabled=true;try{await Api.saveExtensionValues(scope,ownerId,form.values(),description.scheme_id,description.version,description.revision_id);m.close();if(onSaved)await onSaved();toast('已保存','ok');}catch(e){form.errors(e);}finally{save.disabled=false;}});
      m=modal({title:'报账信息',body:form.root,footer:[mkBtn('取消','ghost',()=>m.close()),save]});
    }catch(e){toast(e.message,'err');}
  }
  function payees(schemeId,onClose) { return openOnce('payees', () => withLoading('正在打开收款信息…', () => payeesPage(schemeId,onClose))); }
  async function payeesPage(schemeId=current?.id,onClose) {
    let m; const body=el('div','settings-shell');
    const render=async()=>{
      const [people,scheme,claimants]=await Promise.all([Api.listPayees(),Api.schemeDetails(schemeId),Api.listProfiles()]);
      const personnelLabel=scheme.definition.effective_settings['payee.personnel_number_label']||'人员编号';
      const tail=(person)=>person.account_number?'尾号 '+person.account_number.slice(-4):accountTypeLabels[person.account_type]||'';
      const options=(selected)=>'<option value="">未选择</option>'+people.map(p=>`<option value="${esc(p.id)}"${p.id===selected?' selected':''}>${esc(p.name)}${tail(p)?' · '+esc(tail(p)):''}</option>`).join('');
      body.replaceChildren();
      const intro=el('p','scheme-desc');intro.textContent='只保存在本机，不会写入绑定包。';
      body.append(intro);

      const list=el('section','settings-block');list.append(el('div','settings-block-title','收款对象'));
      if(!people.length)list.append(el('div','settings-list-empty','还没有收款对象'));
      for(const person of people){
        const row=el('div','settings-row'),copy=el('div','settings-row-copy'),name=el('b'),detail=el('span');
        name.textContent=person.name||'（未命名）';
        detail.textContent=[person.bank_name,tail(person),person.personnel_id?`${personnelLabel} ${person.personnel_id}`:''].filter(Boolean).join(' · ')||'未填写账户信息';
        copy.append(name,detail);
        const controls=el('div','settings-row-controls');
        controls.append(mkBtn('编辑','small ghost',()=>editPayee(person,schemeId,render)),mkBtn('删除','small ghost danger',async()=>{
          const ok=await confirmAction({title:`删除收款对象「${person.name}」？`,message:'删除后不可恢复。已被方案、批次或报账人指定使用的收款对象需要先解除指定。',confirmText:'删除',danger:true});
          if(!ok)return;
          try{await Api.deletePayee(person.id);await render();toast('已删除','ok');}catch(e){showErrors(body,e);}
        }));
        row.append(copy,controls);list.append(row);
      }
      body.append(list);

      const byClaimant=payeeModes(scheme.definition).has('by_claimant');
      const pick=el('section','settings-block');pick.append(el('div','settings-block-title',byClaimant?'默认与按报账人指定':'导出时使用'));
      const addPicker=(title,help,selected,save)=>{
        const row=el('div','settings-row'),copy=el('div','settings-row-copy'),name=el('b'),detail=el('span');
        name.textContent=title;copy.append(name);if(help){detail.textContent=help;copy.append(detail);}
        const holder=el('div','settings-row-control'),select=el('select','settings-select');
        select.setAttribute('aria-label',title);select.innerHTML=options(selected);
        select.onchange=async()=>{try{await save(select.value||null);toast('已保存','ok');}catch(e){showErrors(body,e);}};
        holder.append(select);row.append(copy,holder);pick.append(row);
      };
      addPicker('默认收款对象',byClaimant?'没有按报账人指定时使用':'',scheme.default_payee_id,(id)=>Api.setSchemePayee(schemeId,id));
      if(byClaimant)for(const claimant of claimants)addPicker(claimant.name,'',scheme.profile_payee_mappings?.[claimant.id],(id)=>Api.setPayeeMapping(schemeId,claimant.id,id));
      body.append(pick);
      if(m)enhanceNativeSelects(body);
    };
    await render();m=modal({title:'个人收款信息',key:'payees',wide:true,body,onClose,footer:[mkBtn('新建收款对象','ghost',()=>editPayee(null,schemeId,render)),mkBtn('完成','primary',()=>m.close())]});
  }
  async function editPayee(person,schemeId,onSaved) {
    const scheme=await Api.schemeDetails(schemeId);
    const fields=[{id:'name',label:'收款人姓名',type:'text'},{id:'personnel_id',label:scheme.definition.effective_settings['payee.personnel_number_label'],type:'text'},{id:'contact',label:'联系方式',type:'text'},{id:'account_type',label:'账户类型',type:'select',options:[{value:'personal_bank',label:'个人银行账户'},{value:'corporate_bank',label:'单位银行账户'},{value:'none',label:'不使用账户'}]},{id:'bank_name',label:'开户行',type:'text'},{id:'account_number',label:'账号',type:'text'}];
    const form=SchemaForm.create({fields,values:person||{account_type:'personal_bank'}}); let m;
    // 只有方案声明了收款对象级附加字段时，保存后才需要继续填写。
    const hasPayeeFields=scheme.definition.fields.some(f=>f.scope==='payee');
    const save=mkBtn('保存','primary',async()=>{try{const values=form.values();for(const field of ['name','personnel_id','contact','bank_name','account_number'])values[field]=values[field]??'';const updated=await Api.savePayee(person?.id||null,values);m.close();await onSaved();if(hasPayeeFields&&updated.id)await editFields('payee',updated.id,schemeId);}catch(e){form.errors(e);}});
    const type=$('select',form.root);type.onchange=()=>{for(const key of ['bank_name','account_number']){const index=fields.findIndex(f=>f.id===key);form.root.querySelectorAll('.form-row')[index].hidden=type.value==='none';}};type.onchange();
    m=modal({title:person?'编辑收款对象':'新建收款对象',key:'payee-edit',body:form.root,footer:[mkBtn('取消','ghost',()=>m.close()),save]});
  }
  async function revisionHistory(scheme,beforeChange,onChanged) {
    const records=await Api.schemeRevisionHistory(scheme.id);const body=el('div','settings-shell');let m;
    const intro=el('p','scheme-desc');intro.textContent='每次保存方案设置都会生成一个新版本。新建条目使用当前版本；已有条目沿用创建时的版本，不受影响。';body.append(intro);
    const list=el('section','settings-block');
    records.forEach((record,index)=>{
      const current=record.revision_id===scheme.revision_id;
      const row=el('div','settings-row'),copy=el('div','settings-row-copy'),title=el('b'),detail=el('span');
      title.append(document.createTextNode(`第 ${records.length-index} 版`));
      if(current)title.append(el('small','setting-flag is-changed','当前使用'));
      detail.textContent=`${formatTime(record.created_at)} · 配置版本 ${record.revision_id.slice(0,8)}`;
      copy.append(title,detail);row.append(copy);
      if(!current){
        row.append(mkBtn('回到此版本','small ghost',async()=>{
          if(beforeChange&&!(await beforeChange()))return;
          const ok=await confirmAction({title:`回到第 ${records.length-index} 版？`,message:'回到该版本后，新建条目将使用这一版的设置；已有条目不受影响。当前版本仍保留在历史里，可以再切换回来。',confirmText:'回到此版本'});
          if(!ok)return;
          try{await Api.rollbackScheme(scheme.id,record.revision_id,scheme.revision_id);m.close();if(onChanged)await onChanged();toast('已切换版本，后续新建条目采用所选版本','ok');}catch(e){showErrors(body,e);}
        }));
      }
      list.append(row);
    });
    body.append(list);
    m=modal({title:'历史版本',key:'scheme-history',body,footer:[mkBtn('完成','primary',()=>m.close())]});
  }
  async function rebind(ids) {
    await refresh();const entries=await Promise.all(ids.map(id=>Api.getEntry(id)));const body=el('div');body.innerHTML='<p class="hint">预览规则变化。发票原始信息及附件保留。</p><label class="form-row">目标方案<select data-rebind-scheme>'+schemes.map(s=>`<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('')+'</select></label><details class="schema-advanced"><summary>字段和材料对应关系</summary><div data-rebind-mappings></div></details><div data-rebind-preview></div>';let plan=null,m,formVersion=0,mappingRequest=0,mappingLoading=false;
    const redrawMappings=async()=>{
      const request=++mappingRequest;const schemeId=$('[data-rebind-scheme]',body).value;
      formVersion++;plan=null;submit.disabled=true;mappingLoading=true;preview.disabled=true;
      const target=await Api.schemeDetails(schemeId);if(request!==mappingRequest||schemeId!==$('[data-rebind-scheme]',body).value)return;
      formVersion++;const holder=$('[data-rebind-mappings]',body);holder.replaceChildren();
      const sources=new Map();for(const entry of entries){const definition=entry.adapter?.definition;for(const field of definition?.fields||[]){if(field.scope==='entry')sources.set('fields:'+field.id,{kind:'fields',id:field.id,label:field.label,type:field.type});}for(const role of entry.material_roles||[]){if(role.id!=='invoice')sources.set('materials:'+role.id,{kind:'materials',id:role.id,label:role.label});}}
      for(const source of sources.values()){
        const candidates=source.kind==='fields'?target.definition.fields.filter(f=>f.scope==='entry'&&f.type===source.type):target.definition.materials.filter(r=>r.id!=='invoice'&&r.reclassifiable!==false);
        const row=el('label','form-row');row.textContent=source.label;const select=el('select');select.dataset.rebindKind=source.kind;select.dataset.rebindSource=source.id;select.innerHTML='<option value="">按稳定标识保留；不兼容时保留为历史信息</option>'+candidates.map(item=>`<option value="${esc(item.id)}">${esc(item.label)}</option>`).join('');select.onchange=()=>{plan=null;submit.disabled=true;};row.append(select);holder.append(row);
      }
      mappingLoading=false;preview.disabled=false;
    };
    const mappings=()=>{const value={fields:{},materials:{}};for(const select of $$('[data-rebind-source]',body)){if(select.value)value[select.dataset.rebindKind][select.dataset.rebindSource]=select.value;}return value;};
    const preview=mkBtn('预览变化','ghost',async()=>{plan=null;submit.disabled=true;preview.disabled=true;const version=formVersion;try{const candidate=await Api.previewRebind(ids,$('[data-rebind-scheme]',body).value,null,mappings());if(version!==formVersion||!body.isConnected)return;plan=candidate;$('[data-rebind-preview]',body).innerHTML=diagnosticsMarkup(plan.diagnostics||[])+(plan.entries||[]).map(e=>{const entry=entries.find(item=>item.id===e.entry_id);return `<p class="hint">${esc(entry?.invoice_no||e.entry_id)}：${esc({complete:'齐备',partial:'待补材料',draft:'草稿'}[e.old_status]||e.old_status)} → ${esc({complete:'齐备',partial:'待补材料',draft:'草稿'}[e.status]||e.status)}</p>`;}).join('');submit.disabled=!plan.ok;}catch(e){showErrors(body,e);}finally{preview.disabled=mappingLoading;}});
    const submit=mkBtn('应用到这些条目','primary',async()=>{submit.disabled=true;try{await operation('应用报账方案',id=>Api.applyRebind(plan.preview_id,id));m.close();await refreshEntries();toast('已应用并记录规则变化','ok');}catch(e){showErrors(body,e);submit.disabled=false;}});submit.disabled=true;
    body.addEventListener('change',()=>{formVersion++;plan=null;submit.disabled=true;});
    $('[data-rebind-scheme]',body).onchange=()=>redrawMappings().catch(e=>showErrors(body,e));await redrawMappings();m=modal({title:'更改条目报账方案',body,footer:[mkBtn('取消','ghost',()=>m.close()),preview,submit]});
  }
  async function attachRole(entry,role,onSaved) {
    try{const picked=await Api.pickFiles(true,role.extensions?.length?[`${role.label} (${role.extensions.map(e=>'*'+e).join(';')})`]:null);const paths=picked.paths||[];for(const path of paths){let type=role.id.startsWith('custom:')?'other':role.id;if(role.id==='invoice')type=String(path).toLowerCase().endsWith('.xml')?'invoice_xml':'invoice_pdf';await Api.addAttachment(entry.id,path,type,'',{role_id:role.id});}if(paths.length)await onSaved();}catch(e){toast(e.message,'err');}
  }
  // 条目当前方案版本下归属某个材料角色的附件；其他版本冻结下来的材料不计入。
  function roleAttachments(entry,roleId) {
    return (entry.attachments||[]).filter(a=>(a.role_id||(['invoice_pdf','invoice_xml'].includes(a.type)?'invoice':a.type))===roleId&&(!a.role_definition_revision_id||a.role_definition_revision_id===entry.scheme_revision_id));
  }
  // 详情页的材料分组由 app.js 渲染（与改动前一致）；这里只补一个折叠的方案信息区：报账字段、规则提示、来源方案和变更记录。
  function decorateEntry(body,entry,refreshDetail) {
    const definition=entry.adapter?.definition;
    const hasFields=(definition?.fields||[]).some(f=>f.scope==='entry');
    const materialCodes=new Set(['MATERIAL_REQUIRED','RULE_MATERIAL_REQUIRED','INVOICE_BLOCKED']);
    const notices=(entry.diagnostics||[]).filter(d=>!materialCodes.has(d.code));
    const sources=entry.adapter_sources||[];
    const history=entry.extension_history||[];
    // 放在详情页最末且默认折叠；有待处理的字段或提示时，标题上的「待处理」标记仍然可见。
    const section=el('details','detail-section minor-section adapter-entry');
    section.innerHTML=`<summary>报账方案 · ${esc(entry.adapter?.scheme_name||'未选择')}${notices.length?' <span class="badge warning">待处理</span>':''}</summary><p class="scheme-desc">修订 ${esc(entry.scheme_revision_id?.slice(0,8)||'')}</p>`;
    const actions=el('div');
    if(hasFields)actions.append(mkBtn('填写报账信息','small ghost',()=>editFields('entry',entry.id,null,null,refreshDetail)));
    actions.append(mkBtn('更改方案','small ghost',()=>rebind([entry.id])));
    section.append(actions);
    section.insertAdjacentHTML('beforeend',diagnosticsMarkup(notices));
    for(const source of sources){const historical=el('details');historical.innerHTML=`<summary>来源报账信息 · ${esc(source.definition?.manifest?.name||'外部方案')}</summary>`;const fields=source.definition?.fields||[];for(const record of source.extension_values||[]){const field=fields.find(f=>f.id===record.field_id);const row=el('p','hint');row.textContent=(field?.label||record.field_id)+'：'+JSON.stringify(record.value);historical.append(row);}section.append(historical);}
    for(const item of history){const row=el('p','hint');row.textContent=(item.kind==='rebind'?'规则变更':item.kind==='material'?'材料角色变更':'附加信息变更')+' · '+item.changed_at;section.append(row);}
    body.append(section);
  }
  function chooser(selected='') { if(schemes.length<=1)return '';return `<label class="form-row">报账方案<select data-import-scheme>${schemes.map(s=>`<option value="${esc(s.id)}"${s.id===(selected||current?.id)?' selected':''}>${esc(s.name)}</option>`).join('')}</select></label>`; }
  async function batchFields(batchId) {
    const batch=await Api.getBatch(batchId);
    const entries=await Promise.all((batch.entry_ids||[]).map(id=>Api.getEntry(id)));
    const bindings=new Map(entries.map(e=>[e.scheme_id+':'+e.scheme_revision_id,{id:e.scheme_id,revision:e.scheme_revision_id}]));
    if(!bindings.size&&batch.default_scheme_id)bindings.set(batch.default_scheme_id+':'+batch.default_revision_id,{id:batch.default_scheme_id,revision:batch.default_revision_id});
    const body=el('div');let m;const people=await Api.listPayees();const definitions=[];
    for(const {id,revision} of bindings.values()){
      const scheme=await Api.schemeDetails(id);const example=entries.find(e=>e.scheme_id===id&&e.scheme_revision_id===revision);const definition=example?.adapter?.definition||scheme.definition;definitions.push(definition);
      const section=el('div','detail-section');section.innerHTML=`<h3>${esc(scheme.name)} · ${esc(revision.slice(0,8))}</h3>`;
      if(definition.fields.some(f=>f.scope==='batch'))section.append(mkBtn('填写批次信息','ghost',()=>editFields('batch',batchId,id,revision)));
      if(payeeModes(definition).has('single')){const row=el('label','form-row');row.textContent='本批次收款对象';const select=el('select');select.innerHTML='<option value="">沿用方案选择</option>'+people.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}${p.account_number?' · 尾号 '+esc(p.account_number.slice(-4)):''}</option>`).join('');select.value=batch.payee_mappings?.[id]?.id||'';select.onchange=async()=>{try{await Api.setBatchPayee(batchId,id,select.value||null);toast('已保存批次收款对象','ok');}catch(e){showErrors(body,e);}};row.append(select);section.append(row);}body.append(section);
    }
    if(definitions.length){
      const scheme=await Api.schemeDetails([...bindings.values()][0].id);const catalog=scheme.settings_catalog||scheme.settings_descriptions;
      const combined={scheme:{settings:{}},effective_settings:{}};for(const definition of definitions){for(const [key,policy] of Object.entries(definition.scheme.settings)){if(!combined.scheme.settings[key]||'fixed' in policy||policy.editable===false){combined.scheme.settings[key]=policy;combined.effective_settings[key]=definition.effective_settings[key];}}}
      const keys=Object.keys(catalog).filter(key=>catalog[key].scopes.includes('batch')&&definitions.some(d=>settingApplies(key,d)));
      const form=outputSettingsForm(combined,catalog,keys,batch.output_settings||{});const fold=el('details','schema-advanced');fold.innerHTML='<summary>批次输出设置</summary>';fold.append(form.root);
      fold.append(mkBtn('保存批次输出设置','ghost',async()=>{try{const updated=await Api.updateBatchOutputSettings(batchId,form.values(),batch.updated_at);batch.updated_at=updated.updated_at;toast('已保存','ok');}catch(e){form.errors(e);}}));body.append(fold);
    }
    m=modal({title:'批次报账信息',body,footer:[mkBtn('完成','primary',()=>m.close())]});
  }
  // ---- 打印导出 -------------------------------------------------------------
  // 弹窗只放「这次要生成什么」和确实要在这次决定的事；长期默认值在「设置 → 报账方案 → 打印与导出」。
  const GENERIC_OUTPUTS = [
    { id: 'generic_overview', label: '总览 Excel', type: 'xlsx', payee_mode: 'none' },
    { id: 'generic_attachments', label: '附件整理包', type: 'attachment_zip', payee_mode: 'none' },
    { id: 'generic_materials', label: '材料 PDF', type: 'pdf_bundle', payee_mode: 'none' },
  ];
  // 本次可临时覆盖的方案设置，按「哪些输出会用到它」分组；没有对应输出被勾选时整组隐藏。
  // 收款方式（统一／分别）不在这里改，它是方案设置。
  const ADJUST_GROUPS = [
    { id: 'common', keys: ['print.amount_basis', 'print.sort_by'], applies: () => true },
    { id: 'pdf', keys: ['print.numbering', 'print.image_layout', 'print.content_order'], applies: output => output.type === 'pdf_bundle' },
  ];
  // 方案「能不能」收款：看输出声明；「现在」怎么收款：声明的模式再被方案设置 print.payee_mode 改写（与导出预检同一规则）。
  const needsPayee = output => !!output.payee_mode && output.payee_mode !== 'none';
  function outputPayeeMode(definition, output) {
    if (!needsPayee(output)) return 'none';
    const configured = definition?.effective_settings?.['print.payee_mode'] ?? 'none';
    const declaredByScheme = !!definition?.scheme?.settings?.['print.payee_mode'];
    return declaredByScheme || configured !== 'none' ? configured : output.payee_mode;
  }
  function payeeModes(definition = current?.definition) {
    const modes = new Set((definition?.outputs || []).map(output => outputPayeeMode(definition, output)));
    modes.delete('none'); return modes;
  }
  const supportsPayee = (definition = current?.definition) => (definition?.outputs || []).some(needsPayee);
  // 当前生效的方案里有输出要收款对象；没有时（如通用方案）收款信息的入口整体隐藏。
  const usesPayee = (definition = current?.definition) => payeeModes(definition).size > 0;
  // 设置项是否对这个方案有意义：它控制的功能方案根本没有时不显示，避免一屏无关选项。
  function settingApplies(key, definition) {
    const outputs = definition?.outputs || [];
    if (key === 'print.payee_mode' || key === 'payee.personnel_number_label') return supportsPayee(definition);
    if (['print.numbering', 'print.image_layout', 'print.content_order'].includes(key)) return outputs.some(output => output.type === 'pdf_bundle');
    if (key === 'entry.default_title_id') return (definition?.scheme?.titles || []).length > 0;
    return true;
  }
  function adjustRow(definition, catalog, key) {
    const spec = catalog[key]; const policy = definition.scheme.settings[key] || {};
    if (!spec || !settingApplies(key, definition) || 'fixed' in policy || policy.editable === false) return null; // 方案固定的项不允许改，也不占位置
    const isBoolean = spec.type === 'boolean';
    const name = value => value === true || value === 'true' ? '开启' : value === false || value === 'false' ? '关闭' : choiceLabels[value] || value;
    const row = el('div', 'settings-row'); const copy = el('div', 'settings-row-copy');
    copy.innerHTML = `<b>${esc(labels[key])}</b>`; row.title = settingHelp[key] || '';
    const select = el('select'); select.setAttribute('aria-label', labels[key]);
    select.innerHTML = `<option value="">沿用方案设置：${esc(name(definition.effective_settings?.[key]))}</option>` +
      (isBoolean ? ['true', 'false'] : spec.enum || []).map(value => `<option value="${esc(value)}">${esc(name(value))}</option>`).join('');
    const control = el('div', 'settings-row-control'); control.append(select); row.append(copy, control);
    return { key, row, value: () => select.value === '' ? null : isBoolean ? select.value === 'true' : select.value };
  }
  async function print(ids) {
    if (!ids?.length) { toast('请先选择条目', 'err'); return; }
    try {
      const entries = await Promise.all(ids.map(id => Api.getEntry(id)));
      let people = await Api.listPayees(); const component = await Api.printComponentStatus();
      const canRender = type => !['docx', 'pdf_bundle'].includes(type) || !!(component.available && component.ipc_versions?.includes(2) && component.renderers?.includes(type));
      const bindings = new Map(); let missingDefinition = false;
      for (const entry of entries) {
        const definition = entry.adapter?.definition;
        if (!definition) { missingDefinition = true; continue; }
        const binding = entry.scheme_id + ':' + entry.scheme_revision_id;
        if (!bindings.has(binding)) bindings.set(binding, { binding, schemeId: entry.scheme_id, revision: entry.scheme_revision_id, definition, name: entry.adapter?.scheme_name || definition.manifest.name });
      }
      const body = el('div', 'settings-shell export-dialog');
      const meta = new Map(); // 复选框 → { output, binding }
      // 记住上次实际生成时的勾选，下次打开直接沿用（没有记录时用方案的默认选择）。
      const recall = scope => { try { const value = JSON.parse(localStorage.getItem('tidoc.export.outputs.' + scope)); return Array.isArray(value) ? value : null; } catch (_) { return null; } };
      const remember = () => { for (const scope of new Set([...meta.values()].map(item => item.binding ? item.binding.split(':')[0] : 'generic'))) { const ids = chosen().filter(input => (meta.get(input).binding ? meta.get(input).binding.split(':')[0] : 'generic') === scope).map(input => input.dataset.outputId); if (ids.length) try { localStorage.setItem('tidoc.export.outputs.' + scope, JSON.stringify(ids)); } catch (_) {} } };
      const outputRow = (output, binding, checked, definition = null) => {
        const supported = canRender(output.type);
        const row = el('label', 'settings-row export-output' + (supported ? '' : ' is-disabled'));
        const note = supported ? (outputPayeeMode(definition, output) !== 'none' ? '含收款信息' : '') : '需要安装或更新打印导出组件';
        row.innerHTML = `<input type="checkbox" data-output-id="${esc(output.id)}"${binding ? ` data-output-binding="${esc(binding)}"` : ''}${checked && supported ? ' checked' : ''}${supported ? '' : ' disabled'}/><span class="settings-row-copy"><b>${esc(output.label)}</b></span>${note ? `<span class="export-kind${supported ? '' : ' export-warn'}">${esc(note)}</span>` : ''}`;
        meta.set($('input', row), { output, binding, definition });
        return row;
      };
      // 生成内容：方案声明的输出。
      for (const { binding, definition, name } of bindings.values()) {
        const block = el('section', 'settings-block');
        block.append(el('div', 'settings-block-title', esc(bindings.size > 1 ? name + ' · 生成内容' : '生成内容')));
        const defaults = recall(binding.split(':')[0]) ?? definition.effective_settings?.['print.default_outputs'] ?? definition.scheme.default_outputs;
        for (const output of definition.outputs || []) block.append(outputRow(output, binding, defaults != null ? defaults.includes(output.id) : output.default_selected, definition));
        body.append(block);
      }
      // 通用输出：不依赖方案，跨方案或没有安装来源方案时才是主要入口，平时折叠。
      const generic = el('details', 'settings-block export-more');
      const lastGeneric = recall('generic') || [];
      generic.open = missingDefinition || bindings.size !== 1 || lastGeneric.length > 0;
      generic.innerHTML = '<summary>通用输出<span>不依赖报账方案</span></summary>';
      for (const output of GENERIC_OUTPUTS) generic.append(outputRow(output, '', lastGeneric.includes(output.id)));
      body.append(generic);
      // 本次选项：日期与收款对象，用不到的不出现。
      const options = el('section', 'settings-block');
      const dateRow = el('div', 'settings-row');
      dateRow.innerHTML = `<div class="settings-row-copy"><b>文档日期</b></div><input type="date" data-export-date value="${new Date().toLocaleDateString('sv')}"/>`;
      const payeeRow = el('div', 'settings-row');
      const payeeSelect = el('select'); payeeSelect.setAttribute('data-export-payee', ''); payeeSelect.setAttribute('aria-label', '本次收款对象');
      const fillPayees = () => { const keep = payeeSelect.value; payeeSelect.innerHTML = '<option value="">沿用批次或方案默认</option>' + people.map(p => `<option value="${esc(p.id)}">${esc(p.name)}${p.account_number ? ' · 尾号 ' + esc(p.account_number.slice(-4)) : ''}</option>`).join(''); payeeSelect.value = people.some(p => p.id === keep) ? keep : ''; };
      fillPayees();
      const payeeCopy = el('div', 'settings-row-copy'); const payeeControl = el('div', 'settings-row-control');
      const payeeHolder = el('div'); payeeHolder.append(payeeSelect);
      let byClaimantOnly = false;
      const payeeNote = () => {
        const text = byClaimantOnly ? '按报账人分别收款' : people.length ? '' : '还没有收款信息，请先填写';
        payeeCopy.innerHTML = `<b>收款对象</b>${text ? `<span${people.length || byClaimantOnly ? '' : ' class="export-warn"'}>${text}</span>` : ''}`;
      };
      payeeNote();
      payeeRow.append(payeeCopy, payeeControl);
      options.append(dateRow, payeeRow); body.append(options);
      // 方案声明的「导出时填写」字段与批次信息。
      const exportForms = [], batchForms = [];
      const fieldsBlock = el('section', 'settings-block export-fields'); fieldsBlock.append(el('div', 'settings-block-title', '补充信息'));
      const bindForm = (form, scope, schemeId, revision) => { let request = 0; form.root.addEventListener('change', async () => { const mine = ++request; try { const next = await Api.previewFormDescription(scope, 'export-preview', form.values(), schemeId, revision); if (mine === request) form.refreshVisibility(next); } catch (e) { form.errors(e); } }); };
      for (const { binding, schemeId, revision, definition } of bindings.values()) {
        const fields = definition.fields.filter(f => f.scope === 'export');
        if (fields.length) { const form = SchemaForm.create({ fields, values: Object.fromEntries(fields.map(f => [f.id, f.default ?? null])) }); bindForm(form, 'export', schemeId, revision); exportForms.push({ binding, form }); fieldsBlock.append(form.root); }
        const batchFields = definition.fields.filter(f => f.scope === 'batch');
        if (batchFields.length) {
          const inScheme = entries.filter(e => e.scheme_id === schemeId && e.scheme_revision_id === revision);
          const batches = new Map(inScheme.flatMap(e => e.batches || []).map(b => [b.id, b]));
          for (const batch of batches.values()) fieldsBlock.append(mkBtn('填写「' + batch.name + '」的批次信息', 'ghost small', () => editFields('batch', batch.id, schemeId, revision)));
          if (inScheme.some(e => !e.batches?.length)) {
            const form = SchemaForm.create({ fields: batchFields, values: Object.fromEntries(batchFields.map(f => [f.id, f.default ?? null])) });
            const holder = el('div'); holder.innerHTML = '<p class="hint">未进批次的条目，仅用于本次导出。</p>'; holder.append(form.root); fieldsBlock.append(holder);
            bindForm(form, 'batch', schemeId, revision); batchForms.push({ binding, form });
          }
        }
      }
      if (fieldsBlock.children.length > 1) body.append(fieldsBlock);
      // 本次调整：把原来「每个输出一组」合并成一组，默认折叠；方案固定的项直接不出现。
      const adjustForms = []; const adjust = el('details', 'settings-block export-more');
      adjust.innerHTML = '<summary>本次调整<span>仅本次有效</span></summary>';
      for (const { binding, schemeId, definition, name } of bindings.values()) {
        const scheme = await Api.schemeDetails(schemeId); const catalog = scheme.settings_catalog || scheme.settings_descriptions || {};
        const groups = [];
        for (const group of ADJUST_GROUPS) {
          const rows = group.keys.map(key => adjustRow(definition, catalog, key)).filter(Boolean);
          if (!rows.length) continue;
          const holder = el('div'); if (bindings.size > 1) holder.append(el('div', 'settings-block-title', esc(name)));
          rows.forEach(r => holder.append(r.row)); adjust.append(holder); groups.push({ group, rows, holder, binding });
        }
        adjustForms.push({ binding, definition, groups });
      }
      if (adjustForms.some(f => f.groups.length)) body.append(adjust);
      const status = el('div', 'export-status'); status.dataset.exportStatus = ''; status.setAttribute('role', 'status'); status.tabIndex = -1; body.append(status);

      const note = el('span', 'modal-foot-note');
      let m, plan = null, checking = false, generating = false, warned = false, formVersion = 0;
      const chosen = () => $$('[data-output-id]:checked', body);
      const sync = () => {
        const on = chosen().map(input => meta.get(input));
        dateRow.hidden = !on.some(item => item.output.type === 'docx');
        const modes = on.map(item => outputPayeeMode(item.definition, item.output)).filter(mode => mode !== 'none');
        payeeRow.hidden = !modes.length;
        if (byClaimantOnly !== !modes.includes('single')) { byClaimantOnly = !modes.includes('single'); payeeNote(); }
        payeeHolder.hidden = byClaimantOnly;
        options.hidden = dateRow.hidden && payeeRow.hidden;
        for (const { groups } of adjustForms) for (const { group, holder, binding } of groups) holder.hidden = !on.some(item => item.binding === binding && group.applies(item.output));
        adjust.hidden = !adjustForms.some(f => f.groups.some(g => !g.holder.hidden));
        run.disabled = !on.length || checking || generating;
        note.textContent = on.length ? '' : '请先勾选要生成的内容';
      };
      const collect = () => {
        const outputOptions = {};
        for (const { binding, definition, groups } of adjustForms) {
          for (const output of definition.outputs || []) {
            const configured = {};
            for (const { group, rows } of groups) {
              if (!group.applies(output)) continue;
              for (const row of rows) {
                const value = row.value(); if (value === null) continue;
                const name = row.key.slice('print.'.length);
                if (group.id === 'pdf') (configured.pdf ||= {})[name] = value; else configured[name] = value;
              }
            }
            (outputOptions[binding] ||= {})[output.id] = configured;
          }
        }
        return {
          date: $('[data-export-date]', body).value, payee_id: payeeSelect.value || null, output_options_by_binding: outputOptions,
          batch_fields_by_binding: Object.fromEntries(batchForms.map(({ binding, form }) => [binding, form.values()])),
          export_fields_by_binding: Object.fromEntries(exportForms.map(({ binding, form }) => [binding, form.values()])),
          output_ids_by_binding: Object.fromEntries([...bindings.keys()].map(binding => [binding, chosen().filter(input => input.dataset.outputBinding === binding).map(input => input.dataset.outputId)])),
        };
      };
      const showStatus = candidate => {
        // 多个文件（如按抬头拆分）会重复报同一条问题，合并后只显示一次。
        const seen = new Set(); const list = (candidate.diagnostics || []).filter(d => { const key = d.code + '|' + (d.message || ''); if (seen.has(key)) return false; seen.add(key); return true; });
        const blocking = d => ['required', 'blocked'].includes(d.severity);
        const part = (title, items) => items.length ? `<div class="export-status-title">${title}</div>${diagnosticsMarkup(items)}` : '';
        const files = list.some(blocking) ? '' : (candidate.groups || []).map(group => `<p class="hint">${esc(group.filename || '')}${group.payee ? ' · 收款：' + esc(group.payee.name) + ' · 账号尾号 ' + esc(group.payee.account_tail || '') : ''}</p>`).join('');
        status.innerHTML = part('无法生成，请先处理', list.filter(blocking)) + part('提醒', list.filter(d => !blocking(d))) + files;
        status.scrollIntoView({ block: 'nearest' });
      };
      const invalidate = () => { formVersion++; const previous = plan; plan = null; warned = false; run.textContent = '生成'; status.replaceChildren(); if (previous) Api.cancelExport(previous.plan_id).catch(() => {}); sync(); };
      body.addEventListener('change', invalidate);
      const check = async () => {
        const version = formVersion; checking = true; sync(); run.textContent = '检查中…';
        try {
          const candidate = await Api.previewExport(ids, chosen().map(input => input.dataset.outputId), collect());
          if (version !== formVersion || !body.isConnected) { await Api.cancelExport(candidate.plan_id).catch(() => {}); return null; }
          plan = candidate; showStatus(candidate); return candidate;
        } catch (e) { showErrors(body, e); return null; }
        finally { checking = false; run.textContent = warned ? '仍然生成' : '生成'; sync(); }
      };
      const execute = async () => {
        const planId = plan.plan_id;
        generating = true; cancel.textContent = '取消生成'; sync();
        const disabledStates = new Map([...body.querySelectorAll('input,select,textarea,button')].map(control => [control, control.disabled]));
        for (const control of disabledStates.keys()) control.disabled = true;
        const progress = taskProgress('正在准备生成材料…'); let polling = false;
        const timer = setInterval(async () => {
          if (polling || !generating) return; polling = true;
          try { const state = await Api.getExportProgress(planId); if (state && generating) progress.update(cancel.disabled ? '正在取消，等待当前步骤结束…' : state.message + (state.total ? ` ${state.completed}/${state.total}` : '')); }
          catch (_) {} finally { polling = false; }
        }, 400);
        try {
          const result = await Api.runExport(planId);
          if (result.status !== 'completed') {
            showErrors(body, { message: result.status === 'cancelled' ? '已取消生成。' : '生成失败。', diagnostics: result.diagnostics });
            status.innerHTML = diagnosticsMarkup(result.diagnostics || []); plan = null; return;
          }
          remember(); plan = null; m.close(); await jobs(result.job_id || result.id);
        } catch (e) { showErrors(body, e); }
        finally {
          clearInterval(timer); generating = false; progress.close(); cancel.disabled = false; cancel.textContent = '取消';
          if (body.isConnected) { for (const [control, disabled] of disabledStates) control.disabled = disabled; run.textContent = '生成'; warned = false; sync(); }
        }
      };
      // 一个按钮完成「检查 → 生成」：没有问题直接生成；有警告先停下来让用户看一眼，再点一次确认。
      const run = mkBtn('生成', 'primary', async () => {
        if (!(plan && plan.ok && warned)) {
          if (plan) { Api.cancelExport(plan.plan_id).catch(() => {}); plan = null; }
          const candidate = await check(); if (!candidate || !candidate.ok) return;
          if ((candidate.diagnostics || []).some(d => d.severity === 'warning')) { warned = true; run.textContent = '仍然生成'; return; }
        }
        await execute();
      });
      const cancel = mkBtn('取消', 'ghost', async () => { if (plan) await Api.cancelExport(plan.plan_id); if (generating) { cancel.disabled = true; cancel.textContent = '正在取消…'; } else m.close(); });
      const manage = mkBtn('管理收款信息', 'small ghost', () => payees(entries[0]?.scheme_id, async () => { people = await Api.listPayees(); fillPayees(); payeeNote(); enhanceNativeSelects(payeeRow); invalidate(); }));
      payeeControl.append(payeeHolder, manage);
      m = modal({ title: '打印导出', subhead: `已选 ${entries.length} 条发票`, wide: true, body, footer: [note, cancel, run], onClose: () => { if (plan) Api.cancelExport(plan.plan_id).catch(() => {}); } });
      enhanceNativeSelects(body); sync();
      if (!run.disabled) run.focus({ preventScroll: true }); // 回车即按默认选择生成
    } catch (e) { toast(e.message, 'err'); }
  }
  async function batchFill(ids) {
    if(!ids.length)return;
    const descriptions=await Promise.all(ids.map(id=>Api.getFormDescription('entry',id)));
    const first=descriptions[0];const fields=first.fields.filter(f=>descriptions.every(d=>d.package_id===first.package_id&&d.fields.some(other=>other.id===f.id&&other.type===f.type&&JSON.stringify(other.options||[])===JSON.stringify(f.options||[]))));
    const body=el('div');let m;body.innerHTML='<label class="form-row">字段<select data-batch-field>'+fields.map(f=>`<option value="${esc(f.id)}">${esc(f.label)}</option>`).join('')+'</select></label><div data-batch-control></div>';let form;
    const render=()=>{const chosen=fields.find(f=>f.id===$('[data-batch-field]',body).value);form=SchemaForm.create({fields:chosen?[chosen]:[],values:{}});$('[data-batch-control]',body).replaceChildren(form.root);};render();$('[data-batch-field]',body).onchange=render;
    m=modal({title:'批量填写报账信息',body,footer:[mkBtn('取消','ghost',()=>m.close()),mkBtn('保存到所选条目','primary',async()=>{try{const id=$('[data-batch-field]',body).value;await Api.batchSaveExtensionValues(ids,id,form.values()[id]);m.close();await refreshEntries();}catch(e){form.errors(e);}})]});
  }
  // 卡片快捷按钮：内置材料（发票/付款/查验/实物）由 app.js 渲染；这里只补方案自定义的材料角色。
  function customQuickRoles(entry) {
    return (entry.material_roles||[]).filter(r=>r.id.startsWith('custom:')&&r.quick_action&&r.presentation!=='hidden').sort((a,b)=>(a.order||0)-(b.order||0));
  }
  function cardActions(entry) {
    return customQuickRoles(entry).map(role=>{
      const count=entry.role_counts?.[role.id]||0;
      const title=count?`已有 ${count} 份${role.label}；左键继续添加，右键打开最近一份`:`添加${role.label}`;
      return `<button class="${count?'done':''}" data-card-role="${esc(role.id)}" title="${esc(title)}"><span class="action-state"></span>${esc(role.label)}</button>`;
    }).join('');
  }
  function bindCardActions(root,entry) {
    for(const button of root.querySelectorAll('[data-card-role]')){
      const roleId=button.dataset.cardRole;
      button.onclick=async event=>{
        event.stopPropagation();if(event.detail>1){event.preventDefault();return;}
        try{const detail=await Api.getEntry(entry.id);const role=(detail.material_roles||[]).find(r=>r.id===roleId);if(role)await attachRole(detail,role,()=>syncEntryAfterChange(entry.id,{affectsStatus:true}));}catch(e){toast(e.message,'err');}
      };
      button.oncontextmenu=async event=>{
        event.preventDefault();event.stopPropagation();
        try{const detail=await Api.getEntry(entry.id);const list=roleAttachments(detail,roleId);const latest=list[list.length-1];if(!latest){toast('当前条目还没有该材料','err');return;}await Api.openAttachment(latest.id);}catch(e){toast(e.message,'err');}
      };
      button.ondblclick=event=>{event.preventDefault();event.stopPropagation();};
    }
  }
  function jobs(focusId=null) { return openOnce('export-jobs', () => withLoading('正在打开导出记录…', () => jobsPage(focusId))); }
  async function jobsPage(focusId=null) {
    const records = await Api.listExportJobs();
    const body = el('div', 'settings-shell');
    let m;
    const statusBadge = { completed: ['已完成', 'pass'], failed: ['失败', 'blocked'], cancelled: ['已取消', 'warning'], running: ['生成中', 'warning'], planned: ['已预检', ''] };
    const intro = el('p', 'scheme-desc');
    intro.textContent = '每次生成都会保留一份当时的数据快照。重新生成使用这份快照，不受之后修改条目的影响。';
    body.append(intro);
    if (!records.length) body.append(el('div', 'settings-list-empty', '还没有导出记录。生成打印导出后，会在这里留下记录，可以重新打开文件或按原数据重新生成。'));
    for (const job of records) {
      const id = job.id || job.job_id;
      const [statusText, statusClass] = statusBadge[job.status] || [job.status, ''];
      const groups = job.snapshot?.groups || [];
      const schemeNames = [...new Set(groups.map((g) => g.scheme_name).filter(Boolean))];
      const files = (job.files || []).map((file) => (typeof file === 'string' ? { path: file, filename: baseName(file) } : file));
      const card = el('section', 'settings-block job-card' + (id === focusId ? ' is-focus' : ''));

      const head = el('div', 'settings-row');
      const copy = el('div', 'settings-row-copy'), title = el('b'), detail = el('span');
      title.append(document.createTextNode(formatTime(job.created_at)), el('small', 'badge ' + statusClass, esc(statusText)));
      detail.textContent = [
        job.snapshot?.entry_ids ? `${job.snapshot.entry_ids.length} 条发票` : '',
        schemeNames.join('、'),
        `${files.length} 个文件`,
      ].filter(Boolean).join(' · ');
      copy.append(title, detail);
      const controls = el('div', 'settings-row-controls');
      const folder = files.map((f) => f.path || f.absolute_path).find(Boolean)?.replace(/[\\/][^\\/]*$/, '');
      if (folder) controls.append(mkBtn('打开文件夹', 'small ghost', () => Api.openPath(folder).catch((e) => toast(e.message, 'err'))));
      const regenerate = mkBtn('按原数据重新生成', 'small ghost', async () => {
        regenerate.disabled = true;
        const progress = taskProgress('正在按原数据重新生成…');
        try {
          const result = await Api.regenerateExport(id);
          m.close();
          await jobs(result.job_id || result.id);
          toast(result.status === 'completed' ? '已重新生成' : result.status === 'cancelled' ? '已取消生成' : '重新生成失败，原因见记录', result.status === 'completed' ? 'ok' : 'err');
        } catch (e) {
          showErrors(body, e);
          regenerate.disabled = false;
        } finally {
          progress.close();
        }
      });
      controls.append(regenerate);
      head.append(copy, controls);
      card.append(head);

      const list = el('div', 'job-files');
      for (const file of files) {
        const path = file.path || file.absolute_path;
        const row = el('div', 'settings-row'), fileCopy = el('div', 'settings-row-copy'), name = el('b'), kind = el('span');
        name.textContent = file.filename || file.name || baseName(path || '');
        kind.textContent = outputTypeLabels[file.type] || '';
        fileCopy.append(name);
        if (kind.textContent) fileCopy.append(kind);
        row.append(fileCopy);
        if (path) row.append(mkBtn('打开', 'small ghost', () => Api.openPath(path).catch((e) => toast(e.message, 'err'))));
        list.append(row);
      }
      card.append(list);
      if ((job.diagnostics || []).length) {
        const notes = el('div', 'job-diagnostics');
        notes.innerHTML = diagnosticsMarkup(job.diagnostics);
        card.append(notes);
      }
      body.append(card);
    }
    m = modal({ title: '导出记录', key: 'export-jobs', wide: true, body, footer: [mkBtn('完成', 'primary', () => m.close())] });
    $('.is-focus', body)?.scrollIntoView({ block: 'nearest' });
  }
  return {setup,initializeViewPreference,refresh,openSettings,importPackage,reviewerRequired,reviewerControl,settings,usesPayee,payeeModes,settingApplies,editFields,payees,decorateEntry,chooser,print,rebind,batchFields,batchFill,cardActions,bindCardActions,attachRole,roleAttachments,jobs};
})();

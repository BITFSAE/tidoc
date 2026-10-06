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
  async function refresh() {
    schemes = await Api.listSchemes(); current = schemes.find(s => s.is_default) || schemes[0] || null;
    State.schemes = schemes; State.scheme = current;
    State.titleProfiles = current?.definition?.scheme?.titles || [];
    for (const key of Object.keys(TITLE_CLASS)) delete TITLE_CLASS[key];
    for (const key of Object.keys(TITLE_SHORT)) delete TITLE_SHORT[key];
    for (const title of State.titleProfiles) { TITLE_CLASS[title.name] = 'title-' + (title.color || 'neutral'); TITLE_SHORT[title.name] = title.short_name || title.name; }
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
      for (const scheme of schemes.filter(s => ['org.tidoc.generic','org.bitfsae.reimbursement'].includes(s.package_id))) body.append(mkBtn(scheme.name, 'adapter-choice', () => choose(scheme.id)));
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
      body.innerHTML = `<p><b>${esc(manifest.name)}</b> · ${esc(manifest.package_version)}</p><p class="hint">${esc(manifest.description || '')}</p><p class="hint">作者：${esc(manifest.author || '未填写')}</p><div class="adapter-diagnostics">${diagnosticsMarkup(preview.diagnostics || [])}</div><label class="form-row">安装方式<select data-install-mode><option value="install">安装为新方案</option><option value="copy">另存本地副本</option>${schemes.some(s => s.package_id === manifest.package_id) ? '<option value="update">更新已有方案</option>' : ''}</select></label><label class="form-row">已有方案<select data-update-scheme>${schemes.filter(s => s.package_id === manifest.package_id).map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('')}</select></label>`;
      const changes = el('div', 'hint'); body.append(changes);
      const conflictArea = el('div'); body.append(conflictArea);
      const resolutions = {};
      const redraw = () => {
        const updating = $('[data-install-mode]',body).value === 'update';
        const selected = $('[data-update-scheme]',body); selected.parentElement.hidden = !updating;
        changes.replaceChildren(); conflictArea.replaceChildren(); for(const key of Object.keys(resolutions))delete resolutions[key];
        if (!updating) return;
        for (const change of preview.changes?.[selected.value] || []) {
          const item = change.after || change.before || {};
          const names = {fields:'附加信息',materials:'材料要求',rules:'条件要求',outputs:'输出',titles:'报账抬头',organization:'单位信息',settings:'方案设置'};
          const row = el('p'); row.textContent = `${names[change.kind] || '方案内容'}：${item.label || item.name || change.id || ''} ${ {added:'新增',removed:'移除',changed:'变更'}[change.change] || '变更'}`; changes.append(row);
        }
        for (const conflict of preview.conflicts?.[selected.value] || []) {
          const row = el('label', 'form-row'); row.append(document.createTextNode(conflict.message));
          const input = el('select'); input.innerHTML = '<option value="">请选择处理方式</option><option value="incoming">采用新方案，保留历史信息</option>' + (conflict.kind === 'setting' ? '' : '<option value="history">保存为历史信息</option>');
          if(conflict.kind==='local_override')input.innerHTML='<option value="">请选择处理方式</option><option value="incoming">采用新方案</option><option value="local">保留本地内容</option>';
          input.onchange = () => { resolutions[conflict.id] = input.value; }; row.append(input); conflictArea.append(row);
        }
      };
      $('[data-install-mode]',body).onchange = redraw; $('[data-update-scheme]',body).onchange = redraw; redraw();
      let installed = null, m;
      const submit = mkBtn('安装方案', 'primary', async () => {
        submit.disabled = true; submit.textContent = '安装中…';
        try { installed = await operation('安装报账方案',id=>Api.installAdapter(preview.preview_id, { mode: $('[data-install-mode]',body).value, scheme_id: $('[data-update-scheme]',body).value || null, resolutions },id)); await refresh(); m.close(); toast('报账方案已安装', 'ok'); }
        catch (error) { showErrors(body,error); submit.disabled = false; submit.textContent = '安装方案'; }
      });
      m = modal({title:'导入报账方案',body,footer:[mkBtn('取消','ghost',()=>m.close()),submit],onClose:()=>resolve(installed)});
    });
  }
  function showErrors(body,error) {
    let node = $('.schema-errors',body); if (!node) { node = el('div','schema-errors'); node.tabIndex = -1; node.setAttribute('role','alert'); body.prepend(node); }
    node.classList.remove('hidden'); node.textContent = error.message || String(error); node.focus();
  }
  function diagnosticsMarkup(items) { return items.map(d => `<p class="hint ${['required','blocked'].includes(d.severity) ? 'warn' : ''}">${esc(d.message || d.code)}</p>`).join(''); }
  const labels = { 'entry.default_title_id':'默认抬头', 'entry.default_paid_to_invoice':'实付初值取发票总额', 'entry.suggested_tags':'建议标签', 'profile.reviewer_required':'审核人必填', 'profile.reviewer_presentation':'审核人显示方式', 'profile.default_view':'初始报账人视图', 'payee.personnel_number_label':'人员编号名称', 'print.default_outputs':'默认输出', 'print.numbering':'材料编号', 'print.image_layout':'图片布局', 'print.content_order':'材料排序', 'print.amount_basis':'金额口径', 'print.payee_mode':'收款方式', 'print.sort_by':'条目排序', 'assist.cloud_ocr_visible':'显示云识别入口', 'assist.verification_visible':'显示查验入口', 'assist.payment_ocr':'付款识别方式', 'transfer.include_notes':'绑定包包含备注', 'transfer.include_tags':'绑定包包含标签' };
  const choiceLabels={visible:'显示',advanced:'高级选项',hidden:'隐藏',self:'本人报账',delegate:'代填',invoice:'发票金额',paid:'实付金额',single:'统一收款',by_claimant:'分别收款',none:'不含收款信息',entry:'按条目',role:'按材料',selection:'当前选择顺序',invoice_date:'发票日期',invoice_no:'发票号',claimant:'报账人',seller:'销售方',created_at:'创建时间',local:'本地识别',cloud:'云识别',manual:'手动填写',a4_portrait_1:'A4 纵向，每页 1 张',a4_portrait_2:'A4 纵向，每页 2 张',a4_portrait_4:'A4 纵向，每页 4 张',a4_landscape_1:'A4 横向，每页 1 张',a4_landscape_2:'A4 横向，每页 2 张',a4_landscape_4:'A4 横向，每页 4 张'};
  function outputSettingsForm(definition,catalog,keys,values={}) {
    const fields=keys.map(key=>{const spec=catalog[key];const policy=definition.scheme.settings[key]||{};const fixed='fixed' in policy||policy.editable===false;return {id:key,label:labels[key],type:'select',editable:!fixed,presentation:'visible',options:(spec.type==='boolean'?['true','false']:spec.enum||[]).map(value=>({value,label:value==='true'?'开启':value==='false'?'关闭':choiceLabels[value]||value})),help:fixed?'由方案固定。':'留空沿用方案或批次设置。'};});
    const shown={...values};for(const key of keys){const policy=definition.scheme.settings[key]||{};if('fixed' in policy||policy.editable===false)shown[key]=policy.fixed??definition.effective_settings?.[key]??policy.default;}
    const form=SchemaForm.create({fields,values:Object.fromEntries(Object.entries(shown).map(([key,value])=>[key,typeof value==='boolean'?String(value):value]))});
    return {...form,values(){return Object.fromEntries(Object.entries(form.values()).filter(([,value])=>value!==null).map(([key,value])=>[key,catalog[key].type==='boolean'?value==='true':value]));}};
  }
  async function openSettings() {
    await refresh(); const body = el('div'); let m;
    const render = async id => {
      const scheme = await Api.schemeDetails(id || current.id); activeSettings = scheme;
      body.innerHTML = `<div class="settings-row"><label for="adapterScheme">报账方案</label><select id="adapterScheme">${schemes.map(s=>`<option value="${esc(s.id)}"${s.id===scheme.id?' selected':''}>${esc(s.name)}${s.is_default?' · 默认':''}</option>`).join('')}</select></div><p class="hint">${esc(scheme.package_id)} · ${esc(scheme.package_version)} · 修订 ${esc(scheme.revision_id?.slice(0,8))}</p><div class="adapter-actions"></div><div data-adapter-settings></div><div data-adapter-info></div>`;
      $('#adapterScheme',body).onchange = e => render(e.target.value);
      const actions = $('.adapter-actions',body);
      actions.append(mkBtn('设为默认','small ghost',async()=>{try{await Api.setDefaultScheme(scheme.id); await refresh(); await render(scheme.id); await refreshTitleOptions(); toast('后续新建条目使用此方案','ok');}catch(e){showErrors(body,e);}}),mkBtn('复制','small ghost',()=>copy(scheme)),mkBtn('导入','small ghost',async()=>{await importPackage(); await render(scheme.id);}),mkBtn('导出方案','small ghost',()=>exportScheme(scheme)),mkBtn('收款信息','small ghost',()=>payees(scheme.id)),mkBtn('历史修订','small ghost',()=>revisionHistory(scheme)),mkBtn('停用','small ghost',async()=>{try{await Api.disableScheme(scheme.id); await refresh(); await render(current.id);}catch(e){showErrors(body,e);}}));
      const holder = $('[data-adapter-settings]',body);
      const catalog = scheme.settings_catalog || scheme.settings_descriptions || {};
      const defaults = scheme.definition.effective_settings || {};
      const descriptors = scheme.definition.scheme.settings || {};
      const fields = Object.entries(catalog).map(([key,spec]) => { const policy = descriptors[key] || {}; return {...spec,id:key,label:labels[key]||spec.label||key,type:spec.type==='text'?'text':spec.type,options:(spec.enum||[]).map(value=>({value,label:choiceLabels[value]||value})),presentation:policy.presentation||'visible',editable:policy.editable!==false&& !('fixed' in policy),help:'fixed' in policy ? '由当前报账方案固定。' : '保存后创建新修订，已有条目沿用原规则。'}; });
      for(const field of fields){
        if(field.id==='entry.suggested_tags'){field.type='text';}
        if(field.id==='entry.default_title_id'){field.type='select';field.options=scheme.definition.scheme.titles.map(t=>({value:t.id,label:t.short_name||t.name}));}
        if(field.id==='print.default_outputs'){field.options=scheme.definition.outputs.map(o=>({value:o.id,label:o.label}));}
      }
      const displayValues={...defaults,'entry.suggested_tags':(defaults['entry.suggested_tags']||[]).join('、')};
      const form = SchemaForm.create({fields,values:displayValues}); holder.append(form.root);
      const save = mkBtn('保存方案设置','primary',async()=>{save.disabled=true; try{const values=form.values();if('entry.suggested_tags' in values)values['entry.suggested_tags']=String(values['entry.suggested_tags']||'').split(/[、,]/).map(s=>s.trim()).filter(Boolean); await Api.updateSchemeSettings(scheme.id,scheme.revision_id,values); await refresh(); await render(scheme.id); toast('已创建新修订；已有条目沿用原规则','ok');}catch(e){form.errors(e);}finally{save.disabled=false;}}); holder.append(save);
      holder.append(mkBtn('恢复包默认','ghost',async()=>{try{await Api.restoreSchemeDefaults(scheme.id,scheme.revision_id);await refresh();await render(scheme.id);}catch(e){showErrors(body,e);}}));
      const schemeFields = await Api.getFormDescription('scheme',scheme.id,scheme.id);
      if (schemeFields.fields?.length) holder.append(mkBtn('方案附加信息','ghost',()=>editFields('scheme',scheme.id,scheme.id)));
      const info = $('[data-adapter-info]',body); info.innerHTML = `<details><summary>材料、规则和输出说明</summary><p class="hint">${esc(scheme.definition.manifest.description || '')}</p>${scheme.definition.materials.map(r=>`<p class="hint">${esc(r.label)} · 最少 ${r.min_count||0} 份</p>`).join('')}${scheme.definition.outputs.map(o=>`<p class="hint">${esc(o.label)} · ${esc(o.type)}</p>`).join('')}${scheme.definition.rules.map(r=>`<p class="hint">${esc(r.message)}</p>`).join('')}</details>`;
    };
    await render(); m = modal({title:'报账方案',wide:true,body,footer:[mkBtn('完成','primary',()=>m.close())]});
  }
  async function copy(scheme) {
    const body=el('div'); body.innerHTML='<label class="form-row">副本名称<input data-copy-name/></label>'; let m;
    m=modal({title:'复制报账方案',body,footer:[mkBtn('取消','ghost',()=>m.close()),mkBtn('复制','primary',async()=>{try{await Api.copyScheme(scheme.id,$('[data-copy-name]',body).value); await refresh();m.close();toast('已复制，原条目保留原方案','ok');}catch(e){showErrors(body,e);}})]});
  }
  async function exportScheme(scheme) {
    const body=el('div');body.innerHTML=`<p class="hint">公共包包含 ${scheme.definition.scheme.titles.length} 个抬头、${scheme.definition.materials.length} 类材料和 ${scheme.definition.outputs.length} 项输出。收款对象、账号、个人填写值及历史记录不包含在公共包中。</p><label class="form-row">包标识<input data-package-id value="${esc(scheme.package_id)}"/></label><label class="form-row">名称<input data-package-name value="${esc(scheme.definition.manifest.name)}"/></label><label class="form-row">作者<input data-package-author value="${esc(scheme.definition.manifest.author||'')}"/></label><label class="form-row">说明<textarea data-package-description>${esc(scheme.definition.manifest.description||'')}</textarea></label>`;let m;
    m=modal({title:'导出报账方案',body,footer:[mkBtn('取消','ghost',()=>m.close()),mkBtn('导出','primary',async()=>{try{const r=await Api.exportAdapter(scheme.id,{package_id:$('[data-package-id]',body).value,name:$('[data-package-name]',body).value,author:$('[data-package-author]',body).value,description:$('[data-package-description]',body).value});if(!r.path){showErrors(body,new Error(r.message||'请填写修改后的包标识'));return;}m.close();await Api.openPath(r.path);toast('方案已导出','ok');}catch(e){showErrors(body,e);}})]});
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
  async function payees(schemeId=current?.id) {
    let m; const body=el('div');
    const render=async()=>{
      const [people,scheme,claimants]=await Promise.all([Api.listPayees(),Api.schemeDetails(schemeId),Api.listProfiles()]);
      const options=(selected)=>'<option value="">未选择</option>'+people.map(p=>`<option value="${esc(p.id)}"${p.id===selected?' selected':''}>${esc(p.name)}${p.account_number?' · 尾号 '+esc(p.account_number.slice(-4)):''}</option>`).join('');
      body.innerHTML='<p class="hint">收款信息只保存在本机。每次选择完整收款对象。</p><div data-payee-list></div><label class="form-row">方案默认收款对象<select data-default-payee>'+options(scheme.default_payee_id)+'</select></label><div data-claimant-mappings></div>';
      const list=$('[data-payee-list]',body);
      for(const person of people){const row=el('div','settings-row');const label=el('span');label.textContent=person.name+' · '+(person.account_number?'尾号 '+person.account_number.slice(-4):person.account_type);row.append(label,mkBtn('编辑','small ghost',()=>editPayee(person,schemeId,render)));list.append(row);}
      $('[data-default-payee]',body).onchange=async e=>{try{await Api.setSchemePayee(schemeId,e.target.value||null);}catch(err){showErrors(body,err);}};
      for(const claimant of claimants){const row=el('label','form-row');row.textContent=claimant.name+' 的收款对象';const select=el('select');select.innerHTML=options(scheme.profile_payee_mappings?.[claimant.id]);select.onchange=async()=>{try{await Api.setPayeeMapping(schemeId,claimant.id,select.value||null);}catch(e){showErrors(body,e);}};row.append(select);$('[data-claimant-mappings]',body).append(row);}
    };
    await render();m=modal({title:'个人收款信息',body,footer:[mkBtn('新建收款对象','ghost',()=>editPayee(null,schemeId,render)),mkBtn('完成','primary',()=>m.close())]});
  }
  async function editPayee(person,schemeId,onSaved) {
    const scheme=await Api.schemeDetails(schemeId);
    const fields=[{id:'name',label:'收款人姓名',type:'text'},{id:'personnel_id',label:scheme.definition.effective_settings['payee.personnel_number_label'],type:'text'},{id:'contact',label:'联系方式',type:'text'},{id:'account_type',label:'账户类型',type:'select',options:[{value:'personal_bank',label:'个人银行账户'},{value:'corporate_bank',label:'单位银行账户'},{value:'none',label:'不使用账户'}]},{id:'bank_name',label:'开户行',type:'text'},{id:'account_number',label:'账号',type:'text'}];
    const form=SchemaForm.create({fields,values:person||{account_type:'personal_bank'}}); let m;
    const save=mkBtn('保存','primary',async()=>{try{const values=form.values();for(const field of ['name','personnel_id','contact','bank_name','account_number'])values[field]=values[field]??'';const updated=await Api.savePayee(person?.id||null,values);m.close();await onSaved();if(updated.id)await editFields('payee',updated.id,schemeId);}catch(e){form.errors(e);}});
    const type=$('select',form.root);type.onchange=()=>{for(const key of ['bank_name','account_number']){const index=fields.findIndex(f=>f.id===key);form.root.querySelectorAll('.form-row')[index].hidden=type.value==='none';}};type.onchange();
    m=modal({title:'收款对象',body:form.root,footer:[mkBtn('取消','ghost',()=>m.close()),save]});
  }
  async function revisionHistory(scheme) {
    const records=await Api.schemeRevisionHistory(scheme.id);const body=el('div');let m;
    for(const revision of records){const row=el('div','settings-row');row.append(el('span',null,esc(revision.revision_id.slice(0,12))),mkBtn('设为后续默认','small ghost',async()=>{try{await Api.rollbackScheme(scheme.id,revision.revision_id,scheme.revision_id);await refresh();m.close();toast('后续新建条目采用所选修订','ok');}catch(e){showErrors(body,e);}}));body.append(row);}
    m=modal({title:'历史修订',body,footer:[mkBtn('完成','primary',()=>m.close())]});
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
  function decorateEntry(body,entry,refreshDetail) {
    const roles=entry.material_roles||[];
    const section=el('div','detail-section adapter-entry');section.innerHTML=`<h3>报账信息<span class="h3-line"></span></h3><p class="hint">${esc(entry.adapter?.scheme_name||'')} · 修订 ${esc(entry.scheme_revision_id?.slice(0,8)||'')}</p>`;
    section.append(mkBtn('填写报账信息','small ghost',()=>editFields('entry',entry.id,null,null,refreshDetail)),mkBtn('更改方案','small ghost',()=>rebind([entry.id])));
    section.insertAdjacentHTML('beforeend',diagnosticsMarkup(entry.diagnostics||[]));
    for(const source of entry.adapter_sources||[]){const historical=el('details');historical.innerHTML=`<summary>来源报账信息 · ${esc(source.definition?.manifest?.name||'外部方案')}</summary>`;const fields=source.definition?.fields||[];for(const record of source.extension_values||[]){const field=fields.find(f=>f.id===record.field_id);const row=el('p','hint');row.textContent=(field?.label||record.field_id)+'：'+JSON.stringify(record.value);historical.append(row);}section.append(historical);}
    const material=el('div','adapter-materials');
    const used=new Set();
    for(const role of roles){const list=(entry.attachments||[]).filter(a=>(a.role_id||(['invoice_pdf','invoice_xml'].includes(a.type)?'invoice':a.type))===role.id && (!a.role_definition_revision_id||a.role_definition_revision_id===entry.scheme_revision_id));if(role.presentation==='hidden'&&!list.length)continue;
      const group=el('div','att-group');const head=el('div','att-group-head');head.append(el('b',null,esc(role.label)),el('span','hint',`${list.length} 份${role.min_count?' · 最少 '+role.min_count+' 份':''}`),mkBtn('添加','small ghost',()=>attachRole(entry,role,refreshDetail)));group.append(head);
      for(const att of list){used.add(att.id);group.append(materialRow(att,roles,refreshDetail));}material.append(group);
    }
    const historical=(entry.attachments||[]).filter(a=>!used.has(a.id));if(historical.length){const group=el('details');group.innerHTML='<summary>已有材料／外部材料</summary>';for(const att of historical)group.append(materialRow(att,roles,refreshDetail));material.append(group);}
    section.append(material);
    for(const item of entry.extension_history||[]){const row=el('p','hint');row.textContent=(item.kind==='rebind'?'规则变更':item.kind==='material'?'材料角色变更':'附加信息变更')+' · '+item.changed_at;section.append(row);}
    const drop=body.querySelector('#materialDrop');const original=drop?.closest('.detail-section');if(drop)section.insertBefore(drop,material);if(original)original.hidden=true;
    body.append(section);
  }
  function materialRow(att,roles,refreshDetail) {
    const row=el('div','attach-item');const name=el('span','attach-name');name.textContent=att.original_name;row.append(name,mkBtn('打开','small ghost',()=>Api.openAttachment(att.id)));
    row.append(mkBtn('位置','small ghost',()=>Api.revealAttachment(att.id)),mkBtn('替换','small ghost',async()=>{try{const picked=await Api.pickFiles(false);const path=picked.paths?.[0];if(path){await Api.updateAttachment(att.id,{src_path:path,type:att.type});await refreshDetail();}}catch(e){toast(e.message,'err');}}));
    const note=el('input','attach-note');note.setAttribute('aria-label','材料备注');note.placeholder='材料备注';note.value=att.note||'';note.onchange=async()=>{try{await Api.updateAttachment(att.id,{note:note.value});}catch(e){toast(e.message,'err');}};row.append(note);
    if(!['invoice_pdf','invoice_xml'].includes(att.type)){const select=el('select');select.setAttribute('aria-label','材料角色');select.innerHTML='<option value="">调整材料角色</option>'+roles.filter(r=>r.id!=='invoice'&&r.reclassifiable!==false).map(r=>`<option value="${esc(r.id)}">${esc(r.label)}</option>`).join('');select.onchange=async()=>{if(!select.value)return;try{await Api.reclassifyAttachment(att.id,select.value);await refreshDetail();}catch(e){toast(e.message,'err');}};row.append(select);}
    row.append(mkBtn('删除','small danger',async()=>{try{await Api.deleteAttachment(att.id);await refreshDetail();}catch(e){toast(e.message,'err');}}));return row;
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
      const row=el('label','form-row');row.textContent='本批次收款对象';const select=el('select');select.innerHTML='<option value="">沿用方案选择</option>'+people.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}${p.account_number?' · 尾号 '+esc(p.account_number.slice(-4)):''}</option>`).join('');select.value=batch.payee_mappings?.[id]?.id||'';select.onchange=async()=>{try{await Api.setBatchPayee(batchId,id,select.value||null);toast('已保存批次收款对象','ok');}catch(e){showErrors(body,e);}};row.append(select);section.append(row);body.append(section);
    }
    if(definitions.length){
      const scheme=await Api.schemeDetails([...bindings.values()][0].id);const catalog=scheme.settings_catalog||scheme.settings_descriptions;
      const combined={scheme:{settings:{}},effective_settings:{}};for(const definition of definitions){for(const [key,policy] of Object.entries(definition.scheme.settings)){if(!combined.scheme.settings[key]||'fixed' in policy||policy.editable===false){combined.scheme.settings[key]=policy;combined.effective_settings[key]=definition.effective_settings[key];}}}
      const keys=Object.keys(catalog).filter(key=>catalog[key].scopes.includes('batch'));
      const form=outputSettingsForm(combined,catalog,keys,batch.output_settings||{});const fold=el('details','schema-advanced');fold.innerHTML='<summary>批次输出设置</summary>';fold.append(form.root);
      fold.append(mkBtn('保存批次输出设置','ghost',async()=>{try{const updated=await Api.updateBatchOutputSettings(batchId,form.values(),batch.updated_at);batch.updated_at=updated.updated_at;toast('已保存','ok');}catch(e){form.errors(e);}}));body.append(fold);
    }
    m=modal({title:'批次报账信息',body,footer:[mkBtn('完成','primary',()=>m.close())]});
  }
  async function print(ids) {
    if(!ids?.length){toast('请先选择条目','err');return;}
    try{
      const entries=await Promise.all(ids.map(id=>Api.getEntry(id)));const people=await Api.listPayees();const component=await Api.printComponentStatus();const revisions=new Map(entries.map(e=>[e.scheme_id+':'+e.scheme_revision_id,e.adapter?.definition]));const body=el('div');let m,plan=null;
      body.innerHTML='<div data-export-outputs></div><label class="form-row">文档日期<input type="date" data-export-date value="'+new Date().toLocaleDateString('sv')+'"/></label><label class="form-row">本次收款对象<select data-export-payee><option value="">使用批次或方案默认选择</option>'+people.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}${p.account_number?' · 尾号 '+esc(p.account_number.slice(-4)):''}</option>`).join('')+'</select></label><div data-export-fields></div><div data-export-preflight tabindex="-1"></div>';
      const outputs=$('[data-export-outputs]',body);const forms=[],outputForms=[],batchForms=[];let generating=false;
      for(const [binding,definition] of revisions){if(!definition)continue;const [schemeId,revision]=binding.split(':');const example=entries.find(e=>e.scheme_id===schemeId&&e.scheme_revision_id===revision);const section=el('div','detail-section');section.innerHTML=`<h3>${esc(example.adapter?.scheme_name||definition.manifest.name)} · ${esc(revision.slice(0,8))}</h3>`;const defaults=definition.effective_settings?.['print.default_outputs']??definition.scheme.default_outputs;for(const output of definition.outputs||[]){const key=output.id;const label=el('label','chk');const needsPrint=['docx','pdf_bundle'].includes(output.type);const supported=!needsPrint||(component.available&&component.ipc_versions?.includes(2)&&component.renderers?.includes(output.type));label.innerHTML=`<input type="checkbox" data-output-id="${esc(key)}" data-output-binding="${esc(binding)}"${(defaults!=null?defaults.includes(key):output.default_selected)&&supported?' checked':''}${supported?'':' disabled'}/> ${esc(output.label)}${supported?'':' · 需要打印导出组件或更新组件'}`;section.append(label);}outputs.append(section);
        const scheme=await Api.schemeDetails(schemeId);const catalog=scheme.settings_catalog||scheme.settings_descriptions;
        const advanced=el('details','schema-advanced');advanced.innerHTML='<summary>本次输出设置</summary>';
        for(const output of definition.outputs||[]){const keys=['print.amount_basis','print.sort_by'];if(output.payee_mode!=='none')keys.push('print.payee_mode');if(output.type==='pdf_bundle')keys.push('print.numbering','print.image_layout','print.content_order');const form=outputSettingsForm(definition,catalog,keys);const group=el('div','detail-section');group.innerHTML='<h4>'+esc(output.label)+'</h4>';group.append(form.root);advanced.append(group);outputForms.push({binding,id:output.id,form});}section.append(advanced);
        const fields=definition.fields.filter(f=>f.scope==='export');if(fields.length){const form=SchemaForm.create({fields,values:Object.fromEntries(fields.map(f=>[f.id,f.default??null]))});let visibilityRequest=0;form.root.addEventListener('change',async()=>{const request=++visibilityRequest;try{const next=await Api.previewFormDescription('export','export-preview',form.values(),schemeId,revision);if(request===visibilityRequest)form.refreshVisibility(next);}catch(e){form.errors(e);}});forms.push({binding,form});$('[data-export-fields]',body).append(form.root);}
        if(definition.fields.some(f=>f.scope==='batch')){const batches=new Map(entries.filter(e=>e.scheme_id===schemeId&&e.scheme_revision_id===revision).flatMap(e=>e.batches||[]).map(b=>[b.id,b]));for(const batch of batches.values())$('[data-export-fields]',body).append(mkBtn('填写 '+batch.name+' 的批次信息','ghost',()=>editFields('batch',batch.id,schemeId,revision)));}
        if(definition.fields.some(f=>f.scope==='batch')&&entries.some(e=>e.scheme_id===schemeId&&e.scheme_revision_id===revision&&!e.batches?.length)){
          const fields=definition.fields.filter(f=>f.scope==='batch');const form=SchemaForm.create({fields,values:Object.fromEntries(fields.map(f=>[f.id,f.default??null]))});const holder=el('div','detail-section');holder.innerHTML='<h3>未进批次条目的本次信息</h3><p class="hint">仅用于本次导出，保存在导出记录中。</p>';holder.append(form.root);$('[data-export-fields]',body).append(holder);batchForms.push({binding,form});
          let visibilityRequest=0;form.root.addEventListener('change',async()=>{const request=++visibilityRequest;try{const next=await Api.previewFormDescription('batch','export-preview',form.values(),schemeId,revision);if(request===visibilityRequest)form.refreshVisibility(next);}catch(e){form.errors(e);}});
        }
      }
      outputs.insertAdjacentHTML('beforeend','<label class="chk"><input type="checkbox" data-output-id="generic_overview"/> 通用总览 Excel</label><label class="chk"><input type="checkbox" data-output-id="generic_attachments"/> 通用附件整理包</label>');
      const supportsPdf=component.available&&component.ipc_versions?.includes(2)&&component.renderers?.includes('pdf_bundle');outputs.insertAdjacentHTML('beforeend',`<label class="chk"><input type="checkbox" data-output-id="generic_materials"${supportsPdf?'':' disabled'}/> 通用材料 PDF${supportsPdf?'':' · 需要打印导出组件'}</label>`);
      const options=()=>{const outputOptions={};for(const {binding,id,form} of outputForms){const values=form.values();const configured={};for(const [key,value] of Object.entries(values)){const name=key.slice('print.'.length);if(['numbering','image_layout','content_order'].includes(name)){configured.pdf||={};configured.pdf[name]=value;}else configured[name]=value;}(outputOptions[binding]||={})[id]=configured;}return {date:$('[data-export-date]',body).value,payee_id:$('[data-export-payee]',body).value||null,output_options_by_binding:outputOptions,batch_fields_by_binding:Object.fromEntries(batchForms.map(({binding,form})=>[binding,form.values()])),export_fields_by_binding:Object.fromEntries(forms.map(({binding,form})=>[binding,form.values()])),output_ids_by_binding:Object.fromEntries([...revisions.keys()].map(binding=>[binding,$$('[data-output-binding]:checked',body).filter(input=>input.dataset.outputBinding===binding).map(input=>input.dataset.outputId)]))};};
      let formVersion=0;
      const invalidate=()=>{formVersion++;const previous=plan;plan=null;run.disabled=true;if(previous)Api.cancelExport(previous.plan_id).catch(()=>{});};body.addEventListener('change',invalidate);
      const preview=mkBtn('检查生成内容','ghost',async()=>{invalidate();preview.disabled=true;const version=formVersion;try{const chosen=$$('[data-output-id]:checked',body).map(input=>input.dataset.outputId);const candidate=await Api.previewExport(ids,chosen,options());if(version!==formVersion||!body.isConnected){await Api.cancelExport(candidate.plan_id);return;}plan=candidate;const target=$('[data-export-preflight]',body);target.innerHTML=diagnosticsMarkup(plan.diagnostics||[])+(plan.groups||[]).map(group=>`<p class="hint">${esc(group.filename||'')} ${group.payee?' · 收款：'+esc(group.payee.name)+' · 账号尾号 '+esc(group.payee.account_tail||''):''}</p>`).join('');run.disabled=!plan.ok;target.focus();}catch(e){showErrors(body,e);}finally{preview.disabled=false;}});
      const run=mkBtn('生成选中内容','primary',async()=>{
        const planId=plan.plan_id;
        run.disabled=true;preview.disabled=true;generating=true;cancel.textContent='取消生成';
        const disabledStates=new Map([...body.querySelectorAll('input,select,textarea')].map(control=>[control,control.disabled]));
        for(const control of disabledStates.keys())control.disabled=true;
        const progress=taskProgress('正在准备生成材料…');let polling=false;
        const timer=setInterval(async()=>{
          if(polling||!generating)return;polling=true;
          try{const state=await Api.getExportProgress(planId);if(state&&generating)progress.update(cancel.disabled?'正在取消，等待当前步骤结束…':state.message+(state.total?` ${state.completed}/${state.total}`:''));}
          catch(_){}finally{polling=false;}
        },400);
        try{
          const result=await Api.runExport(planId);
          if(result.status!=='completed'){
            showErrors(body,{message:result.status==='cancelled'?'已取消生成。':'生成失败。',diagnostics:result.diagnostics});
            $('[data-export-preflight]',body).innerHTML=diagnosticsMarkup(result.diagnostics||[]);plan=null;return;
          }
          plan=null;m.close();await jobs(result.job_id||result.id);
        }catch(e){showErrors(body,e);}
        finally{
          clearInterval(timer);generating=false;progress.close();preview.disabled=false;cancel.disabled=false;cancel.textContent='取消';
          if(body.isConnected){for(const [control,disabled] of disabledStates)control.disabled=disabled;run.disabled=true;}
        }
      });run.disabled=true;
      const cancel=mkBtn('取消','ghost',async()=>{if(plan)await Api.cancelExport(plan.plan_id);if(generating){cancel.disabled=true;cancel.textContent='正在取消…';}else m.close();});m=modal({title:'打印导出',wide:true,body,footer:[cancel,mkBtn('收款信息','ghost',()=>payees(entries[0]?.scheme_id)),preview,run],onClose:()=>{if(plan)Api.cancelExport(plan.plan_id).catch(()=>{});}});
    }catch(e){toast(e.message,'err');}
  }
  async function batchFill(ids) {
    if(!ids.length)return;
    const descriptions=await Promise.all(ids.map(id=>Api.getFormDescription('entry',id)));
    const first=descriptions[0];const fields=first.fields.filter(f=>descriptions.every(d=>d.package_id===first.package_id&&d.fields.some(other=>other.id===f.id&&other.type===f.type&&JSON.stringify(other.options||[])===JSON.stringify(f.options||[]))));
    const body=el('div');let m;body.innerHTML='<label class="form-row">字段<select data-batch-field>'+fields.map(f=>`<option value="${esc(f.id)}">${esc(f.label)}</option>`).join('')+'</select></label><div data-batch-control></div>';let form;
    const render=()=>{const chosen=fields.find(f=>f.id===$('[data-batch-field]',body).value);form=SchemaForm.create({fields:chosen?[chosen]:[],values:{}});$('[data-batch-control]',body).replaceChildren(form.root);};render();$('[data-batch-field]',body).onchange=render;
    m=modal({title:'批量填写报账信息',body,footer:[mkBtn('取消','ghost',()=>m.close()),mkBtn('保存到所选条目','primary',async()=>{try{const id=$('[data-batch-field]',body).value;await Api.batchSaveExtensionValues(ids,id,form.values()[id]);m.close();await refreshEntries();}catch(e){form.errors(e);}})]});
  }
  function cardActions(entry) {
    const roles=(entry.material_roles||[]).filter(r=>r.quick_action&&r.presentation!=='hidden').sort((a,b)=>(a.order||0)-(b.order||0)).slice(0,4);
    return roles.map(role=>`<button data-card-role="${esc(role.id)}" title="${esc('添加'+role.label)}">${esc(role.label)}</button>`).join('')+`<button data-card-materials>材料</button>`;
  }
  function bindCardActions(root,entry) {
    for(const button of root.querySelectorAll('[data-card-role]'))button.onclick=async event=>{event.stopPropagation();const detail=await Api.getEntry(entry.id);const role=detail.material_roles.find(r=>r.id===button.dataset.cardRole);if(role)await attachRole(detail,role,()=>refreshEntryCard(entry.id));};
    root.querySelector('[data-card-materials]')?.addEventListener('click',async event=>{event.stopPropagation();const detail=await Api.getEntry(entry.id);const body=el('div');let m;for(const role of detail.material_roles||[])body.append(mkBtn(role.label,'ghost',()=>attachRole(detail,role,()=>refreshEntryCard(entry.id))));m=modal({title:'添加材料',body,footer:[mkBtn('完成','primary',()=>m.close())]});});
  }
  async function jobs(focusId=null) {
    const records=await Api.listExportJobs();const body=el('div');let m;
    for(const job of records){const group=el('div','detail-section');group.innerHTML=`<h3>${esc(job.created_at)} · ${esc({completed:'已完成',failed:'失败',cancelled:'已取消',running:'生成中',planned:'已预检'}[job.status]||job.status)}</h3>`;for(const file of job.files||[]){const path=typeof file==='string'?file:file.path||file.absolute_path;const button=mkBtn(typeof file==='string'?baseName(file):file.filename||file.name||baseName(path||''),'small ghost',()=>path&&Api.openPath(path));group.append(button);}group.insertAdjacentHTML('beforeend',diagnosticsMarkup(job.diagnostics||[]));group.append(mkBtn('按原数据重新生成','small ghost',async()=>{try{await Api.regenerateExport(job.id||job.job_id);m.close();await jobs();}catch(e){showErrors(body,e);}}));body.append(group);}
    m=modal({title:'导出记录',wide:true,body,footer:[mkBtn('完成','primary',()=>m.close())]});
  }
  return {setup,initializeViewPreference,refresh,openSettings,importPackage,reviewerRequired,reviewerControl,settings,editFields,payees,decorateEntry,chooser,print,rebind,batchFields,batchFill,cardActions,bindCardActions,jobs};
})();

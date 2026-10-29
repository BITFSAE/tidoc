"""用真实前端函数验证替换确认、取消及自动保存确认。"""

from tests.test_selection_performance import run_selection


def test_inspection_dialog_explicit_replace_keep_and_cancel():
    run_selection(r"""
(async () => {
  const detail = {existing_ids: ['one', 'two'], attachments: [
    {id: 'one', original_name: 'first.pdf'}, {id: 'two', original_name: 'second.pdf'}]};
  let config, focused, selected = {value: 'one'};
  document.createElement = () => ({className: '', innerHTML: '', querySelector: () => selected});
  mkBtn = (text, style, onclick) => ({text, onclick, focus: () => {focused = text;}});
  modal = options => {config = options; return {close() {options.onClose();}};};
  const replace = askInspectionReplacement(detail);
  assert.equal(focused, '取消');
  config.footer.find(btn => btn.text === '替换所选查验单').onclick();
  assert.deepEqual(await replace, {inspection_action: 'replace', inspection_existing_ids: ['one','two'], inspection_replace_id: 'one'});
  const keep = askInspectionReplacement(detail);
  config.footer.find(btn => btn.text === '仍添加一份').onclick();
  assert.equal((await keep).inspection_action, 'keep');
  const cancel = askInspectionReplacement(detail);
  config.onClose();
  assert.equal(await cancel, null);
})().catch(e => {console.error(e);process.exitCode=1;});
""")


def test_shared_add_path_retries_with_explicit_choice_and_preserves_other_options():
    run_selection(r"""
(async () => {
  const calls = [], prompts = [];
  const detail = {existing_ids:['old'], attachments:[{id:'old'}]};
  const choice = {inspection_action:'replace', inspection_existing_ids:['old'], inspection_replace_id:'old'};
  globalThis.Api = {addAttachment: async (...args) => {
    calls.push(args); if(calls.length===1) throw Object.assign(new Error('conflict'),{code:'inspection_conflict',detail});
    return {id:'old'};
  }};
  askInspectionReplacement = async d => {prompts.push(d);return choice;};
  const att = await addAttachmentWithInspectionConfirmation('entry','/new.pdf','inspection_pdf','note',{role_id:'inspection_pdf'});
  assert.equal(att.id,'old'); assert.equal(prompts.length,1);
  assert.deepEqual(calls[1],['entry','/new.pdf','inspection_pdf','note',{role_id:'inspection_pdf',...choice}]);
})().catch(e => {console.error(e);process.exitCode=1;});
""")


def test_cancelled_add_does_not_retry_and_dropped_files_report_no_addition():
    run_selection(r"""
(async () => {
  let calls=0;
  globalThis.Api = {addAttachment: async () => {calls++; throw Object.assign(new Error('conflict'),{code:'inspection_conflict',detail:{}});}};
  askInspectionReplacement = async () => null;
  validateDroppedMaterialsForEntry = async () => {};
  settlePaymentAmountAfterAdd = async () => '';
  const result = await addMaterialInfosToEntry('entry',[{path:'/new.pdf',type:'inspection_pdf'}]);
  assert.equal(calls,1); assert.deepEqual(result.added_paths,[]);
  assert.ok(result.message.includes('未添加'));
})().catch(e => {console.error(e);process.exitCode=1;});
""")


def test_changed_materials_require_a_fresh_confirmation_and_closed_flow_never_retries():
    run_selection(r"""
(async () => {
  const prompts=[], writes=[]; let calls=0;
  askInspectionReplacement = async detail => {
    prompts.push(detail);
    return {inspection_action:'keep',inspection_existing_ids:detail.existing_ids};
  };
  const result=await confirmInspectionOperation(async options=>{
    writes.push(options); calls++;
    if(calls<3) throw Object.assign(new Error('conflict'),{code:'inspection_conflict',detail:{existing_ids:Array(calls).fill('id')}});
    return {state:'attached'};
  });
  assert.equal(result.state,'attached');
  assert.equal(prompts.length,2);
  assert.deepEqual(writes[1].inspection_existing_ids,['id']);
  assert.deepEqual(writes[2].inspection_existing_ids,['id','id']);
  let closed=false, attempts=0;
  askInspectionReplacement=async()=>{closed=true;return {inspection_action:'keep'};};
  const cancelled=await confirmInspectionOperation(async()=>{
    attempts++;throw Object.assign(new Error('conflict'),{code:'inspection_conflict',detail:{}});
  },()=>closed);
  assert.equal(cancelled,null);assert.equal(attempts,1);
})().catch(e => {console.error(e);process.exitCode=1;});
""")

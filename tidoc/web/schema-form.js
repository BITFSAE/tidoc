/* Shared typed controls; backend owns validation, visibility and defaults. */
const SchemaForm = (() => {
  let sequence = 0;
  function create(description) {
    const root = el('div', 'schema-form');
    const summary = el('div', 'schema-errors hidden');
    summary.tabIndex = -1; summary.setAttribute('role', 'alert'); root.append(summary);
    const controls = new Map();
    for (const field of description.fields || []) {
      const value = description.values?.[field.id];
      if (field.presentation === 'hidden' || (field.visible === false && !field.visible_when)) continue;
      const row = el('div', 'form-row');
      row.hidden = field.visible === false;
      const id = `adapter-field-${++sequence}`;
      const label = el('label'); label.htmlFor = id;
      label.textContent = field.label + (field.required_at?.length ? ' *' : ''); row.append(label);
      let input;
      if (field.type === 'select' || field.type === 'multiselect') {
        input = el('select'); input.multiple = field.type === 'multiselect';
        if (!input.multiple) { const empty = el('option'); empty.value = ''; empty.textContent = '请选择'; input.append(empty); }
        for (const choice of field.options || []) { const option = el('option'); option.value = choice.value; option.textContent = choice.label; option.selected = input.multiple ? (value || []).includes(choice.value) : value === choice.value; input.append(option); }
      } else if (field.type === 'multiline') { input = el('textarea'); input.rows = 3; input.value = value ?? ''; }
      else { input = el('input'); input.type = field.type === 'boolean' ? 'checkbox' : field.type === 'date' ? 'date' : field.type === 'integer' ? 'number' : 'text';
        if (field.type === 'boolean') { input.indeterminate = value == null; input.checked = value === true; input.onchange = () => { input.indeterminate = false; }; }
        else input.value = value ?? '';
        if (['decimal', 'money'].includes(field.type)) input.inputMode = 'decimal';
        if (field.type === 'integer') input.step = '1';
        if (field.max_length) input.maxLength = field.max_length;
      }
      input.id = id; input.disabled = field.editable === false; row.append(input);
      const help = el('small', 'hint'); help.id = id + '-help'; help.textContent = field.help || ''; row.append(help);
      const error = el('small', 'field-error'); error.id = id + '-error'; row.append(error);
      input.setAttribute('aria-describedby', help.id + ' ' + error.id);
      if(field.presentation==='advanced'){
        let advanced=root.querySelector('details.schema-advanced');
        if(!advanced){advanced=el('details','schema-advanced');advanced.innerHTML='<summary>高级选项</summary>';root.append(advanced);}
        advanced.append(row);
      }else root.append(row);
      controls.set(field.id, { input, error, field, row });
      input.addEventListener('blur', () => {
        if (['integer', 'decimal', 'money'].includes(field.type) && input.value && !/^-?\d+(\.\d+)?$/.test(input.value)) error.textContent = '请填写有效数字。';
        else error.textContent = '';
        input.setAttribute('aria-invalid', error.textContent ? 'true' : 'false');
      });
    }
    return { root, description, values() {
      const result = {};
      for (const [id, { input, field, row }] of controls) {
        if (input.disabled || row.hidden) continue;
        result[id] = field.type === 'boolean' ? (input.indeterminate ? null : input.checked) : field.type === 'multiselect' ? [...input.selectedOptions].map(x => x.value) : input.value === '' ? null : input.value;
      }
      return result;
    }, refreshVisibility(next) {
      for (const field of next.fields || []) {
        const control = controls.get(field.id);
        if (control) control.row.hidden = field.visible === false || field.presentation === 'hidden';
      }
    }, errors(error) {
      for (const control of controls.values()) { control.error.textContent = ''; control.input.removeAttribute('aria-invalid'); }
      summary.classList.remove('hidden'); summary.replaceChildren();
      const text = el('p'); text.textContent = error.message || String(error); summary.append(text);
      for (const diagnostic of error.diagnostics || []) {
        const target = String(diagnostic.target || diagnostic.field || '');
        const id = controls.has(target) ? target : target.split('.').pop();
        const control = controls.get(id);
        if (control) { control.error.textContent = diagnostic.message; control.input.setAttribute('aria-invalid', 'true'); const link = el('a'); link.href = '#' + control.input.id; link.textContent = diagnostic.message; link.onclick = e => { e.preventDefault(); control.row.hidden = false; const advanced = control.row.closest('details'); if (advanced) advanced.open = true; control.input.focus(); }; summary.append(link); }
      }
      summary.focus();
    }};
  }
  return { create };
})();

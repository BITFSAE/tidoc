// The current tax site uses TDesign fields without ids; input events update
// the form model, while the date picker commits typed text on Enter.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {script, layout} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const model = {}, controls = {}, helpers = [];
let now = 0, mounted = layout !== 'delayed' && layout !== 'missing';
const timers = [];
const document = {
  activeElement: null,
  getElementById(id) {
    return helpers.find(node => node.id === id) ||
      (mounted && layout === 'legacy' ? controls[id] : null);
  },
  querySelector(selector) {
    if (!mounted || layout === 'legacy') return null;
    const key = /^\.t-form-item__(\w+) input$/.exec(selector)?.[1];
    return controls[key] || null;
  },
  createElement() { return {style: {}}; },
  body: {appendChild(node) { helpers.push(node); }},
};
for (const key of ['fpdm', 'fphm', 'kprq', 'kjje', 'kpje', 'yzm']) {
  controls[key] = {
    value: key === 'yzm' ? 'user-captcha' : '',
    focus() { document.activeElement = this; },
    blur() { if (document.activeElement === this) document.activeElement = null; },
    dispatchEvent(event) {
      if (key !== 'kprq' && event.type === 'input') model[key] = this.value;
      if (key === 'kprq' && event.type === 'keydown' && event.key === 'Enter') {
        model[key] = this.value;
      }
    },
  };
}
class TestEvent {
  constructor(type, options) { this.type = type; Object.assign(this, options); }
}
const window = {};
const calendar = [];
if (layout === 'legacy') {
  const jq = input => ({
    data() { return true; },
    datepicker(action, date) {
      calendar.push(action);
      if (action === 'setDate') {
        input.value = `${date.getFullYear()}${String(date.getMonth()+1).padStart(2,'0')}${String(date.getDate()).padStart(2,'0')}`;
        model.kprq = input.value;
      }
    },
  });
  jq.fn = {datepicker() {}};
  window.jQuery = jq;
}
class TestDate extends Date { static now() { return now; } }
const context = vm.createContext({document, window, Event: TestEvent,
  KeyboardEvent: TestEvent, FocusEvent: TestEvent, Date: TestDate,
  setTimeout(fn, delay) { timers.push({fn, at: now + delay}); },
});
const run = () => vm.runInContext(script, context, {timeout: 1000});
run();
if (layout === 'delayed') {
  assert.deepEqual(model, {});
  timers.push({fn() { mounted = true; }, at: 500});
}
let callbacks = 0;
while (timers.length) {
  assert.ok(++callbacks < 400, 'Polling must stop when the form is absent');
  timers.sort((a, b) => a.at - b.at);
  const timer = timers.shift(); now = timer.at; timer.fn();
}
if (layout === 'missing') {
  assert.deepEqual(model, {});
  assert.equal(helpers.length, 0);
  assert.ok(now <= 30100);
} else {
  const amount = layout === 'legacy' ? 'kjje' : 'kpje';
  assert.deepEqual(model, {fphm: '26957000000168907686', kprq: '20260713', [amount]: '76.32'});
  assert.equal(controls.fpdm.value, '');
  assert.equal(controls.yzm.value, 'user-captcha');
  assert.equal(document.activeElement, controls.yzm);
  if (layout === 'legacy') assert.ok(calendar.includes('hide'));
  controls.fphm.value = 'manually-corrected';
  run();
  assert.equal(timers.length, 0);
  assert.equal(controls.fphm.value, 'manually-corrected');
  assert.equal(helpers.length, 1);
}

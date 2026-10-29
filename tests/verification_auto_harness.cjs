// 用最小的 DOM/XHR 模拟验证自动查验脚本的读图、提交、刷新和结果判断。
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {script} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));

const clicks = {captcha: 0, verify: 0, close: 0};
const timers = [];
const input = (value = '') => ({
  value,
  focus() {},
  blur() {},
  dispatchEvent() {},
});
const form = {
  fphm: input('26957000000168907686'),
  kprq: input('20260713'),
  kjje: input('76.32'),
  yzm: input(''),
};
const image = {
  src: 'data:image/png;base64,' + 'A'.repeat(600),
  getClientRects() { return [1]; },
  dispatchEvent(event) { if (event.type === 'click') clicks.captcha++; },
};
const verify = {
  textContent: '查 验',
  disabled: true,
  getClientRects() { return [1]; },
  click() { clicks.verify++; },
};
let resultDialog = null;
const document = {
  body: {innerText: '请输入验证码图片中蓝色文字'},
  querySelector(selector) {
    if (selector === "img[width='120']") return image;
    const key = /^\.t-form-item__(\w+) input$/.exec(selector)?.[1];
    return key ? form[key] : null;
  },
  getElementById() { return null; },
  querySelectorAll(selector) {
    if (selector === 'button') return [verify];
    if (selector === '.t-dialog') return resultDialog ? [resultDialog] : [];
    if (selector === '.t-dialog__mask, .t-dialog__position') return [];
    return [];
  },
  createElement() { return {style: {}}; },
};
class TestEvent {
  constructor(type, options) { this.type = type; Object.assign(this, options); }
}
const window = {innerText: document.body.innerText};
const fakeXhr = {
  listeners: [],
  addEventListener(name, fn) { if (name === 'load') this.listeners.push(fn); },
  responseURL: '',
  responseText: '',
};
const proto = {
  send() {},
};
const context = vm.createContext({
  document, window, XMLHttpRequest: {prototype: proto},
  Event: TestEvent, KeyboardEvent: TestEvent, FocusEvent: TestEvent, MouseEvent: TestEvent,
  setTimeout(fn, delay) { timers.push({fn, delay}); },
  Date,
});
vm.runInContext(script, context, {timeout: 1000});
assert.ok(window.__tidocAutoCaptcha);

const read = window.__tidocAutoCaptcha.read();
assert.equal(read.ready, true);
assert.equal(read.hint, '请输入验证码图片中蓝色文字');
assert.ok(read.image.startsWith('data:image/png'));
assert.equal(read.checks, 0);

assert.equal(window.__tidocAutoCaptcha.refresh(), true);
assert.equal(clicks.captcha, 1);

const submit = window.__tidocAutoCaptcha.submit('xwu');
assert.equal(submit.submitted, true);
assert.equal(submit.disabled, true);
assert.equal(form.yzm.value, 'xwu');
assert.equal(clicks.verify, 0);
while (timers.length) timers.shift().fn();
verify.disabled = false;
assert.equal(clicks.verify, 0, '按钮仍禁用时不应提交');
window.__tidocAutoCaptcha.submit('xwu');
verify.disabled = false;
while (timers.length) timers.shift().fn();
assert.equal(clicks.verify, 1);

const pushCheck = (data) => {
  fakeXhr.responseURL = 'https://dppt.example/fpcy/fpcyConntroller/v1/queryFpcyxx';
  fakeXhr.responseText = JSON.stringify({Response: {Data: data}});
  proto.send.call(fakeXhr, '');
  fakeXhr.listeners.forEach((fn) => fn());
};
pushCheck({CyjgDm: '97', CyjgMsg: '验证码错误！'});
let status = window.__tidocAutoCaptcha.status();
assert.equal(status.captcha_error, true);
assert.equal(status.checks, 1);

pushCheck({reCode: '00', CyjgDm: '7', CyjgMsg: '经查验，发票信息一致'});
status = window.__tidocAutoCaptcha.status();
assert.equal(status.success, true);
assert.equal(status.message, '经查验，发票信息一致');

resultDialog = {
  innerText: '发票详情',
  getClientRects() { return [1]; },
  querySelectorAll() {
    return [{textContent: '关闭', click() { clicks.close++; }}];
  },
};
assert.equal(window.__tidocAutoCaptcha.closeResult(), true);
assert.equal(clicks.close, 1);

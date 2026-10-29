const assert = require('node:assert/strict');
const vm = require('node:vm');
const {script, layout} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let calls = 0, scans = 0, observer, interval, resolveFonts;
let decodes = 0;
const image = {decode: () => ++decodes === 1 ? Promise.reject(Error('QR src changed')) : Promise.resolve()};
const fonts = new Promise(resolve => { resolveFonts = resolve; });
const button = {textContent: ' 打印 ', getClientRects: () => [1], click: () => { calls++; }};
const result = {
  textContent: '发票详情 查验时间：2026-10-29 发票号码： 26000000000000000001',
  getClientRects: () => layout === 'hidden' ? [] : [1],
  querySelectorAll: selector => selector === 'button' ? [button] : layout === 'late-image' ? [image] : [],
};
if (layout === 'wrong') result.textContent = result.textContent.replace(/01$/, '02');
if (layout === 'incomplete') result.textContent = '发票号码：26000000000000000001';
if (layout === 'long-number') result.textContent += '1';
const document = {
  fonts: {ready: fonts}, documentElement: {},
  querySelectorAll: () => [result], getElementById: () => null,
};
const window = {setInterval: fn => { interval = fn; }, __tidocScanPrintFrames: () => { scans++; }};
const context = vm.createContext({window, document, Promise, Array,
  setTimeout() {}, MutationObserver: class {constructor(fn) {observer = fn;} observe() {}},
});
async function run() {
  vm.runInContext(script, context); vm.runInContext(script, context);
  observer(); interval();
  assert.equal(calls, 0, 'Must wait for fonts and avoid racing observers');
  if (layout === 'closed') result.getClientRects = () => [];
  resolveFonts();
  await new Promise(resolve => setImmediate(resolve));
  observer(); interval();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, ['ready', 'late-image'].includes(layout) ? 1 : 0);
  assert.equal(scans, calls);
}
run().catch(error => { console.error(error); process.exitCode = 1; });

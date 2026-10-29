const assert = require('node:assert/strict'), vm = require('node:vm');
const {script, layout} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
class Node {
  constructor(tag = 'div', text = '') {
    this.tagName = tag.toUpperCase(); this.text = text;
    this.children = []; this.attributes = {}; this.classList = new Set();
  }
  appendChild(node) { this.children.push(node); node.parent = this; return node; }
  replaceChildren() { this.children = []; }
  setAttribute(name, value) { this.attributes[name] = value; }
  remove() { this.parent.children = this.parent.children.filter(node => node !== this); }
}
function makeDocument() {
  const doc = {body: new Node('body'), head: new Node('head'), frames: [], listeners: [],
    documentElement: new Node('html'), title: 'Official tax site',
    createElement: tag => new Node(tag),
    importNode(node) {
      const copy = new Node(node.tagName, node.text);
      copy.id = node.id; copy.href = node.href;
      copy.children = node.children.map(child => doc.importNode(child));
      return copy;
    },
    getElementById(id) {
      const find = node => node.id === id ? node : node.children.map(find).find(Boolean);
      return find(doc.body) || find(doc.head);
    },
    querySelectorAll(selector) {
      if (selector === 'iframe') return doc.frames;
      if (selector === 'link[rel="stylesheet"], style') return doc.head.children;
      return doc.head.children.filter(node => node.attributes['data-tidoc-print-source-style']);
    },
    addEventListener(event, callback, capture) { doc.listeners.push({event, callback, capture}); },
  };
  return doc;
}
const document = makeDocument();
document.body.appendChild(new Node('div', 'Invoice search and CAPTCHA must not print'));
let calls = 0;
const window = {document, setInterval() {}, print() { calls++; }};
const context = vm.createContext({document, window, MutationObserver: class {observe() {}},
  WeakSet, Array});
const run = () => vm.runInContext(script, context, {timeout: 1000});
run(); run();
assert.equal(document.listeners.length, 1, 'Repeated injection must not duplicate handlers');
if (layout === 'legacy') {
  const jq = {fn: {printArea() { throw Error('Legacy iframe print should be replaced'); }}};
  window.jQuery = jq;
  window.__tidocScanPrintFrames();
  jq.fn.printArea.call([new Node('article', 'Verified invoice and check time')]);
} else {
  const child = {document: makeDocument(), print() { throw Error('Subframe native print is unsupported'); }};
  const frame = {tagName: 'IFRAME', contentWindow: child};
  // The frame load is captured before Print.js assembles its body and calls print.
  document.listeners[0].callback({target: frame});
  assert.equal(document.listeners[0].capture, true);
  if (layout === 'navigation') child.document = makeDocument();
  child.document.body.appendChild(new Node('article', 'Verified invoice and check time'));
  const style = new Node('link'); style.href = 'https://inv-veri.chinatax.gov.cn/result.css';
  child.document.head.appendChild(style);
  child.print();
  assert.equal(document.head.children.find(node => node.tagName === 'LINK').href, style.href);
}
assert.equal(calls, 1);
const host = document.getElementById('tidoc-native-print-host');
assert.deepEqual(host.children.map(node => node.text), ['Verified invoice and check time']);
assert.ok(document.body.classList.has('tidoc-native-printing'));
assert.equal(document.title, '查验单-26957000000168907686');
assert.ok(document.getElementById('tidoc-native-print-style').textContent.includes('A4 landscape'));

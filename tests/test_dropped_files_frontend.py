"""拖入文件的前端取路径逻辑，用 Node 直接执行真实的 app.js。"""

from tests.test_selection_performance import run_selection


def test_dropped_files_use_native_paths_without_reading_their_content():
    run_selection(r"""
const calls = [];
globalThis.FileReader = class { readAsDataURL() { calls.push('read'); } };
globalThis.Api = {
  takeDroppedPaths: async (files) => { calls.push(['take', files]); return { paths: ['/stage/a.pdf', '/stage/b.png'] }; },
  saveDroppedFiles: async () => { calls.push('save'); return { paths: [] }; },
};
(async () => {
  const files = [{ name: 'a.pdf', size: 10 }, { name: 'b.png', size: 20 }];
  const paths = await droppedFilesToPaths(files, { dropped: true });
  assert.deepEqual(paths, ['/stage/a.pdf', '/stage/b.png']);
  assert.deepEqual(calls, [['take', [{ name: 'a.pdf', size: 10 }, { name: 'b.png', size: 20 }]]]);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_dropped_files_fall_back_to_reading_content_when_the_native_layer_has_no_path():
    run_selection(r"""
const calls = [];
globalThis.FileReader = class {
  readAsDataURL(file) { calls.push('read ' + file.name); this.result = 'data:;base64,AAAA'; this.onload(); }
};
globalThis.Api = {
  takeDroppedPaths: async () => ({ paths: null }),
  saveDroppedFiles: async (payload) => { calls.push(['save', payload.map((item) => item.name)]); return { paths: ['/stage/a.pdf'] }; },
};
(async () => {
  const paths = await droppedFilesToPaths([{ name: 'a.pdf', size: 10 }], { dropped: true });
  assert.deepEqual(paths, ['/stage/a.pdf']);
  assert.deepEqual(calls, ['read a.pdf', ['save', ['a.pdf']]]);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_pasted_files_never_wait_for_a_native_path():
    run_selection(r"""
let asked = false;
globalThis.FileReader = class {
  readAsDataURL() { this.result = 'data:;base64,AAAA'; this.onload(); }
};
globalThis.Api = {
  takeDroppedPaths: async () => { asked = true; return { paths: null }; },
  saveDroppedFiles: async () => ({ paths: ['/stage/clip.png'] }),
};
(async () => {
  const paths = await droppedFilesToPaths([{ name: 'clip.png', size: 5 }]);
  assert.deepEqual(paths, ['/stage/clip.png']);
  assert.equal(asked, false);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_files_that_already_carry_a_path_skip_both_routes():
    run_selection(r"""
globalThis.Api = {
  takeDroppedPaths: async () => { throw new Error('should not ask'); },
  saveDroppedFiles: async () => { throw new Error('should not save'); },
};
(async () => {
  const paths = await droppedFilesToPaths([{ name: 'a.pdf', path: '/x/a.pdf' }], { dropped: true });
  assert.deepEqual(paths, ['/x/a.pdf']);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")

"""Keyboard navigation of the entry list, using the real frontend in Node."""

from tests.test_selection_performance import run_selection


def test_tab_walks_the_cards_one_by_one_and_leaves_the_list_at_both_ends():
    run_selection(r"""
const focused = [];
const stops = State.entries.slice(0, 3).map((entry) => {
  const card = entryCardNodes.get(entry.id);
  card.classList.toggle('entry-card', true);
  card.focus = () => focused.push(entry.id);
  card.scrollIntoView = () => {};
  card.closest = (selector) => (selector.includes('.entry-card') ? card : null);
  return card;
});
document.querySelector = (selector) => (selector === '#entryList'
  ? { querySelectorAll: () => stops } : makeNode());
const press = (card, shiftKey) => {
  const ev = { key: 'Tab', shiftKey, target: card, prevented: false, preventDefault() { this.prevented = true; } };
  stepEntryTab(ev);
  return ev.prevented;
};

assert.equal(press(stops[0], false), true);
assert.deepEqual(focused, ['entry-1']);
assert.equal(press(stops[1], false), true);
assert.deepEqual(focused, ['entry-1', 'entry-2']);
assert.equal(press(stops[2], true), true);
assert.deepEqual(focused, ['entry-1', 'entry-2', 'entry-1']);

// 到头不拦截：Tab 离开最后一张、Shift+Tab 离开第一张，由浏览器接着走。
assert.equal(press(stops[2], false), false);
assert.equal(press(stops[0], true), false);
assert.equal(focused.length, 3);
""")


def test_tab_with_a_modifier_is_left_to_the_browser():
    run_selection(r"""
const card = entryCardNodes.get('entry-0');
card.classList.toggle('entry-card', true);
card.closest = () => card;
document.querySelector = () => ({ querySelectorAll: () => [card, entryCardNodes.get('entry-1')] });
for (const modifier of ['ctrlKey', 'metaKey', 'altKey']) {
  const ev = { key: 'Tab', [modifier]: true, target: card, preventDefault() { assert.fail('should not intercept'); } };
  stepEntryTab(ev);
}
""")

"""Regression guards for shared frontend UI primitives and CSS maintainability."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "tidoc" / "web"


def test_ui_primitives_floating_flip_shift_origin_and_busy():
    script = """
    const fs = require('node:fs');
    const vm = require('node:vm');

    const styles = {};
    const floating = {
      offsetWidth: 200,
      offsetHeight: 160,
      dataset: {},
      style: {
        setProperty(k, v) { styles[k] = v; },
        get left() { return styles.left; },
        set left(v) { styles.left = v; },
        get top() { return styles.top; },
        set top(v) { styles.top = v; },
        get zIndex() { return styles.zIndex; },
        set zIndex(v) { styles.zIndex = v; },
      },
      getBoundingClientRect() { return { width: 200, height: 160 }; },
    };

    const sandbox = {
      window: { innerWidth: 800, innerHeight: 600 },
      document: { querySelectorAll: () => [] },
      getComputedStyle: () => ({ zIndex: '55' }),
    };
    sandbox.globalThis = sandbox;
    vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);

    const { TidocIcons, TidocUI } = sandbox.window;
    // 1. Normal bottom-start placement
    const r1 = TidocUI.positionFloating(floating, { x: 100, y: 100, width: 120, height: 34 }, {
      placement: 'bottom-start', offset: 6, padding: 8, zIndexBase: 400,
    });
    // 2. Flip upward near bottom edge & shift inward near right edge
    const r2 = TidocUI.positionFloating(floating, { x: 700, y: 520, width: 80, height: 34 }, {
      placement: 'bottom-start', offset: 6, padding: 8,
    });
    // 3. Busy button guard
    const attrs = {};
    const classes = new Set();
    const btn = {
      textContent: '保存',
      disabled: false,
      dataset: {},
      classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), has: (c) => classes.has(c) },
      setAttribute: (k, v) => { attrs[k] = v; },
      removeAttribute: (k) => { delete attrs[k]; },
    };
    TidocUI.setBusy(btn, true, '保存中…');
    const busySnap = { text: btn.textContent, disabled: btn.disabled, ariaBusy: attrs['aria-busy'], cls: classes.has('is-busy') };
    TidocUI.setBusy(btn, false);
    const idleSnap = { text: btn.textContent, disabled: btn.disabled, ariaBusy: attrs['aria-busy'] || null, cls: classes.has('is-busy') };

    console.log(JSON.stringify({
      iconCheck: TidocIcons.icon('check', 14),
      r1,
      r2,
      originAfterR2: styles['--transform-origin'],
      zIndex: styles.zIndex,
      busySnap,
      idleSnap,
    }));
    """
    result = subprocess.run(
        ["node", "-e", script, str(WEB / "ui-primitives.js")],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(result.stdout)
    assert 'viewBox="0 0 24 24"' in payload["iconCheck"]
    assert 'stroke-width="1.75"' in payload["iconCheck"]
    assert payload["r1"]["side"] == "bottom"
    assert payload["r1"]["x"] == 100
    assert payload["r1"]["y"] == 140
    assert payload["r2"]["side"] == "top"
    assert payload["r2"]["flipped"] is True
    assert payload["r2"]["x"] == 592  # 800 - 8 - 200
    assert payload["r2"]["y"] == 354  # 520 - 6 - 160
    assert payload["originAfterR2"].endswith("100%")
    assert payload["zIndex"] == "400"
    assert payload["busySnap"] == {"text": "保存中…", "disabled": True, "ariaBusy": "true", "cls": True}
    assert payload["idleSnap"] == {"text": "保存", "disabled": False, "ariaBusy": None, "cls": False}


def test_styles_avoid_shorthand_transition_all_and_define_core_tokens():
    css = (WEB / "styles.css").read_text("utf-8")
    shorthand = re.findall(r"transition:\s*(?:all|\.?[0-9])", css)
    assert shorthand == [], f"Avoid `transition: all` or bare duration shorthand: {shorthand}"
    for token in ("--ease-out:", "--control-h:", "--shadow-control:", "--shadow-popover:", "--tooltip-bg:"):
        assert token in css, f"Missing design token {token}"
    # Base input/checkbox resets must stay zero-specificity (:where) so contextual inputs (.search / .adv-range) never get overridden.
    assert ':where(select, textarea, input:not([type="checkbox"]):not([type="radio"]):not([type="range"]))' in css
    assert ':where(input[type="checkbox"])' in css


def test_index_loads_ui_primitives_before_select_and_app():
    html = (WEB / "index.html").read_text("utf-8")
    assert html.index("ui-primitives.js") < html.index("select.js") < html.index("app.js")

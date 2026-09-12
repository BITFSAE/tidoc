/* ---------------------------------------------------------------------------
 * tidoc 自绘下拉（把原生 <select> 的弹出列表换成应用自己的浮层）
 *
 * 为什么需要：原生 select 关着的时候可以用 appearance:none 画成自己的风格，
 * 但展开后的列表由 WebView2 / WKWebView 用系统控件绘制，CSS 控不了圆角、阴影、
 * 间距、悬浮态和勾选态。这里保留原生 select 提供数据与 API，另画一层触发器 +
 * 浮层列表。
 *
 * 设计要点：
 * - 原生 select 留在 DOM 里（在 .select-field 内绝对定位、透明、不可点），因此
 *   既有代码里的 sel.value = x / .value 读取 / .disabled = true / innerHTML 重建
 *   option 全部照旧可用，不需要改动调用点。
 * - 选项列表每次打开时从 select 实时读取，所以重建 option 之后不需要调用方做任何同步；
 *   触发器上显示的文字由两条路径保持最新：给 select.value 赋值走原型访问器同步，
 *   直接改 option 的 selected 属性由 select 自身的 MutationObserver 兜底。
 * - 只监听 select 自身的 class / disabled / 子树变化刷新触发器外观，不用全局
 *   MutationObserver，避免观察者回调里改 DOM 触发自激循环。
 * - display:none 藏起来的下拉保持原生，不做包装。
 * ------------------------------------------------------------------------- */
(function () {
  'use strict';

  const CHECK_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">' +
    '<path d="m5 12.5 4.6 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="2.1" ' +
    'stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const CHEVRON_ICON = '<svg class="select-chevron" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">' +
    '<path d="m3 4.5 3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.4" ' +
    'stroke-linecap="round" stroke-linejoin="round"/></svg>';

  const WIDTH_CAP = 420;        // 浮层最大宽度（长抬头不撑满整屏）
  const MIN_MENU_WIDTH = 112;
  const MENU_Z = 400;           // 基础层级，实际使用时会压过当前最高的弹窗遮罩
  const EDGE = 8;               // 距视口边缘留白
  const GAP = 6;                // 触发器与浮层间距

  const records = [];           // 已增强的下拉记录
  const bySelect = new WeakMap();
  let openRecord = null;
  let prefix = '';
  let prefixTimer = 0;

  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const decode = (text) => {
    const holder = document.createElement('textarea');
    holder.innerHTML = text;
    return holder.value;
  };

  // ------------------------------------------------------------------ 触发器
  function optionsOf(select) {
    return [...select.options].map((option) => ({
      value: option.value,
      // option.textContent 对 HTML 实体（如 &amp;）不解码，和原生显示不一致
      label: decode(option.innerHTML).replace(/\s+/g, ' ').trim(),
    }));
  }

  function currentLabel(record) {
    const option = record.select.selectedIndex >= 0 ? record.select.options[record.select.selectedIndex] : null;
    if (!option) {
      return record.options[0] ? record.options[0].label : '—';
    }
    return decode(option.innerHTML).replace(/\s+/g, ' ').trim() || '—';
  }

  function syncRecord(record) {
    if (record.destroyed) return;
    record.options = optionsOf(record.select);
    const hidden = record.select.classList.contains('hidden') || record.select.hidden;
    record.field.classList.toggle('hidden', hidden);
    record.trigger.disabled = record.select.disabled;
    record.field.classList.toggle('is-disabled', record.select.disabled);
    if (hidden && openRecord === record) closeMenu();
    const label = currentLabel(record);
    if (record.labelNode.textContent !== label) record.labelNode.textContent = label;
    const full = record.options.find((item) => item.value === record.select.value);
    if (full) record.trigger.setAttribute('aria-label', full.label); else record.trigger.removeAttribute('aria-label');
  }

  function buildTrigger(record) {
    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'select-trigger';
    trigger.innerHTML = '<span class="select-value" data-select-value>—</span>' + CHEVRON_ICON;
    const select = record.select;
    // 上下文类（.attach-type-select / .settings-select 一类）交给包装盒，
    // 让既有的「.容器 > select」布局规则继续生效。
    // 但这些类里往往同时写着边框、底色、内边距，包装盒必须把它们清干净，
    // 否则包装盒自己会变成一个大号控件盒，和触发器叠成两层边框。
    select.classList.forEach((name) => {
      if (name !== 'hidden') record.field.classList.add(name);
    });
    resetFieldPaint(record.field);
    trigger.disabled = select.disabled;
    if (select.style.textAlign) trigger.style.justifyContent = select.style.textAlign;
    if (select.style.textAlignLast && select.style.textAlignLast !== 'auto') {
      trigger.style.justifyContent = select.style.textAlignLast;
    }
    return trigger;
  }

  // 包装盒只负责盒子，不负责画
  function resetFieldPaint(field) {
    field.style.border = '0';
    field.style.outline = '0';
    field.style.padding = '0';
    field.style.margin = '0';
    field.style.background = 'none';
    field.style.boxShadow = 'none';
    field.style.color = 'inherit';
    field.style.font = 'inherit';
    field.style.overflow = 'visible';
  }

  // 只负责「宽度对齐」和方向性样式：宽度要跟随原生控件的 max-width（如工具条 chip 的
  // 124px 截断阈值），其余一律交给样式表。
  //
  // 高度和内边距坚决不复制，理由（都是实测踩出来的）：
  //   - 包装后原生控件是 absolute，盒子由包装盒决定；折叠面板里还会被 flex 拉到 80~100px，
  //     照抄就会把假高度写死进触发器，再反过来撑高整行（自我强化的反馈环）。
  //   - 高度本来就该由上下文 CSS 决定：工具条 chip 30px、高级筛选 34px、设置行 36px。
  // 颜色同理，全部走 CSS 语义变量，避免留下一份过期的主题快照。
  function measureTrigger(record) {
    const styles = getComputedStyle(record.select);
    const isBlock = record.display !== 'inline-block' && record.display !== 'inline-flex';
    record.trigger.style.width = isBlock ? '' : 'auto';
    record.trigger.style.maxWidth = '';
    if (!isBlock) record.trigger.style.flex = '0 0 auto';
    const customMax = styles.maxWidth && styles.maxWidth !== 'none' && !styles.maxWidth.endsWith('%');
    if (customMax) record.trigger.style.maxWidth = styles.maxWidth;
  }

  function enhance(select) {
    if (!select || select.tagName !== 'SELECT' || select.multiple) return null;
    if (bySelect.get(select) || select.closest('.select-field')) return null;  // 已经接管过
    const display = getComputedStyle(select).display;
    if (display === 'none') return null;  // 用 display:none 藏起来的下拉保持原生

    const field = document.createElement('span');
    field.className = 'select-field';
    select.parentNode.insertBefore(field, select);
    field.appendChild(select);

    const record = {
      select,
      field,
      trigger: null,
      labelNode: null,
      options: [],
      observer: null,
      display,          // 拆包前的显示方式，恢复时用
      destroyed: false,
    };
    // 包装盒只负责「占位」：display 跟随原生控件，拆包后外层布局与原来一致，
    // 宽度一律不设（设了也会被 flex-grow 拉满，或在行内上下文里塌成内容宽度）。
    // 触发器负责真正的盒子：块级上下文撑满包装盒，行内上下文收缩到内容宽度。
    if (display === 'block') {
      field.classList.add('is-block');
      field.style.display = 'block';
    } else if (display === 'inline-flex' || display === 'flex') {
      field.style.display = 'inline-flex';
    } else {
      field.style.display = 'inline-block';
    }
    const trigger = buildTrigger(record);
    record.trigger = trigger;
    record.labelNode = trigger.querySelector('[data-select-value]');
    field.appendChild(trigger);

    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    if (select.getAttribute('aria-label')) trigger.setAttribute('aria-label', select.getAttribute('aria-label'));

    trigger.addEventListener('click', (event) => {
      event.preventDefault();
      if (record.select.disabled) return;
      if (openRecord === record) closeMenu(); else openMenu(record, -1);
    });
    trigger.addEventListener('keydown', (event) => onTriggerKeydown(event, record));
    // 原生 select 仍会在 label / 键盘下被激活，直接拦掉它自己的弹出列表
    select.addEventListener('click', (event) => { event.preventDefault(); trigger.focus(); });
    select.addEventListener('focus', () => trigger.focus());

    record.observer = new MutationObserver(() => syncRecord(record));
    record.observer.observe(select, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['class', 'disabled', 'hidden', 'selected', 'value'],
    });

    records.push(record);
    bySelect.set(select, record);
    syncRecord(record);
    measureTrigger(record);
    requestAnimationFrame(() => measureTrigger(record));
    return record;
  }

  function destroy(record) {
    record.destroyed = true;
    record.observer.disconnect();
    record.field.parentNode?.insertBefore(record.select, record.field);
    record.field.remove();
    const index = records.indexOf(record);
    if (index >= 0) records.splice(index, 1);
    bySelect.delete(record.select);
    if (openRecord === record) closeMenu();
  }

  // -------------------------------------------------------------------- 浮层
  function menuEl() {
    let node = document.getElementById('selectMenu');
    if (!node) {
      node = document.createElement('div');
      node.id = 'selectMenu';
      node.className = 'select-menu';
      node.setAttribute('role', 'listbox');
      node.hidden = true;
      document.body.appendChild(node);
      node.addEventListener('pointerdown', (event) => event.preventDefault());
      node.addEventListener('click', (event) => {
        const option = event.target.closest('.select-menu-option');
        if (!option || option.getAttribute('aria-disabled') === 'true') return;
        commit(Number(option.dataset.index));
      });
      // 鼠标移到哪一项，键盘高亮就跟到哪一项（与原生列表一致）
      node.addEventListener('mouseover', (event) => {
        const option = event.target.closest('.select-menu-option');
        if (!option || !openRecord) return;
        highlight(openRecord, Number(option.dataset.index));
      });
    }
    return node;
  }

  function renderMenu(record) {
    const node = menuEl();
    record.options = optionsOf(record.select);
    const selected = record.select.value;
    node.innerHTML = record.options.map((option, index) => {
      const active = option.value === selected;
      return `<div class="select-menu-option${active ? ' is-selected' : ''}" role="option"` +
        ` id="selectMenuOption${index}" data-index="${index}" data-value="${encodeURIComponent(option.value)}"` +
        ` aria-selected="${active ? 'true' : 'false'}" title="${option.label.replace(/"/g, '&quot;')}">` +
        `<span class="select-menu-label">${option.label}</span>` +
        `<span class="select-menu-check" aria-hidden="true">${active ? CHECK_ICON : ''}</span></div>`;
    }).join('');
    return node;
  }

  function openMenu(record, fromIndex) {
    if (record.select.disabled) return;
    if (openRecord && openRecord !== record) closeMenu();
    const node = renderMenu(record);
    if (!record.options.length) return;
    openRecord = record;
    node.hidden = false;
    node.setAttribute('aria-label', record.select.getAttribute('aria-label') || '选项');
    record.trigger.setAttribute('aria-expanded', 'true');
    record.trigger.setAttribute('aria-controls', node.id);
    positionMenu(record);
    let index = typeof fromIndex === 'number' && fromIndex >= 0
      ? fromIndex
      : Math.max(0, record.options.findIndex((option) => option.value === record.select.value));
    highlight(record, index, { scroll: true });
    document.addEventListener('pointerdown', onDocumentPointerDown, true);
    window.addEventListener('resize', onViewportChange, true);
    window.addEventListener('scroll', onViewportChange, true);
  }

  // restoreFocus 只在用户主动选中 / 按 Esc 时用；列表被滚动、控件被隐藏等
  // 被动关闭不抢焦点，否则会打断用户当前的操作位置
  function closeMenu({ restoreFocus = false } = {}) {
    if (!openRecord) return;
    const record = openRecord;
    openRecord = null;
    const node = menuEl();
    node.hidden = true;
    node.innerHTML = '';
    record.trigger.setAttribute('aria-expanded', 'false');
    record.trigger.removeAttribute('aria-controls');
    record.trigger.removeAttribute('aria-activedescendant');
    document.removeEventListener('pointerdown', onDocumentPointerDown, true);
    window.removeEventListener('resize', onViewportChange, true);
    window.removeEventListener('scroll', onViewportChange, true);
    if (restoreFocus && record.trigger.isConnected) record.trigger.focus();
  }

  function onDocumentPointerDown(event) {
    if (!openRecord) return;
    if (openRecord.field.contains(event.target) || menuEl().contains(event.target)) return;
    closeMenu();
  }

  function onViewportChange(event) {
    if (!openRecord) return;
    // 浮层内部自身滚动不该关掉它
    if (event && event.target instanceof Node && menuEl().contains(event.target)) return;
    if (event && event.target instanceof Node && openRecord.trigger.contains(event.target)) return;
    closeMenu();
  }

  function positionMenu(record) {
    const node = menuEl();
    const anchor = record.trigger.getBoundingClientRect();
    const fieldBox = record.field.getBoundingClientRect();
    // 宽度要够把选项完整显示出来：列表项被省略号截断会看不出选的是哪个抬头
    const widest = record.options.reduce((max, option) => Math.max(max, textWidth(option.label)), 0);
    const width = clamp(Math.max(anchor.width, widest + 48), MIN_MENU_WIDTH, WIDTH_CAP);
    node.style.width = width + 'px';
    // 弹窗遮罩是全屏 fixed 且 z-index 按弹窗层数递增，浮层必须压过当前最高的那个，
    // 否则展开的下拉会被遮罩盖住（工具条与弹窗里都会出现）
    const scrimTop = [...document.querySelectorAll('.modal-mask')]
      .reduce((max, el) => Math.max(max, parseInt(getComputedStyle(el).zIndex, 10) || 0), 0);
    node.style.zIndex = String(Math.max(MENU_Z, scrimTop + 2));
    // 先定宽量高，再决定向上还是向下展开
    const height = node.offsetHeight;
    const below = window.innerHeight - anchor.bottom - EDGE;
    const above = anchor.top - EDGE;
    const dropUp = height > below && above > below;
    const top = dropUp ? anchor.top - GAP - height : anchor.bottom + GAP;
    // 左边缘跟包装盒对齐：行内 chip 的触发器因为图标和留白偏右，跟着触发器走会明显错位
    const left = clamp(Math.min(anchor.left, fieldBox.left), EDGE, Math.max(EDGE, window.innerWidth - EDGE - width));
    node.classList.toggle('drop-up', dropUp);
    node.style.top = Math.round(clamp(top, EDGE, Math.max(EDGE, window.innerHeight - EDGE - height))) + 'px';
    node.style.left = Math.round(left) + 'px';
  }

  const measureCanvas = document.createElement('canvas');
  function textWidth(text) {
    const context = measureCanvas.getContext('2d');
    if (!context) return 0;
    const font = getComputedStyle(document.body).font;
    if (font) context.font = font;  // WKWebView 可能返回空串，退回 canvas 默认字体
    return context.measureText(text).width;
  }

  function highlight(record, index, { scroll = false } = {}) {
    const node = menuEl();
    const items = [...node.querySelectorAll('.select-menu-option')];
    if (!items.length) return;
    const next = clamp(index, 0, items.length - 1);
    items.forEach((item, i) => {
      const active = i === next;
      item.classList.toggle('is-active', active);
      if (active && scroll) item.scrollIntoView({ block: 'nearest' });
    });
    record.activeIndex = next;
    record.trigger.setAttribute('aria-activedescendant', items[next].id);
  }

  function commit(index) {
    const record = openRecord;
    if (!record) return;
    const target = Number(index);
    if (!Number.isInteger(target)) return;
    const option = record.options[target];
    if (!option) return;
    const changed = record.select.value !== option.value;
    record.select.value = option.value;
    syncRecord(record);
    closeMenu({ restoreFocus: true });
    if (changed) record.select.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // ------------------------------------------------------------------ 键盘
  function onTriggerKeydown(event, record) {
    const open = openRecord === record;
    const items = open ? [...menuEl().querySelectorAll('.select-menu-option')] : [];
    switch (event.key) {
      case 'Escape':
        if (open) { event.preventDefault(); event.stopPropagation(); closeMenu({ restoreFocus: true }); }
        return;
      case 'Tab':
        if (open) closeMenu();
        return;
      case 'Enter':
      case ' ':
      case 'Spacebar':
        event.preventDefault();
        if (open) commit(record.activeIndex ?? 0); else openMenu(record, -1);
        return;
      case 'ArrowDown':
      case 'ArrowUp': {
        event.preventDefault();
        if (!open) { openMenu(record, -1); return; }
        if (!items.length) return;   // 浮层已开但已无选项：让下一次打开重建
        const step = event.key === 'ArrowDown' ? 1 : -1;
        const next = (record.activeIndex ?? 0) + step;
        highlight(record, next < 0 ? items.length - 1 : (next >= items.length ? 0 : next), { scroll: true });
        return;
      }
      case 'Home':
        if (open) { event.preventDefault(); highlight(record, 0, { scroll: true }); }
        return;
      case 'End':
        if (open) { event.preventDefault(); highlight(record, items.length - 1, { scroll: true }); }
        return;
      case 'Backspace':
        if (open) { event.preventDefault(); prefix = prefix.slice(0, -1); }
        return;
      default:
        break;
    }
    // 原生 select 的「输入首字母跳转」不能退化
    if (open && event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      clearTimeout(prefixTimer);
      prefix += event.key;
      prefixTimer = setTimeout(() => { prefix = ''; }, 700);
      const lower = prefix.toLowerCase();
      const single = prefix.length === 1;
      const from = single ? (record.activeIndex ?? 0) + 1 : 0;
      for (let offset = 0; offset < items.length; offset += 1) {
        const index = (from + offset) % items.length;
        const label = record.options[index]?.label.toLowerCase() || '';
        if (label.startsWith(lower)) { highlight(record, index, { scroll: true }); return; }
      }
    }
  }

  // ------------------------------------------------------------------ 对外
  function enhanceSelects(root = document) {
    const scope = root.nodeType === Node.ELEMENT_NODE ? root : document;
    const list = [];
    if (scope.tagName === 'SELECT') list.push(scope);
    if (scope.querySelectorAll) list.push(...scope.querySelectorAll('select'));
    return list.filter((node) => !bySelect.get(node) && node.tagName === 'SELECT' && !node.multiple)
      .map((node) => enhance(node)).filter(Boolean);
  }

  // 页面里既有代码会直接 sel.value = x（换抬头、回填偏好），而给 select.value 赋值
  // 不会触发 change / MutationObserver，触发器上的文字就会停在旧值。这里在原型上
  // 包一层访问器，赋值后同步一次已接管的下拉。
  // 注：原型被改会把 minified 代码里的 `let value = …` 变成语法错误，所以整段
  // try/catch 兜底；即便这段失效，每次展开列表都会重新读取 select 的当前值。
  function patchValueSetter() {
    try {
      const descriptor = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
      if (!descriptor || !descriptor.set || descriptor.set.length > 1) return;
      const originalSet = descriptor.set;
      Object.defineProperty(HTMLSelectElement.prototype, 'value', {
        configurable: true,
        enumerable: descriptor.enumerable,
        get: descriptor.get,
        set(next) {
          const before = this.value;
          originalSet.call(this, next);
          if (this.value === before) return;
          const record = bySelect.get(this);
          if (record) syncRecord(record);
        },
      });
    } catch (e) {
      // 原型不可改时静默降级：展开列表时仍会按当前值高亮与显示
    }
  }

  // 弹窗关闭、详情列表整段重绘后，被换掉的原生 select 连同包装一起失效，顺手回收
  function prune() {
    records.slice().forEach((record) => { if (!record.select.isConnected) destroy(record); });
  }

  // 窗口缩放后重新对齐触发器的尺寸（不重排 DOM，幂等）
  window.addEventListener('resize', () => {
    prune();
    records.forEach((record) => measureTrigger(record));
    if (openRecord) positionMenu(openRecord);
  });

  patchValueSetter();
  window.TidocSelect = { enhanceSelects, prune, close: closeMenu };
})();

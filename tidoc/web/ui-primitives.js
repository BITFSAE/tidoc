/* ---------------------------------------------------------------------------
 * tidoc 共享 UI 原语（离线、零依赖、兼容 Windows WebView2 与 macOS WKWebView）
 *
 * 1. TidocIcons：统一 24x24 Lucide 几何网格与 1.75 线宽图标集，消除各模块重复 SVG。
 * 2. TidocUI.positionFloating：吸收 Floating UI 的 offset / flip / shift 模型，
 *    同时输出 --transform-origin，使下拉、菜单、悬浮提示从触发点自然展开。
 * 3. TidocUI.setBusy：防双击重复提交的按钮忙碌态管理。
 * ------------------------------------------------------------------------- */
(function (global) {
  'use strict';

  const ICON_PATHS = {
    close: '<path d="M18 6 6 18M6 6l12 12"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    chevronDown: '<path d="m6 9 6 6 6-6"/>',
    moreHorizontal: '<circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="5" cy="12" r="1" fill="currentColor" stroke="none"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    pencil: '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/>',
    note: '<path d="M16 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V8Z"/><path d="M15 3v4a2 2 0 0 0 2 2h4"/><path d="M8 13h8M8 17h5"/>',
    pdf: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 13H8v4M8 15h1.5a1 1 0 0 0 0-2H8M14 13v4h1a2 2 0 0 0 0-4z"/>',
    xml: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="m10 13-2 2 2 2M14 13l2 2-2 2"/>',
    image: '<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
    inspect: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/><path d="m8.5 11 1.7 1.7 3.3-3.4"/>',
    search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    lock: '<rect width="14" height="10" x="5" y="11" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    folderOutput: '<path d="M2 7.5V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H20a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-1.5"/><path d="M2 13h10M5 10l-3 3 3 3"/>',
    tag: '<path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r="1" fill="currentColor"/>',
    wallet: '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>',
    upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/>',
    cloud: '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>',
    trash: '<path d="M3 6h18M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2"/>',
    archive: '<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',
    rotateCcw: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
    eye: '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/>',
    checkCircle: '<circle cx="12" cy="12" r="9"/><path d="m9 12 2 2 4-4"/>',
    filter: '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>',
    doc: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 13h4M10 17h4"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14.5 14.5 0 0 0 0 18 14.5 14.5 0 0 0 0-18"/>',
    bilibili: '<path d="m8 3 3 3M16 3l-3 3"/><rect x="3" y="6" width="18" height="14" rx="3"/><path d="M8 12v2M16 12v2M9 17h6"/>',
    github: '__FILL__<path d="M12 2C6.48 2 2 6.58 2 12.23c0 4.52 2.87 8.35 6.84 9.71.5.1.68-.22.68-.49 0-.24-.01-1.05-.01-1.9-2.78.62-3.37-1.21-3.37-1.21-.45-1.18-1.11-1.49-1.11-1.49-.91-.64.07-.63.07-.63 1 .08 1.53 1.06 1.53 1.06.9 1.57 2.35 1.12 2.92.85.09-.66.35-1.12.64-1.37-2.22-.26-4.56-1.14-4.56-5.06 0-1.12.39-2.03 1.03-2.75-.1-.26-.45-1.3.1-2.71 0 0 .84-.28 2.75 1.05A9.3 9.3 0 0 1 12 6.95a9.3 9.3 0 0 1 2.5.35c1.91-1.33 2.75-1.05 2.75-1.05.55 1.41.2 2.45.1 2.71.64.72 1.03 1.63 1.03 2.75 0 3.93-2.34 4.8-4.57 5.05.36.32.68.94.68 1.9 0 1.37-.01 2.48-.01 2.82 0 .27.18.59.69.49A10.24 10.24 0 0 0 22 12.23C22 6.58 17.52 2 12 2Z"/>',
  };

  function icon(name, size = 15, cls = '') {
    const raw = ICON_PATHS[name];
    if (!raw) return '';
    const classAttr = cls ? ` class="${cls}"` : '';
    if (raw.startsWith('__FILL__')) {
      return `<svg${classAttr} viewBox="0 0 24 24" width="${size}" height="${size}" fill="currentColor" aria-hidden="true">${raw.slice(8)}</svg>`;
    }
    return `<svg${classAttr} viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${raw}</svg>`;
  }

  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

  function topModalZ(baseZ = 120) {
    if (typeof document === 'undefined' || typeof getComputedStyle !== 'function') return baseZ;
    let maxZ = 0;
    document.querySelectorAll('.modal-mask').forEach((node) => {
      const z = parseInt(getComputedStyle(node).zIndex, 10) || 0;
      if (z > maxZ) maxZ = z;
    });
    return Math.max(baseZ, maxZ + 2);
  }

  function toRect(anchor) {
    if (anchor && typeof anchor.getBoundingClientRect === 'function') {
      return anchor.getBoundingClientRect();
    }
    const x = Number(anchor?.x ?? anchor?.left ?? 0);
    const y = Number(anchor?.y ?? anchor?.top ?? 0);
    const width = Number(anchor?.width ?? 0);
    const height = Number(anchor?.height ?? 0);
    return { left: x, top: y, right: x + width, bottom: y + height, width, height };
  }

  /**
   * 统一浮层定位（offset + flip + shift + transform-origin）。
   * placement 支持：bottom-start | bottom | top-start | top | right | left
   */
  function positionFloating(floatingEl, anchor, options = {}) {
    if (!floatingEl || typeof window === 'undefined') return { x: 0, y: 0, side: 'bottom', flipped: false };
    const {
      placement = 'bottom-start',
      offset = 6,
      padding = 8,
      zIndexBase = 0,
    } = options;

    if (zIndexBase > 0) {
      floatingEl.style.zIndex = String(topModalZ(zIndexBase));
    }

    const ref = toRect(anchor);
    const width = floatingEl.offsetWidth || floatingEl.getBoundingClientRect().width;
    const height = floatingEl.offsetHeight || floatingEl.getBoundingClientRect().height;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    const [baseSide, align = 'center'] = placement.split('-');
    let side = baseSide;
    let flipped = false;

    const spaceBelow = vh - ref.bottom - padding;
    const spaceAbove = ref.top - padding;
    const spaceRight = vw - ref.right - padding;
    const spaceLeft = ref.left - padding;

    if (side === 'bottom' && height + offset > spaceBelow && spaceAbove > spaceBelow) {
      side = 'top';
      flipped = true;
    } else if (side === 'top' && height + offset > spaceAbove && spaceBelow > spaceAbove) {
      side = 'bottom';
      flipped = true;
    } else if (side === 'right' && width + offset > spaceRight && spaceLeft > spaceRight) {
      side = 'left';
      flipped = true;
    } else if (side === 'left' && width + offset > spaceLeft && spaceRight > spaceLeft) {
      side = 'right';
      flipped = true;
    }

    let x = 0;
    let y = 0;
    if (side === 'bottom' || side === 'top') {
      y = side === 'bottom' ? ref.bottom + offset : ref.top - offset - height;
      if (align === 'start') x = ref.left;
      else if (align === 'end') x = ref.right - width;
      else x = ref.left + (ref.width - width) / 2;
    } else {
      x = side === 'right' ? ref.right + offset : ref.left - offset - width;
      if (align === 'start') y = ref.top;
      else if (align === 'end') y = ref.bottom - height;
      else y = ref.top + (ref.height - height) / 2;
    }

    const clampedX = Math.round(clamp(x, padding, Math.max(padding, vw - padding - width)));
    const clampedY = Math.round(clamp(y, padding, Math.max(padding, vh - padding - height)));

    let originX = '50%';
    let originY = '50%';
    if (side === 'bottom') {
      originY = '0%';
      originX = ref.width > 0
        ? `${Math.round(clamp(ref.left + ref.width / 2 - clampedX, 12, Math.max(12, width - 12)))}px`
        : `${Math.round(clamp(ref.left - clampedX, 0, Math.max(0, width)))}px`;
    } else if (side === 'top') {
      originY = '100%';
      originX = ref.width > 0
        ? `${Math.round(clamp(ref.left + ref.width / 2 - clampedX, 12, Math.max(12, width - 12)))}px`
        : `${Math.round(clamp(ref.left - clampedX, 0, Math.max(0, width)))}px`;
    } else if (side === 'right') {
      originX = '0%';
      originY = `${Math.round(clamp(ref.top + ref.height / 2 - clampedY, 8, Math.max(8, height - 8)))}px`;
    } else if (side === 'left') {
      originX = '100%';
      originY = `${Math.round(clamp(ref.top + ref.height / 2 - clampedY, 8, Math.max(8, height - 8)))}px`;
    }

    floatingEl.style.left = `${clampedX}px`;
    floatingEl.style.top = `${clampedY}px`;
    floatingEl.style.setProperty('--transform-origin', `${originX} ${originY}`);
    floatingEl.dataset.side = side;

    return { x: clampedX, y: clampedY, side, flipped };
  }

  /**
   * 按钮防重入忙碌态；保留原文本，结束自动恢复。
   */
  function setBusy(button, busy, busyLabel) {
    if (!button) return;
    if (busy) {
      if (button.dataset.uiIdleText == null) button.dataset.uiIdleText = button.textContent || '';
      button.disabled = true;
      button.classList.add('is-busy');
      button.setAttribute('aria-busy', 'true');
      if (busyLabel) button.textContent = busyLabel;
    } else {
      button.disabled = false;
      button.classList.remove('is-busy');
      button.removeAttribute('aria-busy');
      if (button.dataset.uiIdleText != null) {
        button.textContent = button.dataset.uiIdleText;
        delete button.dataset.uiIdleText;
      }
    }
  }

  global.TidocIcons = { icon, paths: ICON_PATHS };
  global.TidocUI = { positionFloating, topModalZ, setBusy };
})(typeof window !== 'undefined' ? window : globalThis);

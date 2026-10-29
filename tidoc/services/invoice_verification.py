"""全国增值税发票查验平台的本地辅助逻辑。

这里不代替税务平台发起查验，也不识别验证码。Tidoc 只从已有发票材料中
准备表单值、打开官方网页，并把用户已查到的结果保存或归入当前条目。
"""

from __future__ import annotations

import json
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

TAX_VERIFICATION_URL = "https://inv-veri.chinatax.gov.cn/index.html"
# 两端都由页面内容自己缩放（CSS zoom），原生打印缩放保持 100%，版式才一致。
# 官网的发票版心是固定宽度：macOS 原生缩放只缩小排好版的页面，内容会贴在纸张左侧；
# 原来 macOS 的「原生 80%」叠加 WebKit 按纸宽收缩，实际约为原尺寸的 72%，
# 直接用 80% 会让带两行明细的专用发票在 A4 横向上挤出第二页，因此取 70%。
VERIFICATION_PRINT_ZOOM = 0.7


def build_verification_info(entry: dict) -> dict:
    """准备数电发票查验所需的号码、日期和价税合计。"""
    info = {
        "invoice_no": str(entry.get("invoice_no") or "").strip(),
        "invoice_date": _compact_date(entry.get("invoice_date")),
        "verification_value": _money_text(entry.get("total")),
        "verification_value_label": "价税合计",
    }
    info["complete"] = bool(
        info["invoice_no"] and info["invoice_date"] and info["verification_value"]
    )
    return info


def make_prefill_script(info: dict, *, auto_save: bool = False) -> str:
    """生成只填写官网表单、不提交查验的脚本。"""
    hint = (
        '验证码通常不区分大小写。填写后点击“查验”，显示发票详情后会自动保存查验单到当前条目，请稍候。'
        if auto_save else
        '验证码通常不区分大小写。查验成功后点击官网“打印”，另存为 PDF 后请等待约 1–2 秒，软件识别处理完成后会自动归入条目。'
    )
    payload = json.dumps(
        {
            "fphm": str(info.get("invoice_no") or ""),
            "kprq": _compact_date(info.get("invoice_date")),
            "kjje": str(info.get("verification_value") or ""),
        },
        ensure_ascii=False,
    )
    return f"""
(() => {{
  const values = {payload};
  // 新版官网用 TDesign 表单，输入框没有旧版的 id；金额字段改为 kpje。
  const findInput = (id) => document.getElementById(id) || document.querySelector(
    '.t-form-item__' + (id === 'kjje' ? 'kpje' : id) + ' input'
  );
  const dispatch = (input, names) => names.forEach((name) => {{
    const EventType = name === 'keyup' ? KeyboardEvent : Event;
    input.dispatchEvent(new EventType(name, {{ bubbles: true }}));
  }});
  const commitValue = (id, value) => {{
    const input = findInput(id);
    if (!input || !value) return;
    input.focus();
    input.value = value;
    dispatch(input, ['input', 'keyup', 'change']);
    input.blur();
    input.dispatchEvent(new FocusEvent('focusout', {{ bubbles: true }}));
  }};
  const commitDate = (value) => {{
    const input = findInput('kprq');
    if (!input || !value) return;
    const jq = window.jQuery;
    const match = /^([0-9]{{4}})([0-9]{{2}})([0-9]{{2}})$/.exec(value);
    if (jq?.fn?.datepicker && jq(input).data('datepicker') && match) {{
      input.focus();
      jq(input).datepicker('setDate', new Date(
        Number(match[1]), Number(match[2]) - 1, Number(match[3])
      ));
      jq(input).datepicker('hide');
      dispatch(input, ['input', 'keyup', 'change']);
      input.blur();
      input.dispatchEvent(new FocusEvent('focusout', {{ bubbles: true }}));
      return;
    }}
    commitValue('kprq', value);
    // TDesign 日期框在 Enter 时确认输入，并把值写回表单。
    input.dispatchEvent(new KeyboardEvent('keydown', {{
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
    }}));
    input.blur();
  }};
  if (window.__tidocVerificationPrefillStarted) return;
  window.__tidocVerificationPrefillStarted = true;
  const deadline = Date.now() + 30000;
  const fillWhenReady = () => {{
    // 页面加载完成时，新版官网的表单可能仍未挂载。
    if (!['fphm', 'kprq', 'kjje', 'yzm'].every((id) => findInput(id))) {{
      if (Date.now() < deadline) setTimeout(fillWhenReady, 100);
      return;
    }}
    commitValue('fphm', values.fphm);
    setTimeout(() => {{
      commitDate(values.kprq);
    }}, 180);
    setTimeout(() => {{
      commitValue('kjje', values.kjje);
    }}, 420);
    setTimeout(() => {{
      const dateInput = findInput('kprq');
      if (window.jQuery?.fn?.datepicker && dateInput) {{
        window.jQuery(dateInput).datepicker('hide');
      }}
      document.activeElement?.blur?.();
      const captcha = findInput('yzm');
      if (captcha) captcha.focus();
    }}, 700);
    if (!document.getElementById('tidoc-verification-helper')) {{
      const helper = document.createElement('div');
      helper.id = 'tidoc-verification-helper';
      helper.textContent = {json.dumps(hint, ensure_ascii=False)};
      helper.style.cssText = [
        'position:fixed', 'left:18px', 'bottom:18px', 'z-index:2147483647',
        'max-width:360px', 'padding:10px 14px', 'border-radius:9px',
        'background:#173a70', 'color:#fff', 'font:13px/1.55 sans-serif',
        'box-shadow:0 8px 24px rgba(0,0,0,.22)'
      ].join(';');
      helper.onclick = () => helper.remove();
      helper.title = '点击关闭';
      document.body.appendChild(helper);
    }}
  }};
  fillWhenReady();
}})();
"""


def make_auto_captcha_script() -> str:
    """自动查验用的页面助手：读验证码、提交、刷新、读取查验结果。

    只在当前页面内操作，不代替官网生成签名或请求。查验响应通过拦截
    XMLHttpRequest 的 load 事件读取，和官网自己看到的响应一致。
    """
    return r"""
(() => {
  if (window.__tidocAutoCaptcha) return;
  if (!window.__tidocCheckCaptureInstalled) {
    window.__tidocCheckCaptureInstalled = true;
    window.__tidocChecks = [];
    const send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function(body) {
      this.addEventListener('load', () => {
        try {
          if (String(this.responseURL || '').includes('queryFpcyxx')) {
            window.__tidocChecks.push({at: Date.now(), body: this.responseText});
            if (window.__tidocChecks.length > 20) window.__tidocChecks.shift();
          }
        } catch (_) {}
      });
      return send.apply(this, arguments);
    };
  }
  const findInput = (id) => document.getElementById(id) || document.querySelector(
    '.t-form-item__' + (id === 'kjje' ? 'kpje' : id) + ' input'
  );
  const visible = (node) => !!node && node.getClientRects().length > 0;
  const captchaImage = () => document.querySelector("img[width='120']");
  const hintText = () => {
    const match = document.body.innerText.match(/请输入验证码[^\n]*/);
    return match ? match[0].trim() : '';
  };
  const commit = (input, value) => {
    input.focus();
    input.value = value;
    ['input', 'keyup', 'change'].forEach((name) => {
      const Type = name === 'keyup' ? KeyboardEvent : Event;
      input.dispatchEvent(new Type(name, {bubbles: true}));
    });
    input.blur();
    input.dispatchEvent(new FocusEvent('focusout', {bubbles: true}));
  };
  const parseCheck = () => {
    const checks = window.__tidocChecks || [];
    const last = checks[checks.length - 1];
    if (!last) return null;
    try {
      const data = JSON.parse(last.body)?.Response?.Data || {};
      return {
        code: String(data.CyjgDm || data.reCode || ''),
        message: String(data.CyjgMsg || ''),
        invoice: String(data.DzfpFpxxVO?.Fphm || ''),
      };
    } catch (_) {
      return {code: '', message: '', invoice: ''};
    }
  };
  window.__tidocAutoCaptcha = {
    read() {
      const image = captchaImage();
      const src = image ? image.src : '';
      return {
        ready: !!(findInput('yzm') && findInput('fphm')),
        image: /^data:image\//.test(src) && src.length > 500 ? src : '',
        hint: hintText(),
        checks: (window.__tidocChecks || []).length,
      };
    },
    refresh() {
      const image = captchaImage();
      if (!image) return false;
      image.dispatchEvent(new MouseEvent('click', {bubbles: true}));
      return true;
    },
    submit(answer) {
      const input = findInput('yzm');
      if (!input) return {submitted: false, disabled: true};
      commit(input, String(answer || ''));
      const button = Array.from(document.querySelectorAll('button'))
        .find((node) => visible(node) && node.textContent.replace(/\s/g, '') === '查验');
      if (!button) return {submitted: false, disabled: true};
      // 表单校验是异步的：填完后按钮要到下一帧才会启用。
      const click = () => { if (!button.disabled) button.click(); };
      const disabled = button.disabled;
      if (disabled) setTimeout(click, 150);
      else click();
      return {submitted: true, disabled};
    },
    status() {
      const text = document.body.innerText;
      const dialogs = Array.from(document.querySelectorAll('.t-dialog')).filter(visible);
      const last = parseCheck();
      const success = dialogs.some((dialog) =>
        /发票详情|查验时间/.test(dialog.innerText));
      return {
        success: success || (last && (last.code === '7' || last.code === '00')),
        captcha_error: !!(last && last.code === '97') || /验证码错误/.test(text),
        frequent: /访问过于频繁/.test(text),
        message: last ? last.message : '',
        invoice: last ? last.invoice : '',
        checks: (window.__tidocChecks || []).length,
        dialog: dialogs.length ? dialogs[dialogs.length - 1].innerText.slice(0, 160) : '',
      };
    },
    closeResult() {
      const dialogs = Array.from(document.querySelectorAll('.t-dialog')).filter(visible);
      for (const dialog of dialogs) {
        const button = Array.from(dialog.querySelectorAll('button'))
          .find((node) => ['关闭', '确定'].includes(node.textContent.trim()));
        if (button) button.click();
      }
      document.querySelectorAll('.t-dialog__mask, .t-dialog__position')
        .forEach((node) => node.remove());
      return true;
    },
  };
})();
"""


def verification_print_title(invoice_no: str = "") -> str:
    """生成系统打印任务使用的稳定文件名，不包含扩展名。"""
    cleaned = re.sub(r"[^0-9A-Za-z_-]", "", str(invoice_no or ""))
    return f"查验单-{cleaned}"


def make_auto_save_script(invoice_no: str) -> str:
    """仅在本次发票的结果弹窗出现后触发官网打印，不提交表单。"""
    script = """
(() => {
  if (window.__tidocAutoSaveStarted) return;
  window.__tidocAutoSaveStarted = true;
  const invoiceNo = __INVOICE_NO__;
  let pending = false;
  let saved = false;
  let renderResult = null;
  let renderDeadline = 0;
  const visible = (node) => node.getClientRects().length > 0;
  const findResult = () => Array.from(document.querySelectorAll('.t-dialog'))
    .find((dialog) => visible(dialog)
      && /查验时间/.test(dialog.textContent)
      && /发票号码[：:]?\\s*([0-9]{20})(?![0-9])/.exec(dialog.textContent)?.[1] === invoiceNo);
  const scan = async () => {
    if (pending || saved) return;
    const result = findResult();
    const button = result && Array.from(result.querySelectorAll('button'))
      .find((node) => visible(node) && node.textContent.trim() === '打印' && !node.disabled);
    if (!button) return;
    if (renderResult !== result) {
      renderResult = result;
      renderDeadline = Date.now() + 10000;
    }
    pending = true;
    // 等二维码、样式和字体完成；超时保留官网的手动打印入口。
    try {
      await Promise.race([
        Promise.all([
          document.fonts.ready,
          ...Array.from(result.querySelectorAll('img')).map((img) => img.decode()),
        ]),
        new Promise((_, reject) => setTimeout(() => reject(Error('timeout')),
          Math.max(0, renderDeadline - Date.now()))),
      ]);
      if (findResult() !== result || !visible(button)) return;
      window.__tidocScanPrintFrames?.();
      saved = true;
      button.click();
    } catch (_) {
      // 官网二维码的 src 会在结果弹窗出现后更新，第一次 decode 可能被中断。
      // 在同一次准备期限内重试，不把这个短暂状态当成永久失败。
      if (Date.now() < renderDeadline) return;
      saved = true;
      const helper = document.getElementById('tidoc-verification-helper');
      if (helper) helper.textContent = '查验结果尚未加载完整，请稍后点击官网“打印”保存查验单。';
    } finally {
      pending = false;
    }
  };
  new MutationObserver(scan).observe(document.documentElement, {childList: true, subtree: true});
  window.setInterval(scan, 800);
  scan();
})();
"""
    return script.replace('__INVOICE_NO__', json.dumps(str(invoice_no)))


def make_print_compatibility_script(
    invoice_no: str = "",
    *,
    print_message: list[str] | None = None,
) -> str:
    """把官网 PrintArea 选定内容提升到顶层 WebView，供原生打印使用。

    兼容旧版 PrintArea 和新版 Print.js 的同源 iframe，把官网已准备的打印
    内容复制到顶层文档，再调用原生打印，保留官网自己的材料排版。

    打印内容按 VERIFICATION_PRINT_ZOOM 缩放并水平居中；Windows 与 macOS 的原生缩放都保持 100%。
    """
    print_title = json.dumps(verification_print_title(invoice_no), ensure_ascii=False)
    script = """
(() => {
  if (window.__tidocPrintCompatibilityStarted) {
    window.__tidocScanPrintFrames?.();
    return;
  }
  window.__tidocPrintCompatibilityStarted = true;

  const topWindow = window;
  const topDocument = document;
  const bodyClass = 'tidoc-native-printing';
  const hostId = 'tidoc-native-print-host';
  const printTitle = __TIDOC_PRINT_TITLE__;

  const prepareTopLevelPrint = (selection, sourceWindow) => {
    let host = topDocument.getElementById(hostId);
    if (!host) {
      host = topDocument.createElement('div');
      host.id = hostId;
      topDocument.body.appendChild(host);
    }
    host.replaceChildren();
    Array.from(selection).forEach((node) => {
      host.appendChild(topDocument.importNode(node, true));
    });

    topDocument.querySelectorAll('[data-tidoc-print-source-style]')
      .forEach((node) => node.remove());
    sourceWindow.document
      .querySelectorAll('link[rel="stylesheet"], style')
      .forEach((sourceStyle) => {
        const copy = topDocument.importNode(sourceStyle, true);
        copy.setAttribute('data-tidoc-print-source-style', '1');
        if (copy.tagName === 'LINK') copy.href = sourceStyle.href;
        topDocument.head.appendChild(copy);
      });

    if (!topDocument.getElementById('tidoc-native-print-style')) {
      const style = topDocument.createElement('style');
      style.id = 'tidoc-native-print-style';
      style.textContent = `
        #${hostId} { display: none; }
        @media print {
          @page { size: A4 landscape; margin: 8mm; }
          html, body.${bodyClass}, body.${bodyClass} #${hostId} {
            background: #fff !important;
          }
          body.${bodyClass} > :not(#${hostId}) { display: none !important; }
          body.${bodyClass} #${hostId} {
            display: block !important;
            width: max-content !important;
            margin: 0 auto !important;
            zoom: __TIDOC_PRINT_ZOOM__ !important;
          }
        }
      `;
      topDocument.head.appendChild(style);
    }

    topDocument.body.classList.add(bodyClass);
    if (printTitle !== '查验单-') topDocument.title = printTitle;
    __TIDOC_NATIVE_PRINT__
  };

  const observedDocuments = new WeakSet();
  const installInWindow = (targetWindow) => {
    // Print.js 在隐藏 iframe 的 load 回调中组装内容并调用子窗口 print。
    // 捕获阶段先装上转发，调用时才读取已组装好的内容，避免漏掉首次打印。
    const targetDocument = targetWindow.document;
    if (!observedDocuments.has(targetDocument)) {
      observedDocuments.add(targetDocument);
      targetDocument.addEventListener('load', (event) => {
        const frame = event.target;
        if (frame.tagName === 'IFRAME') {
          try { if (frame.contentWindow) scan(frame.contentWindow); } catch (_) {}
        }
      }, true);
    }
    if (targetWindow !== topWindow && !targetWindow.print.__tidocNativePrint) {
      const nativeFramePrint = () => {
        prepareTopLevelPrint(targetWindow.document.body.children, targetWindow);
      };
      nativeFramePrint.__tidocNativePrint = true;
      targetWindow.print = nativeFramePrint;
    }
    const jq = targetWindow.jQuery;
    if (!jq || !jq.fn || typeof jq.fn.printArea !== 'function') return false;
    if (jq.fn.printArea.__tidocNativePrint) return true;

    const nativePrintArea = function() {
      prepareTopLevelPrint(this, targetWindow);
      return this;
    };
    nativePrintArea.__tidocNativePrint = true;
    jq.fn.printArea = nativePrintArea;
    return true;
  };

  const scan = (targetWindow = topWindow) => {
    try {
      installInWindow(targetWindow);
      targetWindow.document.querySelectorAll('iframe').forEach((frame) => {
        try {
          if (frame.contentWindow) scan(frame.contentWindow);
        } catch (_) {}
      });
    } catch (_) {}
  };

  topWindow.__tidocScanPrintFrames = scan;
  scan();
  new MutationObserver(() => scan()).observe(topDocument.documentElement, {
    childList: true,
    subtree: true,
  });
  topWindow.setInterval(scan, 800);
})();
"""
    native_print = (
        f"topWindow.chrome.webview.postMessage({json.dumps(print_message)});"
        if print_message else "topWindow.print();"
    )
    return (script.replace("__TIDOC_PRINT_TITLE__", print_title)
            .replace("__TIDOC_PRINT_ZOOM__", f"{VERIFICATION_PRINT_ZOOM:g}")
            .replace("__TIDOC_NATIVE_PRINT__", native_print))


def default_watch_directories(home: Path | None = None) -> list[Path]:
    root = home or Path.home()
    candidates = [root / "Downloads", root / "Desktop", root / "Documents"]
    return [path for path in candidates if path.is_dir()]


def snapshot_pdfs(directories: list[Path]) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for directory in directories:
        for path in directory.glob("*.pdf"):
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def changed_pdf_candidates(
    directories: list[Path],
    before: dict[str, tuple[int, int]],
    started_ns: int,
) -> list[Path]:
    """返回会话开始后新增或覆盖的 PDF，最新文件排在前面。"""
    out: list[tuple[int, Path]] = []
    slack_ns = 2_000_000_000
    for directory in directories:
        for path in directory.glob("*.pdf"):
            try:
                resolved = path.resolve()
                stat = resolved.stat()
            except OSError:
                continue
            previous = before.get(str(resolved))
            current = (stat.st_mtime_ns, stat.st_size)
            if current == previous:
                continue
            if previous is None and stat.st_mtime_ns < started_ns - slack_ns:
                continue
            out.append((stat.st_mtime_ns, resolved))
    return [path for _, path in sorted(out, reverse=True)]


def new_session_snapshot(home: Path | None = None) -> dict:
    directories = default_watch_directories(home)
    return {
        "watch_directories": directories,
        "before": snapshot_pdfs(directories),
        "started_ns": time.time_ns(),
    }


def _compact_date(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[:8] if len(digits) >= 8 else digits


def _money_text(value: str) -> str:
    text = str(value or "").replace(",", "").replace("，", "").strip()
    if not text:
        return ""
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return ""
    return f"{amount:.2f}"

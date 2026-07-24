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


def make_prefill_script(info: dict) -> str:
    """生成只填写官网表单、不提交查验的脚本。"""
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
  const setValue = (id, value) => {{
    const input = document.getElementById(id);
    if (!input || !value) return;
    input.focus();
    input.value = value;
    ['input', 'change', 'keyup', 'blur'].forEach((name) =>
      input.dispatchEvent(new Event(name, {{ bubbles: true }})));
  }};
  setValue('fphm', values.fphm);
  setValue('kprq', values.kprq);
  setTimeout(() => {{
    setValue('kjje', values.kjje);
    const captcha = document.getElementById('yzm');
    if (captcha) {{ captcha.value = ''; captcha.focus(); }}
  }}, 250);
  if (!document.getElementById('tidoc-verification-helper')) {{
    const helper = document.createElement('div');
    helper.id = 'tidoc-verification-helper';
    helper.textContent = '验证码通常不区分大小写。看到查验明细后，请回到 tidoc 保存到条目。';
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
}})();
"""


def make_print_compatibility_script() -> str:
    """把官网 PrintArea 选定内容提升到顶层 WebView，供保存或打印使用。

    查验结果自身也在 iframe 中，脚本会持续发现同源 frame、替换其中 PrintArea
    的出口，并把官网选定内容复制到顶层文档；普通调用仍进入系统打印。
    """
    return """
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

  const prepareTopLevelPrint = (selection, sourceWindow) => {
    let host = topDocument.getElementById(hostId);
    if (!host) {
      host = topDocument.createElement('div');
      host.id = hostId;
      topDocument.body.appendChild(host);
    }
    host.replaceChildren();
    selection.each(function() {
      host.appendChild(topDocument.importNode(this, true));
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
        body.tidoc-native-exporting > :not(#${hostId}) { display: none !important; }
        body.tidoc-native-exporting #${hostId} {
          display: block !important;
          width: 1120px !important;
          margin: 0 !important;
        }
        @media print {
          @page { size: landscape; margin: 8mm; }
          body.${bodyClass} > :not(#${hostId}) { display: none !important; }
          body.${bodyClass} #${hostId} {
            display: block !important;
            width: 100% !important;
            margin: 0 !important;
          }
        }
      `;
      topDocument.head.appendChild(style);
    }

    topDocument.body.classList.add(bodyClass);
    topWindow.__tidocPreparedPrintHost = true;
    if (!topWindow.__tidocSuppressPrint) topWindow.print();
  };

  const installInWindow = (targetWindow) => {
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


def make_trigger_print_script(*, suppress_print: bool = False) -> str:
    """调用当前结果的官网打印逻辑；可只准备内容而不弹出打印面板。"""
    script = """
(() => {
  window.__tidocScanPrintFrames?.();
  window.__tidocPreparedPrintHost = false;
  window.__tidocSuppressPrint = __TIDOC_SUPPRESS_PRINT__;
  const visit = (targetWindow) => {
    try {
      const button = [...targetWindow.document.querySelectorAll(
        'button, input[type="button"], input[type="submit"], a'
      )].find((node) => (
        (node.textContent || node.value || '').replace(/\\s/g, '') === '打印'
      ));
      if (button) {
        button.click();
        return true;
      }
      for (const frame of targetWindow.document.querySelectorAll('iframe')) {
        try {
          if (frame.contentWindow && visit(frame.contentWindow)) return true;
        } catch (_) {}
      }
    } catch (_) {}
    return false;
  };
  const triggered = visit(window);
  window.__tidocSuppressPrint = false;
  const host = document.getElementById('tidoc-native-print-host');
  const ready = Boolean(triggered && window.__tidocPreparedPrintHost && host);
  if (ready && __TIDOC_SUPPRESS_PRINT__) {
    document.body.classList.add('tidoc-native-exporting');
    const width = Math.ceil(Math.max(1120, host.scrollWidth));
    const height = Math.ceil(Math.max(1, host.scrollHeight));
    return { triggered, ready, width, height };
  }
  return { triggered, ready };
})();
"""
    return script.replace(
        "__TIDOC_SUPPRESS_PRINT__", "true" if suppress_print else "false"
    )


def make_finish_pdf_export_script() -> str:
    """恢复直接导出 PDF 前临时切换的页面显示状态。"""
    return """
(() => {
  document.body.classList.remove('tidoc-native-exporting');
  const host = document.getElementById('tidoc-native-print-host');
  if (host) host.style.removeProperty('width');
})();
"""


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

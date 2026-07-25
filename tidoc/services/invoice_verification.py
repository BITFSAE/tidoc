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
  const dispatch = (input, names) => names.forEach((name) => {{
    const EventType = name === 'keyup' ? KeyboardEvent : Event;
    input.dispatchEvent(new EventType(name, {{ bubbles: true }}));
  }});
  const commitValue = (id, value) => {{
    const input = document.getElementById(id);
    if (!input || !value) return;
    input.focus();
    input.value = value;
    dispatch(input, ['input', 'keyup', 'change']);
    input.blur();
    input.dispatchEvent(new FocusEvent('focusout', {{ bubbles: true }}));
  }};
  const commitDate = (value) => {{
    const input = document.getElementById('kprq');
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
  }};
  commitValue('fphm', values.fphm);
  setTimeout(() => {{
    commitDate(values.kprq);
  }}, 180);
  setTimeout(() => {{
    commitValue('kjje', values.kjje);
  }}, 420);
  setTimeout(() => {{
    const dateInput = document.getElementById('kprq');
    if (window.jQuery?.fn?.datepicker && dateInput) {{
      window.jQuery(dateInput).datepicker('hide');
    }}
    document.activeElement?.blur?.();
    const captcha = document.getElementById('yzm');
    if (captcha) {{ captcha.value = ''; captcha.focus(); }}
  }}, 700);
  if (!document.getElementById('tidoc-verification-helper')) {{
    const helper = document.createElement('div');
    helper.id = 'tidoc-verification-helper';
    helper.textContent = '验证码通常不区分大小写。查验成功后点击官网“打印”，另存为 PDF 后请等待约 1–2 秒，软件识别处理完成后会自动归入条目。';
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


def verification_print_title(invoice_no: str = "") -> str:
    """生成系统打印任务使用的稳定文件名，不包含扩展名。"""
    cleaned = re.sub(r"[^0-9A-Za-z_-]", "", str(invoice_no or ""))
    return f"查验单-{cleaned}"


def make_print_compatibility_script(invoice_no: str = "") -> str:
    """把官网 PrintArea 选定内容提升到顶层 WebView，供原生打印使用。

    查验结果自身也在 iframe 中，脚本会持续发现同源 frame、替换其中 PrintArea
    的出口，并把官网选定内容复制到顶层文档；普通调用仍进入系统打印。
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
        @media print {
          @page { size: A4 landscape; margin: 8mm; }
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
    if (printTitle !== '查验单-') topDocument.title = printTitle;
    topWindow.print();
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
    return script.replace("__TIDOC_PRINT_TITLE__", print_title)


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

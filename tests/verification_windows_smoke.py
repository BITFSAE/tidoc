"""手动运行的真实 WebView2 测试：python tests/verification_windows_smoke.py。

使用虚构数据，不联网；验证官网式 iframe 打印、异步 PDF、缩放与多页保留。
"""

import json
from pathlib import Path
import sys
import tempfile
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform != 'win32':
    raise SystemExit('This smoke test requires Windows and WebView2.')

import webview
from pypdf import PdfReader
from tidoc.api_verification import InvoiceVerificationApi
from tidoc.services.inspection_pdf import read_inspection_pdf
from tidoc.services.invoice_verification import make_auto_save_script, make_print_compatibility_script

NUMBER = '26000000000000000001'
root = webview.create_window('Windows PDF smoke test', html='Preparing local test', hidden=True)
results = []


def run():
    try:
        api = InvoiceVerificationApi()
        api._install_verification_landscape_print()
        for name, rows in [('compact', 1), ('multipage', 90)]:
            with tempfile.TemporaryDirectory(prefix='tidoc-webview-smoke-') as directory:
                output = Path(directory) / '查验单.pdf'
                session = {'id': name, 'auto_save_path': output, 'window_closed': False}
                html = '''<html><body><div>Search form should never print</div>
                <div class="t-dialog">发票号码：NUMBER 查验时间：2026-10-29
                <button onclick="const frame=document.createElement('iframe');
                frame.onload=()=>{frame.contentDocument.body.innerHTML=document.getElementById('material').innerHTML;frame.contentWindow.print()};
                document.body.appendChild(frame)">打印</button>
                <div id="material"><article style="font:16px sans-serif;width:950px">
                <img id="qr" width="10" height="10">
                <p>当日查验次数：第2次　查验时间：2026-10-29 15:00:00</p>
                <p>发票号码：NUMBER</p><div style="height:620px;border:1px solid #777">Invoice table</div>
                ROWS<p>开票人：测试人员　END-OF-MATERIAL</p></article></div></div>
                <script>setTimeout(()=>{const canvas=document.createElement('canvas');
                canvas.width=canvas.height=10;canvas.getContext('2d').fillRect(0,0,10,10);
                document.getElementById('qr').src=canvas.toDataURL()},700)</script>
                </body></html>'''.replace('NUMBER', NUMBER).replace('ROWS', ''.join(f'<p>Material row {i}</p>' for i in range(rows)))
                window = webview.create_window('tidoc · 发票查验', html=html)
                window._tidoc_verification_session = session
                window._tidoc_save_pdf = lambda browser, s=session: api._save_windows_verification_pdf(browser, s)
                assert window.events.loaded.wait(30)
                window.evaluate_js(make_print_compatibility_script(NUMBER, print_message=['_tidocVerificationPrint', name]))
                window.evaluate_js(make_auto_save_script(NUMBER))
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if output.exists() and not session.get('auto_save_printing'):
                        break
                    if session.get('auto_save_rejected'):
                        raise RuntimeError(session)
                    time.sleep(.1)
                reader = PdfReader(output)
                text = '\n'.join(page.extract_text() or '' for page in reader.pages)
                assert 'END-OF-MATERIAL' in text
                assert 'Search form' not in text
                assert read_inspection_pdf(output)['invoice_no'] == NUMBER
                assert read_inspection_pdf(output)['count'] == 2
                assert all(float(page.mediabox.width) > float(page.mediabox.height) for page in reader.pages)
                assert (len(reader.pages) == 1) if name == 'compact' else (len(reader.pages) > 1)
                results.append({'case': name, 'pages': len(reader.pages), 'body_confirmed': True, 'last_line_preserved': True})
                window.destroy()
    except Exception:
        results.append({'error': traceback.format_exc()})
    finally:
        report = Path(__file__).resolve().parents[1] / 'output/windows-verification/native-smoke.json'
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(results, indent=2), encoding='utf-8')
        root.destroy()


webview.start(run, gui='edgechromium')
print(json.dumps(results))
sys.exit(1 if any('error' in result for result in results) else 0)

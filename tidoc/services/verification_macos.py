"""macOS 查验单打印的异步完成通知。仅在 macOS 自动保存时导入。"""

import Foundation
import objc


class TidocVerificationPrintDelegate(Foundation.NSObject):
    @objc.typedSelector(b"v@:@" + objc._C_NSBOOL + b"^v")
    def printOperation_didRun_contextInfo_(self, operation, success, context):
        callback = self.callback
        self.callback = None
        callback(bool(success))


def run_pdf_print(operation, native_window, callback):
    """WKWebView 必须异步准备分页，并保留 delegate 直到输出结束。"""
    delegate = TidocVerificationPrintDelegate.alloc().init()
    delegate.callback = callback
    operation.runOperationModalForWindow_delegate_didRunSelector_contextInfo_(
        native_window, delegate, "printOperation:didRun:contextInfo:", None
    )
    return delegate

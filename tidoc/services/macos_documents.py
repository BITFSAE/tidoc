"""Receive Finder document-open events without blocking Cocoa's main thread."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable


def install_document_handler(enqueue: Callable[[str], None]) -> None:
    """Extend pywebview's delegate before its first native window is created.

    Finder sends Apple events to an already-running app rather than launching a
    second process with argv. Keep pywebview's termination/restoration behavior,
    and hand paths to the existing worker that opens the import preview.
    """
    if sys.platform != "darwin":
        return

    from AppKit import NSApplicationDelegateReplyFailure, NSApplicationDelegateReplySuccess
    from webview.platforms.cocoa import BrowserView

    def receive(filename) -> bool:
        try:
            path = Path(str(filename))
            if path.suffix.lower() != ".tidoc" or not path.is_file():
                return False
            enqueue(str(path))
            return True
        except OSError:
            return False

    class TidocDocumentAppDelegate(BrowserView.AppDelegate):
        def application_openFile_(self, _app, filename):
            return receive(filename)

        def application_openFiles_(self, app, filenames):
            # Consume every file, even when an earlier one is unsupported.
            accepted = [receive(filename) for filename in filenames]
            app.replyToOpenOrPrint_(
                NSApplicationDelegateReplySuccess
                if accepted and all(accepted)
                else NSApplicationDelegateReplyFailure
            )

    BrowserView.AppDelegate = TidocDocumentAppDelegate

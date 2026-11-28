"""Disconnect stays responsive while a real HTTP response is stalled."""

import http.server
import threading

from tidoc.services.hub_sync import HubClient, HubError


def test_close_interrupts_a_stalled_response_without_waiting_for_request_lock():
    entered = threading.Event()
    release = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            entered.set()
            release.wait(5)
            try:
                self.wfile.write(b"{}")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    client = HubClient(f"http://127.0.0.1:{server.server_port}")

    def request():
        try:
            client.info()
        except HubError:
            pass

    pending = threading.Thread(target=request, daemon=True)
    closer = threading.Thread(target=client.close, daemon=True)
    try:
        pending.start()
        assert entered.wait(3)
        closer.start()
        closer.join(timeout=1)
        assert not closer.is_alive(), "disconnect waited behind an in-flight response"
        pending.join(timeout=1)
        assert not pending.is_alive(), "closed connection did not interrupt the response"
    finally:
        release.set()
        closer.join(timeout=2)
        pending.join(timeout=2)
        client.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)

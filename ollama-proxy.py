#!/usr/bin/env python3
"""Threaded HTTP proxy: rewrites Host header so Ollama doesn't 403."""
import http.server, http.client, socketserver

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 11434
LISTEN_PORT   = 11435

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def proxy(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else None
        hdrs   = {k: v for k, v in self.headers.items()
                  if k.lower() not in ("host", "connection", "transfer-encoding")}
        try:
            conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=600)
            conn.request(self.command, self.path, body=body, headers=hdrs)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding",):
                    self.send_header(k, v)
            self.end_headers()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as e:
            try:
                self.send_error(502, str(e))
            except Exception:
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = proxy

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

if __name__ == "__main__":
    server = ThreadedHTTPServer(("127.0.0.1", LISTEN_PORT), ProxyHandler)
    print(f"Threaded proxy on :{LISTEN_PORT} -> {UPSTREAM_HOST}:{UPSTREAM_PORT}", flush=True)
    server.serve_forever()

"""Finto Telegram: registra i messaggi inviati e risponde a getUpdates con una chat."""
import json, re, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
LOG = sys.argv[2]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _out(self, obj, code=200):
        b = json.dumps(obj).encode(); self.send_response(code); self.end_headers(); self.wfile.write(b)
    def _go(self):
        m = re.fullmatch(r"/bot([^/]+)/(\w+)", self.path)
        if not m or m.group(1) != "123:TOKEN":
            return self._out({"ok": False, "description": "Unauthorized"}, 401)
        if m.group(2) == "getUpdates":
            return self._out({"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 4242, "first_name": "Roby"}}}]})
        if m.group(2) == "sendMessage":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with open(LOG, "a") as f: f.write(json.dumps(body) + "\n")
            return self._out({"ok": True, "result": {"message_id": 1}})
        self._out({"ok": False, "description": "Not Found"}, 404)
    do_GET = do_POST = _go
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()

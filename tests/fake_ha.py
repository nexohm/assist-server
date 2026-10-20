"""Finto Home Assistant + Supervisor per i test."""
import base64
import hashlib
import json
import os
import struct
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

now = datetime.now(timezone.utc)
STATES = [
    {"entity_id": "light.cucina", "state": "on", "attributes": {"friendly_name": "Luce cucina"}},
    {"entity_id": "light.giardino", "state": "unavailable", "attributes": {"friendly_name": "Luce giardino"}},
    {"entity_id": "cover.soggiorno", "state": "unavailable", "attributes": {"friendly_name": "Tapparella soggiorno"}},
    {"entity_id": "sensor.vecchio", "state": "unavailable", "attributes": {"restored": True}},
    {"entity_id": "sensor.porta_batteria", "state": "12", "attributes": {"device_class": "battery", "friendly_name": "Sensore porta batteria"}},
    {"entity_id": "sensor.termo_batteria", "state": "85", "attributes": {"device_class": "battery"}},
    {"entity_id": "update.home_assistant_core_update", "state": "on", "attributes": {"friendly_name": "Home Assistant Core", "installed_version": "2026.10.1", "latest_version": "2026.10.2"}},
    {"entity_id": "automation.luci_sera", "state": "off", "attributes": {"friendly_name": "Luci sera"}},
    {"entity_id": "button.riavvia", "state": "unavailable", "attributes": {}},
]
ROUTES = {
    "/core/api/states": STATES,
    # Come una casa reale con rete locale "Automatico" e Home Assistant Cloud:
    # nella configurazione gli indirizzi sono vuoti, li dà solo il websocket (network/url).
    "/core/api/config": {"version": "2026.10.1", "location_name": "Casa Bianchi",
                         "internal_url": None, "external_url": None},
    "/core/info": {"result": "ok", "data": {"version": "2026.10.1", "port": 8123, "ssl": False}},
    "/addons/self/info": {"result": "ok", "data": {"slug": "abc123_installer_agent"}},
    "/core/api/services": [{"domain": "light", "services": {"turn_on": {}}},
                           {"domain": "notify", "services": {"persistent_notification": {}, "send_message": {},
                                                             "mobile_app_pixel_7": {}, "telegram_casa": {}}}],
    "/network/info": {"result": "ok", "data": {"interfaces": [
        {"interface": "end0", "primary": True, "ipv4": {"address": ["192.168.1.50/24"]}}]}},
    "/os/info": {"result": "ok", "data": {"version": "16.2"}},
    "/host/info": {"result": "ok", "data": {"disk_used": 26.4, "disk_total": 31.0}},
    "/backups": {"result": "ok", "data": {"backups": [{"date": (now - timedelta(days=12)).isoformat()}]}},
}

NETWORK_URL = {"internal": "http://192.168.1.50:8123", "external": None, "cloud": "https://bianchi123.ui.nabu.casa"}


def ws_send(sock, obj):
    data = json.dumps(obj).encode()
    head = bytes([0x81]) + (bytes([len(data)]) if len(data) < 126 else bytes([126]) + struct.pack("!H", len(data)))
    sock.sendall(head + data)


def ws_recv(sock):
    head = sock.recv(2)
    if len(head) < 2:
        raise ConnectionError("client chiuso")
    b1, b2 = head
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", sock.recv(2))[0]
    mask = sock.recv(4)
    data = b""
    while len(data) < n:
        data += sock.recv(n - len(data))
    return json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def websocket(self):
        if os.environ.get("FAKE_HA_NO_WS"):
            self.send_response(404); self.end_headers(); return
        accept = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] +
                                                "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        # Handshake e primo messaggio nello stesso pacchetto: il caso più difficile per il client
        first = json.dumps({"type": "auth_required", "ha_version": "2026.10.1"}).encode()
        s = self.connection
        s.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode() + bytes([0x81, len(first)]) + first)
        try:
            auth = ws_recv(s)
        except ConnectionError:
            return
        if auth.get("access_token") != "test-token":
            ws_send(s, {"type": "auth_invalid", "message": "Invalid access token"}); return
        ws_send(s, {"type": "auth_ok", "ha_version": "2026.10.1"})
        try:
            msg = ws_recv(s)
        except ConnectionError:
            return
        if msg.get("type") == "network/url":
            ws_send(s, {"id": msg["id"], "type": "result", "success": True, "result": NETWORK_URL})
        else:
            ws_send(s, {"id": msg["id"], "type": "result", "success": False, "error": {"message": "Unknown command."}})
        self.close_connection = True

    def do_GET(self):
        if self.path == "/core/websocket" and self.headers.get("Upgrade", "").lower() == "websocket":
            return self.websocket()
        if self.headers.get("Authorization") != "Bearer test-token" or self.path not in ROUTES:
            self.send_response(401 if self.path in ROUTES else 404); self.end_headers(); return
        body = json.dumps(ROUTES[self.path]).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        # chiamate ai servizi (es. notifiche): registrate in un file per i test
        if self.headers.get("Authorization") != "Bearer test-token" or not self.path.startswith("/core/api/services/"):
            self.send_response(401); self.end_headers(); return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        domain, service = self.path.removeprefix("/core/api/services/").split("/", 1)
        if os.environ.get("FAKE_HA_LOG"):
            with open(os.environ["FAKE_HA_LOG"], "a") as f:
                f.write(json.dumps({"domain": domain, "service": service, "data": body}) + "\n")
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(b"[]")


ThreadingHTTPServer(("127.0.0.1", 8123), H).serve_forever()

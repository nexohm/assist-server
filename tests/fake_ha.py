"""Finto Home Assistant + Supervisor per i test."""
import json
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
    "/core/api/config": {"version": "2026.10.1", "location_name": "Casa Bianchi"},
    "/core/info": {"result": "ok", "data": {"version": "2026.10.1"}},
    "/os/info": {"result": "ok", "data": {"version": "16.2"}},
    "/host/info": {"result": "ok", "data": {"disk_used": 26.4, "disk_total": 31.0}},
    "/backups": {"result": "ok", "data": {"backups": [{"date": (now - timedelta(days=12)).isoformat()}]}},
}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer test-token" or self.path not in ROUTES:
            self.send_response(401 if self.path in ROUTES else 404); self.end_headers(); return
        body = json.dumps(ROUTES[self.path]).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

ThreadingHTTPServer(("127.0.0.1", 8123), H).serve_forever()

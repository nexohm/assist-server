"""Controllo degli aggiornamenti: nuova versione del server e dell'agente su GitHub.

Il controllo parte da solo al massimo ogni 6 ore, in un thread separato, alla prima pagina
aperta: nessuna attesa per chi usa la dashboard. Se GitHub non risponde si riprova al giro dopo.
Per disattivarlo (es. server senza internet): UPDATE_CHECK=0.
"""
import logging
import os
import re
import threading
import time
import urllib.request
import json

from . import db

log = logging.getLogger("updates")

ENABLED = os.environ.get("UPDATE_CHECK", "1") != "0"
SERVER_REPO = os.environ.get("UPDATE_SERVER_REPO", "nexohm/assist-server")
ADDON_REPO = os.environ.get("UPDATE_ADDON_REPO", "nexohm/ha-addons")
API_BASE = os.environ.get("UPDATE_API_BASE", "https://api.github.com").rstrip("/")
RAW_BASE = os.environ.get("UPDATE_RAW_BASE", "https://raw.githubusercontent.com").rstrip("/")
INTERVAL = int(os.environ.get("UPDATE_INTERVAL", 6 * 3600))
CURRENT = os.environ.get("SUITE_VERSION", "dev")

_lock = threading.Lock()


def parse(version):
    """'v0.2.1' -> (0, 2, 1); None se non è una versione (es. 'dev')."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (version or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(candidate, current):
    a, b = parse(candidate), parse(current)
    return bool(a and b and a > b)


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": f"NexohmAssist/{CURRENT}",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode()


def _latest_server():
    tags = json.loads(_get(f"{API_BASE}/repos/{SERVER_REPO}/tags?per_page=100"))
    versions = [t["name"].lstrip("v") for t in tags if parse(t.get("name"))]
    return max(versions, key=parse) if versions else None


def _latest_agent():
    text = _get(f"{RAW_BASE}/{ADDON_REPO}/main/installer_agent/config.yaml")
    m = re.search(r'^version:\s*"?([0-9]+\.[0-9]+\.[0-9]+)"?', text, re.M)
    return m.group(1) if m else None


def _check():
    for key, fetch in (("latest_server_version", _latest_server), ("latest_agent_version", _latest_agent)):
        try:
            value = fetch()
            if value:
                db.set_setting(key, value)
        except Exception as e:  # noqa: BLE001 — un controllo fallito non deve dare fastidio
            log.info("Controllo aggiornamenti (%s) non riuscito: %s", key, e)


def maybe_check():
    """Avvia un controllo in background se l'ultimo è più vecchio di INTERVAL."""
    if not ENABLED:
        return
    last = float(db.get_setting("update_checked_at", "0") or 0)
    if time.time() - last < INTERVAL or not _lock.acquire(blocking=False):
        return
    db.set_setting("update_checked_at", str(time.time()))

    def run():
        try:
            _check()
        finally:
            _lock.release()

    threading.Thread(target=run, daemon=True, name="update-check").start()


def server_update():
    """Versione più nuova del server, se ce n'è una rispetto a quella in uso."""
    latest = db.get_setting("latest_server_version")
    return latest if newer(latest, CURRENT) else None


def latest_agent():
    return db.get_setting("latest_agent_version")


def agent_outdated(version):
    return newer(latest_agent(), version)

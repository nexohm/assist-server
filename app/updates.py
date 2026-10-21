"""Controllo degli aggiornamenti: nuova versione del server e dell'agente su GitHub.

Il controllo parte da solo al massimo ogni ora, in un thread separato, alla prima pagina
aperta: nessuna attesa per chi usa la dashboard. Se GitHub non risponde si riprova dopo 10 minuti.
Da Impostazioni si può anche controllare subito («Controlla adesso»).
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
INTERVAL = int(os.environ.get("UPDATE_INTERVAL", 3600))
RETRY = min(INTERVAL, 600)   # dopo un controllo fallito
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
    """Legge le ultime versioni pubblicate. Restituisce il messaggio d'errore, o None se è andato tutto bene."""
    errors = []
    for key, what, fetch in (("latest_server_version", "server", _latest_server),
                             ("latest_agent_version", "agente", _latest_agent)):
        try:
            value = fetch()
            if value:
                db.set_setting(key, value)
            else:
                errors.append(f"{what}: nessuna versione trovata")
        except Exception as e:  # noqa: BLE001 — un controllo fallito non deve dare fastidio
            log.info("Controllo aggiornamenti (%s) non riuscito: %s", what, e)
            errors.append(f"{what}: {str(e)[:150]}")
    now = time.time()
    db.set_setting("update_attempt_at", str(now))
    db.set_setting("update_error", "; ".join(errors))
    if not errors:
        db.set_setting("update_checked_at", str(now))
    return "; ".join(errors) or None


def check_now():
    """Controllo immediato (pulsante in Impostazioni). Aspetta un controllo già in corso."""
    if not ENABLED:
        return "controllo degli aggiornamenti disattivato (UPDATE_CHECK=0)"
    with _lock:
        return _check()


def maybe_check():
    """Avvia un controllo in background se l'ultimo riuscito è più vecchio di INTERVAL
    (o, dopo un errore, se sono passati RETRY secondi dall'ultimo tentativo)."""
    if not ENABLED:
        return
    now = time.time()
    last_ok = float(db.get_setting("update_checked_at", "0") or 0)
    last_try = float(db.get_setting("update_attempt_at", "0") or 0)
    if now - last_ok < INTERVAL or now - last_try < RETRY or not _lock.acquire(blocking=False):
        return
    db.set_setting("update_attempt_at", str(now))

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


def is_release(version=CURRENT):
    """False per le immagini di prova (es. 'sviluppo-4217082') e per 'dev'."""
    return parse(version) is not None


def status():
    """Riepilogo per la pagina Impostazioni."""
    def ts(key):
        v = float(db.get_setting(key, "0") or 0)
        return v or None
    return {"enabled": ENABLED, "current": CURRENT, "release": is_release(),
            "latest_server": db.get_setting("latest_server_version"), "server_update": server_update(),
            "latest_agent": latest_agent(), "checked_at": ts("update_checked_at"),
            "attempt_at": ts("update_attempt_at"), "error": db.get_setting("update_error") or None}


def latest_agent():
    return db.get_setting("latest_agent_version")


def agent_outdated(version):
    return newer(latest_agent(), version)

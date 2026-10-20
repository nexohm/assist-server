"""Notifiche Telegram per l'installatore.

Configurazione (Impostazioni della dashboard): token del bot creato con @BotFather e chat ID.
Eventi: nuovo ticket, risposta del cliente, impianto offline / di nuovo online, impianto in stato critico.
L'invio avviene in un thread: la dashboard e gli agenti non aspettano mai Telegram.
"""
import asyncio
import html
import json
import logging
import os
import threading
import urllib.error
import urllib.request

from . import db

log = logging.getLogger("notify")

API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
WATCH_INTERVAL = int(os.environ.get("NOTIFY_WATCH_INTERVAL", "60"))

EVENTS = {
    "notify_tickets": "Nuovi ticket e risposte dei clienti",
    "notify_offline": "Impianto offline e di nuovo online",
    "notify_critical": "Impianto in stato critico",
}


def configured():
    return bool(db.get_setting("telegram_token") and db.get_setting("telegram_chat_id"))


def enabled(event):
    return configured() and db.get_setting(event, "1") == "1"


def _call(method, payload=None, token=None):
    token = token or db.get_setting("telegram_token", "")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API_BASE}/bot{token}/{method}", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read().decode())
        except ValueError:
            raise RuntimeError(f"Telegram ha risposto {e.code}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError(f"Telegram non raggiungibile: {getattr(e, 'reason', e)}") from e
    if not res.get("ok"):
        raise RuntimeError(res.get("description", "errore sconosciuto"))
    return res.get("result")


def send_now(text):
    """Invio sincrono (per il messaggio di prova). Solleva RuntimeError con un messaggio leggibile."""
    return _call("sendMessage", {"chat_id": db.get_setting("telegram_chat_id"), "text": text,
                                 "parse_mode": "HTML", "disable_web_page_preview": True})


def send(event, text):
    """Invio in background, solo se l'evento è attivo nelle impostazioni."""
    if not enabled(event):
        return

    def run():
        try:
            send_now(text)
        except RuntimeError as e:
            log.warning("Notifica Telegram non inviata: %s", e)

    threading.Thread(target=run, daemon=True, name="telegram").start()


def detect_chat_id(token):
    """Chat ID dell'ultimo messaggio ricevuto dal bot (l'installatore gli scrive /start)."""
    updates = _call("getUpdates", token=token) or []
    for update in reversed(updates):
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            return str(chat["id"]), name or chat.get("username") or ""
    return None, None


# ---------------------------------------------------------------- testi

def esc(value):
    return html.escape(str(value or ""))


def link(path):
    base = db.get_setting("public_url") or db.get_setting("public_url_detected")
    return f'\n<a href="{esc(base.rstrip("/") + path)}">Apri nella dashboard</a>' if base else ""


def plant_label(plant_id):
    p = db.one("SELECT p.name, c.name AS client FROM plants p JOIN clients c ON c.id = p.client_id WHERE p.id = ?",
               plant_id)
    return f"{esc(p['client'])} · {esc(p['name'])}" if p else f"impianto {plant_id}"


def new_ticket(ticket_id, plant_id, title, description):
    excerpt = (description or "").strip()
    excerpt = esc(excerpt[:300] + ("…" if len(excerpt) > 300 else ""))
    send("notify_tickets", f"🎫 <b>Nuovo ticket</b> — {plant_label(plant_id)}\n<b>{esc(title)}</b>"
                           + (f"\n{excerpt}" if excerpt else "") + link(f"/ticket/{ticket_id}"))


def client_reply(ticket_id, plant_id, title, text):
    excerpt = esc(text[:300] + ("…" if len(text) > 300 else ""))
    send("notify_tickets", f"💬 <b>Risposta del cliente</b> — {plant_label(plant_id)}\nTicket: {esc(title)}\n{excerpt}"
                           + link(f"/ticket/{ticket_id}"))


# ---------------------------------------------------------------- controllo periodico

def check_plants():
    """Confronta lo stato attuale con quello già notificato e avvisa solo sui cambiamenti."""
    from .agent_api import is_online  # import locale: evita dipendenze circolari

    plants = db.all_("SELECT * FROM plants WHERE paired_at IS NOT NULL AND revoked = 0")
    for p in plants:
        online = 1 if is_online(p) else 0
        diag = db.loads(p.get("last_diag"), {}) or {}
        status = diag.get("status") if online else None
        label = plant_label(p["id"])

        if p.get("alert_online") is None:
            pass  # primo controllo: memorizziamo lo stato senza avvisare
        elif online != p["alert_online"]:
            if online:
                send("notify_offline", f"🟢 <b>Di nuovo online</b> — {label}" + link(f"/impianti/{p['id']}"))
            else:
                send("notify_offline", f"🔴 <b>Impianto offline</b> — {label}\nNessun contatto da più di 3 minuti."
                                       + link(f"/impianti/{p['id']}"))

        prev_status = p.get("alert_status")
        if online and status == "critico" and prev_status != "critico":
            problems = "\n".join(f"• {esc(i.get('message'))}" for i in diag.get("issues", [])
                                 if i.get("severity") == "critico")
            send("notify_critical", f"⚠️ <b>Stato critico</b> — {label}\n{problems}" + link(f"/impianti/{p['id']}"))
        elif online and prev_status == "critico" and status and status != "critico":
            send("notify_critical", f"✅ <b>Rientrato dallo stato critico</b> — {label}" + link(f"/impianti/{p['id']}"))

        new_status = status if online else prev_status
        if p.get("alert_online") != online or prev_status != new_status:
            db.run("UPDATE plants SET alert_online = ?, alert_status = ? WHERE id = ?", online, new_status, p["id"])


async def watcher():
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        try:
            await asyncio.to_thread(check_plants)
        except Exception:  # noqa: BLE001 — il controllo non deve mai fermarsi
            log.exception("Errore nel controllo degli impianti")

"""Notifiche Telegram per l'installatore.

Configurazione per azienda (Impostazioni della dashboard): token del bot creato con @BotFather e chat ID.
Ogni azienda ha il suo bot: gli avvisi di un impianto vanno solo all'azienda a cui appartiene.
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

# Minuti senza contatto prima dell'avviso «offline»: valore dell'azienda, che ogni impianto può cambiare.
OFFLINE_DEFAULT = 3
OFFLINE_MIN, OFFLINE_MAX = 3, 1440


def parse_minutes(value):
    """Minuti scritti in un modulo: numero intero tra OFFLINE_MIN e OFFLINE_MAX, altrimenti None."""
    value = (value or "").strip() if isinstance(value, str) else ""
    if not value.isdigit():
        return None
    n = int(value)
    return n if OFFLINE_MIN <= n <= OFFLINE_MAX else None


def company_offline_minutes(company_id):
    try:
        n = int(db.get_csetting(company_id, "offline_minutes", OFFLINE_DEFAULT))
    except (TypeError, ValueError):
        return OFFLINE_DEFAULT
    return n if OFFLINE_MIN <= n <= OFFLINE_MAX else OFFLINE_DEFAULT


def offline_minutes(plant, company_id):
    """Soglia dell'avviso per un impianto: la sua, se l'ha, altrimenti quella dell'azienda."""
    n = plant.get("offline_minutes")
    return n if isinstance(n, int) and OFFLINE_MIN <= n <= OFFLINE_MAX else company_offline_minutes(company_id)


def fmt_minutes(n):
    if n >= 60 and n % 60 == 0:
        h = n // 60
        return "1 ora" if h == 1 else f"{h} ore"
    return "1 minuto" if n == 1 else f"{n} minuti"


def configured(company_id):
    return bool(db.get_csetting(company_id, "telegram_token") and db.get_csetting(company_id, "telegram_chat_id"))


def enabled(company_id, event):
    return configured(company_id) and db.get_csetting(company_id, event, "1") == "1"


def _call(method, payload=None, token=None):
    token = token or ""
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


def send_now(company_id, text):
    """Invio sincrono (per il messaggio di prova). Solleva RuntimeError con un messaggio leggibile."""
    return _call("sendMessage", {"chat_id": db.get_csetting(company_id, "telegram_chat_id"), "text": text,
                                 "parse_mode": "HTML", "disable_web_page_preview": True},
                 token=db.get_csetting(company_id, "telegram_token", ""))


def send(company_id, event, text):
    """Invio in background, solo se l'azienda ha attivato quell'evento."""
    if not company_id or not enabled(company_id, event):
        return

    def run():
        try:
            send_now(company_id, text)
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


def _company_id(plant_id):
    row = db.one("SELECT c.company_id FROM plants p JOIN clients c ON c.id = p.client_id WHERE p.id = ?", plant_id)
    return row["company_id"] if row else None


def new_ticket(ticket_id, plant_id, title, description, author=None):
    excerpt = (description or "").strip()
    excerpt = esc(excerpt[:300] + ("…" if len(excerpt) > 300 else ""))
    send(_company_id(plant_id), "notify_tickets", f"🎫 <b>Nuovo ticket</b> — {plant_label(plant_id)}\n<b>{esc(title)}</b>"
                           + (f"\nAperto da {esc(author)}" if author else "")
                           + (f"\n{excerpt}" if excerpt else "") + link(f"/ticket/{ticket_id}"))


def client_reply(ticket_id, plant_id, title, text, author=None):
    excerpt = esc(text[:300] + ("…" if len(text) > 300 else ""))
    who = f" ({esc(author)})" if author else ""
    send(_company_id(plant_id), "notify_tickets", f"💬 <b>Risposta del cliente</b>{who} — {plant_label(plant_id)}\nTicket: {esc(title)}\n{excerpt}"
                           + link(f"/ticket/{ticket_id}"))


# ---------------------------------------------------------------- controllo periodico

def check_plants():
    """Confronta lo stato attuale con quello già notificato e avvisa solo sui cambiamenti.
    L'avviso «offline» parte dopo i minuti scelti (dell'impianto o dell'azienda); quello «di nuovo
    online» solo dopo un avviso offline, appena l'agente torna in contatto."""
    from .agent_api import is_online, seen_within  # import locale: evita dipendenze circolari

    plants = db.all_("SELECT p.*, c.company_id FROM plants p JOIN clients c ON c.id = p.client_id "
                     "JOIN companies co ON co.id = c.company_id "
                     "WHERE p.paired_at IS NOT NULL AND p.revoked = 0 AND co.suspended_at IS NULL")
    for p in plants:
        co = p["company_id"]
        online = 1 if is_online(p) else 0
        diag = db.loads(p.get("last_diag"), {}) or {}
        status = diag.get("status") if online else None
        label = plant_label(p["id"])

        alert = p.get("alert_online")
        if alert is None:
            alert = online  # primo controllo: memorizziamo lo stato senza avvisare
        elif alert == 1:
            minutes = offline_minutes(p, co)
            if not seen_within(p, minutes):
                alert = 0
                send(co, "notify_offline", f"🔴 <b>Impianto offline</b> — {label}\nNessun contatto da più di "
                                       f"{fmt_minutes(minutes)}." + link(f"/impianti/{p['id']}"))
        elif online:
            alert = 1
            send(co, "notify_offline", f"🟢 <b>Di nuovo online</b> — {label}" + link(f"/impianti/{p['id']}"))

        prev_status = p.get("alert_status")
        if online and status == "critico" and prev_status != "critico":
            problems = "\n".join(f"• {esc(i.get('message'))}" for i in diag.get("issues", [])
                                 if i.get("severity") == "critico")
            send(co, "notify_critical", f"⚠️ <b>Stato critico</b> — {label}\n{problems}" + link(f"/impianti/{p['id']}"))
        elif online and prev_status == "critico" and status and status != "critico":
            send(co, "notify_critical", f"✅ <b>Rientrato dallo stato critico</b> — {label}" + link(f"/impianti/{p['id']}"))

        new_status = status if online else prev_status
        if p.get("alert_online") != alert or prev_status != new_status:
            db.run("UPDATE plants SET alert_online = ?, alert_status = ? WHERE id = ?", alert, new_status, p["id"])


async def watcher():
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        try:
            await asyncio.to_thread(check_plants)
        except Exception:  # noqa: BLE001 — il controllo non deve mai fermarsi
            log.exception("Errore nel controllo degli impianti")

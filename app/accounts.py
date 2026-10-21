"""Recupero della password: link monouso e (se il gestore lo attiva) invio per email.

Un link vale RESET_MINUTES minuti e una volta sola. Nel database c'è solo l'hash del link:
chi legge il database non può usarlo. Usato un link, gli altri dello stesso utente non valgono più.
Il link si ottiene:
  - dal server, con  sudo ha-suite password <utente>  (per il gestore stesso, o in emergenza);
  - dal gestore, per gli amministratori delle altre aziende (pagina Aziende);
  - dall'amministratore di un'azienda, per i suoi utenti (pagina Utenti);
  - per email, da «Password dimenticata?», se il gestore ha configurato un server SMTP.

Controllo periodico della posta (se l'SMTP è configurato): ogni giorno il server verifica che
l'accesso al servizio funzioni, e ogni KEEPALIVE_DAYS giorni manda un'email di controllo al
gestore. Così ci si accorge subito di una chiave scaduta o revocata, e i servizi che disattivano
le chiavi non usate (es. Brevo dopo 90 giorni) non la disattivano mai.
"""
import asyncio
import os
import time
import logging
import smtplib
import ssl
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import db
from .security import hash_password, hash_token, new_token

log = logging.getLogger("accounts")

RESET_MINUTES = 30
CHECK_EVERY = 24 * 3600          # verifica dell'accesso SMTP
KEEPALIVE_DAYS = 30              # email di controllo al gestore (ben sotto i 90 giorni di Brevo)
WATCH_INTERVAL = int(os.environ.get("MAIL_WATCH_INTERVAL", "3600"))
SMTP_KEYS = ("smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password", "smtp_from")
SMTP_SECURITY = {"starttls": "STARTTLS (porta 587)", "ssl": "SSL/TLS (porta 465)", "nessuna": "Nessuna (sconsigliato)"}


# ---------------------------------------------------------------- link monouso

def create_reset(user_id, created_by):
    """Nuovo link per l'utente. Restituisce il codice in chiaro (da mettere nell'indirizzo)."""
    token = new_token()
    now = datetime.now(timezone.utc)
    db.run("INSERT INTO password_resets(token_hash, user_id, created_by, created_at, expires_at) VALUES(?, ?, ?, ?, ?)",
           hash_token(token), user_id, (created_by or "")[:80], now.isoformat(timespec="seconds"),
           (now + timedelta(minutes=RESET_MINUTES)).isoformat(timespec="seconds"))
    # i link scaduti o usati da più di un giorno non servono più
    db.run("DELETE FROM password_resets WHERE expires_at < ?", (now - timedelta(days=1)).isoformat(timespec="seconds"))
    return token


def reset_url(token, base):
    return f"{(base or '').rstrip('/')}/recupero/{token}"


def base_url():
    return db.get_setting("public_url") or db.get_setting("public_url_detected") or ""


def find_reset(token):
    """Utente del link se il link è valido (non usato, non scaduto, utente attivo, azienda non sospesa)."""
    if not isinstance(token, str) or not 20 <= len(token) <= 100:
        return None
    return db.one(
        "SELECT u.* FROM password_resets r JOIN users u ON u.id = r.user_id "
        "LEFT JOIN companies co ON co.id = u.company_id "
        "WHERE r.token_hash = ? AND r.used_at IS NULL AND r.expires_at > ? AND u.active = 1 "
        "AND co.suspended_at IS NULL", hash_token(token), db.now())


def use_reset(token, new_password):
    """Imposta la nuova password consumando il link. False se il link non vale (più)."""
    user = find_reset(token)
    if not user:
        return None
    with db.connect() as c:
        taken = c.execute("UPDATE password_resets SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                          (db.now(), hash_token(token))).rowcount
        if not taken:  # usato da un'altra richiesta nello stesso istante
            return None
        c.execute("UPDATE users SET password_hash = ?, must_change = 0 WHERE id = ?", (hash_password(new_password), user["id"]))
        c.execute("UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL", (db.now(), user["id"]))
    log.info("Password di %s reimpostata con un link di recupero", user["username"])
    return user


# ---------------------------------------------------------------- email (facoltativa)

def smtp_settings():
    return {k: db.get_setting(k, "") or "" for k in SMTP_KEYS}


def email_enabled():
    """«Password dimenticata?» compare solo se il gestore l'ha attivato e l'SMTP è configurato."""
    s = smtp_settings()
    return db.get_setting("email_reset", "0") == "1" and bool(s["smtp_host"] and s["smtp_from"])


def smtp_configured():
    s = smtp_settings()
    return bool(s["smtp_host"] and s["smtp_from"])


def _connect(s):
    """Apre la connessione al server SMTP e fa il login. Restituisce la connessione aperta."""
    security = s["smtp_security"] or "starttls"
    try:
        port = int(s["smtp_port"] or (465 if security == "ssl" else 587))
    except ValueError:
        raise RuntimeError("porta SMTP non valida") from None
    ctx = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(s["smtp_host"], port, timeout=15, context=ctx)
    else:
        server = smtplib.SMTP(s["smtp_host"], port, timeout=15)
    try:
        if security == "starttls":
            server.starttls(context=ctx)
        if s["smtp_user"]:
            server.login(s["smtp_user"], s["smtp_password"])
    except BaseException:
        server.close()
        raise
    return server


def _record(ok, error=None, sent=False):
    now = str(time.time())
    db.set_setting("mail_check_at", now)
    if ok:
        db.set_setting("mail_error", "")
        db.set_setting("mail_alerted", "0")
        if sent:
            db.set_setting("mail_sent_at", now)
    else:
        db.set_setting("mail_error", (error or "errore sconosciuto")[:300])


def check_login():
    """Verifica che server, utente e password/chiave SMTP funzionino, senza inviare nulla."""
    s = smtp_settings()
    if not smtp_configured():
        raise RuntimeError("server SMTP non configurato")
    try:
        with _connect(s) as server:
            server.noop()
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        _record(False, f"accesso non riuscito: {e}")
        raise RuntimeError(f"accesso non riuscito: {e}") from e
    _record(True)


def send_mail(to, subject, body):
    """Invio sincrono. Solleva RuntimeError con un messaggio leggibile."""
    s = smtp_settings()
    if not smtp_configured():
        raise RuntimeError("server SMTP non configurato")
    msg = EmailMessage()
    msg["From"] = formataddr((db.get_setting("email_sender_name", "") or "Assistenza impianti", s["smtp_from"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg["Content-Language"] = "it"
    msg.set_content(body)
    try:
        with _connect(s) as server:
            server.send_message(msg)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        _record(False, f"invio non riuscito: {e}")
        raise RuntimeError(f"invio non riuscito: {e}") from e
    _record(True, sent=True)


def send_reset_email(user, base):
    """Invio in background: chi chiede il recupero riceve sempre la stessa risposta, subito."""
    token = create_reset(user["id"], "email")
    company = db.company(user["company_id"]) if user.get("company_id") else None
    body = (f"Ciao {user.get('full_name') or user['username']},\n\n"
            f"qualcuno (speriamo tu) ha chiesto di reimpostare la password di «{user['username']}»"
            + (f" per {company['name']}" if company else "") + ".\n\n"
            f"Apri questo link entro {RESET_MINUTES} minuti per sceglierne una nuova:\n{reset_url(token, base)}\n\n"
            "Il link vale una volta sola. Se non l'hai chiesto tu, ignora questo messaggio: la password resta quella di prima.\n")

    def run():
        try:
            send_mail(user["email"], "Recupero della password", body)
        except RuntimeError as e:
            log.warning("Email di recupero per %s non inviata: %s", user["username"], e)

    threading.Thread(target=run, daemon=True, name="reset-mail").start()


# ---------------------------------------------------------------- controllo periodico

def manager_email():
    row = db.one("SELECT email FROM users WHERE is_manager = 1 AND active = 1 AND email IS NOT NULL "
                 "AND email != '' ORDER BY id LIMIT 1")
    return row["email"] if row else None


def mail_status():
    def ts(key):
        v = float(db.get_setting(key, "0") or 0)
        return v or None
    sent = ts("mail_sent_at")
    return {"configured": smtp_configured(), "check_at": ts("mail_check_at"), "sent_at": sent,
            "error": db.get_setting("mail_error") or None, "manager_email": manager_email(),
            "keepalive_due": (sent or 0) + KEEPALIVE_DAYS * 86400 if sent else None,
            "keepalive_days": KEEPALIVE_DAYS}


def maintain(check_now=False):
    """Un giro del controllo: manda l'email di controllo se è il momento, altrimenti verifica
    l'accesso (una volta al giorno, o subito con check_now). Restituisce l'errore o None."""
    if not smtp_configured():
        return None
    now = time.time()
    st = mail_status()
    try:
        if not st["sent_at"] or now - st["sent_at"] >= KEEPALIVE_DAYS * 86400:
            to = st["manager_email"]
            if not to:
                _record(False, "manca l'email del gestore in «Il mio account»: serve per l'email di controllo mensile")
                return db.get_setting("mail_error")
            send_mail(to, "Controllo mensile della posta del server di assistenza",
                      "Questa è l'email di controllo che il server di assistenza invia ogni "
                      f"{KEEPALIVE_DAYS} giorni.\n\nServe a verificare che l'invio delle email di recupero della "
                      "password funzioni e a tenere attiva la chiave del servizio di posta (alcuni servizi la "
                      "disattivano se resta inutilizzata per un po').\n\nNon devi fare nulla.\n")
        elif check_now or not st["check_at"] or now - st["check_at"] >= CHECK_EVERY:
            check_login()
    except RuntimeError as e:
        return str(e)
    return None


def _alert_once(error):
    """Avvisa il gestore su Telegram (se l'ha configurato) una volta per ogni guasto."""
    if db.get_setting("mail_alerted") == "1":
        return
    db.set_setting("mail_alerted", "1")
    from . import notify  # import locale: evita dipendenze circolari
    row = db.one("SELECT company_id FROM users WHERE is_manager = 1 AND active = 1 ORDER BY id LIMIT 1")
    if row and notify.configured(row["company_id"]):
        try:
            notify.send_now(row["company_id"], "📧 <b>Posta del server non funzionante</b>\n"
                            f"{notify.esc(error)}\nIl recupero della password via email non funziona finché non "
                            "sistemi le impostazioni." + notify.link("/impostazioni#email"))
        except RuntimeError as e:
            log.warning("Avviso Telegram sulla posta non inviato: %s", e)


async def watcher():
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        try:
            error = await asyncio.to_thread(maintain)
            if error:
                log.warning("Controllo della posta: %s", error)
                await asyncio.to_thread(_alert_once, error)
        except Exception:  # noqa: BLE001 — il controllo non deve mai fermarsi
            log.exception("Errore nel controllo della posta")

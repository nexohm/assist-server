"""Recupero della password: link monouso e (se il gestore lo attiva) invio per email.

Un link vale RESET_MINUTES minuti e una volta sola. Nel database c'è solo l'hash del link:
chi legge il database non può usarlo. Usato un link, gli altri dello stesso utente non valgono più.
Il link si ottiene:
  - dal server, con  sudo ha-suite password <utente>  (per il gestore stesso, o in emergenza);
  - dal gestore, per gli amministratori delle altre aziende (pagina Aziende);
  - dall'amministratore di un'azienda, per i suoi utenti (pagina Utenti);
  - per email, da «Password dimenticata?», se il gestore ha configurato un server SMTP.
"""
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


def send_mail(to, subject, body):
    """Invio sincrono. Solleva RuntimeError con un messaggio leggibile."""
    s = smtp_settings()
    if not s["smtp_host"] or not s["smtp_from"]:
        raise RuntimeError("server SMTP non configurato")
    msg = EmailMessage()
    msg["From"] = formataddr((db.get_setting("email_sender_name", "") or "Assistenza impianti", s["smtp_from"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    security = s["smtp_security"] or "starttls"
    try:
        port = int(s["smtp_port"] or (465 if security == "ssl" else 587))
    except ValueError:
        raise RuntimeError("porta SMTP non valida") from None
    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(s["smtp_host"], port, timeout=15, context=ctx)
        else:
            server = smtplib.SMTP(s["smtp_host"], port, timeout=15)
        with server:
            if security == "starttls":
                server.starttls(context=ctx)
            if s["smtp_user"]:
                server.login(s["smtp_user"], s["smtp_password"])
            server.send_message(msg)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        raise RuntimeError(f"invio non riuscito: {e}") from e


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

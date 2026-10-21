"""Dashboard web dell'installatore."""
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from . import db, notify, sanitize, secrets_store, updates
from .agent_api import is_online, ring
from . import accounts
from .security import (ROLE_LABELS, ROLES, can, hash_password, new_pair_code, new_setup_code,
                       verify_password, DUMMY_HASH)

log = logging.getLogger("web")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

ALLOWED_COMMANDS = {
    "run_diagnostics": "Esegui diagnostica",
    "ping": "Ping",
}
TICKET_STATUS = {"aperto": "Aperto", "in_lavorazione": "In lavorazione", "chiuso": "Chiuso"}
# Comandi dell'accesso amministratore: hanno pulsanti e permesso propri (li concede il cliente)
ADMIN_COMMANDS = {"admin_password": "Accesso amministratore: nuova password", "admin_close": "Accesso amministratore: chiusura"}
COMMAND_STATUS = {"in_attesa": "In attesa", "inviato": "Inviato", "completato": "Completato", "errore": "Errore"}


# ---------- utilità ----------

def _as_ts(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return db.parse_ts(value)


def fmt_ts(value):
    ts = _as_ts(value)
    if not ts:
        return "—"
    return ts.astimezone().strftime("%d/%m/%Y %H:%M")


def ago(value):
    ts = _as_ts(value)
    if not ts:
        return "mai"
    s = int((datetime.now(timezone.utc) - ts).total_seconds())
    if s < 60:
        return "adesso"
    if s < 3600:
        return f"{s // 60} min fa"
    if s < 86400:
        return f"{s // 3600} h fa"
    return f"{s // 86400} g fa"


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_asset_versions = {}


def asset(name):
    """Indirizzo di un file statico con l'impronta del contenuto (?v=...): dopo un aggiornamento
    il browser scarica per forza la versione nuova invece di usare quella in cache."""
    if name not in _asset_versions:
        try:
            with open(os.path.join(STATIC_DIR, name), "rb") as f:
                _asset_versions[name] = hashlib.sha256(f.read()).hexdigest()[:10]
        except OSError:
            _asset_versions[name] = "0"
    return f"/static/{name}?v={_asset_versions[name]}"


templates.env.filters["ts"] = fmt_ts
templates.env.filters["ago"] = ago
templates.env.globals.update(agent_outdated=updates.agent_outdated, latest_agent=updates.latest_agent,
                             TICKET_STATUS=TICKET_STATUS, COMMAND_STATUS=COMMAND_STATUS,
                             ROLE_LABELS=ROLE_LABELS, ALLOWED_COMMANDS=ALLOWED_COMMANDS,
                             COMMAND_LABELS={**ALLOWED_COMMANDS, **ADMIN_COMMANDS}, can=can,
                             is_online=is_online, asset=asset)


def pw_mark(user):
    """Impronta della password nella sessione: cambiata la password (anche con un link di
    recupero), le sessioni aperte altrove smettono di valere."""
    return hashlib.sha256(user["password_hash"].encode()).hexdigest()[:16]


def current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.one("SELECT u.*, co.suspended_at AS company_suspended FROM users u "
                  "LEFT JOIN companies co ON co.id = u.company_id WHERE u.id = ? AND u.active = 1", uid)
    if not user or user["company_suspended"]:
        return None  # azienda sospesa: fuori anche chi era già collegato
    mark = request.session.get("pw")
    if mark is None:
        request.session["pw"] = pw_mark(user)  # sessione aperta con una versione precedente
    elif not secrets.compare_digest(mark, pw_mark(user)):
        return None
    return user


SERVER_TITLE = "Assistenza impianti"   # login e primo avvio: nessuna azienda ancora scelta
DEFAULT_COLOR = "#0f766e"


def valid_email(value):
    return isinstance(value, str) and len(value) <= 200 and re.fullmatch(r"[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+", value) is not None


def valid_color(value):
    return isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) is not None


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


# ---------- limite ai tentativi (login e primo avvio) ----------
# Due contatori: per indirizzo IP e per nome utente. Il secondo serve perché l'IP si può
# falsificare (X-Forwarded-For) o cambiare: chi prova password su un utente viene fermato lo stesso.
WINDOW = 15 * 60
MAX_PER_IP = 5
MAX_PER_USER = 10
_failures: dict[str, list[float]] = defaultdict(list)


def _recent(key):
    now = time.time()
    hits = [t for t in _failures.get(key, []) if now - t < WINDOW]
    if hits:
        _failures[key] = hits
    else:
        _failures.pop(key, None)
    return hits


def too_many_attempts(ip, username=None):
    if len(_failures) > 10_000:  # niente crescita senza limite della memoria
        for key in list(_failures):
            _recent(key)
    return len(_recent(f"ip:{ip}")) >= MAX_PER_IP or (username and len(_recent(f"user:{username}")) >= MAX_PER_USER)


def record_failure(ip, username=None):
    _failures[f"ip:{ip}"].append(time.time())
    if username:
        _failures[f"user:{username}"].append(time.time())


# ---------- codice di installazione ----------
# Al primo avvio la pagina /setup crea l'amministratore. Senza protezione, chiunque arrivi
# per primo sul dominio (i bot scandagliano i nuovi certificati HTTPS in pochi minuti) diventa
# amministratore. Per questo serve un codice che si legge solo dal server: nei log o con
# "ha-suite codice".
SETUP_CODE_PATH = os.path.join(db.DATA_DIR, "setup-code.txt")


def ensure_setup_code():
    if db.one("SELECT id FROM users LIMIT 1"):
        if os.path.exists(SETUP_CODE_PATH):
            os.remove(SETUP_CODE_PATH)
        return None
    if not os.path.exists(SETUP_CODE_PATH):
        fd = os.open(SETUP_CODE_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(new_setup_code())
    with open(SETUP_CODE_PATH) as f:
        code = f.read().strip()
    print("\n" + "=" * 60 + f"\n  Codice di installazione: {code}\n"
          "  Serve per la prima configurazione dal browser.\n"
          "  Per rivederlo: sudo ha-suite codice\n" + "=" * 60 + "\n", flush=True)
    return code


def setup_code_ok(given):
    try:
        with open(SETUP_CODE_PATH) as f:
            expected = f.read().strip()
    except OSError:
        return False
    given = "".join(ch for ch in (given or "").upper() if ch.isalnum())
    return bool(expected) and secrets.compare_digest(given, expected.replace("-", ""))


def csrf_token(request: Request):
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]


def guess_public_url(request: Request) -> str:
    """Indirizzo con cui il server è raggiunto da fuori, anche dietro tunnel o proxy.
    Cloudflare aggiunge l'intestazione CF-Visitor con lo schema originale del browser:
    vale anche quando in mezzo c'è un proxy (es. Nginx Proxy Manager) che parla in http."""
    scheme = request.url.scheme
    if '"https"' in request.headers.get("cf-visitor", "") or request.headers.get("x-forwarded-proto") == "https":
        scheme = "https"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


def public_url(request: Request) -> str:
    return db.get_setting("public_url") or guess_public_url(request)


def render(request, name, **ctx):
    updates.maybe_check()
    if request.session.get("uid") and not db.get_setting("public_url"):
        detected = guess_public_url(request)
        if detected != db.get_setting("public_url_detected"):
            db.set_setting("public_url_detected", detected)  # per i link nelle notifiche Telegram
    user = current_user(request)
    co = db.company(user["company_id"]) if user and user.get("company_id") else None
    ctx.update(
        server_update=updates.server_update(),
        request=request,
        user=user,
        csrf=csrf_token(request),
        company=co["name"] if co else SERVER_TITLE,
        usage=usage(co["id"]) if co else {},
        brand_color=co["brand_color"] if co else DEFAULT_COLOR,
        public_url=public_url(request),
        flash=take_flash(request, ctx),
    )
    return templates.TemplateResponse(request, name, ctx)


def redirect(url, flash=None, request=None, error=False):
    """Reindirizza e mostra un messaggio (conferma con la spunta, oppure errore con la X)."""
    if flash and request is not None:
        request.session["flash"] = {"text": flash, "error": bool(error)}
    return RedirectResponse(url, status_code=303)


def take_flash(request, ctx):
    """Messaggio da mostrare: quello salvato prima del reindirizzamento, oppure gli errori del modulo."""
    flash = request.session.pop("flash", None)
    if isinstance(flash, str):  # sessioni create da versioni precedenti
        flash = {"text": flash, "error": False}
    errors = ctx.get("errors")
    if not flash and errors:
        flash = {"text": errors[0] if len(errors) == 1 else "Controlla i dati inseriti: ci sono errori nel modulo.",
                 "error": True}
    return flash


async def read_form(request: Request):
    form = await request.form()
    expected = request.session.get("csrf") or ""
    given = form.get("csrf") or ""
    # Un token vuoto non vale mai: altrimenti una sessione senza token (es. appena fatto il login)
    # accetterebbe moduli senza token da qualsiasi sito.
    if not expected or not isinstance(given, str) or not secrets.compare_digest(given, expected):
        return None
    return form


# Ogni id nell'indirizzo deve appartenere all'azienda di chi è collegato: il controllo è qui,
# una volta sola, così nessuna pagina può dimenticarlo. Un id di un'altra azienda risponde
# «non trovato», esattamente come un id inesistente.
OWNERSHIP = {
    "cid": "SELECT 1 FROM clients WHERE id = ? AND company_id = ?",
    "pid": "SELECT 1 FROM plants p JOIN clients c ON c.id = p.client_id WHERE p.id = ? AND c.company_id = ?",
    "tid": ("SELECT 1 FROM tickets t JOIN plants p ON p.id = t.plant_id JOIN clients c ON c.id = p.client_id "
            "WHERE t.id = ? AND c.company_id = ?"),
    "uid": "SELECT 1 FROM users WHERE id = ? AND company_id = ?",
    "cmd": ("SELECT 1 FROM commands m JOIN plants p ON p.id = m.plant_id JOIN clients c ON c.id = p.client_id "
            "WHERE m.id = ? AND c.company_id = ?"),
}


def owns(user, params):
    for key, value in params.items():
        if key == "co":  # azienda: solo il gestore del server agisce sulle aziende
            if not can(user, "companies") or not db.company(value):
                return False
            continue
        sql = OWNERSHIP.get(key)
        if sql is None:
            raise RuntimeError(f"parametro {key} senza controllo dell'azienda")  # nuovo indirizzo da aggiungere qui
        if not user.get("company_id") or not db.one(sql, value, user["company_id"]):
            return False
    return True


def guard(permission="view"):
    """Decoratore: richiede login e permesso. Per i POST verifica anche il token CSRF."""
    def wrap(handler):
        async def inner(request: Request):
            if not db.one("SELECT id FROM users LIMIT 1"):
                return redirect("/setup")
            user = current_user(request)
            if not user:
                return redirect(f"/login?next={request.url.path}")
            if not can(user, permission):
                return render(request, "error.html", message="Non hai i permessi per questa operazione.")
            form = None
            if request.method == "POST":
                form = await read_form(request)
                if form is None:
                    return render(request, "error.html", message="Sessione scaduta: ricarica la pagina e riprova.")
            if not owns(user, request.path_params):
                if request.url.path.endswith(".json"):
                    return JSONResponse({"error": "not found"}, status_code=404)
                return render(request, "error.html", message="Non trovato.")
            if user["must_change"] and request.url.path != "/account":
                return redirect("/account", "Prima di continuare scegli una password nuova, che conosci solo tu.", request)
            request.state.form = form
            request.state.user = user
            return await handler(request)
        return inner
    return wrap


# ---------- primo avvio e accesso ----------

async def setup(request: Request):
    if db.one("SELECT id FROM users LIMIT 1"):
        return redirect("/login")
    if request.method == "GET":
        return render(request, "setup.html", errors=[])
    ip = client_ip(request)
    if too_many_attempts(ip):
        return render(request, "setup.html", errors=["Troppi tentativi falliti. Riprova tra 15 minuti."])
    form = await read_form(request)
    if form is None:
        return redirect("/setup")
    if not setup_code_ok(form.get("setup_code")):
        record_failure(ip)
        return render(request, "setup.html", errors=["Codice di installazione errato. Lo trovi nei log del server "
                                                     "o con il comando: sudo ha-suite codice"], form=form)
    errors = []
    company = form.get("company_name", "").strip()
    username = form.get("username", "").strip().lower()
    password = form.get("password", "")
    if not company:
        errors.append("Inserisci il nome dell'azienda.")
    if len(username) < 3:
        errors.append("Il nome utente deve avere almeno 3 caratteri.")
    if len(password) < 10:
        errors.append("La password deve avere almeno 10 caratteri.")
    if password != form.get("password2", ""):
        errors.append("Le due password non coincidono.")
    if errors:
        return render(request, "setup.html", errors=errors, form=form)
    # Ultimo controllo e creazione insieme: due richieste contemporanee non creano due amministratori.
    with db.connect() as c:
        if c.execute("SELECT id FROM users LIMIT 1").fetchone():
            return redirect("/login")
        color = form.get("brand_color", "")
        co = c.execute("INSERT INTO companies(name, brand_color, created_at) VALUES(?, ?, ?)",
                       (company[:80], color if valid_color(color) else DEFAULT_COLOR, db.now())).lastrowid
        # chi installa il server ne è il gestore: crea le altre aziende e ne vede solo i numeri
        uid = c.execute("INSERT INTO users(username, full_name, password_hash, role, company_id, is_manager, created_at) "
                        "VALUES(?, ?, ?, 'admin', ?, 1, ?)", (username, form.get("full_name", "").strip(),
                                                               hash_password(password), co, db.now())).lastrowid
    try:
        os.remove(SETUP_CODE_PATH)
    except OSError:
        pass
    request.session.clear()
    request.session["uid"] = uid
    return redirect("/", "Configurazione completata. Benvenuto!", request)


async def login(request: Request):
    if not db.one("SELECT id FROM users LIMIT 1"):
        return redirect("/setup")
    next_url = request.query_params.get("next", "/")
    # Solo percorsi interni: niente "//sito", "/\\sito" o caratteri di controllo che i browser
    # potrebbero interpretare come un altro dominio.
    if (not next_url.startswith("/") or next_url.startswith("//") or "\\" in next_url
            or any(ord(ch) < 32 for ch in next_url)):
        next_url = "/"
    if request.method == "GET":
        return render(request, "login.html", error=None, next=next_url, email_reset=accounts.email_enabled())
    form = await read_form(request)
    if form is None:
        return redirect("/login")
    ip = client_ip(request)
    username = form.get("username", "").strip().lower()[:100]
    if too_many_attempts(ip, username):
        return render(request, "login.html", error="Troppi tentativi falliti. Riprova tra 15 minuti.", next=next_url,
                      email_reset=accounts.email_enabled())
    user = db.one("SELECT u.*, co.suspended_at AS company_suspended FROM users u "
                  "LEFT JOIN companies co ON co.id = u.company_id WHERE u.username = ? AND u.active = 1", username)
    # Anche per un utente inesistente si calcola un hash: il tempo di risposta non rivela chi esiste.
    valid = verify_password(form.get("password", ""), user["password_hash"] if user else DUMMY_HASH)
    if not user or not valid:
        record_failure(ip, username)
        return render(request, "login.html", error="Nome utente o password errati.", next=next_url,
                      email_reset=accounts.email_enabled())
    _failures.pop(f"ip:{ip}", None)
    _failures.pop(f"user:{username}", None)
    if user["company_suspended"]:
        return render(request, "login.html", error="L'accesso della tua azienda è sospeso: contatta il gestore del server.",
                      next=next_url)
    request.session.clear()
    request.session["uid"] = user["id"]
    request.session["pw"] = pw_mark(user)
    if user["must_change"]:
        return redirect("/account", "Prima di continuare scegli una password nuova, che conosci solo tu.", request)
    return redirect(next_url)


# ---------- recupero della password ----------

RESET_REQUESTS_PER_IP = 5


async def reset_request(request: Request):
    """«Password dimenticata?»: solo se il gestore ha attivato l'email. La risposta è sempre la stessa,
    così non si scopre quali utenti esistono."""
    if not accounts.email_enabled():
        return render(request, "reset.html", mode="off")
    if request.method == "GET":
        return render(request, "reset.html", mode="ask")
    form = await read_form(request)
    if form is None:
        return redirect("/recupero")
    ip = client_ip(request)
    if len(_recent(f"reset:{ip}")) >= RESET_REQUESTS_PER_IP:
        return render(request, "reset.html", mode="ask", errors=["Troppe richieste. Riprova tra 15 minuti."])
    _failures[f"reset:{ip}"].append(time.time())
    who = form.get("who", "").strip().lower()[:200]
    if who:
        user = db.one("SELECT u.* FROM users u LEFT JOIN companies co ON co.id = u.company_id "
                      "WHERE (u.username = ? OR lower(u.email) = ?) AND u.active = 1 AND u.email IS NOT NULL "
                      "AND co.suspended_at IS NULL ORDER BY u.id LIMIT 1", who, who)
        if user and len(_recent(f"resetuser:{user['id']}")) < 3:   # niente valanga di email allo stesso utente
            _failures[f"resetuser:{user['id']}"].append(time.time())
            accounts.send_reset_email(user, public_url(request))
    return render(request, "reset.html", mode="sent")


async def reset_use(request: Request):
    token = request.path_params["token"]
    user = accounts.find_reset(token)
    if not user:
        return render(request, "reset.html", mode="invalid")
    if request.method == "GET":
        return render(request, "reset.html", mode="new", target=user, token=token, errors=[])
    form = await read_form(request)
    if form is None:
        return redirect(f"/recupero/{token}")
    new, new2 = form.get("password", ""), form.get("password2", "")
    errors = []
    if len(new) < MIN_PASSWORD:
        errors.append(f"La password deve avere almeno {MIN_PASSWORD} caratteri.")
    if new != new2:
        errors.append("Le due password non coincidono.")
    if errors:
        return render(request, "reset.html", mode="new", target=user, token=token, errors=errors)
    if not accounts.use_reset(token, new):
        return render(request, "reset.html", mode="invalid")
    request.session.clear()
    return redirect("/login", "Password impostata: ora accedi con quella nuova.", request)


async def logout(request: Request):
    # Con il token CSRF: un altro sito non può far uscire l'utente a sua insaputa.
    if await read_form(request) is None:
        return redirect("/")
    request.session.clear()
    return redirect("/login")


# ---------- panoramica ----------

def remote_access(p):
    """Il cliente consente l'accesso remoto (e non è scaduto)?"""
    if not p.get("consent"):
        return False
    until = db.parse_ts(p.get("consent_until"))
    return not (until and until < datetime.now(timezone.utc))


templates.env.globals["remote_access"] = remote_access


def plant_status(p):
    if p["revoked"]:
        return "revocato"
    if not p["paired_at"]:
        return "da_abbinare"
    if not is_online(p):
        return "offline"
    return sanitize.stored_diagnostics(p["last_diag"]).get("status", "ok")


@guard()
async def dashboard(request: Request):
    co = request.state.user["company_id"]
    plants = db.all_("SELECT p.*, c.name AS client_name FROM plants p JOIN clients c ON c.id = p.client_id "
                     "WHERE c.company_id = ? ORDER BY c.name, p.name", co)
    for p in plants:
        p["status"] = plant_status(p)
        p["diag"] = sanitize.stored_diagnostics(p["last_diag"])
    order = {"offline": 0, "critico": 1, "attenzione": 2, "da_abbinare": 3, "ok": 4, "revocato": 5}
    plants.sort(key=lambda p: order.get(p["status"], 9))
    counts = defaultdict(int)
    for p in plants:
        counts[p["status"]] += 1
    # Le caselle in alto filtrano la tabella (?stato=offline, critico, attenzione)
    filtro = request.query_params.get("stato")
    if filtro in ("offline", "critico", "attenzione"):
        shown = [p for p in plants if p["status"] == filtro]
    else:
        filtro, shown = None, plants
    open_tickets = db.all_("SELECT t.*, p.name AS plant_name, c.name AS client_name FROM tickets t "
                           "JOIN plants p ON p.id = t.plant_id JOIN clients c ON c.id = p.client_id "
                           "WHERE t.status != 'chiuso' AND c.company_id = ? ORDER BY t.updated_at DESC LIMIT 10", co)
    n_open = db.one("SELECT COUNT(*) AS n FROM tickets t JOIN plants p ON p.id = t.plant_id "
                    "JOIN clients c ON c.id = p.client_id WHERE t.status != 'chiuso' AND c.company_id = ?", co)["n"]
    return render(request, "dashboard.html", plants=shown, total=len(plants), counts=counts, filtro=filtro,
                  open_tickets=open_tickets, n_open=n_open)


# ---------- clienti ----------

@guard()
async def clients_list(request: Request):
    q = request.query_params.get("q", "").strip()
    co = request.state.user["company_id"]
    sql = ("SELECT c.*, COUNT(p.id) AS n_plants FROM clients c LEFT JOIN plants p ON p.client_id = c.id "
           "WHERE c.company_id = ? {where} GROUP BY c.id ORDER BY c.name")
    if q:
        rows = db.all_(sql.format(where="AND (c.name LIKE ? OR c.email LIKE ? OR c.address LIKE ?)"),
                       co, f"%{q}%", f"%{q}%", f"%{q}%")
    else:
        rows = db.all_(sql.format(where=""), co)
    return render(request, "clients.html", clients=rows, q=q)


CLIENT_FIELDS = ("name", "email", "phone", "address", "notes")


@guard("edit")
async def client_new(request: Request):
    if request.method == "GET":
        return render(request, "client_form.html", client={}, errors=[])
    form = request.state.form
    data = {f: form.get(f, "").strip() for f in CLIENT_FIELDS}
    if not data["name"]:
        return render(request, "client_form.html", client=data, errors=["Il nome è obbligatorio."])
    co = request.state.user["company_id"]
    # controllo del limite e inserimento nella stessa istruzione: due richieste insieme non lo superano
    with db.connect() as c:
        cur = c.execute("INSERT INTO clients(name, email, phone, address, notes, company_id, created_at) "
                        "SELECT ?, ?, ?, ?, ?, ?, ? WHERE " + LIMIT_CLIENTS,
                        (*[data[f] for f in CLIENT_FIELDS], co, db.now(), co, co, co))
        cid = cur.lastrowid if cur.rowcount else None
    if not cid:
        return render(request, "client_form.html", client=data, errors=[LIMIT_MSG["clients"].format(**usage(co))])
    return redirect(f"/clienti/{cid}", "Cliente creato.", request)


@guard()
async def client_detail(request: Request):
    client = db.one("SELECT * FROM clients WHERE id = ?", request.path_params["cid"])
    if not client:
        return render(request, "error.html", message="Cliente non trovato.")
    plants = db.all_("SELECT * FROM plants WHERE client_id = ? ORDER BY name", client["id"])
    for p in plants:
        p["status"] = plant_status(p)
        p["diag"] = sanitize.stored_diagnostics(p["last_diag"])
    return render(request, "client_detail.html", client=client, plants=plants)


@guard("edit")
async def client_edit(request: Request):
    cid = request.path_params["cid"]
    client = db.one("SELECT * FROM clients WHERE id = ?", cid)
    if not client:
        return render(request, "error.html", message="Cliente non trovato.")
    if request.method == "GET":
        return render(request, "client_form.html", client=client, errors=[])
    form = request.state.form
    data = {f: form.get(f, "").strip() for f in CLIENT_FIELDS}
    if not data["name"]:
        return render(request, "client_form.html", client={**client, **data}, errors=["Il nome è obbligatorio."])
    db.run("UPDATE clients SET name = ?, email = ?, phone = ?, address = ?, notes = ? WHERE id = ?",
           *[data[f] for f in CLIENT_FIELDS], cid)
    return redirect(f"/clienti/{cid}", "Cliente aggiornato.", request)


def _confirmed(form, name):
    """Le eliminazioni chiedono di riscrivere il nome: un clic per sbaglio non basta."""
    return " ".join((form.get("confirm_name") or "").split()).casefold() == " ".join((name or "").split()).casefold()


@guard("settings")
async def client_delete(request: Request):
    cid = request.path_params["cid"]
    client = db.one("SELECT * FROM clients WHERE id = ?", cid)
    if not client:
        return render(request, "error.html", message="Cliente non trovato.")
    if not _confirmed(request.state.form, client["name"]):
        return redirect(f"/clienti/{cid}/modifica", "Per eliminare il cliente scrivi il suo nome esatto.", request, error=True)
    n = db.one("SELECT COUNT(*) AS n FROM plants WHERE client_id = ?", cid)["n"]
    db.run("DELETE FROM clients WHERE id = ?", cid)   # impianti, ticket e comandi vanno via a cascata
    log.info("Cliente %s «%s» eliminato da %s (%s impianti)", cid, client["name"], request.state.user["username"], n)
    return redirect("/clienti", f"Cliente «{client['name']}» eliminato" + (f" insieme a {n} impianti." if n > 1 else
                    " insieme al suo impianto." if n == 1 else "."), request)


# ---------- impianti ----------

# Limiti decisi dal gestore per azienda (NULL = nessun limite). Contano tutti gli impianti
# inseriti, anche non abbinati o revocati.
LIMIT_CLIENTS = ("((SELECT max_clients FROM companies WHERE id = ?) IS NULL OR "
                 "(SELECT COUNT(*) FROM clients WHERE company_id = ?) < (SELECT max_clients FROM companies WHERE id = ?))")
LIMIT_PLANTS = ("((SELECT max_plants FROM companies WHERE id = ?) IS NULL OR "
                "(SELECT COUNT(*) FROM plants p JOIN clients c ON c.id = p.client_id WHERE c.company_id = ?) "
                "< (SELECT max_plants FROM companies WHERE id = ?))")
LIMIT_MSG = {"clients": "Hai raggiunto il limite di {max_clients} clienti previsto per la tua azienda: "
                        "per aggiungerne altri chiedi al gestore del server di alzarlo.",
             "plants": "Hai raggiunto il limite di {max_plants} impianti previsto per la tua azienda: "
                       "per aggiungerne altri elimina un impianto o chiedi al gestore di alzare il limite."}


def usage(company_id):
    """Clienti e impianti dell'azienda rispetto ai limiti (per pagine e messaggi)."""
    u = db.one("SELECT co.max_clients, co.max_plants, "
               "(SELECT COUNT(*) FROM clients WHERE company_id = co.id) AS clients, "
               "(SELECT COUNT(*) FROM plants p JOIN clients c ON c.id = p.client_id WHERE c.company_id = co.id) AS plants "
               "FROM companies co WHERE co.id = ?", company_id) or {}
    u["clients_full"] = bool(u.get("max_clients")) and u["clients"] >= u["max_clients"]
    u["plants_full"] = bool(u.get("max_plants")) and u["plants"] >= u["max_plants"]
    return u

def _issue_pair_code(plant_id):
    code = new_pair_code()
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
    db.run("UPDATE plants SET pair_code = ?, pair_expires = ? WHERE id = ?", code, expires, plant_id)
    return code


@guard("edit")
async def plant_new(request: Request):
    cid = request.path_params["cid"]
    if not db.one("SELECT id FROM clients WHERE id = ?", cid):
        return render(request, "error.html", message="Cliente non trovato.")
    name = request.state.form.get("name", "").strip() or "Impianto principale"
    co = request.state.user["company_id"]
    with db.connect() as c:
        cur = c.execute("INSERT INTO plants(client_id, name, created_at) SELECT ?, ?, ? WHERE " + LIMIT_PLANTS,
                        (cid, name, db.now(), co, co, co))
        pid = cur.lastrowid if cur.rowcount else None
    if not pid:
        return redirect(f"/clienti/{cid}", LIMIT_MSG["plants"].format(**usage(co)), request, error=True)
    _issue_pair_code(pid)
    return redirect(f"/impianti/{pid}", "Impianto creato. Inserisci il codice di abbinamento nell'add-on.", request)


def _load_plant(pid):
    p = db.one("SELECT p.*, c.name AS client_name FROM plants p JOIN clients c ON c.id = p.client_id "
               "WHERE p.id = ?", pid)
    if p:
        p["status"] = plant_status(p)
        p["online"] = is_online(p)
        p["diag"] = sanitize.stored_diagnostics(p["last_diag"])
        p["admin"] = sanitize.admin_state(db.loads(p.get("admin_state"), None)) or {}
    return p


@guard()
async def plant_detail(request: Request):
    plant = _load_plant(request.path_params["pid"])
    if not plant:
        return render(request, "error.html", message="Impianto non trovato.")
    commands = db.all_("SELECT c.*, u.username FROM commands c LEFT JOIN users u ON u.id = c.created_by "
                       "WHERE plant_id = ? ORDER BY c.id DESC LIMIT 15", plant["id"])
    tickets = db.all_("SELECT * FROM tickets WHERE plant_id = ? ORDER BY updated_at DESC LIMIT 20", plant["id"])
    # La password dell'accesso amministratore si mostra solo a chi può usarla (non al ruolo lettura)
    admin_secret = secrets_store.get(plant["id"]) if can(request.state.user, "edit") else None
    return render(request, "plant_detail.html", plant=plant, commands=commands, tickets=tickets, admin_secret=admin_secret,
                  now_ts=time.time())


@guard()
async def plant_state_json(request: Request):
    """Usato dalla pagina impianto per aggiornarsi da sola senza ricaricare."""
    plant = _load_plant(request.path_params["pid"])
    if not plant:
        return JSONResponse({"error": "not found"}, status_code=404)
    cmds = db.all_("SELECT id, status FROM commands WHERE plant_id = ? ORDER BY id DESC LIMIT 15", plant["id"])
    return JSONResponse({"online": plant["online"], "status": plant["status"], "last_seen": ago(plant["last_seen"]),
                         "last_diag_at": plant["last_diag_at"], "commands": cmds, "admin": admin_key(plant)})


def admin_key(plant):
    """Riassunto dello stato amministratore: se cambia, la pagina dell'impianto si ricarica da sola."""
    a = plant.get("admin") or {}
    return f'{a.get("active")}|{a.get("until")}|{a.get("username")}|{plant.get("admin_requested_at")}|{a.get("declined")}'


templates.env.globals["admin_key"] = admin_key


@guard("edit")
async def plant_pair_code(request: Request):
    pid = request.path_params["pid"]
    plant = db.one("SELECT * FROM plants WHERE id = ?", pid)
    if not plant:
        return render(request, "error.html", message="Impianto non trovato.")
    # Nuovo abbinamento: il token precedente smette di funzionare.
    db.run("UPDATE plants SET token_hash = NULL, paired_at = NULL, revoked = 0 WHERE id = ?", pid)
    _issue_pair_code(pid)
    return redirect(f"/impianti/{pid}", "Nuovo codice generato. Il vecchio abbinamento non è più valido.", request)


@guard("edit")
async def plant_revoke(request: Request):
    pid = request.path_params["pid"]
    db.run("UPDATE plants SET revoked = 1, token_hash = NULL, pair_code = NULL WHERE id = ?", pid)
    return redirect(f"/impianti/{pid}", "Accesso revocato: l'agente non potrà più collegarsi.", request)


@guard("edit")
async def plant_rename(request: Request):
    pid = request.path_params["pid"]
    name = " ".join(request.state.form.get("name", "").split())[:80]
    if not name:
        return redirect(f"/impianti/{pid}", "Il nome dell'impianto non può essere vuoto.", request, error=True)
    db.run("UPDATE plants SET name = ? WHERE id = ?", name, pid)
    return redirect(f"/impianti/{pid}", "Nome dell'impianto aggiornato.", request)


@guard("settings")
async def plant_delete(request: Request):
    pid = request.path_params["pid"]
    plant = db.one("SELECT * FROM plants WHERE id = ?", pid)
    if not plant:
        return render(request, "error.html", message="Impianto non trovato.")
    if not _confirmed(request.state.form, plant["name"]):
        return redirect(f"/impianti/{pid}", "Per eliminare l'impianto scrivi il suo nome esatto.", request, error=True)
    db.run("DELETE FROM plants WHERE id = ?", pid)   # ticket e comandi vanno via a cascata
    log.info("Impianto %s «%s» eliminato da %s", pid, plant["name"], request.state.user["username"])
    return redirect(f"/clienti/{plant['client_id']}", f"Impianto «{plant['name']}» eliminato.", request)


@guard("commands")
async def plant_command(request: Request):
    pid = request.path_params["pid"]
    plant = _load_plant(pid)
    name = request.state.form.get("name", "")
    if not plant or name not in ALLOWED_COMMANDS:
        return render(request, "error.html", message="Comando non valido.")
    consent = remote_access(plant)
    if name != "ping" and not consent:
        return redirect(f"/impianti/{pid}", "Il cliente ha sospeso l'accesso remoto: comando non inviato.", request, error=True)
    db.run("INSERT INTO commands(plant_id, name, args, created_by, created_at) VALUES(?, ?, '{}', ?, ?)",
           plant["id"], name, request.state.user["id"], db.now())
    ring(plant["id"])
    return redirect(f"/impianti/{pid}", f"Comando «{ALLOWED_COMMANDS[name]}» inviato.", request)


# ---------- accesso amministratore temporaneo ----------

def person_name(user):
    """Nome con cui il cliente vede la persona dell'installatore (nome completo, altrimenti utente)."""
    return ((user.get("full_name") or "").strip() or user["username"])[:60]


def _admin_command(plant, name, request):
    # il nome di chi agisce viaggia col comando: il cliente vede nella notifica chi è entrato o ha chiuso
    db.run("INSERT INTO commands(plant_id, name, args, created_by, created_at) VALUES(?, ?, ?, ?, ?)",
           plant["id"], name, json.dumps({"by": person_name(request.state.user)}), request.state.user["id"], db.now())
    ring(plant["id"])


@guard("edit")
async def plant_admin(request: Request):
    pid = request.path_params["pid"]
    plant = _load_plant(pid)
    if not plant or not plant["paired_at"] or plant["revoked"]:
        return render(request, "error.html", message="Impianto non trovato o non collegato.")
    op = request.state.form.get("op")
    active = plant["admin"].get("active")
    if op == "richiedi":
        if active:
            return redirect(f"/impianti/{pid}", "Il cliente ha già concesso l'accesso amministratore.", request)
        db.run("UPDATE plants SET admin_requested_at = ?, admin_requested_by = ? WHERE id = ?",
               db.now(), person_name(request.state.user), pid)
        return redirect(f"/impianti/{pid}", "Richiesta inviata: il cliente riceverà una notifica in Home Assistant.", request)
    if op == "annulla":
        db.run("UPDATE plants SET admin_requested_at = NULL, admin_requested_by = NULL WHERE id = ?", pid)
        return redirect(f"/impianti/{pid}", "Richiesta annullata.", request)
    if op == "password":
        if not active:
            return redirect(f"/impianti/{pid}", "Il cliente non ha concesso l'accesso amministratore.", request, error=True)
        secrets_store.drop(plant["id"])
        _admin_command(plant, "admin_password", request)
        return redirect(f"/impianti/{pid}", "Richiesta inviata all'agente: tra pochi secondi compare la password.", request)
    if op == "chiudi":
        secrets_store.drop(plant["id"])
        _admin_command(plant, "admin_close", request)
        return redirect(f"/impianti/{pid}", "Chiusura inviata: l'agente cancellerà l'utente amministratore.", request)
    return render(request, "error.html", message="Operazione non valida.")


@guard()
async def command_detail(request: Request):
    cmd = db.one("SELECT * FROM commands WHERE id = ?", request.path_params["cmd"])
    if not cmd:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({**cmd, "result": db.loads(cmd["result"])})


# ---------- ticket ----------

@guard()
async def tickets_list(request: Request):
    status = request.query_params.get("stato", "aperti")
    where = {"aperti": "t.status != 'chiuso'", "chiusi": "t.status = 'chiuso'", "tutti": "1=1"}.get(status, "1=1")
    rows = db.all_("SELECT t.*, p.name AS plant_name, c.name AS client_name, u.username AS assigned "
                   "FROM tickets t JOIN plants p ON p.id = t.plant_id JOIN clients c ON c.id = p.client_id "
                   f"LEFT JOIN users u ON u.id = t.assigned_to WHERE c.company_id = ? AND {where} "
                   "ORDER BY t.updated_at DESC", request.state.user["company_id"])
    return render(request, "tickets.html", tickets=rows, stato=status)


@guard()
async def ticket_detail(request: Request):
    t = db.one("SELECT t.*, p.name AS plant_name, p.id AS plant_id, c.name AS client_name, c.id AS client_id, "
               "c.phone AS client_phone, c.email AS client_email FROM tickets t JOIN plants p ON p.id = t.plant_id "
               "JOIN clients c ON c.id = p.client_id WHERE t.id = ?", request.path_params["tid"])
    if not t:
        return render(request, "error.html", message="Ticket non trovato.")
    t["diag"] = sanitize.stored_diagnostics(t["diagnostics"])
    messages = db.all_("SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY id", t["id"])
    users = db.all_("SELECT id, username, full_name FROM users WHERE active = 1 AND role != 'lettura' "
                    "AND company_id = ? ORDER BY username", request.state.user["company_id"])
    return render(request, "ticket_detail.html", t=t, messages=messages, users=users)


@guard("edit")
async def ticket_reply(request: Request):
    tid = request.path_params["tid"]
    if not db.one("SELECT id FROM tickets WHERE id = ?", tid):
        return render(request, "error.html", message="Ticket non trovato.")
    form = request.state.form
    text = form.get("text", "").strip()
    assigned = form.get("assigned_to")
    if assigned:
        if not assigned.isdigit() or not db.one("SELECT id FROM users WHERE id = ? AND active = 1 AND role != 'lettura' "
                                                "AND company_id = ?", int(assigned), request.state.user["company_id"]):
            return redirect(f"/ticket/{tid}", "Utente da assegnare non valido.", request, error=True)
    ts = db.now()
    if text:
        u = request.state.user
        db.run("INSERT INTO ticket_messages(ticket_id, author_type, author_name, text, created_at) "
               "VALUES(?, 'installatore', ?, ?, ?)", tid, person_name(u), text[:5000], ts)
    status = form.get("status")
    if status in TICKET_STATUS:
        db.run("UPDATE tickets SET status = ? WHERE id = ?", status, tid)
    if assigned is not None:
        db.run("UPDATE tickets SET assigned_to = ? WHERE id = ?", int(assigned) if assigned else None, tid)
    db.run("UPDATE tickets SET updated_at = ? WHERE id = ?", ts, tid)
    return redirect(f"/ticket/{tid}", "Ticket aggiornato.", request)


# ---------- utenti e impostazioni (solo amministratore) ----------

@guard("users")
async def users_page(request: Request):
    errors = []
    if request.method == "POST":
        form = request.state.form
        username = form.get("username", "").strip().lower()
        password = form.get("password", "")
        role = form.get("role", "tecnico")
        if len(username) < 3:
            errors.append("Il nome utente deve avere almeno 3 caratteri.")
        if len(password) < 10:
            errors.append("La password deve avere almeno 10 caratteri.")
        if role not in ROLES:
            errors.append("Ruolo non valido.")
        email = form.get("email", "").strip()
        if email and not valid_email(email):
            errors.append("Indirizzo email non valido.")
        # i nomi utente sono unici su tutto il server: il login è uno solo per tutte le aziende
        if db.one("SELECT id FROM users WHERE username = ?", username):
            errors.append("Nome utente già in uso (anche da un'altra azienda dello stesso server): scegline un altro.")
        if not errors:
            # password iniziale scelta da altri: al primo accesso va cambiata
            db.run("INSERT INTO users(username, full_name, email, password_hash, role, company_id, must_change, created_at) "
                   "VALUES(?, ?, ?, ?, ?, ?, 1, ?)", username, form.get("full_name", "").strip(), email or None,
                   hash_password(password), role, request.state.user["company_id"], db.now())
            return redirect("/utenti", "Utente creato: al primo accesso dovrà scegliere una password sua.", request)
    return _users_render(request, errors)


def _users_render(request, errors=None, reset=None):
    users = db.all_("SELECT * FROM users WHERE company_id = ? ORDER BY active DESC, username", request.state.user["company_id"])
    return render(request, "users.html", users=users, errors=errors or [], reset=reset,
                  reset_minutes=accounts.RESET_MINUTES)


MIN_PASSWORD = 10


@guard()
async def account_page(request: Request):
    """Il mio account: nome visualizzato e cambio password (per qualsiasi ruolo)."""
    user = request.state.user
    errors = []
    if request.method == "POST":
        form = request.state.form
        if form.get("azione") == "profilo":
            email = form.get("email", "").strip()
            if email and not valid_email(email):
                return redirect("/account", "Indirizzo email non valido.", request, error=True)
            db.run("UPDATE users SET full_name = ?, email = ? WHERE id = ?", form.get("full_name", "").strip(),
                   email or None, user["id"])
            return redirect("/account", "Profilo aggiornato.", request)
        current, new, new2 = form.get("current", ""), form.get("password", ""), form.get("password2", "")
        if not verify_password(current, user["password_hash"]):
            errors.append("La password attuale non è corretta.")
        if len(new) < MIN_PASSWORD:
            errors.append(f"La nuova password deve avere almeno {MIN_PASSWORD} caratteri.")
        if new != new2:
            errors.append("Le due password nuove non coincidono.")
        if new and new == current:
            errors.append("La nuova password deve essere diversa da quella attuale.")
        if not errors:
            db.run("UPDATE users SET password_hash = ?, must_change = 0 WHERE id = ?", hash_password(new), user["id"])
            request.session["pw"] = pw_mark(db.one("SELECT password_hash FROM users WHERE id = ?", user["id"]))
            return redirect("/", "Password cambiata.", request) if user["must_change"] else \
                redirect("/account", "Password cambiata.", request)
    return render(request, "account.html", errors=errors)


@guard("users")
async def user_reset_link(request: Request):
    """L'amministratore crea un link di recupero per un suo utente: la nuova password la sceglie l'utente."""
    uid = request.path_params["uid"]
    target = db.one("SELECT * FROM users WHERE id = ?", uid)
    if not target or not target["active"]:
        return redirect("/utenti", "Utente non trovato o disattivato.", request, error=True)
    if target["is_manager"]:
        # un altro amministratore dell'azienda non deve poter prendere il posto del gestore del server
        return redirect("/utenti", "Il gestore recupera la password solo dal server: sudo ha-suite password " +
                        target["username"], request, error=True)
    token = accounts.create_reset(target["id"], request.state.user["username"])
    log.info("Link di recupero per %s creato da %s", target["username"], request.state.user["username"])
    return _users_render(request, reset={"username": target["username"], "url": accounts.reset_url(token, public_url(request))})


@guard("users")
async def user_toggle(request: Request):
    uid = request.path_params["uid"]
    if uid == request.state.user["id"]:
        return redirect("/utenti", "Non puoi disattivare te stesso.", request, error=True)
    if db.one("SELECT id FROM users WHERE id = ? AND is_manager = 1", uid):
        return redirect("/utenti", "Il gestore del server non si può disattivare.", request, error=True)
    db.run("UPDATE users SET active = 1 - active WHERE id = ?", uid)
    return redirect("/utenti", "Utente aggiornato.", request)


@guard("settings")
async def settings_page(request: Request):
    user = request.state.user
    co = user["company_id"]
    if request.method == "POST":
        form = request.state.form
        name = " ".join(form.get("company_name", "").split())[:80]
        if name:
            db.run("UPDATE companies SET name = ? WHERE id = ?", name, co)
        color = form.get("brand_color", "")
        if valid_color(color):
            db.run("UPDATE companies SET brand_color = ? WHERE id = ?", color, co)
        # l'indirizzo pubblico è uno solo per tutto il server: lo decide il gestore
        if can(user, "server") and "public_url" in form:
            url = form.get("public_url", "").strip().rstrip("/")
            if url and not (url.startswith("https://") or url.startswith("http://")):
                return redirect("/impostazioni", "L'indirizzo pubblico deve iniziare con https://", request, error=True)
            db.set_setting("public_url", url)
        return redirect("/impostazioni", "Impostazioni salvate.", request)
    return render(request, "settings.html", guessed_url=guess_public_url(request),
                  saved_url=db.get_setting("public_url", ""),
                  tg_token_set=bool(db.get_csetting(co, "telegram_token")),
                  tg_chat_id=db.get_csetting(co, "telegram_chat_id", ""),
                  tg_events={k: (label, db.get_csetting(co, k, "1") == "1") for k, label in notify.EVENTS.items()},
                  upd=_update_status(co) if can(user, "server") else None,
                  mail=_mail_settings() if can(user, "server") else None)


def _mail_settings():
    m = accounts.smtp_settings()
    m["password_set"] = bool(m.pop("smtp_password"))
    m["enabled"] = db.get_setting("email_reset", "0") == "1"
    m["sender_name"] = db.get_setting("email_sender_name", "")
    m["security_options"] = accounts.SMTP_SECURITY
    return m


@guard("server")
async def email_settings(request: Request):
    """Il gestore sceglie se attivare «Password dimenticata?» via email e configura il server SMTP."""
    form, user = request.state.form, request.state.user
    action = form.get("azione", "salva")
    if action == "disattiva":
        db.set_setting("email_reset", "0")
        return redirect("/impostazioni#email", "Recupero della password via email disattivato.", request)
    port = form.get("smtp_port", "").strip()
    if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
        return redirect("/impostazioni#email", "Porta SMTP non valida.", request, error=True)
    sender = form.get("smtp_from", "").strip()
    if sender and not valid_email(sender):
        return redirect("/impostazioni#email", "Indirizzo del mittente non valido.", request, error=True)
    security = form.get("smtp_security", "starttls")
    db.set_setting("smtp_host", form.get("smtp_host", "").strip()[:200])
    db.set_setting("smtp_port", port)
    db.set_setting("smtp_security", security if security in accounts.SMTP_SECURITY else "starttls")
    db.set_setting("smtp_user", form.get("smtp_user", "").strip()[:200])
    db.set_setting("smtp_from", sender)
    db.set_setting("email_sender_name", " ".join(form.get("email_sender_name", "").split())[:80])
    if form.get("smtp_password"):
        db.set_setting("smtp_password", form.get("smtp_password"))
    enable = form.get("email_reset") == "1"
    if enable and not (db.get_setting("smtp_host") and sender):
        return redirect("/impostazioni#email", "Per attivarlo servono almeno server SMTP e mittente.", request, error=True)
    db.set_setting("email_reset", "1" if enable else "0")
    if action == "prova":
        if not user.get("email"):
            return redirect("/impostazioni#email", "Inserisci prima la tua email in «Il mio account»: la prova arriva lì.",
                            request, error=True)
        try:
            await asyncio.to_thread(accounts.send_mail, user["email"], "Prova del server di assistenza",
                                    "Se leggi questo messaggio, l'invio delle email di recupero della password funziona.")
        except RuntimeError as e:
            return redirect("/impostazioni#email", f"Email non inviata: {e}", request, error=True)
        return redirect("/impostazioni#email", f"Email di prova inviata a {user['email']}.", request)
    return redirect("/impostazioni#email", "Impostazioni email salvate." + (" «Password dimenticata?» ora compare nella pagina di accesso."
                                                                             if enable else ""), request)


def _update_status(company_id):
    st = updates.status()
    for k in ("checked_at", "attempt_at"):
        st[k] = datetime.fromtimestamp(st[k], timezone.utc) if st[k] else None
    agents = db.all_("SELECT p.agent_version FROM plants p JOIN clients c ON c.id = p.client_id WHERE c.company_id = ? "
                     "AND p.paired_at IS NOT NULL AND p.revoked = 0 AND p.agent_version IS NOT NULL", company_id)
    st["outdated_agents"] = sum(1 for p in agents if updates.agent_outdated(p["agent_version"]))
    return st


@guard("server")
async def update_check(request: Request):
    error = await asyncio.to_thread(updates.check_now)
    if error:
        return redirect("/impostazioni#aggiornamenti", f"Controllo non riuscito: {error}", request, error=True)
    new = updates.server_update()
    return redirect("/impostazioni#aggiornamenti",
                    f"È disponibile la versione {new} del server." if new else "Controllo eseguito: nessun aggiornamento del server.",
                    request)


@guard("settings")
async def telegram_settings(request: Request):
    form = request.state.form
    co = request.state.user["company_id"]
    action = form.get("azione", "salva")
    token = form.get("telegram_token", "").strip()
    if token:
        db.set_csetting(co, "telegram_token", token)
    if action == "rimuovi":
        for k in ("telegram_token", "telegram_chat_id"):
            db.set_csetting(co, k, "")
        return redirect("/impostazioni#telegram", "Notifiche Telegram disattivate.", request)
    if action == "rileva":
        if not db.get_csetting(co, "telegram_token"):
            return redirect("/impostazioni#telegram", "Inserisci prima il token del bot.", request, error=True)
        try:
            chat_id, name = await asyncio.to_thread(notify.detect_chat_id, db.get_csetting(co, "telegram_token"))
        except RuntimeError as e:
            return redirect("/impostazioni#telegram", f"Telegram: {e}", request, error=True)
        if not chat_id:
            return redirect("/impostazioni#telegram",
                            "Nessun messaggio trovato: apri il bot su Telegram, scrivigli /start e riprova.", request, error=True)
        db.set_csetting(co, "telegram_chat_id", chat_id)
        return redirect("/impostazioni#telegram", f"Chat trovata: {name or chat_id}. Ora prova con «Invia prova».", request)
    chat_id = form.get("telegram_chat_id", "").strip()
    if chat_id:
        db.set_csetting(co, "telegram_chat_id", chat_id)
    for key in notify.EVENTS:
        db.set_csetting(co, key, "1" if form.get(key) == "1" else "0")
    if action == "prova":
        if not notify.configured(co):
            return redirect("/impostazioni#telegram", "Servono sia il token del bot sia il chat ID.", request, error=True)
        try:
            await asyncio.to_thread(notify.send_now, co, f"✅ Notifiche di <b>{notify.esc(db.company(co)['name'])}</b> "
                                                     "attive: riceverai qui gli avvisi sui tuoi impianti.")
        except RuntimeError as e:
            return redirect("/impostazioni#telegram", f"Invio non riuscito: {e}", request, error=True)
        return redirect("/impostazioni#telegram", "Messaggio di prova inviato: controlla Telegram.", request)
    return redirect("/impostazioni#telegram", "Impostazioni Telegram salvate.", request)


# ---------- aziende (solo il gestore del server) ----------

@guard("companies")
async def companies_page(request: Request):
    """Il gestore crea le aziende e ne vede solo i numeri: nessun nome di cliente, impianto o ticket."""
    errors, form_data = [], {}
    if request.method == "POST":
        form = request.state.form
        form_data = {k: form.get(k, "") for k in ("name", "brand_color", "full_name", "username", "email",
                                                  "max_clients", "max_plants")}
        name = " ".join(form.get("name", "").split())[:80]
        username = form.get("username", "").strip().lower()
        password = form.get("password", "")
        if not name:
            errors.append("Inserisci il nome dell'azienda.")
        elif db.one("SELECT id FROM companies WHERE lower(name) = lower(?)", name):
            errors.append("Esiste già un'azienda con questo nome.")
        if len(username) < 3:
            errors.append("Il nome utente dell'amministratore deve avere almeno 3 caratteri.")
        elif db.one("SELECT id FROM users WHERE username = ?", username):
            errors.append("Nome utente già in uso: scegline un altro.")
        if len(password) < MIN_PASSWORD:
            errors.append(f"La password iniziale deve avere almeno {MIN_PASSWORD} caratteri.")
        email = form.get("email", "").strip()
        if email and not valid_email(email):
            errors.append("Indirizzo email non valido.")
        limits = {}
        for key, label in (("max_clients", "clienti"), ("max_plants", "impianti")):
            raw = form.get(key, "").strip()
            if raw and (not raw.isdigit() or int(raw) < 1 or int(raw) > 100000):
                errors.append(f"Limite di {label} non valido: un numero da 1 in su, o vuoto per nessun limite.")
            limits[key] = int(raw) if raw.isdigit() else None
        if not errors:
            color = form.get("brand_color", "")
            with db.connect() as c:
                co = c.execute("INSERT INTO companies(name, brand_color, max_clients, max_plants, created_at) "
                               "VALUES(?, ?, ?, ?, ?)", (name, color if valid_color(color) else DEFAULT_COLOR,
                                                         limits["max_clients"], limits["max_plants"], db.now())).lastrowid
                c.execute("INSERT INTO users(username, full_name, email, password_hash, role, company_id, must_change, created_at) "
                          "VALUES(?, ?, ?, ?, 'admin', ?, 1, ?)", (username, form.get("full_name", "").strip(), email or None,
                                                                    hash_password(password), co, db.now()))
            log.info("Azienda %s «%s» creata da %s", co, name, request.state.user["username"])
            return redirect("/aziende", f"Azienda «{name}» creata. Comunica a {username} indirizzo, nome utente e "
                                        "password iniziale: al primo accesso dovrà sceglierne una sua.", request)
    return _companies_render(request, errors, form_data)


def _companies_render(request, errors=None, form_data=None, reset=None):
    companies = db.all_(
        "SELECT co.id, co.name, co.brand_color, co.created_at, co.suspended_at, co.max_clients, co.max_plants, "
        "(SELECT COUNT(*) FROM clients c WHERE c.company_id = co.id) AS n_clients, "
        "(SELECT COUNT(*) FROM plants p JOIN clients c ON c.id = p.client_id WHERE c.company_id = co.id) AS n_plants "
        "FROM companies co ORDER BY co.name")
    for c in companies:
        # per il link di recupero: solo gli amministratori attivi (nessun altro dato dell'azienda)
        c["admins"] = db.all_("SELECT id, username, full_name FROM users WHERE company_id = ? AND role = 'admin' "
                              "AND active = 1 AND is_manager = 0 ORDER BY username", c["id"])
    totals = {"companies": len(companies), "clients": sum(c["n_clients"] for c in companies),
              "plants": sum(c["n_plants"] for c in companies)}
    return render(request, "companies.html", companies=companies, totals=totals, errors=errors or [],
                  form=form_data or {}, reset=reset, reset_minutes=accounts.RESET_MINUTES)


def _company_counts(co):
    return db.one("SELECT (SELECT COUNT(*) FROM clients WHERE company_id = ?) AS clients, "
                  "(SELECT COUNT(*) FROM plants p JOIN clients c ON c.id = p.client_id WHERE c.company_id = ?) AS plants",
                  co, co)


@guard("companies")
async def company_limits(request: Request):
    co = request.path_params["co"]
    form, counts = request.state.form, _company_counts(co)
    values = {}
    for key, label, used in (("max_clients", "clienti", counts["clients"]), ("max_plants", "impianti", counts["plants"])):
        raw = form.get(key, "").strip()
        if not raw:
            values[key] = None
            continue
        if not raw.isdigit() or not 1 <= int(raw) <= 100000:
            return redirect(f"/aziende#a{co}", f"Limite di {label} non valido: un numero da 1 in su, o vuoto per nessun limite.",
                            request, error=True)
        if int(raw) < used:
            # non si cancella nulla da soli: l'azienda deve prima eliminare quelli in più
            return redirect(f"/aziende#a{co}", f"L'azienda ha già {used} {label}: per scendere a {raw} deve prima "
                                                f"eliminarne {used - int(raw)}.", request, error=True)
        values[key] = int(raw)
    db.run("UPDATE companies SET max_clients = ?, max_plants = ? WHERE id = ?", values["max_clients"], values["max_plants"], co)
    return redirect(f"/aziende#a{co}", "Limiti aggiornati.", request)


@guard("companies")
async def company_suspend(request: Request):
    co = request.path_params["co"]
    if co == request.state.user["company_id"]:
        return redirect("/aziende", "Non puoi sospendere la tua azienda.", request, error=True)
    company = db.company(co)
    if company["suspended_at"]:
        db.run("UPDATE companies SET suspended_at = NULL WHERE id = ?", co)
        # gli agenti si ricollegano entro qualche minuto: niente avvisi «offline» per la pausa appena finita
        db.run("UPDATE plants SET alert_online = NULL WHERE client_id IN (SELECT id FROM clients WHERE company_id = ?)", co)
        log.info("Azienda %s «%s» riattivata da %s", co, company["name"], request.state.user["username"])
        return redirect(f"/aziende#a{co}", f"«{company['name']}» riattivata: i suoi utenti e impianti tornano a funzionare.", request)
    db.run("UPDATE companies SET suspended_at = ? WHERE id = ?", db.now(), co)
    for p in db.all_("SELECT p.id FROM plants p JOIN clients c ON c.id = p.client_id WHERE c.company_id = ?", co):
        secrets_store.drop(p["id"])
    log.info("Azienda %s «%s» sospesa da %s", co, company["name"], request.state.user["username"])
    return redirect(f"/aziende#a{co}", f"«{company['name']}» sospesa: i suoi utenti non entrano più e i suoi impianti "
                                       "sono in pausa (nessun dato perso).", request)


@guard("companies")
async def company_delete(request: Request):
    co = request.path_params["co"]
    user, form = request.state.user, request.state.form
    company = db.company(co)
    if co == user["company_id"]:
        return redirect("/aziende", "Non puoi eliminare la tua azienda.", request, error=True)
    if not _confirmed(form, company["name"]) or not verify_password(form.get("password", ""), user["password_hash"]):
        return redirect(f"/aziende#a{co}", "Per eliminare l'azienda scrivi il suo nome esatto e la tua password.",
                        request, error=True)
    path = await asyncio.to_thread(db.backup, f"prima-di-eliminare-azienda{co}")
    plants = [p["id"] for p in db.all_("SELECT p.id FROM plants p JOIN clients c ON c.id = p.client_id "
                                       "WHERE c.company_id = ?", co)]
    counts = _company_counts(co)
    db.run("DELETE FROM companies WHERE id = ?", co)  # utenti, clienti, impianti, ticket e comandi a cascata
    for pid in plants:
        secrets_store.drop(pid)
    log.info("Azienda %s «%s» eliminata da %s (%s clienti, %s impianti); copia di sicurezza: %s",
             co, company["name"], user["username"], counts["clients"], counts["plants"], path)
    return redirect("/aziende", f"Azienda «{company['name']}» eliminata con {counts['clients']} clienti e "
                                f"{counts['plants']} impianti. Copia di sicurezza: {os.path.basename(path)}", request)


@guard("companies")
async def company_reset_link(request: Request):
    co = request.path_params["co"]
    uid = request.state.form.get("user_id", "")
    target = db.one("SELECT * FROM users WHERE id = ? AND company_id = ? AND role = 'admin' AND active = 1 AND is_manager = 0",
                    int(uid) if uid.isdigit() else 0, co)
    if not target:
        return redirect(f"/aziende#a{co}", "Scegli un amministratore dell'azienda.", request, error=True)
    token = accounts.create_reset(target["id"], request.state.user["username"])
    log.info("Link di recupero per %s (azienda %s) creato dal gestore %s", target["username"], co, request.state.user["username"])
    return _companies_render(request, reset={"username": target["username"], "company": db.company(co)["name"],
                                             "url": accounts.reset_url(token, public_url(request))})


@guard("edit")
async def plant_ha_url(request: Request):
    pid = request.path_params["pid"]
    url = request.state.form.get("ha_url_manual", "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        return redirect(f"/impianti/{pid}", "L'indirizzo deve iniziare con http:// o https://", request, error=True)
    db.run("UPDATE plants SET ha_url_manual = ? WHERE id = ?", url or None, pid)
    return redirect(f"/impianti/{pid}", "Indirizzo di Home Assistant salvato." if url else
                    "Indirizzo manuale rimosso: uso quello comunicato dall'agente.", request)


async def health(request: Request):
    return Response("ok")


routes = [
    Route("/health", health),
    Route("/setup", setup, methods=["GET", "POST"]),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/recupero", reset_request, methods=["GET", "POST"]),
    Route("/recupero/{token}", reset_use, methods=["GET", "POST"]),
    Route("/", dashboard),
    Route("/clienti", clients_list),
    Route("/clienti/nuovo", client_new, methods=["GET", "POST"]),
    Route("/clienti/{cid:int}", client_detail),
    Route("/clienti/{cid:int}/modifica", client_edit, methods=["GET", "POST"]),
    Route("/clienti/{cid:int}/impianti", plant_new, methods=["POST"]),
    Route("/impianti/{pid:int}", plant_detail),
    Route("/impianti/{pid:int}/stato.json", plant_state_json),
    Route("/impianti/{pid:int}/codice", plant_pair_code, methods=["POST"]),
    Route("/impianti/{pid:int}/revoca", plant_revoke, methods=["POST"]),
    Route("/impianti/{pid:int}/nome", plant_rename, methods=["POST"]),
    Route("/impianti/{pid:int}/amministratore", plant_admin, methods=["POST"]),
    Route("/impianti/{pid:int}/elimina", plant_delete, methods=["POST"]),
    Route("/clienti/{cid:int}/elimina", client_delete, methods=["POST"]),
    Route("/impianti/{pid:int}/comandi", plant_command, methods=["POST"]),
    Route("/comandi/{cmd:int}.json", command_detail),
    Route("/ticket", tickets_list),
    Route("/ticket/{tid:int}", ticket_detail),
    Route("/ticket/{tid:int}/rispondi", ticket_reply, methods=["POST"]),
    Route("/utenti", users_page, methods=["GET", "POST"]),
    Route("/utenti/{uid:int}/attiva", user_toggle, methods=["POST"]),
    Route("/utenti/{uid:int}/recupero", user_reset_link, methods=["POST"]),
    Route("/account", account_page, methods=["GET", "POST"]),
    Route("/aziende", companies_page, methods=["GET", "POST"]),
    Route("/aziende/{co:int}/limiti", company_limits, methods=["POST"]),
    Route("/aziende/{co:int}/sospendi", company_suspend, methods=["POST"]),
    Route("/aziende/{co:int}/elimina", company_delete, methods=["POST"]),
    Route("/aziende/{co:int}/recupero", company_reset_link, methods=["POST"]),
    Route("/impostazioni", settings_page, methods=["GET", "POST"]),
    Route("/impostazioni/telegram", telegram_settings, methods=["POST"]),
    Route("/impostazioni/aggiornamenti", update_check, methods=["POST"]),
    Route("/impostazioni/email", email_settings, methods=["POST"]),
    Route("/impianti/{pid:int}/ha-url", plant_ha_url, methods=["POST"]),
]

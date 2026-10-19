"""Dashboard web dell'installatore."""
import json
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from . import db
from .agent_api import is_online, ring
from .security import ROLE_LABELS, ROLES, can, hash_password, new_pair_code, verify_password

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

ALLOWED_COMMANDS = {
    "run_diagnostics": "Esegui diagnostica",
    "ping": "Ping",
}
TICKET_STATUS = {"aperto": "Aperto", "in_lavorazione": "In lavorazione", "chiuso": "Chiuso"}
COMMAND_STATUS = {"in_attesa": "In attesa", "inviato": "Inviato", "completato": "Completato", "errore": "Errore"}


# ---------- utilità ----------

def fmt_ts(value):
    ts = db.parse_ts(value) if isinstance(value, str) else value
    if not ts:
        return "—"
    return ts.astimezone().strftime("%d/%m/%Y %H:%M")


def ago(value):
    ts = db.parse_ts(value) if isinstance(value, str) else value
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


templates.env.filters["ts"] = fmt_ts
templates.env.filters["ago"] = ago
templates.env.globals.update(TICKET_STATUS=TICKET_STATUS, COMMAND_STATUS=COMMAND_STATUS,
                             ROLE_LABELS=ROLE_LABELS, ALLOWED_COMMANDS=ALLOWED_COMMANDS, can=can,
                             is_online=is_online)


def current_user(request: Request):
    uid = request.session.get("uid")
    return db.one("SELECT * FROM users WHERE id = ? AND active = 1", uid) if uid else None


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
    ctx.update(
        request=request,
        user=current_user(request),
        csrf=csrf_token(request),
        company=db.get_setting("company_name", "Assistenza"),
        brand_color=db.get_setting("brand_color", "#0f766e"),
        public_url=public_url(request),
        flash=request.session.pop("flash", None),
    )
    return templates.TemplateResponse(request, name, ctx)


def redirect(url, flash=None, request=None):
    if flash and request is not None:
        request.session["flash"] = flash
    return RedirectResponse(url, status_code=303)


async def read_form(request: Request):
    form = await request.form()
    if not secrets.compare_digest(form.get("csrf", ""), request.session.get("csrf", "")):
        return None
    return form


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
    form = await read_form(request)
    if form is None:
        return redirect("/setup")
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
    db.set_setting("company_name", company)
    db.set_setting("brand_color", form.get("brand_color") or "#0f766e")
    uid = db.run("INSERT INTO users(username, full_name, password_hash, role, created_at) VALUES(?, ?, ?, 'admin', ?)",
                 username, form.get("full_name", "").strip(), hash_password(password), db.now())
    request.session["uid"] = uid
    return redirect("/", "Configurazione completata. Benvenuto!", request)


_failed_logins: dict[str, list[float]] = defaultdict(list)


async def login(request: Request):
    if not db.one("SELECT id FROM users LIMIT 1"):
        return redirect("/setup")
    next_url = request.query_params.get("next", "/")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    if request.method == "GET":
        return render(request, "login.html", error=None, next=next_url)
    ip = request.client.host if request.client else "?"
    recent = [t for t in _failed_logins[ip] if time.time() - t < 900]
    _failed_logins[ip] = recent
    if len(recent) >= 5:
        return render(request, "login.html", error="Troppi tentativi falliti. Riprova tra 15 minuti.", next=next_url)
    form = await read_form(request)
    if form is None:
        return redirect("/login")
    user = db.one("SELECT * FROM users WHERE username = ? AND active = 1", form.get("username", "").strip().lower())
    if not user or not verify_password(form.get("password", ""), user["password_hash"]):
        _failed_logins[ip].append(time.time())
        return render(request, "login.html", error="Nome utente o password errati.", next=next_url)
    _failed_logins.pop(ip, None)
    request.session.clear()
    request.session["uid"] = user["id"]
    return redirect(next_url)


async def logout(request: Request):
    request.session.clear()
    return redirect("/login")


# ---------- panoramica ----------

def plant_status(p):
    if p["revoked"]:
        return "revocato"
    if not p["paired_at"]:
        return "da_abbinare"
    if not is_online(p):
        return "offline"
    diag = db.loads(p["last_diag"], {}) or {}
    return diag.get("status", "ok")


@guard()
async def dashboard(request: Request):
    plants = db.all_("SELECT p.*, c.name AS client_name FROM plants p JOIN clients c ON c.id = p.client_id "
                     "ORDER BY c.name, p.name")
    for p in plants:
        p["status"] = plant_status(p)
        p["diag"] = db.loads(p["last_diag"], {}) or {}
    order = {"offline": 0, "critico": 1, "attenzione": 2, "da_abbinare": 3, "ok": 4, "revocato": 5}
    plants.sort(key=lambda p: order.get(p["status"], 9))
    counts = defaultdict(int)
    for p in plants:
        counts[p["status"]] += 1
    open_tickets = db.all_("SELECT t.*, p.name AS plant_name, c.name AS client_name FROM tickets t "
                           "JOIN plants p ON p.id = t.plant_id JOIN clients c ON c.id = p.client_id "
                           "WHERE t.status != 'chiuso' ORDER BY t.updated_at DESC LIMIT 10")
    return render(request, "dashboard.html", plants=plants, counts=counts, open_tickets=open_tickets)


# ---------- clienti ----------

@guard()
async def clients_list(request: Request):
    q = request.query_params.get("q", "").strip()
    sql = ("SELECT c.*, COUNT(p.id) AS n_plants FROM clients c LEFT JOIN plants p ON p.client_id = c.id "
           "{where} GROUP BY c.id ORDER BY c.name")
    if q:
        rows = db.all_(sql.format(where="WHERE c.name LIKE ? OR c.email LIKE ? OR c.address LIKE ?"),
                       f"%{q}%", f"%{q}%", f"%{q}%")
    else:
        rows = db.all_(sql.format(where=""))
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
    cid = db.run("INSERT INTO clients(name, email, phone, address, notes, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                 *[data[f] for f in CLIENT_FIELDS], db.now())
    return redirect(f"/clienti/{cid}", "Cliente creato.", request)


@guard()
async def client_detail(request: Request):
    client = db.one("SELECT * FROM clients WHERE id = ?", request.path_params["cid"])
    if not client:
        return render(request, "error.html", message="Cliente non trovato.")
    plants = db.all_("SELECT * FROM plants WHERE client_id = ? ORDER BY name", client["id"])
    for p in plants:
        p["status"] = plant_status(p)
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


# ---------- impianti ----------

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
    pid = db.run("INSERT INTO plants(client_id, name, created_at) VALUES(?, ?, ?)", cid, name, db.now())
    _issue_pair_code(pid)
    return redirect(f"/impianti/{pid}", "Impianto creato. Inserisci il codice di abbinamento nell'add-on.", request)


def _load_plant(pid):
    p = db.one("SELECT p.*, c.name AS client_name FROM plants p JOIN clients c ON c.id = p.client_id "
               "WHERE p.id = ?", pid)
    if p:
        p["status"] = plant_status(p)
        p["online"] = is_online(p)
        p["diag"] = db.loads(p["last_diag"], {}) or {}
    return p


@guard()
async def plant_detail(request: Request):
    plant = _load_plant(request.path_params["pid"])
    if not plant:
        return render(request, "error.html", message="Impianto non trovato.")
    commands = db.all_("SELECT c.*, u.username FROM commands c LEFT JOIN users u ON u.id = c.created_by "
                       "WHERE plant_id = ? ORDER BY c.id DESC LIMIT 15", plant["id"])
    tickets = db.all_("SELECT * FROM tickets WHERE plant_id = ? ORDER BY updated_at DESC LIMIT 20", plant["id"])
    return render(request, "plant_detail.html", plant=plant, commands=commands, tickets=tickets)


@guard()
async def plant_state_json(request: Request):
    """Usato dalla pagina impianto per aggiornarsi da sola senza ricaricare."""
    plant = _load_plant(request.path_params["pid"])
    if not plant:
        return JSONResponse({"error": "not found"}, status_code=404)
    cmds = db.all_("SELECT id, status FROM commands WHERE plant_id = ? ORDER BY id DESC LIMIT 15", plant["id"])
    return JSONResponse({"online": plant["online"], "status": plant["status"], "last_seen": ago(plant["last_seen"]),
                         "last_diag_at": plant["last_diag_at"], "commands": cmds})


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


@guard("commands")
async def plant_command(request: Request):
    pid = request.path_params["pid"]
    plant = _load_plant(pid)
    name = request.state.form.get("name", "")
    if not plant or name not in ALLOWED_COMMANDS:
        return render(request, "error.html", message="Comando non valido.")
    if name != "ping" and not plant["consent"]:
        return redirect(f"/impianti/{pid}", "Il cliente ha sospeso l'accesso remoto: comando non inviato.", request)
    db.run("INSERT INTO commands(plant_id, name, args, created_by, created_at) VALUES(?, ?, '{}', ?, ?)",
           plant["id"], name, request.state.user["id"], db.now())
    ring(plant["id"])
    return redirect(f"/impianti/{pid}", f"Comando «{ALLOWED_COMMANDS[name]}» inviato.", request)


@guard()
async def command_detail(request: Request):
    cmd = db.one("SELECT * FROM commands WHERE id = ?", request.path_params["cid"])
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
                   f"LEFT JOIN users u ON u.id = t.assigned_to WHERE {where} ORDER BY t.updated_at DESC")
    return render(request, "tickets.html", tickets=rows, stato=status)


@guard()
async def ticket_detail(request: Request):
    t = db.one("SELECT t.*, p.name AS plant_name, p.id AS plant_id, c.name AS client_name, c.id AS client_id, "
               "c.phone AS client_phone, c.email AS client_email FROM tickets t JOIN plants p ON p.id = t.plant_id "
               "JOIN clients c ON c.id = p.client_id WHERE t.id = ?", request.path_params["tid"])
    if not t:
        return render(request, "error.html", message="Ticket non trovato.")
    t["diag"] = db.loads(t["diagnostics"])
    messages = db.all_("SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY id", t["id"])
    users = db.all_("SELECT id, username, full_name FROM users WHERE active = 1 AND role != 'lettura' ORDER BY username")
    return render(request, "ticket_detail.html", t=t, messages=messages, users=users)


@guard("edit")
async def ticket_reply(request: Request):
    tid = request.path_params["tid"]
    form = request.state.form
    text = form.get("text", "").strip()
    ts = db.now()
    if text:
        u = request.state.user
        db.run("INSERT INTO ticket_messages(ticket_id, author_type, author_name, text, created_at) "
               "VALUES(?, 'installatore', ?, ?, ?)", tid, u["full_name"] or u["username"], text[:5000], ts)
    status = form.get("status")
    if status in TICKET_STATUS:
        db.run("UPDATE tickets SET status = ? WHERE id = ?", status, tid)
    assigned = form.get("assigned_to")
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
        if db.one("SELECT id FROM users WHERE username = ?", username):
            errors.append("Nome utente già in uso.")
        if not errors:
            db.run("INSERT INTO users(username, full_name, password_hash, role, created_at) VALUES(?, ?, ?, ?, ?)",
                   username, form.get("full_name", "").strip(), hash_password(password), role, db.now())
            return redirect("/utenti", "Utente creato.", request)
    users = db.all_("SELECT * FROM users ORDER BY active DESC, username")
    return render(request, "users.html", users=users, errors=errors)


@guard("users")
async def user_toggle(request: Request):
    uid = request.path_params["uid"]
    if uid == request.state.user["id"]:
        return redirect("/utenti", "Non puoi disattivare te stesso.", request)
    db.run("UPDATE users SET active = 1 - active WHERE id = ?", uid)
    return redirect("/utenti", "Utente aggiornato.", request)


@guard("settings")
async def settings_page(request: Request):
    if request.method == "POST":
        form = request.state.form
        name = form.get("company_name", "").strip()
        if name:
            db.set_setting("company_name", name)
        color = form.get("brand_color", "")
        if color.startswith("#") and len(color) == 7:
            db.set_setting("brand_color", color)
        url = form.get("public_url", "").strip().rstrip("/")
        if url and not (url.startswith("https://") or url.startswith("http://")):
            return redirect("/impostazioni", "L'indirizzo pubblico deve iniziare con https://", request)
        db.set_setting("public_url", url)
        return redirect("/impostazioni", "Impostazioni salvate.", request)
    return render(request, "settings.html", guessed_url=guess_public_url(request),
                  saved_url=db.get_setting("public_url", ""))


async def health(request: Request):
    return Response("ok")


routes = [
    Route("/health", health),
    Route("/setup", setup, methods=["GET", "POST"]),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
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
    Route("/impianti/{pid:int}/comandi", plant_command, methods=["POST"]),
    Route("/comandi/{cid:int}.json", command_detail),
    Route("/ticket", tickets_list),
    Route("/ticket/{tid:int}", ticket_detail),
    Route("/ticket/{tid:int}/rispondi", ticket_reply, methods=["POST"]),
    Route("/utenti", users_page, methods=["GET", "POST"]),
    Route("/utenti/{uid:int}/attiva", user_toggle, methods=["POST"]),
    Route("/impostazioni", settings_page, methods=["GET", "POST"]),
]

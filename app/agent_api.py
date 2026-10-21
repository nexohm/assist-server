"""API usata dagli add-on agente installati presso i clienti (vedi protocol/PROTOCOL.md)."""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import db, notify, sanitize, secrets_store
from .security import hash_token, new_token, normalize_pair_code

PROTOCOL_VERSION = 1
MAX_WAIT = 25          # secondi massimi di long-polling
MAX_BODY = 512 * 1024  # una diagnostica sta comodamente sotto i 512 KB

# Un "campanello" per impianto: quando la dashboard crea un comando, sveglia il long-polling.
# Funziona con un solo processo server (va benissimo per un installatore).
_bells: dict[int, asyncio.Event] = {}


def ring(plant_id: int):
    bell = _bells.get(plant_id)
    if bell:
        bell.set()


def error(status, message):
    return JSONResponse({"error": message}, status_code=status)


MAX_TICKETS_PER_DAY = 20  # protezione da un agente che apre ticket a raffica


class BadRequest(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


async def read_json(request: Request) -> dict:
    """Corpo JSON della richiesta: deve essere un oggetto e stare sotto MAX_BODY.
    Il corpo si legge a pezzi e ci si ferma appena supera il limite: una richiesta enorme
    non arriva mai per intero in memoria."""
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise BadRequest("Content-Length non valido") from None
    if declared > MAX_BODY:
        raise BadRequest("richiesta troppo grande", 413)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise BadRequest("richiesta troppo grande", 413)
        chunks.append(chunk)
    try:
        data = json.loads(b"".join(chunks) or b"{}")
    except ValueError:
        raise BadRequest("JSON non valido") from None
    if not isinstance(data, dict):
        raise BadRequest("il corpo deve essere un oggetto JSON")
    return data


def authenticate(request: Request):
    """Restituisce (impianto, None) oppure (None, risposta di errore)."""
    try:
        proto = int(request.headers.get("x-agent-protocol", "0"))
    except ValueError:
        proto = 0
    if proto != PROTOCOL_VERSION:
        return None, error(426, f"protocollo {PROTOCOL_VERSION} richiesto: aggiornare l'add-on")
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None, error(401, "token mancante")
    plant = db.one("SELECT * FROM plants WHERE token_hash = ? AND revoked = 0", hash_token(auth[7:].strip()))
    if not plant:
        return None, error(401, "token non valido o impianto revocato")
    db.run("UPDATE plants SET last_seen = ?, agent_version = COALESCE(?, agent_version) WHERE id = ?",
           db.now(), sanitize.text(request.headers.get("x-agent-version"), 40), plant["id"])
    return plant, None


async def pair(request: Request):
    try:
        data = await read_json(request)
    except BadRequest as e:
        return error(e.status, str(e))
    code = normalize_pair_code(sanitize.text(data.get("code"), 20) or "")
    plant = db.one("SELECT * FROM plants WHERE pair_code = ? AND revoked = 0", code) if code else None
    if not plant:
        return error(404, "codice di abbinamento inesistente o già usato")
    expires = db.parse_ts(plant["pair_expires"])
    if not expires or expires < datetime.now(timezone.utc):
        return error(410, "codice di abbinamento scaduto: generarne uno nuovo dalla dashboard")
    token = new_token()
    # Aggiornamento condizionato al codice: se due agenti usano lo stesso codice nello stesso
    # istante, solo il primo lo "consuma" e riceve un token valido.
    taken = db.changed(
        "UPDATE plants SET token_hash = ?, pair_code = NULL, pair_expires = NULL, paired_at = ?, "
        "last_seen = ?, ha_version = ?, agent_version = ? WHERE id = ? AND pair_code = ? AND revoked = 0",
        hash_token(token), db.now(), db.now(), sanitize.text(data.get("ha_version"), 40),
        sanitize.text(data.get("agent_version"), 40), plant["id"], code)
    if not taken:
        return error(404, "codice di abbinamento inesistente o già usato")
    return JSONResponse({
        "plant_id": plant["id"],
        "token": token,
        "server_name": db.get_setting("company_name", "Assistenza"),
        "brand_color": db.get_setting("brand_color", "#0f766e"),
        "protocol": PROTOCOL_VERSION,
    })


async def report(request: Request):
    plant, err = authenticate(request)
    if err:
        return err
    try:
        data = await read_json(request)
    except BadRequest as e:
        return error(e.status, str(e))
    consent = 0 if data.get("consent", True) is False else 1
    until = sanitize.timestamp(data.get("consent_until")) if consent else None
    db.run("UPDATE plants SET consent = ?, consent_until = ?, ha_version = COALESCE(?, ha_version) WHERE id = ?",
           consent, until, sanitize.text(data.get("ha_version"), 40), plant["id"])
    urls = data.get("ha_urls")
    if isinstance(urls, dict):
        def clean_url(v):
            v = sanitize.text(v, 300) if isinstance(v, str) else None
            return v if v and re.fullmatch(r"https?://[^\s\"'<>]+", v) else None
        db.run("UPDATE plants SET ha_internal_url = ?, ha_external_url = ? WHERE id = ?",
               clean_url(urls.get("internal")), clean_url(urls.get("external")), plant["id"])
    admin = sanitize.admin_state(data.get("admin"))
    if admin is not None:
        db.run("UPDATE plants SET admin_state = ? WHERE id = ?", json.dumps(admin), plant["id"])
        if not admin["active"] or not admin["username"]:
            secrets_store.drop(plant["id"])  # accesso chiuso o utente cancellato: la password non vale più
        # richiesta soddisfatta (accesso concesso) o rifiutata dal cliente: non va più riproposta
        req = plant.get("admin_requested_at")
        if req and (admin["active"] or admin["declined"] == req):
            db.run("UPDATE plants SET admin_requested_at = NULL, admin_requested_by = NULL "
                   "WHERE id = ? AND admin_requested_at = ?", plant["id"], req)
    diag = sanitize.diagnostics(data.get("diagnostics"))
    if consent and diag:
        db.run("UPDATE plants SET last_diag = ?, last_diag_at = ? WHERE id = ?",
               json.dumps(diag), db.now(), plant["id"])
    pending = db.one("SELECT COUNT(*) AS n FROM commands WHERE plant_id = ? AND status = 'in_attesa'", plant["id"])
    # Nome e colore dell'installatore viaggiano a ogni rapporto: se cambiano, il pannello del cliente si aggiorna.
    # Ultimo aggiornamento dei ticket dell'impianto: se cambia, l'agente li rilegge e avvisa il cliente.
    last_ticket = db.one("SELECT MAX(updated_at) AS ts FROM tickets WHERE plant_id = ?", plant["id"])
    requested = db.one("SELECT admin_requested_at, admin_requested_by FROM plants WHERE id = ?", plant["id"])
    return JSONResponse({"ok": True, "pending_commands": pending["n"],
                         "admin_requested": requested["admin_requested_at"],
                         "admin_requested_by": requested["admin_requested_by"] if requested["admin_requested_at"] else None,
                         "tickets_updated": last_ticket["ts"],
                         "server_name": db.get_setting("company_name", "Assistenza"),
                         "brand_color": db.get_setting("brand_color", "#0f766e")})


RESEND_AFTER = 60   # secondi senza risultato prima di rispedire un comando
MAX_ATTEMPTS = 3


def _take_pending(plant_id):
    """Comandi da consegnare: quelli nuovi, più quelli inviati ma rimasti senza risposta
    (la risposta del long-polling può perdersi se la rete cade nel momento sbagliato).
    I comandi della lista sono tutti ripetibili senza effetti collaterali."""
    stale = (datetime.now(timezone.utc) - timedelta(seconds=RESEND_AFTER)).isoformat(timespec="seconds")
    db.run("UPDATE commands SET status = 'errore', done_at = ?, result = ? WHERE plant_id = ? AND status = 'inviato' "
           "AND sent_at < ? AND attempts >= ?", db.now(), json.dumps({"error": "l'agente non ha risposto"}),
           plant_id, stale, MAX_ATTEMPTS)
    rows = db.all_("SELECT id, name, args FROM commands WHERE plant_id = ? AND (status = 'in_attesa' "
                   "OR (status = 'inviato' AND sent_at < ?)) ORDER BY id", plant_id, stale)
    for r in rows:
        db.run("UPDATE commands SET status = 'inviato', sent_at = ?, attempts = attempts + 1 WHERE id = ?",
               db.now(), r["id"])
    return [{"id": r["id"], "name": r["name"], "args": db.loads(r["args"], {})} for r in rows]


async def commands(request: Request):
    plant, err = authenticate(request)
    if err:
        return err
    try:
        wait = max(0, min(MAX_WAIT, int(request.query_params.get("wait", MAX_WAIT))))
    except ValueError:
        wait = MAX_WAIT
    pending = _take_pending(plant["id"])
    if pending or wait == 0:
        return JSONResponse({"commands": pending})
    bell = _bells.setdefault(plant["id"], asyncio.Event())
    bell.clear()
    try:
        await asyncio.wait_for(bell.wait(), timeout=wait)
    except asyncio.TimeoutError:
        pass
    db.run("UPDATE plants SET last_seen = ? WHERE id = ?", db.now(), plant["id"])
    return JSONResponse({"commands": _take_pending(plant["id"])})


async def command_result(request: Request):
    plant, err = authenticate(request)
    if err:
        return err
    cmd = db.one("SELECT * FROM commands WHERE id = ? AND plant_id = ?", int(request.path_params["cid"]), plant["id"])
    if not cmd:
        return error(404, "comando inesistente")
    try:
        data = await read_json(request)
    except BadRequest as e:
        return error(e.status, str(e))
    ok = data.get("ok") is True
    if ok and cmd["name"] == "admin_password":
        # La password resta solo in memoria, per pochi minuti: nel database va un segnaposto.
        res = data.get("result") if isinstance(data.get("result"), dict) else {}
        username, password = sanitize.text(res.get("username"), 60), res.get("password")
        if (username and re.fullmatch(r"[a-z0-9_]{3,60}", username) and isinstance(password, str)
                and re.fullmatch(r"[A-Za-z0-9]{16,64}", password)):
            secrets_store.put(cmd["id"], plant["id"], username, password)
            payload = {"username": username, "password": "(mostrata una volta, non salvata)"}
        else:
            ok, payload = False, {"error": "credenziali non valide"}
    elif ok and cmd["name"] == "run_diagnostics":
        payload = sanitize.diagnostics(data.get("result"))
        ok = payload is not None
        if not ok:
            payload = {"error": "diagnostica non valida"}
    elif ok:
        payload = data.get("result") if isinstance(data.get("result"), dict) else {}
    else:
        payload = {"error": sanitize.text(data.get("error"), 500) or "errore sconosciuto"}
    # Solo un comando ancora in corso accetta un risultato: uno già concluso non si riscrive.
    if not db.changed("UPDATE commands SET status = ?, result = ?, done_at = ? WHERE id = ? "
                      "AND status IN ('in_attesa', 'inviato')",
                      "completato" if ok else "errore", json.dumps(payload), db.now(), cmd["id"]):
        return error(409, "comando già concluso")
    if ok and cmd["name"] == "run_diagnostics":
        db.run("UPDATE plants SET last_diag = ?, last_diag_at = ? WHERE id = ?",
               json.dumps(payload), db.now(), plant["id"])
    return JSONResponse({"ok": True})


async def tickets(request: Request):
    plant, err = authenticate(request)
    if err:
        return err
    if request.method == "POST":
        try:
            data = await read_json(request)
        except BadRequest as e:
            return error(e.status, str(e))
        title = sanitize.text(data.get("title"), 200)
        description = sanitize.text(data.get("description"), 5000)
        if not title:
            return error(400, "il titolo è obbligatorio")
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        recent = db.one("SELECT COUNT(*) AS n FROM tickets WHERE plant_id = ? AND created_at > ?", plant["id"], since)
        if recent["n"] >= MAX_TICKETS_PER_DAY:
            return error(429, "troppe richieste aperte nelle ultime 24 ore: riprova più tardi")
        diagnostics = sanitize.diagnostics(data.get("diagnostics")) if plant["consent"] else None
        ts = db.now()
        tid = db.run("INSERT INTO tickets(plant_id, title, contact, diagnostics, created_at, updated_at) "
                     "VALUES(?, ?, ?, ?, ?, ?)", plant["id"], title, sanitize.text(data.get("contact"), 200),
                     json.dumps(diagnostics) if diagnostics else None, ts, ts)
        if description:
            db.run("INSERT INTO ticket_messages(ticket_id, author_type, author_name, text, created_at) "
                   "VALUES(?, 'cliente', 'Cliente', ?, ?)", tid, description, ts)
        notify.new_ticket(tid, plant["id"], title, description)
        return JSONResponse({"ticket_id": tid})

    # chi segue la richiesta: il cliente vede nelle notifiche il nome del tecnico
    rows = db.all_("SELECT t.id, t.title, t.status, t.created_at, t.updated_at, "
                   "COALESCE(NULLIF(TRIM(u.full_name), ''), u.username) AS assigned_name "
                   "FROM tickets t LEFT JOIN users u ON u.id = t.assigned_to WHERE t.plant_id = ? "
                   "ORDER BY t.updated_at DESC LIMIT 50", plant["id"])
    for t in rows:
        t["messages"] = db.all_("SELECT author_type, author_name, text, created_at FROM ticket_messages "
                                "WHERE ticket_id = ? ORDER BY id", t["id"])
    return JSONResponse({"tickets": rows})


async def ticket_message(request: Request):
    plant, err = authenticate(request)
    if err:
        return err
    ticket = db.one("SELECT * FROM tickets WHERE id = ? AND plant_id = ?", int(request.path_params["tid"]), plant["id"])
    if not ticket:
        return error(404, "ticket inesistente")
    try:
        data = await read_json(request)
    except BadRequest as e:
        return error(e.status, str(e))
    text = sanitize.text(data.get("text"), 5000)
    if not text:
        return error(400, "messaggio vuoto")
    ts = db.now()
    db.run("INSERT INTO ticket_messages(ticket_id, author_type, author_name, text, created_at) "
           "VALUES(?, 'cliente', 'Cliente', ?, ?)", ticket["id"], text, ts)
    # Se il cliente riscrive su un ticket chiuso, lo riapriamo.
    status = "aperto" if ticket["status"] == "chiuso" else ticket["status"]
    db.run("UPDATE tickets SET updated_at = ?, status = ? WHERE id = ?", ts, status, ticket["id"])
    notify.client_reply(ticket["id"], plant["id"], ticket["title"], text)
    return JSONResponse({"ok": True})


def is_online(plant) -> bool:
    seen = db.parse_ts(plant.get("last_seen"))
    return bool(seen and plant.get("paired_at") and not plant.get("revoked")
                and datetime.now(timezone.utc) - seen < timedelta(minutes=3))


routes = [
    Route("/api/agent/pair", pair, methods=["POST"]),
    Route("/api/agent/report", report, methods=["POST"]),
    Route("/api/agent/commands", commands, methods=["GET"]),
    Route("/api/agent/commands/{cid:int}/result", command_result, methods=["POST"]),
    Route("/api/agent/tickets", tickets, methods=["GET", "POST"]),
    Route("/api/agent/tickets/{tid:int}/messages", ticket_message, methods=["POST"]),
]

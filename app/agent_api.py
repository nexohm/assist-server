"""API usata dagli add-on agente installati presso i clienti (vedi protocol/PROTOCOL.md)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import db
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


async def read_json(request: Request):
    body = await request.body()
    if len(body) > MAX_BODY:
        raise ValueError("richiesta troppo grande")
    return json.loads(body or b"{}")


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
           db.now(), request.headers.get("x-agent-version"), plant["id"])
    return plant, None


async def pair(request: Request):
    try:
        data = await read_json(request)
    except ValueError:
        return error(400, "JSON non valido")
    code = normalize_pair_code(data.get("code", ""))
    plant = db.one("SELECT * FROM plants WHERE pair_code = ? AND revoked = 0", code) if code else None
    if not plant:
        return error(404, "codice di abbinamento inesistente o già usato")
    if db.parse_ts(plant["pair_expires"]) < datetime.now(timezone.utc):
        return error(410, "codice di abbinamento scaduto: generarne uno nuovo dalla dashboard")
    token = new_token()
    db.run("UPDATE plants SET token_hash = ?, pair_code = NULL, pair_expires = NULL, paired_at = ?, "
           "last_seen = ?, ha_version = ?, agent_version = ? WHERE id = ?",
           hash_token(token), db.now(), db.now(), data.get("ha_version"), data.get("agent_version"), plant["id"])
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
    except ValueError as e:
        return error(400, str(e))
    consent = 1 if data.get("consent", True) else 0
    db.run("UPDATE plants SET consent = ?, ha_version = COALESCE(?, ha_version) WHERE id = ?",
           consent, data.get("ha_version"), plant["id"])
    if consent and isinstance(data.get("diagnostics"), dict):
        db.run("UPDATE plants SET last_diag = ?, last_diag_at = ? WHERE id = ?",
               json.dumps(data["diagnostics"]), db.now(), plant["id"])
    pending = db.one("SELECT COUNT(*) AS n FROM commands WHERE plant_id = ? AND status = 'in_attesa'", plant["id"])
    # Nome e colore dell'installatore viaggiano a ogni rapporto: se cambiano, il pannello del cliente si aggiorna.
    return JSONResponse({"ok": True, "pending_commands": pending["n"],
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
    except ValueError as e:
        return error(400, str(e))
    ok = bool(data.get("ok"))
    payload = data.get("result") if ok else {"error": data.get("error", "errore sconosciuto")}
    db.run("UPDATE commands SET status = ?, result = ?, done_at = ? WHERE id = ?",
           "completato" if ok else "errore", json.dumps(payload), db.now(), cmd["id"])
    if ok and cmd["name"] == "run_diagnostics" and isinstance(payload, dict):
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
        except ValueError as e:
            return error(400, str(e))
        title = (data.get("title") or "").strip()[:200]
        description = (data.get("description") or "").strip()[:5000]
        if not title:
            return error(400, "il titolo è obbligatorio")
        diagnostics = data.get("diagnostics") if plant["consent"] else None
        ts = db.now()
        tid = db.run("INSERT INTO tickets(plant_id, title, contact, diagnostics, created_at, updated_at) "
                     "VALUES(?, ?, ?, ?, ?, ?)", plant["id"], title, (data.get("contact") or "")[:200],
                     json.dumps(diagnostics) if isinstance(diagnostics, dict) else None, ts, ts)
        if description:
            db.run("INSERT INTO ticket_messages(ticket_id, author_type, author_name, text, created_at) "
                   "VALUES(?, 'cliente', 'Cliente', ?, ?)", tid, description, ts)
        return JSONResponse({"ticket_id": tid})

    rows = db.all_("SELECT id, title, status, created_at, updated_at FROM tickets WHERE plant_id = ? "
                   "ORDER BY updated_at DESC LIMIT 50", plant["id"])
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
    except ValueError as e:
        return error(400, str(e))
    text = (data.get("text") or "").strip()[:5000]
    if not text:
        return error(400, "messaggio vuoto")
    ts = db.now()
    db.run("INSERT INTO ticket_messages(ticket_id, author_type, author_name, text, created_at) "
           "VALUES(?, 'cliente', 'Cliente', ?, ?)", ticket["id"], text, ts)
    # Se il cliente riscrive su un ticket chiuso, lo riapriamo.
    status = "aperto" if ticket["status"] == "chiuso" else ticket["status"]
    db.run("UPDATE tickets SET updated_at = ?, status = ? WHERE id = ?", ts, status, ticket["id"])
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

"""Pulizia dei dati che arrivano dagli agenti.

Il server non controlla cosa gira a casa del cliente: un add-on modificato, guasto o di una
versione futura può mandare qualsiasi JSON. Qui lo riportiamo alla forma che la dashboard si
aspetta, così un dato strano non può rompere le pagine né finire dove non deve.
"""
import math
import re

from . import db

STATUSES = {"ok", "attenzione", "critico"}
SEVERITIES = {"critico", "attenzione", "info"}
MAX_ISSUES = 100
MAX_ITEMS = 100


def text(value, limit=200):
    """Stringa ripulita e accorciata; None se il valore non è una stringa (o un numero)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = "".join(ch for ch in value if ch >= " " or ch in "\n\t").strip()
    return value[:limit] or None


def number(value):
    """Numero finito (int o float), altrimenti None. Niente NaN/Infinity: JSON di Python li accetta."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, int):
        return value if abs(value) < 10 ** 15 else None
    if isinstance(value, float) and math.isfinite(value) and abs(value) < 1e15:
        return round(value, 2)
    return None


def timestamp(value):
    """Data ISO normalizzata in UTC, oppure None."""
    ts = db.parse_ts(value)
    return ts.isoformat(timespec="seconds") if ts else None


def _dict(value):
    return value if isinstance(value, dict) else {}


ENTITY_ID = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")


def entity_names(value):
    """Nomi leggibili delle entità di un problema ({entity_id: nome}), mandati dagli agenti recenti."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for eid, name in list(value.items())[:MAX_ITEMS]:
        name = text(name, 120)
        if isinstance(eid, str) and ENTITY_ID.fullmatch(eid) and name:
            out[eid] = name
    return out


def diagnostics(value):
    """Diagnostica nella forma prevista da PROTOCOL.md; None se non è un oggetto."""
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    summary, system = _dict(value.get("summary")), _dict(value.get("system"))
    issues = []
    raw_issues = value.get("issues")
    for issue in (raw_issues if isinstance(raw_issues, list) else [])[:MAX_ISSUES]:
        if not isinstance(issue, dict):
            continue
        severity = issue.get("severity")
        items = issue.get("items")
        issues.append({
            "severity": severity if severity in SEVERITIES else "attenzione",
            "code": text(issue.get("code"), 40),
            "message": text(issue.get("message"), 500) or "—",
            "items": [t for t in (text(i, 200) for i in (items if isinstance(items, list) else [])[:MAX_ITEMS]) if t],
            "names": entity_names(issue.get("names")),
        })
    updates = value.get("updates_list")
    return {
        "status": status if status in STATUSES else "attenzione",
        "collected_at": timestamp(value.get("collected_at")),
        "summary": {k: number(summary.get(k)) for k in ("entities", "unavailable", "low_battery", "updates", "automations_off", "excluded")},
        "system": {
            "ha_version": text(system.get("ha_version"), 40),
            "os_version": text(system.get("os_version"), 40),
            "disk_used_gb": number(system.get("disk_used_gb")),
            "disk_total_gb": number(system.get("disk_total_gb")),
            "last_backup": timestamp(system.get("last_backup")),
            "last_backup_age_days": number(system.get("last_backup_age_days")),
        },
        "issues": issues,
        # dall'agente 0.1.9: aggiornamenti installabili e backup (None con agenti più vecchi)
        "updates_list": update_list(updates) if isinstance(updates, list) else None,
        "backups": backups(value.get("backups")) if isinstance(value.get("backups"), dict) else None,
    }


UPDATE_KINDS = {"core", "os", "supervisor", "addon", "altro"}
UPDATE_ENTITY = re.compile(r"update\.[a-z0-9_]{1,120}")


def update_list(value):
    out = []
    for u in value[:50]:
        if not isinstance(u, dict) or not isinstance(u.get("entity_id"), str) or not UPDATE_ENTITY.fullmatch(u["entity_id"]):
            continue
        out.append({"entity_id": u["entity_id"], "title": text(u.get("title"), 80) or u["entity_id"],
                    "installed": text(u.get("installed"), 40), "latest": text(u.get("latest"), 40),
                    "kind": u.get("kind") if u.get("kind") in UPDATE_KINDS else "altro",
                    "in_progress": u.get("in_progress") is True, "installable": u.get("installable") is True})
    return out


RECURRENCES = {"never", "daily", "custom_days"}


def backups(value):
    items = []
    raw = value.get("items")
    for b in (raw if isinstance(raw, list) else [])[:15]:
        if not isinstance(b, dict):
            continue
        locs = b.get("locations")
        items.append({"name": text(b.get("name"), 80), "date": timestamp(b.get("date")),
                      "size_mb": number(b.get("size_mb")),
                      "kind": b.get("kind") if b.get("kind") in ("completo", "parziale") else None,
                      "locations": [t for t in (text(l, 60) for l in (locs if isinstance(locs, list) else [])[:5]) if t],
                      "automatic": b.get("automatic") if isinstance(b.get("automatic"), bool) else None,
                      "protected": b.get("protected") is True})
    auto = value.get("automatic")
    automatic = None
    if isinstance(auto, dict):
        days = auto.get("days")
        locs = auto.get("locations")
        automatic = {
            "recurrence": auto.get("recurrence") if auto.get("recurrence") in RECURRENCES else None,
            "days": [d for d in (days if isinstance(days, list) else []) if d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")],
            "time": text(auto.get("time"), 8) if isinstance(auto.get("time"), str) and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", auto["time"]) else None,
            "copies": number(auto.get("copies")), "retention_days": number(auto.get("retention_days")),
            "locations": [t for t in (text(l, 60) for l in (locs if isinstance(locs, list) else [])[:5]) if t],
            # nomi dell'agente (come in Home Assistant) o già ripuliti (diagnostica salvata nel database)
            "last_attempted": timestamp(auto.get("last_attempted_automatic_backup") or auto.get("last_attempted")),
            "last_completed": timestamp(auto.get("last_completed_automatic_backup") or auto.get("last_completed")),
            "next": timestamp(auto.get("next_automatic_backup") or auto.get("next")),
        }
    return {"items": items, "automatic": automatic}


JOB_STATUS = {"in_corso", "completato", "errore"}


def jobs(value):
    """Backup e aggiornamenti avviati dall'installatore, come li racconta l'agente (ultimi 10)."""
    out = []
    for j in (value if isinstance(value, list) else [])[-10:]:
        if not isinstance(j, dict) or type(j.get("id")) is not int:
            continue
        eid = j.get("entity_id")
        out.append({"id": j["id"], "kind": "backup" if j.get("kind") == "backup" else "update",
                    "title": text(j.get("title"), 80) or "—",
                    "entity_id": eid if isinstance(eid, str) and UPDATE_ENTITY.fullmatch(eid) else None,
                    "from": text(j.get("from"), 40), "to": text(j.get("to"), 40),
                    "status": j.get("status") if j.get("status") in JOB_STATUS else "errore",
                    "message": text(j.get("message"), 200), "by": person_name(j.get("by")),
                    "started_at": timestamp(j.get("started_at")), "finished_at": timestamp(j.get("finished_at"))})
    return out


def person_name(value):
    """Nome di una persona mandato da un agente: testo semplice su una riga, al massimo 60 caratteri."""
    name = text(value, 60)
    if name:
        name = " ".join("".join(ch for ch in name if ch.isprintable()).split()) or None
    return name


def ha_user(value):
    """Utente di Home Assistant che scrive dal pannello dell'add-on: id (32 caratteri esadecimali,
    come quelli di Home Assistant) e nome visualizzato. (None, None) se manca o non è valido."""
    if not isinstance(value, dict):
        return None, None
    uid = value.get("id")
    uid = uid if isinstance(uid, str) and re.fullmatch(r"[0-9a-f]{32}", uid) else None
    return (uid, person_name(value.get("name"))) if uid else (None, None)


def admin_state(value):
    """Stato dell'accesso amministratore comunicato dall'agente (mai la password)."""
    if not isinstance(value, dict):
        return None
    username = text(value.get("username"), 60)
    if username and not re.fullmatch(r"[a-z0-9_]{3,60}", username):
        username = None
    active = value.get("active") is True
    return {"active": active, "permanent": active and value.get("permanent") is True,
            "until": timestamp(value.get("until")) if active else None,
            "username": username, "last_reveal": timestamp(value.get("last_reveal")),
            "declined": text(value.get("declined"), 40)}


def stored_diagnostics(raw):
    """Diagnostica salvata nel database (anche da versioni precedenti), già ripulita."""
    return diagnostics(db.loads(raw)) or {}

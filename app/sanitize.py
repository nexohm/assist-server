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
        })
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
    }


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

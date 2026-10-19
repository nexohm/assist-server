"""Database SQLite del server installatore.

Un file solo (/data/suite.db), nessun ORM: facile da leggere, da fare backup e da migrare.
Le migrazioni sono numerate: per cambiare lo schema si aggiunge una voce a MIGRATIONS.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "suite.db")

MIGRATIONS = [
    # 1 — schema iniziale
    """
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);

    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        full_name TEXT,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('admin','tecnico','lettura')),
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    CREATE TABLE clients (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT, phone TEXT, address TEXT, notes TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE plants (
        id INTEGER PRIMARY KEY,
        client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        token_hash TEXT UNIQUE,
        pair_code TEXT UNIQUE,
        pair_expires TEXT,
        paired_at TEXT,
        revoked INTEGER NOT NULL DEFAULT 0,
        last_seen TEXT,
        ha_version TEXT,
        agent_version TEXT,
        consent INTEGER NOT NULL DEFAULT 1,
        last_diag TEXT,
        last_diag_at TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE tickets (
        id INTEGER PRIMARY KEY,
        plant_id INTEGER NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'aperto' CHECK (status IN ('aperto','in_lavorazione','chiuso')),
        contact TEXT,
        diagnostics TEXT,
        assigned_to INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE ticket_messages (
        id INTEGER PRIMARY KEY,
        ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
        author_type TEXT NOT NULL CHECK (author_type IN ('cliente','installatore')),
        author_name TEXT,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE commands (
        id INTEGER PRIMARY KEY,
        plant_id INTEGER NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        args TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'in_attesa' CHECK (status IN ('in_attesa','inviato','completato','errore')),
        result TEXT,
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL,
        done_at TEXT
    );

    CREATE INDEX idx_plants_client ON plants(client_id);
    CREATE INDEX idx_tickets_plant ON tickets(plant_id);
    CREATE INDEX idx_commands_plant_status ON commands(plant_id, status);
    """,
    # 2 — riconsegna dei comandi se la risposta del long-polling va persa (rete instabile)
    """
    ALTER TABLE commands ADD COLUMN sent_at TEXT;
    ALTER TABLE commands ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
    """,
    # 3 — accesso remoto a tempo, scelto dal cliente nel pannello dell'add-on
    """
    ALTER TABLE plants ADD COLUMN consent_until TEXT;
    """,
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def connect():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


KEEP_BACKUPS = 10


def _backup_before_migration(version):
    """Copia di sicurezza del database prima di cambiarne lo schema (aggiornamento dell'immagine)."""
    backup_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = os.path.join(backup_dir, f"suite-schema{version}-{stamp}.db")
    src = _connect()
    try:
        dst = sqlite3.connect(target)
        src.backup(dst)
        dst.close()
    finally:
        src.close()
    old = sorted(f for f in os.listdir(backup_dir) if f.startswith("suite-") and f.endswith(".db"))
    for name in old[:-KEEP_BACKUPS]:
        os.remove(os.path.join(backup_dir, name))
    return target


def migrate():
    os.makedirs(DATA_DIR, exist_ok=True)
    existed = os.path.exists(DB_PATH)
    with connect() as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]
    if existed and version < len(MIGRATIONS):
        print(f"Aggiornamento database: schema {version} -> {len(MIGRATIONS)}, "
              f"copia di sicurezza in {_backup_before_migration(version)}", flush=True)
    with connect() as c:
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            c.executescript(sql)
            c.execute(f"PRAGMA user_version = {i}")


def one(sql, *args):
    with connect() as c:
        row = c.execute(sql, args).fetchone()
        return dict(row) if row else None


def all_(sql, *args):
    with connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def run(sql, *args) -> int:
    """Esegue una scrittura e restituisce l'id della riga inserita."""
    with connect() as c:
        return c.execute(sql, args).lastrowid


def get_setting(key, default=None):
    row = one("SELECT value FROM settings WHERE key = ?", key)
    return row["value"] if row else default


def set_setting(key, value):
    run("INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, value)


def loads(value, default=None):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default

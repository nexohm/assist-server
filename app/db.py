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
    # 4 — notifiche (stato già notificato) e indirizzi di Home Assistant del cliente
    """
    ALTER TABLE plants ADD COLUMN alert_online INTEGER;
    ALTER TABLE plants ADD COLUMN alert_status TEXT;
    ALTER TABLE plants ADD COLUMN ha_internal_url TEXT;
    ALTER TABLE plants ADD COLUMN ha_external_url TEXT;
    ALTER TABLE plants ADD COLUMN ha_url_manual TEXT;
    """,
    # 5 — accesso amministratore temporaneo: richiesta dell'installatore e stato comunicato dall'agente
    #     (la password non viene mai salvata nel database)
    """
    ALTER TABLE plants ADD COLUMN admin_requested_at TEXT;
    ALTER TABLE plants ADD COLUMN admin_state TEXT;
    """,
    # 6 — nome della persona che ha chiesto l'accesso amministratore (il cliente vede chi lo chiede)
    """
    ALTER TABLE plants ADD COLUMN admin_requested_by TEXT;
    """,
    # 7 — più aziende sullo stesso server. Ogni utente e ogni cliente appartiene a un'azienda;
    #     impianti, ticket e comandi la seguono attraverso il cliente. I dati già presenti
    #     diventano l'azienda 1 e il primo amministratore ne è il gestore.
    #     Le vecchie impostazioni restano nella tabella settings: tornando a una versione
    #     precedente il server continua a funzionare (con nome e Telegram com'erano prima).
    """
    CREATE TABLE companies (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        brand_color TEXT NOT NULL DEFAULT '#0f766e',
        created_at TEXT NOT NULL
    );
    CREATE TABLE company_settings (
        company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        value TEXT,
        PRIMARY KEY (company_id, key)
    );
    ALTER TABLE users ADD COLUMN company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE;
    ALTER TABLE users ADD COLUMN is_manager INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE clients ADD COLUMN company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE;

    INSERT INTO companies(id, name, brand_color, created_at)
        SELECT 1,
               COALESCE((SELECT NULLIF(TRIM(value), '') FROM settings WHERE key = 'company_name'), 'Assistenza'),
               COALESCE((SELECT value FROM settings WHERE key = 'brand_color'), '#0f766e'),
               strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
        WHERE EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM clients);
    UPDATE users SET company_id = 1 WHERE EXISTS (SELECT 1 FROM companies WHERE id = 1);
    UPDATE clients SET company_id = 1 WHERE EXISTS (SELECT 1 FROM companies WHERE id = 1);
    UPDATE users SET is_manager = 1
        WHERE id = (SELECT MIN(id) FROM users WHERE role = 'admin' AND active = 1);
    INSERT INTO company_settings(company_id, key, value)
        SELECT 1, key, value FROM settings
        WHERE key IN ('telegram_token', 'telegram_chat_id', 'notify_tickets', 'notify_offline', 'notify_critical')
          AND EXISTS (SELECT 1 FROM companies WHERE id = 1);

    CREATE INDEX idx_users_company ON users(company_id);
    CREATE INDEX idx_clients_company ON clients(company_id);
    """,
    # 8 — aziende sospese e limiti (decisi dal gestore); email degli utenti, cambio password
    #     obbligatorio e link di recupero monouso (nel database solo l'hash del link)
    """
    ALTER TABLE companies ADD COLUMN suspended_at TEXT;
    ALTER TABLE companies ADD COLUMN max_clients INTEGER;
    ALTER TABLE companies ADD COLUMN max_plants INTEGER;
    ALTER TABLE users ADD COLUMN email TEXT;
    ALTER TABLE users ADD COLUMN must_change INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE password_resets (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_by TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT
    );
    CREATE INDEX idx_password_resets_user ON password_resets(user_id);
    """,
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value):
    """Data ISO -> datetime con fuso orario (UTC se manca), None se non valida.
    Le date possono arrivare dagli agenti: una data senza fuso non deve rompere i confronti."""
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


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
    return backup(f"schema{version}")


def backup(label):
    """Copia di sicurezza del database in /data/backups (si tengono le ultime KEEP_BACKUPS)."""
    backup_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = os.path.join(backup_dir, f"suite-{label}-{stamp}.db")
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


def changed(sql, *args) -> int:
    """Esegue una scrittura e restituisce quante righe ha modificato."""
    with connect() as c:
        return c.execute(sql, args).rowcount


def get_setting(key, default=None):
    row = one("SELECT value FROM settings WHERE key = ?", key)
    return row["value"] if row else default


def set_setting(key, value):
    run("INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, value)


# ---- aziende ----

def company(company_id):
    return one("SELECT * FROM companies WHERE id = ?", company_id)


def company_of_plant(plant_id):
    return one("SELECT co.* FROM companies co JOIN clients c ON c.company_id = co.id "
               "JOIN plants p ON p.client_id = c.id WHERE p.id = ?", plant_id)


def get_csetting(company_id, key, default=None):
    """Impostazione di un'azienda (Telegram e scelte delle notifiche)."""
    row = one("SELECT value FROM company_settings WHERE company_id = ? AND key = ?", company_id, key)
    return row["value"] if row else default


def set_csetting(company_id, key, value):
    run("INSERT INTO company_settings(company_id, key, value) VALUES(?, ?, ?) "
        "ON CONFLICT(company_id, key) DO UPDATE SET value = excluded.value", company_id, key, value)


def loads(value, default=None):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default

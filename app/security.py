"""Password, token, codici di abbinamento e controllo dei ruoli."""
import hashlib
import hmac
import os
import secrets

PBKDF2_ITERATIONS = 240_000

# Cosa può fare ogni ruolo. Il controllo è centralizzato qui.
ROLES = {
    "admin":   {"view", "edit", "commands", "users", "settings"},
    "tecnico": {"view", "edit", "commands"},
    "lettura": {"view"},
}
ROLE_LABELS = {"admin": "Amministratore", "tecnico": "Tecnico", "lettura": "Sola lettura"}

# Niente caratteri ambigui (0/O, 1/I/L) nel codice di abbinamento: va dettato o digitato a mano.
PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


# Hash di una password a caso: si verifica contro questo quando l'utente non esiste,
# così il login impiega lo stesso tempo e non rivela quali nomi utente sono validi.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_token() -> str:
    return secrets.token_urlsafe(36)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_pair_code() -> str:
    raw = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def new_setup_code() -> str:
    """Codice di installazione del primo avvio: 12 caratteri, a gruppi di 4 (es. K7Q2-M9XA-3HPT)."""
    raw = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalize_pair_code(code: str) -> str:
    raw = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else ""


# Permessi del gestore del server (oltre a quelli del suo ruolo nella propria azienda):
# creare aziende, vederne i numeri, aggiornamenti e indirizzo pubblico del server.
MANAGER_PERMISSIONS = {"companies", "server"}


def can(user, permission: str) -> bool:
    if not user:
        return False
    if permission in MANAGER_PERMISSIONS:
        return bool(user.get("is_manager")) and user["role"] == "admin"
    return permission in ROLES.get(user["role"], set())

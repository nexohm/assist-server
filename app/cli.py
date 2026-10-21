"""Comandi da riga di comando, eseguiti dentro il container (vedi lo script ha-suite).

    python -m app.cli link <utente>     link monouso per scegliere una password nuova
"""
import sys

from . import accounts, db


def main(argv):
    if len(argv) != 2 or argv[0] != "link":
        print("uso: python -m app.cli link <utente>", file=sys.stderr)
        return 2
    user = db.one("SELECT * FROM users WHERE username = ?", argv[1].strip().lower())
    if not user:
        names = ", ".join(u["username"] for u in db.all_("SELECT username FROM users WHERE is_manager = 1 ORDER BY id"))
        print(f"Utente «{argv[1]}» inesistente." + (f" Gestore del server: {names}" if names else ""), file=sys.stderr)
        return 1
    if not user["active"]:
        db.run("UPDATE users SET active = 1 WHERE id = ?", user["id"])
        print(f"L'utente {user['username']} era disattivato: riattivato.")
    token = accounts.create_reset(user["id"], "server")
    base = accounts.base_url()
    url = accounts.reset_url(token, base) if base else f"/recupero/{token}  (davanti metti l'indirizzo del server)"
    print(f"Link per scegliere una password nuova per {user['username']} "
          f"(vale {accounts.RESET_MINUTES} minuti, una volta sola):\n\n  {url}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

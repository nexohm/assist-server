"""Accesso con il provider OpenID Connect dell'azienda (Authentik…). Avviare con tests/run_security.sh.

Server di prova su BASE con un finto provider (tests/fake_oidc.py) su OIDC: si prova il percorso
normale e gli attacchi (token falsi, scaduti, per un'altra app, codice riusato, email cambiata…).
"""
import os
import re
import sqlite3
import sys

import requests

BASE = os.environ.get("BASE", "http://127.0.0.1:8081")
OIDC = os.environ.get("OIDC", "http://127.0.0.1:8125")
ISSUER = OIDC + "/application/o/assist/"
DATA = sys.argv[1]
results = []


def check(ok, name, detail=""):
    results.append((ok, name, detail))
    print(("OK          " if ok else "VULNERABILE ") + name + (f"  [{detail}]" if detail and not ok else ""))


def csrf(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def form_post(sess, path, page, data, **kw):
    return sess.post(BASE + path, data={"csrf": csrf(sess.get(BASE + page).text), **data}, **kw)


def provider(**identity):
    requests.post(OIDC + "/control", json=identity).raise_for_status()


def sso(sess, key, **identity):
    """Accesso completo dal link dell'azienda: dashboard -> provider -> dashboard."""
    provider(**identity)
    return sess.get(BASE + f"/accesso/{key}")


def logged_as(sess):
    page = sess.get(BASE + "/account").text
    m = re.search(r'<label>Nome utente</label><input value="([^"]+)"', page)
    return m.group(1) if m else None


db = sqlite3.connect(os.path.join(DATA, "suite.db"))
setup_code = open(os.path.join(DATA, "setup-code.txt")).read().strip()

# ---------------------------------------------------------------- preparazione
admin = requests.Session()
admin.post(BASE + "/setup", data={"csrf": csrf(admin.get(BASE + "/setup").text), "setup_code": setup_code,
                                  "company_name": "Rossi Impianti", "full_name": "Mario Rossi", "username": "mario",
                                  "password": "passwordsicura1", "password2": "passwordsicura1"})
for username, email, role in (("luca", "luca@rossi.it", "tecnico"), ("anna", "anna@rossi.it", "admin"),
                              ("gemello1", "doppia@rossi.it", "tecnico"), ("gemello2", "doppia@rossi.it", "lettura")):
    form_post(admin, "/utenti", "/utenti", {"username": username, "email": email, "password": "passwordiniziale1",
                                            "role": role, "full_name": username.title()})
form_post(admin, "/aziende", "/aziende", {"name": "Bianchi Elettrica", "username": "bianchi", "email": "luca@rossi.it",
                                          "password": "passwordbianchi1"})
check(db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 6, "(preparazione) utenti e seconda azienda creati")

# ---------------------------------------------------------------- impostazioni
r = form_post(admin, "/impostazioni/accesso", "/impostazioni", {"oidc_enabled": "1", "oidc_issuer": "https://127.0.0.1:1/x/",
                                                                "oidc_client_id": "assist-client",
                                                                "oidc_client_secret": "segreto-del-client-123"})
check("Provider non collegato" in r.text and db.execute(
    "SELECT value FROM company_settings WHERE company_id = 1 AND key = 'oidc_enabled'").fetchone()[0] == "0",
    "impostazioni: provider irraggiungibile -> errore chiaro e accesso non attivato")
r = form_post(admin, "/impostazioni/accesso", "/impostazioni", {"oidc_enabled": "1", "oidc_issuer": OIDC + "/application/o/altro/",
                                                                "oidc_client_id": "assist-client"})
check("Provider non collegato" in r.text, "impostazioni: indirizzo sbagliato del provider rifiutato")
r = form_post(admin, "/impostazioni/accesso", "/impostazioni", {"oidc_enabled": "1", "oidc_issuer": "javascript:alert(1)",
                                                                "oidc_client_id": "x"})
check("non valido" in r.text, "impostazioni: solo indirizzi http(s) per il provider")
tec = requests.Session()
tec.post(BASE + "/login", data={"csrf": csrf(tec.get(BASE + "/login").text), "username": "luca", "password": "passwordiniziale1"})
form_post(tec, "/account", "/account", {"azione": "password", "current": "passwordiniziale1", "password": "passwordluca12",
                                        "password2": "passwordluca12"})
r = form_post(tec, "/impostazioni/accesso", "/", {"oidc_enabled": "1", "oidc_issuer": "https://evil.example/"})
check("Non hai i permessi" in r.text, "impostazioni: un tecnico non può cambiare il provider")
r = form_post(admin, "/impostazioni/accesso", "/impostazioni", {"azione": "prova", "oidc_issuer": ISSUER,
                                                                "oidc_client_id": "assist-client", "oidc_label": "Authentik Rossi"})
check("configurazione è corretta" in r.text, "impostazioni: «Prova collegamento» con il provider giusto")
r = form_post(admin, "/impostazioni/accesso", "/impostazioni", {"oidc_enabled": "1", "oidc_issuer": ISSUER,
                                                                "oidc_client_id": "assist-client", "oidc_label": "Authentik Rossi"})
key = re.search(r"/accesso/([A-Za-z0-9_-]{10,})<", r.text)
check("Accesso con il provider attivo" in r.text and key and "segreto-del-client-123" not in r.text
      and BASE + "/accesso/callback" in r.text,
      "impostazioni: attivato, con link dell'azienda e indirizzo di ritorno; il client secret non compare")
key = key.group(1) if key else "x"

# ---------------------------------------------------------------- pagina di accesso e link
anon = requests.Session()
check("Accedi con" not in anon.get(BASE + "/login").text, "pagina di accesso: nessun provider mostrato a chi non ha il link")
check(anon.get(BASE + "/accesso/chiavesbagliata123").status_code == 404, "link con una chiave sbagliata -> non valido")

# ---------------------------------------------------------------- utente non ancora collegato
r = sso(anon, key, sub="authentik-luca", email="luca@rossi.it")
check("non è ancora collegato" in r.text and not logged_as(anon),
      "senza collegamento non si entra, nemmeno con un'email che esiste nella dashboard")
check("Accedi con Authentik Rossi" in anon.get(BASE + "/login").text,
      "dopo aver usato il link il browser mostra il pulsante del provider nella pagina di accesso")

# ---------------------------------------------------------------- collegamento da «Il mio account»
provider(sub="authentik-luca", email="luca@rossi.it")
r = form_post(tec, "/account/accesso", "/account", {"azione": "collega"})
check("Collegato a Authentik Rossi come authentik-luca" in r.text and db.execute(
    "SELECT oidc_sub FROM users WHERE username = 'luca'").fetchone()[0] == "authentik-luca",
      "collegamento: il tecnico, entrato con la password, collega il suo account del provider")
log = requests.get(OIDC + "/log").json()
auth = [e for e in log if "authorize" in e][-1]["authorize"]
tok = [e for e in log if "token" in e][-1]
check(auth["code_challenge_method"] == "S256" and len(auth["state"]) > 20 and len(auth["nonce"]) > 20 and tok["auth_ok"],
      "richieste al provider: PKCE S256, state e nonce casuali, client secret inviato solo al token endpoint")
check("segreto-del-client-123" not in str(auth), "il client secret non passa mai dal browser")

browser = requests.Session()
r = sso(browser, key, sub="authentik-luca", email="un-altra-email@rossi.it")
check(logged_as(browser) == "luca" and "Panoramica" in r.text,
      "accesso con il provider: entra l'utente collegato, anche se nel provider l'email è cambiata")
check(browser.get(BASE + "/utenti").status_code == 200 and "Non hai i permessi" in browser.get(BASE + "/utenti").text,
      "accesso con il provider: il ruolo resta quello della dashboard (tecnico)")
form_post(browser, "/logout", "/", {})
check(not logged_as(browser), "uscita dopo l'accesso con il provider")

r = form_post(admin, "/account/accesso", "/account", {"azione": "collega"})   # provider ancora su authentik-luca
check("già collegato a un altro utente" in r.text and db.execute(
    "SELECT oidc_sub FROM users WHERE username = 'mario'").fetchone()[0] is None,
      "collegamento: un'identità del provider già usata non si collega a un secondo utente")

# ---------------------------------------------------------------- token falsi o sbagliati
for mode, name in (("badsig", "firmato con un'altra chiave"), ("none", "senza firma (alg none)"),
                   ("wrongaud", "per un'altra applicazione"), ("wrongiss", "di un altro provider"),
                   ("expired", "scaduto"), ("badnonce", "di un altro accesso (nonce)")):
    s = requests.Session()
    r = sso(s, key, sub="authentik-luca", mode=mode)
    check(not logged_as(s) and "non riuscito" in r.text, f"token d'identità {name} rifiutato")
s = requests.Session()
sso(s, key, sub="authentik-luca", mode="hs256")
check(logged_as(s) == "luca", "token firmato HS256 col client secret (Authentik senza chiave di firma) accettato")
s = requests.Session()
r = sso(s, key, sub="authentik-luca", mode="denied")
check(not logged_as(s) and "non ha concesso" in r.text, "accesso annullato sul provider -> messaggio chiaro")

# ---------------------------------------------------------------- state e codice
provider(sub="authentik-luca")
victim = requests.Session()
r = victim.get(BASE + f"/accesso/{key}", allow_redirects=False)
to_provider = r.headers["Location"]
r = requests.get(to_provider, allow_redirects=False)
callback = r.headers["Location"]
attacker = requests.Session()
check(not logged_as(attacker) and "richiesta scaduta" in attacker.get(BASE + callback.replace(BASE, "")).text
      and not logged_as(attacker),
      "risposta del provider usata in un'altra sessione (state non suo) -> rifiutata")
victim.get(callback)
check(logged_as(victim) == "luca", "(controllo) la stessa risposta nella sessione giusta funziona")
again = requests.Session()
again.get(BASE + f"/accesso/{key}", allow_redirects=False)
check(not logged_as(again) and "richiesta scaduta" in again.get(callback).text,
      "risposta del provider riusata -> rifiutata")
s = requests.Session()
provider(sub="authentik-luca")
s.get(BASE + f"/accesso/{key}?next=//evil.example/x")
check(logged_as(s) == "luca" and s.get(BASE + f"/accesso/{key}?next=//evil.example/x").url.startswith(BASE),
      "dopo l'accesso con il provider si torna solo a pagine della dashboard (next)")

# ---------------------------------------------------------------- collegamento automatico per email
s = requests.Session()
r = sso(s, key, sub="authentik-anna", email="anna@rossi.it")
check(not logged_as(s), "collegamento per email spento (predefinito): l'email da sola non basta")
form_post(admin, "/impostazioni/accesso", "/impostazioni", {"oidc_enabled": "1", "oidc_issuer": ISSUER, "oidc_auto_email": "1",
                                                            "oidc_client_id": "assist-client", "oidc_label": "Authentik Rossi"})
check(db.execute("SELECT oidc_sub FROM users WHERE username = 'luca'").fetchone()[0] == "authentik-luca",
      "salvare senza cambiare provider non scollega gli utenti")
r = sso(s, key, sub="authentik-anna", email="anna@rossi.it", email_verified=False)
check(not logged_as(s) and "non è verificata" in r.text, "collegamento per email: un'email non verificata non vale")
r = sso(s, key, sub="authentik-anna", email="ANNA@rossi.it")
check(logged_as(s) == "anna", "collegamento per email (attivato dall'amministratore): entra l'utente con quell'email")
s = requests.Session()
r = sso(s, key, sub="authentik-x", email="doppia@rossi.it")
check(not logged_as(s) and "più utenti" in r.text, "collegamento per email: email di due utenti -> nessuno dei due")
s = requests.Session()
r = sso(s, key, sub="authentik-y", email="anna@rossi.it")
check(not logged_as(s), "collegamento per email: un'altra identità con l'email di un utente già collegato non entra")
s = requests.Session()
r = sso(s, key, sub="authentik-z", email="bianchi@rossi.it")
check(not logged_as(s) and db.execute("SELECT oidc_sub FROM users WHERE username = 'bianchi'").fetchone()[0] is None,
      "collegamento per email: mai verso utenti di un'altra azienda (stessa email di luca in Bianchi Elettrica)")

# ---------------------------------------------------------------- utente disattivato, cambio provider, sospensione
luca_id = db.execute("SELECT id FROM users WHERE username = 'luca'").fetchone()[0]
form_post(admin, f"/utenti/{luca_id}/attiva", "/utenti", {})
s = requests.Session()
r = sso(s, key, sub="authentik-luca")
check(not logged_as(s) and "disattivato" in r.text, "utente disattivato nella dashboard: non entra con il provider")
form_post(admin, f"/utenti/{luca_id}/attiva", "/utenti", {})
bia = requests.Session()
bia.post(BASE + "/login", data={"csrf": csrf(bia.get(BASE + "/login").text), "username": "bianchi", "password": "passwordbianchi1"})
form_post(bia, "/account", "/account", {"azione": "password", "current": "passwordbianchi1", "password": "nuovabianchi12",
                                        "password2": "nuovabianchi12"})
r = form_post(bia, f"/utenti/{luca_id}/scollega", "/utenti", {})
check(db.execute("SELECT oidc_sub FROM users WHERE id = ?", (luca_id,)).fetchone()[0] == "authentik-luca",
      "un'altra azienda non può scollegare i tuoi utenti")
check(bia.get(BASE + "/impostazioni").text.count("/accesso/" + key) == 0,
      "un'altra azienda non vede il link d'accesso della tua")
form_post(admin, "/impostazioni/accesso", "/impostazioni", {"oidc_enabled": "1", "oidc_issuer": ISSUER, "oidc_auto_email": "1",
                                                            "oidc_client_id": "altro-client"})
check(db.execute("SELECT COUNT(*) FROM users WHERE company_id = 1 AND oidc_sub IS NOT NULL").fetchone()[0] == 0,
      "provider o applicazione cambiati: i collegamenti vecchi non valgono più")
form_post(admin, "/impostazioni/accesso", "/impostazioni", {"oidc_enabled": "1", "oidc_issuer": ISSUER,
                                                            "oidc_client_id": "assist-client"})
s = requests.Session()
form_post(admin, "/aziende/1/sospendi", "/aziende", {})   # il gestore non può sospendere la propria: niente effetto
co2 = db.execute("SELECT id FROM companies WHERE name = 'Bianchi Elettrica'").fetchone()[0]
form_post(bia, "/impostazioni/accesso", "/impostazioni", {"oidc_enabled": "1", "oidc_issuer": ISSUER,
                                                          "oidc_client_id": "assist-client", "oidc_client_secret": "segreto-del-client-123"})
bia_key = db.execute("SELECT value FROM company_settings WHERE company_id = ? AND key = 'oidc_key'", (co2,)).fetchone()
bia_key = bia_key[0] if bia_key else ""
provider(sub="authentik-bianchi")
form_post(bia, "/account/accesso", "/account", {"azione": "collega"})
check(db.execute("SELECT oidc_sub FROM users WHERE username = 'bianchi'").fetchone()[0] == "authentik-bianchi",
      "(preparazione) la seconda azienda collega il suo provider e il suo utente")
form_post(admin, f"/aziende/{co2}/sospendi", "/aziende", {})
s = requests.Session()
r = sso(s, bia_key, sub="authentik-bianchi")
check(bia_key and bia_key != key and not logged_as(s), "azienda sospesa: il suo link d'accesso con il provider non funziona")
s = requests.Session()
r = sso(s, key, sub="authentik-bianchi")
check(not logged_as(s), "l'identità collegata in un'azienda non apre un'altra azienda con lo stesso provider")

bad = [r for r in results if not r[0]]
print(f"\n{len(results) - len(bad)} difesi, {len(bad)} vulnerabili su {len(results)} controlli")
sys.exit(1 if bad else 0)

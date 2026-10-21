"""Test di sicurezza del server installatore. Avviare con tests/run_security.sh.

Ogni controllo prova un attacco e stampa OK (difeso) oppure VULNERABILE.
Il server deve essere appena installato (nessun utente), così si prova anche il primo avvio.
"""
import json
import os
import re
import sqlite3
import sys
import threading
import time

import requests

BASE = os.environ.get("BASE", "http://127.0.0.1:8080")
DATA = sys.argv[1]
results = []


def check(ok, name, detail=""):
    results.append((ok, name, detail))
    print(("OK          " if ok else "VULNERABILE ") + name + (f"  [{detail}]" if detail and not ok else ""))


def csrf(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def setup_code():
    path = os.path.join(DATA, "setup-code.txt")
    return open(path).read().strip() if os.path.exists(path) else ""


def form_post(sess, path, page, data, **kw):
    return sess.post(BASE + path, data={"csrf": csrf(sess.get(BASE + page).text), **data}, **kw)


def agent_headers(token):
    return {"Authorization": f"Bearer {token}", "X-Agent-Protocol": "1", "X-Agent-Version": "0.1.3"}


# ---------------------------------------------------------------- primo avvio
attacker = requests.Session()
r = attacker.post(BASE + "/setup", data={"csrf": csrf(attacker.get(BASE + "/setup").text), "company_name": "Pirata",
                                         "username": "pirata", "password": "passwordpirata1", "password2": "passwordpirata1"},
                  allow_redirects=False)
db = sqlite3.connect(os.path.join(DATA, "suite.db"))
taken = db.execute("SELECT COUNT(*) FROM users WHERE username = 'pirata'").fetchone()[0] > 0
check(not taken, "primo avvio: uno sconosciuto non può creare l'amministratore senza codice di installazione")
if taken:  # per proseguire con gli altri test ripartiamo da un database pulito
    db.execute("DELETE FROM users"); db.commit()

admin = requests.Session()
admin.post(BASE + "/setup", data={"csrf": csrf(admin.get(BASE + "/setup").text), "setup_code": setup_code(),
                                  "company_name": "Rossi Impianti", "brand_color": "#123456;}body{display:none",
                                  "full_name": "Mario", "username": "mario", "password": "passwordsicura1",
                                  "password2": "passwordsicura1"})
check("Panoramica" in admin.get(BASE + "/").text, "(preparazione) amministratore creato con il codice corretto")
color = db.execute("SELECT brand_color FROM companies ORDER BY id LIMIT 1").fetchone()
check(bool(color) and re.fullmatch(r"#[0-9a-fA-F]{6}", color[0] or ""), "colore del marchio validato al primo avvio",
      f"salvato: {color[0] if color else None}")
code_file = os.path.join(DATA, "setup-code.txt")
check(not os.path.exists(code_file), "codice di installazione eliminato dopo l'uso")

# ---------------------------------------------------------------- utenti di prova
for user, role in (("tecnico1", "tecnico"), ("lettore1", "lettura")):
    form_post(admin, "/utenti", "/utenti", {"username": user, "full_name": user, "password": "passwordtecnico1", "role": role})


def login(user, password="passwordtecnico1", headers=None):
    # utenti di prova: il cambio password del primo accesso è provato a parte (e2e)
    db.execute("UPDATE users SET must_change = 0 WHERE username = ?", (user,)); db.commit()
    s = requests.Session()
    r = s.post(BASE + "/login", data={"csrf": csrf(s.get(BASE + "/login").text), "username": user, "password": password},
               headers=headers or {}, allow_redirects=False)
    return s, r


tec, _ = login("tecnico1")
let, _ = login("lettore1")

# cliente e due impianti abbinati
r = form_post(admin, "/clienti/nuovo", "/clienti/nuovo", {"name": "<script>alert('cliente')</script>"})
cid = int(re.search(r"/clienti/(\d+)", r.url).group(1))
tokens, pids = [], []
for name in ("A", "B"):
    r = form_post(admin, f"/clienti/{cid}/impianti", f"/clienti/{cid}", {"name": name})
    pid = int(re.search(r"/impianti/(\d+)", r.url).group(1))
    code = re.search(r'class="code">([A-Z0-9-]{9})<', r.text).group(1)
    res = requests.post(BASE + "/api/agent/pair", json={"code": code, "ha_version": "2026.10.1", "agent_version": "0.1.3"}).json()
    tokens.append(res["token"]); pids.append(pid)
ta, tb = tokens
pa, pb = pids

# ---------------------------------------------------------------- accesso e sessioni
limited = None
for i in range(15):
    _, r = login("mario", "sbagliata!!", headers={"X-Forwarded-For": f"203.0.113.{i + 1}"})
    if "Troppi tentativi" in r.text:
        limited = i
        break
check(limited is not None, "login: il blocco dei tentativi non si aggira cambiando X-Forwarded-For",
      "15 password sbagliate con IP falsificati, nessun blocco")
time.sleep(0.2)

# enumerazione utenti dal tempo di risposta (si usano IP diversi per non incappare nel blocco)
def timed(user, ip):
    t = time.perf_counter(); login(user, "x", headers={"X-Forwarded-For": ip}); return time.perf_counter() - t
exist = min(timed("tecnico1", f"198.51.100.{i}") for i in range(1, 4))
ghost = min(timed("nessuno_qui", f"198.51.100.{i}") for i in range(10, 13))
check(abs(exist - ghost) < 0.5 * max(exist, ghost), "login: il tempo di risposta non rivela se l'utente esiste",
      f"esistente {exist * 1000:.0f} ms, inesistente {ghost * 1000:.0f} ms")

for nxt in ("//evil.example", "/\\evil.example", "/%5Cevil.example", "https://evil.example"):
    s = requests.Session()
    page = s.get(BASE + "/login", params={"next": nxt}).text
    r = s.post(BASE + "/login?next=" + requests.utils.quote(nxt, safe=""),
               data={"csrf": csrf(page), "username": "tecnico1", "password": "passwordtecnico1"},
               headers={"X-Forwarded-For": "192.0.2.50"}, allow_redirects=False)
    loc = r.headers.get("location", "")
    bad = loc.startswith(("//", "/\\")) or "://" in loc
    if bad:
        break
check(not bad, "login: nessun reindirizzamento verso siti esterni (parametro next)", f"next={nxt} -> {loc}")

r = tec.post(BASE + "/logout", allow_redirects=False)
still = "Panoramica" in tec.get(BASE + "/").text
check(still, "logout: non si può far uscire un utente da un altro sito (CSRF sul logout)")
tec, _ = login("tecnico1")

cookies = dict(tec.cookies)
tec_id = db.execute("SELECT id FROM users WHERE username = 'tecnico1'").fetchone()[0]
form_post(admin, f"/utenti/{tec_id}/attiva", "/utenti", {})
check("Panoramica" not in requests.get(BASE + "/", cookies=cookies).text, "utente disattivato: la sua sessione aperta smette di funzionare")
form_post(admin, f"/utenti/{tec_id}/attiva", "/utenti", {})
tec, _ = login("tecnico1")

# ---------------------------------------------------------------- permessi per ruolo
def refused(sess, path, page, data):
    before = db.execute("SELECT (SELECT COUNT(*) FROM clients) + (SELECT COUNT(*) FROM users) + "
                        "(SELECT COUNT(*) FROM commands) + (SELECT COUNT(*) FROM settings) + (SELECT COUNT(*) FROM companies)").fetchone()[0]
    r = form_post(sess, path, page, data)
    after = db.execute("SELECT (SELECT COUNT(*) FROM clients) + (SELECT COUNT(*) FROM users) + "
                       "(SELECT COUNT(*) FROM commands) + (SELECT COUNT(*) FROM settings) + (SELECT COUNT(*) FROM companies)").fetchone()[0]
    return after == before and ("permessi" in r.text or r.status_code in (302, 303, 403))

check(refused(let, "/clienti/nuovo", "/", {"name": "da lettore"}), "ruolo lettura: non può creare clienti")
check(refused(let, f"/impianti/{pa}/comandi", "/", {"name": "ping"}), "ruolo lettura: non può inviare comandi")
check(refused(let, "/utenti", "/", {"username": "x_lettore", "password": "passwordlunga1", "role": "admin"}), "ruolo lettura: non può creare utenti")
check(refused(tec, "/utenti", "/", {"username": "x_tecnico", "password": "passwordlunga1", "role": "admin"}), "ruolo tecnico: non può creare amministratori")
check(refused(tec, "/impostazioni", "/", {"company_name": "Cambiata", "brand_color": "#000000", "public_url": "https://evil.example"}),
      "ruolo tecnico: non può cambiare le impostazioni")
# 0.2.7: eliminazioni solo per l'amministratore, e solo riscrivendo il nome
counts = lambda: db.execute("SELECT (SELECT COUNT(*) FROM clients), (SELECT COUNT(*) FROM plants)").fetchone()
cid_a, pname_a = db.execute("SELECT client_id, name FROM plants WHERE id = ?", (pa,)).fetchone()
cname_a = db.execute("SELECT name FROM clients WHERE id = ?", (cid_a,)).fetchone()[0]
before = tuple(counts())
form_post(tec, f"/impianti/{pa}/elimina", f"/impianti/{pa}", {"confirm_name": pname_a})
form_post(tec, f"/clienti/{cid_a}/elimina", f"/clienti/{cid_a}", {"confirm_name": cname_a})
form_post(let, f"/impianti/{pa}/nome", f"/impianti/{pa}", {"name": "rinominato da lettore"})
admin.post(BASE + f"/clienti/{cid_a}/elimina", data={"confirm_name": cname_a, "csrf": "sbagliato"})
check(tuple(counts()) == before and db.execute("SELECT name FROM plants WHERE id = ?", (pa,)).fetchone()[0] == pname_a,
      "eliminazioni: vietate a tecnico e lettore, e senza token CSRF; il lettore non rinomina")
r = requests.post(BASE + "/clienti/nuovo", data={"name": "anonimo"}, allow_redirects=False)
check(r.status_code in (302, 303) and "/login" in r.headers.get("location", ""), "senza login: le pagine riservate rimandano al login")

fresh, _ = login("tecnico1")  # sessione appena aperta, nessuna pagina ancora caricata
fresh.post(BASE + "/clienti/nuovo", data={"name": "csrf vuoto"})
check(not db.execute("SELECT 1 FROM clients WHERE name = 'csrf vuoto'").fetchone(),
      "CSRF: subito dopo il login un modulo senza token viene rifiutato")
r = admin.post(BASE + "/clienti/nuovo", data={"name": "senza csrf", "csrf": "sbagliato"})
check(not db.execute("SELECT 1 FROM clients WHERE name = 'senza csrf'").fetchone(), "CSRF: modulo con token sbagliato rifiutato")

# ---------------------------------------------------------------- API agente
r = requests.post(BASE + "/api/agent/report", headers=agent_headers(ta), json={"consent": True}).status_code
check(r == 200, "(preparazione) l'impianto A risponde con il suo token")
# ticket dell'impianto B, poi A prova a leggerlo/scriverci
tid_b = requests.post(BASE + "/api/agent/tickets", headers=agent_headers(tb), json={"title": "segreto di B", "description": "riservato"}).json()["ticket_id"]
r = requests.post(BASE + f"/api/agent/tickets/{tid_b}/messages", headers=agent_headers(ta), json={"text": "intruso"})
listing = requests.get(BASE + "/api/agent/tickets", headers=agent_headers(ta)).json()
check(r.status_code == 404 and "segreto di B" not in json.dumps(listing), "agente: un impianto non legge né scrive i ticket di un altro")

form_post(admin, f"/impianti/{pb}/comandi", f"/impianti/{pb}", {"name": "ping"})
cmd_b = db.execute("SELECT id FROM commands WHERE plant_id = ? ORDER BY id DESC", (pb,)).fetchone()[0]
r = requests.post(BASE + f"/api/agent/commands/{cmd_b}/result", headers=agent_headers(ta), json={"ok": True, "result": {"x": 1}})
check(r.status_code == 404, "agente: un impianto non può rispondere ai comandi di un altro")

cmds = requests.get(BASE + "/api/agent/commands?wait=0", headers=agent_headers(tb)).json()["commands"]
requests.post(BASE + f"/api/agent/commands/{cmd_b}/result", headers=agent_headers(tb), json={"ok": True, "result": {"pong": 1}})
requests.post(BASE + f"/api/agent/commands/{cmd_b}/result", headers=agent_headers(tb), json={"ok": True, "result": {"falso": 1}})
res = db.execute("SELECT result FROM commands WHERE id = ?", (cmd_b,)).fetchone()[0]
check("falso" not in res, "agente: il risultato di un comando già concluso non si può riscrivere")

r = requests.post(BASE + "/api/agent/report", headers=agent_headers("token-inventato"), json={})
check(r.status_code == 401, "agente: token inventato rifiutato")

# codice di abbinamento usato due volte nello stesso istante
r = form_post(admin, f"/clienti/{cid}/impianti", f"/clienti/{cid}", {"name": "C"})
code_c = re.search(r'class="code">([A-Z0-9-]{9})<', r.text).group(1)
wins = []
def grab():
    rr = requests.post(BASE + "/api/agent/pair", json={"code": code_c})
    if rr.status_code == 200:
        wins.append(rr.json()["token"])
threads = [threading.Thread(target=grab) for _ in range(12)]
[t.start() for t in threads]; [t.join() for t in threads]
check(len(wins) <= 1, "abbinamento: un codice monouso non vale per due agenti in contemporanea", f"{len(wins)} token rilasciati")

# corpi malformati o enormi
codes = []
for body in (b"[1,2,3]", b'"stringa"', b"\xff\xfe", b'{"code": 12345}', b'{"code": "AAAA-AAAA", "ha_version": {"a": 1}}'):
    codes.append(requests.post(BASE + "/api/agent/pair", data=body, headers={"Content-Type": "application/json"}).status_code)
check(all(c < 500 for c in codes), "agente: JSON malformato non fa andare in errore il server", f"risposte {codes}")
codes = [requests.post(BASE + "/api/agent/report", headers=agent_headers(ta), data=b, ).status_code
         for b in (b"[]", b'{"ha_version": ["x"]}', b'{"consent_until": 123}')]
check(all(c < 500 for c in codes), "agente: rapporti malformati non fanno andare in errore il server", f"risposte {codes}")

pid_srv = int(os.environ.get("SERVER_PID", "0"))
def rss():
    try:
        return int(re.search(r"VmRSS:\s+(\d+)", open(f"/proc/{pid_srv}/status").read()).group(1)) // 1024
    except Exception:
        return 0
before = rss()
big = b"{" + b" " * (120 * 1024 * 1024) + b"}"
t0 = time.time()
try:
    r = requests.post(BASE + "/api/agent/pair", data=big, headers={"Content-Type": "application/json"}, timeout=60)
    status = r.status_code
except requests.RequestException as e:
    status = f"connessione chiusa ({type(e).__name__})"
grow = rss() - before
check(status == 413 or (isinstance(status, str) and grow < 30), "agente: una richiesta da 120 MB viene respinta senza caricarla in memoria",
      f"risposta {status}, memoria del server +{grow} MB")
del big

# ---------------------------------------------------------------- dati dell'agente mostrati in dashboard
evil = "<img src=x onerror=alert(1)>"
requests.post(BASE + "/api/agent/tickets", headers={**agent_headers(ta), "X-Agent-Version": evil},
              json={"title": evil, "description": evil, "contact": evil,
                    "diagnostics": {"status": evil, "issues": [{"severity": evil, "message": evil, "items": [evil]}]}})
requests.post(BASE + "/api/agent/report", headers={**agent_headers(ta), "X-Agent-Version": evil},
              json={"consent": True, "ha_version": evil, "diagnostics": {"status": evil, "summary": {"entities": evil},
                                                                         "issues": [{"severity": evil, "message": evil}]}})
pages = [admin.get(BASE + p).text for p in ("/", f"/impianti/{pa}", f"/clienti/{cid}", "/ticket")]
tid_a = db.execute("SELECT id FROM tickets WHERE plant_id = ? ORDER BY id DESC", (pa,)).fetchone()[0]
pages.append(admin.get(BASE + f"/ticket/{tid_a}").text)
check(not any("<img src=x" in p or "<script>alert" in p for p in pages), "XSS: testi del cliente e dell'agente mostrati in modo sicuro")

broken = []
for diag in ({"issues": ["solo una stringa"]}, {"collected_at": 12345, "system": {"last_backup": 99}},
             {"summary": "x", "system": [1, 2], "issues": {"a": 1}}, {"issues": [{"items": 5}]}):
    requests.post(BASE + "/api/agent/report", headers=agent_headers(ta), json={"consent": True, "diagnostics": diag})
    requests.post(BASE + "/api/agent/tickets", headers=agent_headers(ta), json={"title": "diag rotta", "diagnostics": diag})
    tid = db.execute("SELECT id FROM tickets ORDER BY id DESC").fetchone()[0]
    for p in ("/", f"/impianti/{pa}", f"/ticket/{tid}"):
        try:
            code = admin.get(BASE + p).status_code
        except requests.RequestException:
            code = 599
        if code >= 500:
            broken.append(f"{p} con {json.dumps(diag)}")
requests.post(BASE + "/api/agent/report", headers=agent_headers(ta), json={"consent": True, "consent_until": "2030-01-01T10:00:00"})
if admin.get(BASE + f"/impianti/{pa}").status_code >= 500:
    broken.append("consent_until senza fuso orario")
check(not broken, "un agente con dati strani non può rompere le pagine della dashboard", "; ".join(broken[:2]))

# ---------------------------------------------------------------- accesso amministratore temporaneo
def report_a(admin_state):
    return requests.post(BASE + "/api/agent/report", headers=agent_headers(ta), json={"consent": True, "admin": admin_state}).json()

report_a({"active": False})
form_post(admin, f"/impianti/{pa}/amministratore", f"/impianti/{pa}", {"op": "password"})
check(not db.execute("SELECT 1 FROM commands WHERE plant_id = ? AND name = 'admin_password'", (pa,)).fetchone(),
      "amministratore: senza il consenso del cliente il server non chiede nessuna password all'agente")
form_post(let, f"/impianti/{pa}/amministratore", f"/impianti/{pa}", {"op": "richiedi"})
check(db.execute("SELECT admin_requested_at FROM plants WHERE id = ?", (pa,)).fetchone()[0] is None,
      "amministratore: il ruolo lettura non può chiedere l'accesso")
report_a({"active": True, "until": "2099-01-01T00:00:00+00:00", "username": "assistenza_abc123"})
form_post(admin, f"/impianti/{pa}/amministratore", f"/impianti/{pa}", {"op": "password"})
cmd = db.execute("SELECT id FROM commands WHERE plant_id = ? AND name = 'admin_password' ORDER BY id DESC", (pa,)).fetchone()
SECRET = "Zx9" + "q" * 21
r = requests.post(BASE + f"/api/agent/commands/{cmd[0]}/result", headers=agent_headers(ta),
                  json={"ok": True, "result": {"username": "assistenza_abc123", "password": SECRET}})
dump = "\n".join(str(row) for t in ("commands", "plants", "settings") for row in db.execute(f"SELECT * FROM {t}"))
check(SECRET not in dump and SECRET in admin.get(BASE + f"/impianti/{pa}").text,
      "amministratore: la password si vede in dashboard ma non viene mai salvata nel database")
check(SECRET not in let.get(BASE + f"/impianti/{pa}").text and SECRET not in admin.get(BASE + f"/impianti/{pb}").text,
      "amministratore: la password non si vede col ruolo lettura né nella pagina di un altro impianto")
r = requests.post(BASE + f"/api/agent/commands/{cmd[0]}/result", headers=agent_headers(tb),
                  json={"ok": True, "result": {"username": "assistenza_evil", "password": "A" * 24}})
check(r.status_code == 404, "amministratore: un altro impianto non può inserire credenziali al posto dell'agente giusto")
report_a({"active": False})
check(SECRET not in admin.get(BASE + f"/impianti/{pa}").text, "amministratore: chiuso l'accesso, la password sparisce subito dalla dashboard")
report_a({"active": True, "username": "<script>x</script>", "until": "x"})
check("<script>x" not in admin.get(BASE + f"/impianti/{pa}").text, "amministratore: dati strani dall'agente non finiscono nella pagina")
report_a({"active": False})

# ---------------------------------------------------------------- moduli della dashboard
def safe_status(fn):
    try:
        return fn().status_code
    except requests.RequestException:
        return 599
codes = [safe_status(lambda: form_post(admin, "/ticket/99999/rispondi", "/ticket", {"text": "ciao"})),
         safe_status(lambda: form_post(admin, f"/ticket/{tid_b}/rispondi", f"/ticket/{tid_b}", {"text": "", "assigned_to": "abc"})),
         safe_status(lambda: form_post(admin, f"/ticket/{tid_b}/rispondi", f"/ticket/{tid_b}", {"text": "", "assigned_to": "99999"}))]
check(all(c < 500 for c in codes), "dashboard: risposte a ticket inesistenti o dati errati non danno errore 500", f"risposte {codes}")

# ---------------------------------------------------------------- più aziende sullo stesso server
check(refused(tec, "/aziende", "/", {"name": "Azienda pirata", "username": "pirata2", "password": "passwordlunga1"}),
      "aziende: un tecnico non può creare aziende")
form_post(admin, "/aziende", "/aziende", {"name": "Bianchi Elettrica", "brand_color": "#aa0000", "full_name": "Luigi Bianchi",
                                          "username": "bianchi", "password": "passwordbianchi1"})
r = form_post(admin, "/aziende", "/aziende", {"name": "Altra", "username": "tecnico1", "password": "passwordlunga1"})
check(db.execute("SELECT COUNT(*) FROM users WHERE username = 'tecnico1'").fetchone()[0] == 1
      and not db.execute("SELECT 1 FROM companies WHERE name = 'Altra'").fetchone(),
      "aziende: un nome utente già usato da un'altra azienda non si può riusare")
bia, _ = login("bianchi", "passwordbianchi1")
check("Panoramica" in bia.get(BASE + "/").text, "(preparazione) amministratore della seconda azienda collegato")
mario_id = db.execute("SELECT id FROM users WHERE username = 'mario'").fetchone()[0]
tec_id = db.execute("SELECT id FROM users WHERE username = 'tecnico1'").fetchone()[0]

# letture: ogni id della prima azienda risponde «non trovato» alla seconda
leaks = []
for path in (f"/clienti/{cid}", f"/clienti/{cid}/modifica", f"/impianti/{pa}", f"/impianti/{pa}/stato.json",
             f"/ticket/{tid_b}", f"/comandi/{cmd_b}.json"):
    r = bia.get(BASE + path)
    if r.status_code == 200 and ("Non trovato" not in r.text and "not found" not in r.text):
        leaks.append(path)
lists = "".join(bia.get(BASE + p).text for p in ("/", "/clienti", "/ticket?stato=tutti", "/utenti"))
if "segreto di B" in lists or "diag rotta" in lists or "tecnico1" in lists or "alert(&#39;cliente" in lists:
    leaks.append("elenchi")
check(not leaks, "aziende: la seconda azienda non vede clienti, impianti, ticket, comandi e utenti della prima", ", ".join(leaks))

# scritture: nessuna modifica ai dati della prima azienda
snap = lambda: db.execute("SELECT (SELECT group_concat(name || revoked || COALESCE(pair_code, '')) FROM plants), "
                          "(SELECT COUNT(*) FROM commands), (SELECT COUNT(*) FROM ticket_messages), "
                          "(SELECT group_concat(status) FROM tickets), (SELECT group_concat(password_hash || active) FROM users WHERE company_id = 1), "
                          "(SELECT COUNT(*) FROM clients), (SELECT COUNT(*) FROM plants)").fetchone()
before = snap()
for path, data in ((f"/impianti/{pa}/nome", {"name": "preso"}), (f"/impianti/{pa}/revoca", {}), (f"/impianti/{pa}/codice", {}),
                   (f"/impianti/{pa}/comandi", {"name": "ping"}), (f"/impianti/{pa}/amministratore", {"op": "richiedi"}),
                   (f"/impianti/{pa}/ha-url", {"ha_url_manual": "https://evil.example"}),
                   (f"/impianti/{pa}/elimina", {"confirm_name": pname_a}), (f"/clienti/{cid}/elimina", {"confirm_name": cname_a}),
                   (f"/clienti/{cid}/impianti", {"name": "intruso"}), (f"/clienti/{cid}/modifica", {"name": "preso"}),
                   (f"/ticket/{tid_b}/rispondi", {"text": "intruso", "status": "chiuso"}),
                   (f"/utenti/{tec_id}/password", {"password": "passwordrubata1"}), (f"/utenti/{tec_id}/attiva", {})):
    form_post(bia, path, "/", data)
check(snap() == before, "aziende: la seconda azienda non può modificare, eliminare o comandare nulla della prima")

# la seconda azienda ha il suo cliente, impianto e ticket: niente assegnazioni a utenti di altre aziende
r = form_post(bia, "/clienti/nuovo", "/clienti/nuovo", {"name": "Cliente riservato Bianchi"})
cid_x = int(re.search(r"/clienti/(\d+)", r.url).group(1))
r = form_post(bia, f"/clienti/{cid_x}/impianti", f"/clienti/{cid_x}", {"name": "X"})
code_x = re.search(r'class="code">([A-Z0-9-]{9})<', r.text).group(1)
paired = requests.post(BASE + "/api/agent/pair", json={"code": code_x, "ha_version": "2026.10.1", "agent_version": "0.1.8"}).json()
check(paired.get("server_name") == "Bianchi Elettrica" and paired.get("brand_color") == "#aa0000",
      "aziende: l'impianto della seconda azienda riceve il suo nome e il suo colore")
tid_x = requests.post(BASE + "/api/agent/tickets", headers=agent_headers(paired["token"]), json={"title": "guasto X"}).json()["ticket_id"]
form_post(bia, f"/ticket/{tid_x}/rispondi", f"/ticket/{tid_x}", {"text": "", "assigned_to": str(tec_id)})
check(db.execute("SELECT assigned_to FROM tickets WHERE id = ?", (tid_x,)).fetchone()[0] is None,
      "aziende: un ticket non si assegna a un utente di un'altra azienda")
check("guasto X" not in admin.get(BASE + "/ticket?stato=tutti").text and "Cliente riservato Bianchi" not in admin.get(BASE + "/clienti").text,
      "aziende: anche la prima azienda (del gestore) non vede i dati della seconda")
page = admin.get(BASE + "/aziende").text
check("Bianchi Elettrica" in page and "Cliente riservato Bianchi" not in page and "guasto X" not in page,
      "aziende: il gestore vede le aziende e i loro numeri, non i clienti né i ticket")

# solo il gestore: elenco delle aziende, indirizzo pubblico e aggiornamenti del server
check(refused(bia, "/aziende", "/", {"name": "Terza", "username": "terzo", "password": "passwordlunga1"}),
      "aziende: l'amministratore di un'azienda non può crearne altre")
form_post(bia, "/impostazioni", "/impostazioni", {"company_name": "Bianchi Elettrica", "public_url": "https://evil.example"})
st = bia.get(BASE + "/impostazioni").text
check(db.execute("SELECT value FROM settings WHERE key = 'public_url'").fetchone() in (None, ("",))
      and 'id="aggiornamenti"' not in st and 'name="public_url"' not in st,
      "aziende: indirizzo pubblico e aggiornamenti del server restano al gestore")
form_post(bia, "/impostazioni/telegram", "/impostazioni", {"telegram_token": "777:BIANCHI", "telegram_chat_id": "42", "azione": "salva"})
check(db.execute("SELECT COUNT(*) FROM company_settings WHERE value = '777:BIANCHI' AND company_id != "
                 "(SELECT company_id FROM users WHERE username = 'bianchi')").fetchone()[0] == 0
      and "777:BIANCHI" not in admin.get(BASE + "/impostazioni").text,
      "aziende: il bot Telegram di un'azienda resta suo")

# un altro amministratore dell'azienda del gestore non può prenderne il posto
form_post(admin, "/utenti", "/utenti", {"username": "admin2", "full_name": "Secondo", "password": "passwordadmin2", "role": "admin"})
adm2, _ = login("admin2", "passwordadmin2")
hash_before = db.execute("SELECT password_hash FROM users WHERE id = ?", (mario_id,)).fetchone()[0]
form_post(adm2, f"/utenti/{mario_id}/recupero", "/utenti", {})
form_post(adm2, f"/utenti/{mario_id}/attiva", "/utenti", {})
check(db.execute("SELECT password_hash, active FROM users WHERE id = ?", (mario_id,)).fetchone() == (hash_before, 1)
      and not db.execute("SELECT 1 FROM password_resets WHERE user_id = ?", (mario_id,)).fetchone()
      and "Aziende" not in adm2.get(BASE + "/").text.split("<main>")[0],
      "aziende: un altro amministratore non può cambiare password o disattivare il gestore, né vedere le aziende")

# ---------------------------------------------------------------- recupero della password
import socket, socketserver  # noqa: E401,E402

mails = []


class FakeSMTP(socketserver.StreamRequestHandler):
    """Server SMTP minimo per i test: raccoglie i messaggi."""
    def handle(self):
        self.wfile.write(b"220 prova\r\n")
        data = None
        while True:
            line = self.rfile.readline()
            if not line:
                return
            if data is not None:
                if line in (b".\r\n", b".\n"):
                    mails.append(b"".join(data).decode(errors="replace")); data = None
                    self.wfile.write(b"250 ok\r\n")
                else:
                    data.append(line)
                continue
            cmd = line[:4].upper()
            if cmd in (b"EHLO", b"HELO"):
                self.wfile.write(b"250 prova\r\n")
            elif cmd == b"DATA":
                data = []; self.wfile.write(b"354 vai\r\n")
            elif cmd == b"QUIT":
                self.wfile.write(b"221 ciao\r\n"); return
            else:
                self.wfile.write(b"250 ok\r\n")


smtp = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeSMTP)
threading.Thread(target=smtp.serve_forever, daemon=True).start()
smtp_port = smtp.server_address[1]
bia_id = db.execute("SELECT id FROM users WHERE username = 'bianchi'").fetchone()[0]
bia_co = db.execute("SELECT company_id FROM users WHERE username = 'bianchi'").fetchone()[0]
resets = lambda uid: db.execute("SELECT COUNT(*) FROM password_resets WHERE user_id = ?", (uid,)).fetchone()[0]

form_post(bia, f"/utenti/{tec_id}/recupero", "/utenti", {})
form_post(bia, f"/aziende/{bia_co}/recupero", "/", {"user_id": str(bia_id)})
form_post(admin, f"/aziende/{bia_co}/recupero", "/aziende", {"user_id": str(tec_id)})
check(resets(tec_id) == 0 and resets(bia_id) == 0,
      "recupero: niente link per utenti di altre aziende, e solo il gestore li crea dalla pagina Aziende")
r = form_post(admin, f"/aziende/{bia_co}/recupero", "/aziende", {"user_id": str(bia_id)})
check(resets(bia_id) == 1 and "/recupero/" in r.text, "(preparazione) il gestore crea il link per l'amministratore di un'azienda")
db.execute("INSERT INTO password_resets(token_hash, user_id, created_at, expires_at) VALUES(?, ?, ?, ?)",
           (__import__("hashlib").sha256(b"scaduto-scaduto-scaduto-123").hexdigest(), bia_id, "2020-01-01", "2020-01-01T00:30:00+00:00"))
db.commit()
check(all("non vale più" in requests.get(BASE + f"/recupero/{t}").text for t in ("inventato-inventato-inventato-1", "scaduto-scaduto-scaduto-123")),
      "recupero: link inventati o scaduti non valgono")

# email: solo il gestore la configura; la risposta non rivela chi esiste; il link arriva e funziona
form_post(bia, "/impostazioni/email", "/impostazioni", {"email_reset": "1", "smtp_host": "evil.example", "smtp_from": "a@evil.example"})
check(db.execute("SELECT value FROM settings WHERE key = 'smtp_host'").fetchone() is None, "email: solo il gestore configura l'SMTP")
form_post(admin, "/impostazioni/email", "/impostazioni", {"email_reset": "1", "smtp_host": "127.0.0.1", "smtp_port": str(smtp_port),
                                                         "smtp_security": "nessuna", "smtp_from": "server@example.com", "azione": "salva"})
db.execute("UPDATE users SET email = 'luigi@example.com' WHERE id = ?", (bia_id,)); db.commit()
check("Password dimenticata" in requests.get(BASE + "/login").text, "(preparazione) recupero via email attivato dal gestore")
def ask(who, ip):
    s2 = requests.Session()
    return s2.post(BASE + "/recupero", data={"csrf": csrf(s2.get(BASE + "/recupero").text), "who": who},
                   headers={"X-Forwarded-For": ip}).text
same = ask("nessuno_cosi", "198.51.100.1"), ask("bianchi", "198.51.100.2")
got = None
for _ in range(50):
    if mails:
        got = mails[-1]; break
    time.sleep(0.1)
strip = lambda t: re.sub(r'name="csrf" value="[^"]+"', "", t)
check(strip(same[0]) == strip(same[1]) and "Se il nome utente" in same[0]
      and got and "luigi@example.com" in got and "/recupero/" in got,
      "email: stessa risposta per utenti esistenti e non, il link arriva solo all'interessato")
import email as _email  # noqa: E402
body = _email.message_from_string(got).get_payload(decode=True).decode() if got else ""
m = re.search(r"(/recupero/[A-Za-z0-9_-]{20,})", body)
if m:
    s2 = requests.Session(); pg = s2.get(BASE + m.group(1)).text
    s2.post(BASE + m.group(1), data={"csrf": csrf(pg), "password": "nuovabianchi1", "password2": "nuovabianchi1"})
_, r = login("bianchi", "nuovabianchi1")
check(r.status_code in (302, 303) and "/login" not in r.headers.get("location", ""), "email: il link ricevuto imposta la nuova password")
n_before = len(mails)
for i in range(8):
    ask("bianchi", "198.51.100.9")
time.sleep(1)
check(len(mails) - n_before <= 3, "email: niente valanga di messaggi allo stesso utente", f"{len(mails) - n_before} email")
bia, _ = login("bianchi", "nuovabianchi1")

# controllo periodico della posta: email di controllo al gestore, avviso se smette di funzionare
db.execute("UPDATE users SET email = 'gestore@example.com' WHERE id = ?", (mario_id,))
db.execute("UPDATE settings SET value = ? WHERE key = 'mail_sent_at'", (str(time.time() - 31 * 86400),)); db.commit()
n_before = len(mails)
form_post(admin, "/impostazioni/email", "/impostazioni", {"azione": "controlla"})
check(any("gestore@example.com" in m and "Controllo mensile" in m for m in mails[n_before:]),
      "posta: dopo 30 giorni senza invii parte l'email di controllo al gestore (la chiave del servizio resta attiva)")
db.execute("UPDATE settings SET value = '1' WHERE key = 'smtp_port'"); db.commit()
r = form_post(admin, "/impostazioni/email", "/impostazioni", {"azione": "controlla"})
check("La posta del server non funziona" in admin.get(BASE + "/").text and "posta del server" not in bia.get(BASE + "/").text,
      "posta: se l'accesso smette di funzionare il gestore (e solo lui) vede l'avviso")
db.execute("UPDATE settings SET value = ? WHERE key = 'smtp_port'", (str(smtp_port),)); db.commit()
form_post(admin, "/impostazioni/email", "/impostazioni", {"azione": "controlla"})
check("La posta del server non funziona" not in admin.get(BASE + "/").text, "posta: sistemato il problema, l'avviso sparisce")

# ---------------------------------------------------------------- limiti
plants_of = lambda: db.execute("SELECT COUNT(*) FROM plants p JOIN clients c ON c.id = p.client_id WHERE c.company_id = ?",
                               (bia_co,)).fetchone()[0]
n = plants_of()
form_post(bia, f"/aziende/{bia_co}/limiti", "/", {"max_plants": "1000"})
form_post(admin, f"/aziende/{bia_co}/limiti", "/aziende", {"max_plants": str(n - 1)})
check(db.execute("SELECT max_plants FROM companies WHERE id = ?", (bia_co,)).fetchone()[0] is None,
      "limiti: solo il gestore li cambia, e non sotto gli impianti che l'azienda ha già")
form_post(admin, f"/aziende/{bia_co}/limiti", "/aziende", {"max_plants": str(n + 1)})
page = bia.get(BASE + f"/clienti/{cid_x}").text
results_add = []
def add():
    results_add.append(bia.post(BASE + f"/clienti/{cid_x}/impianti", data={"csrf": csrf(page), "name": "corsa"}).status_code)
ts = [threading.Thread(target=add) for _ in range(6)]
[t.start() for t in ts]; [t.join() for t in ts]
check(plants_of() == n + 1, "limiti: richieste contemporanee non superano il limite di impianti", f"{plants_of()} su {n + 1}, risposte {results_add}")
form_post(admin, f"/aziende/{bia_co}/limiti", "/aziende", {"max_clients": "1"})
before_c = db.execute("SELECT COUNT(*) FROM clients WHERE company_id = ?", (bia_co,)).fetchone()[0]
form_post(bia, "/clienti/nuovo", "/clienti/nuovo", {"name": "oltre il limite"})
check(db.execute("SELECT COUNT(*) FROM clients WHERE company_id = ?", (bia_co,)).fetchone()[0] == before_c,
      "limiti: oltre il limite di clienti non se ne aggiungono")

# ---------------------------------------------------------------- sospensione
token_x = paired["token"]
form_post(bia, f"/aziende/{bia_co}/sospendi", "/", {})
mario_co = db.execute("SELECT company_id FROM users WHERE id = ?", (mario_id,)).fetchone()[0]
form_post(admin, f"/aziende/{mario_co}/sospendi", "/aziende", {})
check(db.execute("SELECT COUNT(*) FROM companies WHERE suspended_at IS NOT NULL").fetchone()[0] == 0,
      "sospensione: solo il gestore sospende, e non la propria azienda")
form_post(admin, f"/aziende/{bia_co}/sospendi", "/aziende", {})
r_agent = requests.post(BASE + "/api/agent/report", headers=agent_headers(token_x), json={"consent": True})
_, r_login = login("bianchi", "nuovabianchi1")
check("/login" in bia.get(BASE + "/").url and "sospeso" in r_login.text and r_agent.status_code == 423,
      "sospensione: utenti fuori (anche le sessioni aperte), agenti in pausa senza perdere l'abbinamento",
      f"{bia.get(BASE + '/').url} {r_login.status_code} {r_agent.status_code} {r_login.text[:0]}")
form_post(admin, f"/aziende/{bia_co}/sospendi", "/aziende", {})
check(requests.post(BASE + "/api/agent/report", headers=agent_headers(token_x), json={"consent": True}).status_code == 200,
      "sospensione: riattivata l'azienda, l'agente riparte con lo stesso abbinamento")

# ---------------------------------------------------------------- eliminazione
form_post(admin, f"/aziende/{bia_co}/elimina", "/aziende", {"confirm_name": "Bianchi Elettrica", "password": "sbagliata!!"})
form_post(bia, f"/aziende/{bia_co}/elimina", "/", {"confirm_name": "Bianchi Elettrica", "password": "nuovabianchi1"})
form_post(admin, f"/aziende/{mario_co}/elimina", "/aziende", {"confirm_name": "Rossi Impianti", "password": "passwordsicura1"})
check(db.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 2,
      "eliminazione: serve la password del gestore, non la può fare un'azienda, e non la propria")
backups = lambda: [f for f in os.listdir(os.path.join(DATA, "backups"))] if os.path.isdir(os.path.join(DATA, "backups")) else []
form_post(admin, f"/aziende/{bia_co}/elimina", "/aziende", {"confirm_name": "bianchi  elettrica", "password": "passwordsicura1"})
left = db.execute("SELECT (SELECT COUNT(*) FROM companies WHERE id = ?), (SELECT COUNT(*) FROM users WHERE company_id = ?), "
                  "(SELECT COUNT(*) FROM clients WHERE company_id = ?)", (bia_co, bia_co, bia_co)).fetchone()
check(left == (0, 0, 0) and any("azienda" in f for f in backups())
      and requests.post(BASE + "/api/agent/report", headers=agent_headers(token_x), json={}).status_code == 401
      and "Panoramica" in admin.get(BASE + "/").text,
      "eliminazione: azienda cancellata con i suoi dati dopo una copia di sicurezza; i suoi agenti risultano revocati")

# ---------------------------------------------------------------- intestazioni HTTP
h = admin.get(BASE + "/").headers
missing = [k for k in ("x-frame-options", "x-content-type-options", "referrer-policy", "content-security-policy") if k not in h]
check(not missing, "intestazioni di sicurezza (anti-clickjacking, nosniff, CSP)", "mancano: " + ", ".join(missing))
h = requests.get(BASE + "/login").headers
check("x-frame-options" in h or "frame-ancestors" in h.get("content-security-policy", ""), "la pagina di login non si può incorniciare in un altro sito")

# ---------------------------------------------------------------- riepilogo
bad = [r for r in results if not r[0]]
print(f"\n{len(results) - len(bad)} difesi, {len(bad)} vulnerabili su {len(results)} controlli")
sys.exit(1 if bad else 0)

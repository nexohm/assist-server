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
color = db.execute("SELECT value FROM settings WHERE key = 'brand_color'").fetchone()
check(bool(color) and re.fullmatch(r"#[0-9a-fA-F]{6}", color[0] or ""), "colore del marchio validato al primo avvio",
      f"salvato: {color[0] if color else None}")
code_file = os.path.join(DATA, "setup-code.txt")
check(not os.path.exists(code_file), "codice di installazione eliminato dopo l'uso")

# ---------------------------------------------------------------- utenti di prova
for user, role in (("tecnico1", "tecnico"), ("lettore1", "lettura")):
    form_post(admin, "/utenti", "/utenti", {"username": user, "full_name": user, "password": "passwordtecnico1", "role": role})


def login(user, password="passwordtecnico1", headers=None):
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
                        "(SELECT COUNT(*) FROM commands) + (SELECT COUNT(*) FROM settings)").fetchone()[0]
    r = form_post(sess, path, page, data)
    after = db.execute("SELECT (SELECT COUNT(*) FROM clients) + (SELECT COUNT(*) FROM users) + "
                       "(SELECT COUNT(*) FROM commands) + (SELECT COUNT(*) FROM settings)").fetchone()[0]
    return after == before and ("permessi" in r.text or r.status_code in (302, 303, 403))

check(refused(let, "/clienti/nuovo", "/", {"name": "da lettore"}), "ruolo lettura: non può creare clienti")
check(refused(let, f"/impianti/{pa}/comandi", "/", {"name": "ping"}), "ruolo lettura: non può inviare comandi")
check(refused(let, "/utenti", "/", {"username": "x_lettore", "password": "passwordlunga1", "role": "admin"}), "ruolo lettura: non può creare utenti")
check(refused(tec, "/utenti", "/", {"username": "x_tecnico", "password": "passwordlunga1", "role": "admin"}), "ruolo tecnico: non può creare amministratori")
check(refused(tec, "/impostazioni", "/", {"company_name": "Cambiata", "brand_color": "#000000", "public_url": "https://evil.example"}),
      "ruolo tecnico: non può cambiare le impostazioni")
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

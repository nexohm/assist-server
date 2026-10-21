"""Test di sicurezza dell'add-on agente. Avviare con tests/run_security.sh.

Simula un server dell'installatore ostile (o compromesso) e controlla che l'agente non metta
a rischio l'Home Assistant del cliente: il pannello gira nell'origine di HA, quindi un XSS
lì dentro vorrebbe dire accesso completo alla casa.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT_DIR = os.path.abspath(os.environ["AGENT_DIR"])
TMP = tempfile.mkdtemp()
os.environ.update(DATA_DIR=TMP, PANEL_ALLOW_IPS="127.0.0.1", HA_URL="http://127.0.0.1:1/core/api",
                  SUPERVISOR_URL="http://127.0.0.1:1", SUPERVISOR_TOKEN="x")
sys.path.insert(0, AGENT_DIR)

from agent import link as link_mod  # noqa: E402
from agent.main import Agent  # noqa: E402
from agent.panel import Panel  # noqa: E402

results = []


def check(ok, name, detail=""):
    results.append((ok, name, detail))
    print(("OK          " if ok else "VULNERABILE ") + name + (f"  [{detail}]" if detail and not ok else ""))


EVIL = '"><img src=x onerror=alert(1)>'
stolen = []
mode = {"tickets": "evil"}


class Evil(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/steal"):
            stolen.append(self.headers.get("Authorization"))
            return self._json({"commands": []})
        if self.path.startswith("/api/agent/commands"):
            self.send_response(302); self.send_header("Location", f"http://localhost:{PORT}/steal"); self.end_headers(); return
        if self.path.startswith("/api/agent/tickets"):
            if mode["tickets"] == "broken":
                return self._json({"tickets": [{"id": 1, "title": "x"}, "stringa", {"messages": [None]}]})
            return self._json({"tickets": [{
                "id": EVIL, "title": EVIL, "status": EVIL, "created_at": EVIL, "updated_at": EVIL,
                "messages": [{"author_type": EVIL, "author_name": EVIL, "text": EVIL, "created_at": EVIL}]}]})
        self._json({"error": "?"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/api/agent/pair":
            return self._json({"plant_id": 1, "token": "segreto-del-cliente", "server_name": EVIL,
                               "brand_color": "red}</style><script>alert(1)</script>", "protocol": 1})
        if self.path == "/api/agent/report":
            return self._json({"ok": True, "pending_commands": 0, "server_name": EVIL, "brand_color": "#fff;x"})
        self._json({"ok": True})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Evil)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
SERVER = f"http://127.0.0.1:{PORT}"

json.dump({"server_url": SERVER, "pair_code": "ABCD-EFGH"}, open(os.path.join(TMP, "options.json"), "w"))
agent = Agent()
agent.link.pair(SERVER, "ABCD-EFGH", "2026.10.1", "Casa")
try:
    agent.link.report("2026.10.1", None)
except Exception:  # noqa: BLE001
    pass
agent.state.update(last_diag={"status": EVIL, "collected_at": EVIL, "issues": [{"severity": "critico", "message": EVIL}]})
panel = Panel(agent, 0)

# ---------------------------------------------------------------- XSS nel pannello
html = panel.page()
raw = [frag for frag in ("<img src=x", "<script>alert") if frag in html]
check(not raw, "pannello: dati di un server ostile non possono eseguire codice nell'Home Assistant del cliente",
      "trovato nel pannello: " + ", ".join(raw))

mode["tickets"] = "broken"
try:
    html = panel.page()
    crashed = "Le tue richieste" not in html
except Exception as e:  # noqa: BLE001
    crashed = f"{type(e).__name__}: {e}"
check(not crashed, "pannello: risposte malformate del server non lo mandano in errore", str(crashed))
mode["tickets"] = "evil"

# ---------------------------------------------------------------- reindirizzamenti
try:
    agent.link.poll_commands(wait=0)
except Exception:  # noqa: BLE001
    pass
check(not stolen, "agente: il token non viene inviato a un altro indirizzo seguendo un reindirizzamento",
      f"ricevuto da /steal: {stolen[:1]}")

# ---------------------------------------------------------------- server in chiaro
def accepts_http(url):
    """True se l'agente prova davvero a collegarsi a un server http pubblico (token in chiaro su internet)."""
    try:
        agent.link.pair(url, "ABCD-EFGH", None, None)
    except link_mod.ServerError as e:
        return "https" not in str(e).lower()
    return True
check(not accepts_http("http://assistenza.example.invalid"), "agente: rifiuta un server http:// pubblico (token e dati in chiaro)")
agent.link.pair(SERVER, "ABCD-EFGH", None, None)  # in rete locale l'http resta permesso
check(agent.state.get("token") == "segreto-del-cliente", "(controllo) in rete locale l'http è ancora ammesso")

# ---------------------------------------------------------------- CSRF sul pannello
httpd = panel.start()
pport = httpd.server_address[1]
agent.state.set_consent(False)
req = urllib.request.Request(f"http://127.0.0.1:{pport}/consenso", data=urllib.parse.urlencode({"enabled": "1", "durata": "sempre"}).encode(),
                             method="POST", headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://evil.example"})
try:
    urllib.request.urlopen(req, timeout=5)
except Exception:  # noqa: BLE001
    pass
check(not agent.state.get("consent"), "pannello: un altro sito non può riattivare l'accesso remoto al posto del cliente (CSRF)")

req = urllib.request.Request(f"http://127.0.0.1:{pport}/consenso", data=b"enabled=1", method="POST",
                             headers={"Content-Length": "-1"})
t0 = time.time()
try:
    urllib.request.urlopen(req, timeout=5)
    hung = False
except Exception:  # noqa: BLE001
    hung = time.time() - t0 >= 4.5
check(not hung, "pannello: Content-Length negativo non blocca il pannello")

html_page = urllib.request.urlopen(f"http://127.0.0.1:{pport}/", timeout=5)
h = html_page.headers
check("x-content-type-options" in {k.lower() for k in h.keys()}, "pannello: intestazioni di sicurezza presenti")

# ---------------------------------------------------------------- 0.1.7: nuove funzioni del pannello
# CSRF anche sui moduli nuovi: un altro sito non può nascondere entità all'installatore
before = list(agent.state.get("excluded") or [])
req = urllib.request.Request(f"http://127.0.0.1:{pport}/escludi", data=b"entity=sensor.allarme",
                             method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
try:
    urllib.request.urlopen(req, timeout=5)
except Exception:  # noqa: BLE001
    pass
check(agent.state.get("excluded") == before, "pannello: un altro sito non può escludere entità dai controlli (CSRF)")

# Testo deciso dal server che finisce nelle notifiche di Home Assistant (Markdown): niente link né immagini
from agent.main import Agent as _Agent  # noqa: E402
dirty = _Agent._plain("Aggiorna [qui](https://evil.example) ![t](https://evil.example/pixel.png) <img src=x onerror=alert(1)>")
check("](" not in dirty.replace("\\]\\(", "") and "<img" not in dirty and "\\[qui\\]" in dirty,
      "notifiche: link e immagini inviati dal server non diventano cliccabili in Home Assistant", dirty)

# Nome di entità da escludere: solo dominio.nome (niente caratteri che finirebbero in HTML o nei filtri)
import re as _re  # noqa: E402
from agent.panel import ENTITY_PATTERN  # noqa: E402
weird = ["javascript:alert(1)", "sensor.x\"><b>", "*", "a" * 70 + ".b", "../etc.passwd", "sensor.x y"]
check(not any(ENTITY_PATTERN.fullmatch(w) for w in weird) and ENTITY_PATTERN.fullmatch("vacuum.robot_*"),
      "pannello: i nomi di entità da escludere sono validati")

# ---------------------------------------------------------------- 0.1.8: schede ed etichette
before = list(agent.state.get("only_labels") or [])
req = urllib.request.Request(f"http://127.0.0.1:{pport}/etichette", data=b"label=importante",
                             method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
try:
    urllib.request.urlopen(req, timeout=5)
except Exception:  # noqa: BLE001
    pass
check(agent.state.get("only_labels") == before, "pannello: un altro sito non può cambiare il filtro per etichette (CSRF)")

page = urllib.request.urlopen(f"http://127.0.0.1:{pport}/?s=%3Cscript%3Ealert(1)%3C/script%3E", timeout=5).read().decode()
check("<script" not in page, "pannello: il parametro della scheda non finisce nella pagina")

# Filtro per etichette attivo ma registro di HA illeggibile: niente controllo piuttosto che
# mandare all'installatore entità che il cliente ha escluso.
from agent import ha as ha_mod  # noqa: E402
agent.state.update(only_labels=["importante"], last_diag=None)
orig = ha_mod.entity_labels
def _broken():
    raise ha_mod.HAError("registro non disponibile")
ha_mod.entity_labels = _broken
try:
    agent.fresh_diagnostics()
    leaked = True
except ha_mod.HAError:
    leaked = False
except Exception:  # noqa: BLE001
    leaked = agent.state.get("last_diag") is not None
finally:
    ha_mod.entity_labels = orig
    agent.state.update(only_labels=[])
check(not leaked and agent.state.get("last_diag") is None,
      "etichette: se il filtro non si può applicare la diagnostica non parte (nessuna entità esclusa inviata)")

# ---------------------------------------------------------------- accesso amministratore temporaneo
import time as _time  # noqa: E402
from agent import admin as admin_mod  # noqa: E402

fake_users, ws_log, fail = {}, [], {"delete": False}
def fake_ws(command, timeout=10, **params):
    ws_log.append((command, params))
    if command == "config/auth/create":
        uid = f"u{len(ws_log)}"
        fake_users[uid] = {"group_ids": params.get("group_ids"), "username": None}
        return {"user": {"id": uid}}
    if command == "config/auth_provider/homeassistant/create":
        fake_users[params["user_id"]]["username"] = params["username"]
        return {}
    if command == "config/auth/delete":
        if fail["delete"]:
            raise ha_mod.HAError("Home Assistant non risponde")
        fake_users.pop(params["user_id"], None)
        return {}
    raise ha_mod.HAError("comando sconosciuto")
orig_ws = ha_mod.ws_command
ha_mod.ws_command = fake_ws
notified = []
agent.notify_client = lambda title, message, tag: notified.append(title) or 1
adm = agent.admin
try:
    ok, _, err = agent.execute({"name": "admin_password"})
    check(not ok and not fake_users, "amministratore: il server da solo non può far creare un utente (serve il consenso del cliente)", err)

    adm.grant("1h")
    creds = adm.reveal()
    state_text = open(os.path.join(TMP, "state.json")).read()
    check(len(fake_users) == 1 and list(fake_users.values())[0]["group_ids"] == ["system-admin"]
          and creds["password"] not in state_text and len(creds["password"]) >= 20,
          "amministratore: utente creato con password lunga, mai salvata dall'agente")
    first = adm.get()["user_id"]
    adm.reveal()
    check(first not in fake_users and len(fake_users) == 1,
          "amministratore: ogni nuova password ricrea l'utente (password e sessioni vecchie non valgono più)")

    # scadenza controllata dall'agente, anche se il server non risponde
    srv.shutdown()
    a = adm.get(); a["until"] = _time.time() - 1; agent.state.update(admin=a)
    adm.tick()
    check(not fake_users and not adm.active(), "amministratore: alla scadenza l'utente viene cancellato anche senza server")

    # HA non risponde alla chiusura: il permesso si toglie subito, la cancellazione viene ritentata
    adm.grant("4h"); adm.reveal(); fail["delete"] = True
    adm.close("cliente")
    stuck = bool(fake_users) and not adm.active()
    fail["delete"] = False; adm.tick()
    check(stuck and not fake_users, "amministratore: se HA non risponde, l'utente viene cancellato al tentativo successivo")

    # senza scadenza: utente cancellato dopo 30 giorni senza uso, il permesso resta; promemoria ogni 30 giorni
    adm.grant("sempre"); adm.reveal()
    a = adm.get(); a["last_reveal"] = _time.time() - 31 * 86400; a["last_reminder"] = _time.time() - 31 * 86400
    agent.state.update(admin=a); notified.clear()
    adm.tick()
    check(not fake_users and adm.active() and any("ancora attivo" in n for n in notified),
          "senza scadenza: utente cancellato dopo 30 giorni senza uso e promemoria al cliente, il permesso resta")

    # impianto scollegato dall'installatore: l'accesso si chiude
    agent.state.update(connection="revocato", token=None)
    adm.reveal() if adm.active() else None
    adm.tick()
    check(not adm.active() and not fake_users, "amministratore: se l'impianto viene scollegato l'accesso si chiude")
    check(admin_mod.DURATIONS.get("sempre", 0) is None and "status" in dir(adm) and "password" not in json.dumps(adm.status()),
          "amministratore: lo stato inviato al server non contiene mai la password")

    # nome del tecnico deciso dal server: niente HTML nel pannello, niente link o righe in più nelle notifiche
    agent.state.update(connection="collegato", token="t")
    evil_by = EVIL + " [clicca](https://evil.example)\n\nRiga finta"
    messages = []
    agent.notify_client = lambda title, message, tag: messages.append(message) or 1
    adm.on_server_request("2099-01-01T00:00:00+00:00", evil_by)
    stored = adm.get()["request_by"] or ""
    page_html = panel._admin_html()
    check("\n" not in stored and len(stored) <= 60 and "<img" not in page_html
          and messages and "](" not in messages[-1].replace("\\]\\(", "") and "<img" not in messages[-1],
          "amministratore: il nome del tecnico inviato dal server non diventa HTML né link", messages[-1] if messages else stored)
finally:
    ha_mod.ws_command = orig_ws

bad = [r for r in results if not r[0]]
print(f"\n{len(results) - len(bad)} difesi, {len(bad)} vulnerabili su {len(results)} controlli")
sys.exit(1 if bad else 0)

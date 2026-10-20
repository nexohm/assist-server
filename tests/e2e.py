"""Prova completa: server + agente + finto Home Assistant. Avviare con assist-server/tests/run_e2e.sh"""
import json, os, re, sqlite3, subprocess, sys, time
import requests

S = sys.argv[1]
AGENT_DIR = os.path.abspath(os.environ["AGENT_DIR"])
BASE = "http://127.0.0.1:8080"
PANEL = "http://127.0.0.1:8099/"
ok_count = 0

def check(cond, msg):
    global ok_count
    if not cond:
        print("FAIL:", msg); sys.exit(1)
    ok_count += 1; print("ok  -", msg)

def csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

def panel_post(action, data):
    """Modulo del pannello: ogni invio porta il token anti-CSRF letto dalla pagina."""
    return requests.post(PANEL + action, data={"csrf": csrf(requests.get(PANEL).text), **data})

def wait_for(fn, timeout=30, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        r = fn()
        if r: return r
        time.sleep(step)
    return None

web = requests.Session()
# 1. setup guidato
r = web.get(BASE + "/setup")
r0 = web.post(BASE + "/setup", data={"csrf": csrf(r.text), "company_name": "Pirata", "username": "pirata",
                                      "password": "passwordpirata1", "password2": "passwordpirata1"})
check("Codice di installazione errato" in r0.text, "setup senza codice di installazione rifiutato")
setup_code = open(f"{S}/srvdata/setup-code.txt").read().strip()
r = web.post(BASE + "/setup", data={"csrf": csrf(r.text), "setup_code": setup_code, "company_name": "Rossi Impianti", "brand_color": "#1d4ed8",
                                     "full_name": "Mario Rossi", "username": "mario", "password": "passwordsicura1", "password2": "passwordsicura1"})
check("Panoramica" in r.text and "Rossi Impianti" in r.text, "setup completato e login automatico")
check(web.get(BASE + "/setup").url.endswith("/login") or "Panoramica" in web.get(BASE + "/setup").text, "setup non ripetibile")

# CSRF
r2 = web.post(BASE + "/clienti/nuovo", data={"name": "X", "csrf": "sbagliato"})
check("Sessione scaduta" in r2.text, "POST senza CSRF valido rifiutato")

# 1b. notifiche Telegram (finto Telegram su :8124)
TG_LOG = os.path.join(S, "telegram.jsonl")
def tg_messages():
    try:
        return [json.loads(l)["text"] for l in open(TG_LOG)]
    except FileNotFoundError:
        return []
st = web.get(BASE + "/impostazioni").text
r = web.post(BASE + "/impostazioni/telegram", data={"csrf": csrf(st), "telegram_token": "123:TOKEN", "azione": "rileva"})
check("Chat trovata: Roby" in r.text and 'value="4242"' in r.text, "Telegram: token salvato e chat ID rilevato")
r = web.post(BASE + "/impostazioni/telegram", data={"csrf": csrf(r.text), "azione": "prova", "notify_tickets": "1",
                                                    "notify_offline": "1", "notify_critical": "1"})
check("Messaggio di prova inviato" in r.text and "Rossi Impianti" in tg_messages()[-1], "Telegram: messaggio di prova")
r = web.post(BASE + "/impostazioni/telegram", data={"csrf": csrf(r.text), "telegram_token": "999:SBAGLIATO", "azione": "prova",
                                                    "notify_tickets": "1", "notify_offline": "1", "notify_critical": "1"})
check("Invio non riuscito: Unauthorized" in r.text, "Telegram: token sbagliato -> errore chiaro")
web.post(BASE + "/impostazioni/telegram", data={"csrf": csrf(r.text), "telegram_token": "123:TOKEN", "azione": "salva",
                                                "notify_tickets": "1", "notify_offline": "1", "notify_critical": "1"})
check("impostato" in web.get(BASE + "/impostazioni").text, "Telegram: il token non viene mostrato nella pagina")

# 2. cliente + impianto
r = web.get(BASE + "/clienti/nuovo")
r = web.post(BASE + "/clienti/nuovo", data={"csrf": csrf(r.text), "name": "Famiglia Bianchi", "phone": "333 1112233",
                                            "email": "bianchi@example.it", "address": "Via Roma 1, Torino", "notes": "Cancello lento"})
check("Famiglia Bianchi" in r.text, "cliente creato")
cid = int(re.search(r"/clienti/(\d+)", r.url).group(1))
r = web.post(BASE + f"/clienti/{cid}/impianti", data={"csrf": csrf(r.text), "name": "Casa"})
code = re.search(r'class="code">([A-Z0-9]{4}-[A-Z0-9]{4})<', r.text).group(1)
pid = int(re.search(r"/impianti/(\d+)", r.url).group(1))
check(bool(code), f"impianto creato con codice {code}")

# 3. avvio agente
os.makedirs(f"{S}/agentdata", exist_ok=True)
json.dump({"server_url": BASE, "pair_code": code.replace("-", "").lower(), "heartbeat_seconds": 15, "diagnostics_minutes": 1},
          open(f"{S}/agentdata/options.json", "w"))
env = dict(os.environ, DATA_DIR=f"{S}/agentdata", HA_URL="http://127.0.0.1:8123/core/api", SUPERVISOR_URL="http://127.0.0.1:8123",
           SUPERVISOR_TOKEN="test-token", PANEL_ALLOW_IPS="127.0.0.1", PANEL_PORT="8099")
agent = subprocess.Popen([sys.executable, "-u", "-m", "agent.main"], cwd=AGENT_DIR,
                         env=env, stdout=open(f"{S}/agent.log", "w"), stderr=subprocess.STDOUT)
try:
    st = wait_for(lambda: (lambda j: j if j["online"] and j["last_diag_at"] else None)(web.get(BASE + f"/impianti/{pid}/stato.json").json()), 60)
    check(st is not None, f"agente abbinato, online, diagnostica ricevuta (stato: {st and st['status']})")
    db = sqlite3.connect(f"{S}/srvdata/suite.db"); db.row_factory = sqlite3.Row
    p = db.execute("SELECT * FROM plants WHERE id=?", (pid,)).fetchone()
    check(p["pair_code"] is None and p["token_hash"] and len(p["token_hash"]) == 64, "codice consumato, salvato solo hash del token")
    d = json.loads(p["last_diag"])
    check(d["summary"]["unavailable"] == 2, "diagnostica: 2 entità non disponibili (ignorati button e orfane)")
    check(d["summary"]["low_battery"] == 1 and d["summary"]["updates"] == 1 and d["summary"]["automations_off"] == 1, "diagnostica: batterie, aggiornamenti, automazioni")
    codes = {i["code"]: i["severity"] for i in d["issues"]}
    check(codes.get("disk_high") == "attenzione" and codes.get("backup_old") == "attenzione" and "orphan_entities" in codes, "diagnostica: disco 85%, backup 12 giorni, orfane")
    check(d["status"] == "attenzione" and d["system"]["os_version"] == "16.2", "stato complessivo 'attenzione'")
    check(p["ha_version"] == "2026.10.1" and p["agent_version"].startswith("0.1."), "versioni HA e agente registrate")

    # 4. pagine della dashboard
    for path in ["/", "/clienti", f"/clienti/{cid}", f"/impianti/{pid}", "/ticket", "/utenti", "/impostazioni", f"/clienti/{cid}/modifica"]:
        rr = web.get(BASE + path)
        check(rr.status_code == 200 and "Traceback" not in rr.text, f"pagina {path} ok")
    page = web.get(BASE + f"/impianti/{pid}").text
    check("cover.soggiorno" in page and "Disco occupato" in page, "pagina impianto mostra i problemi")

    # 5. comando remoto via long-polling
    t0 = time.time()
    web.post(BASE + f"/impianti/{pid}/comandi", data={"csrf": csrf(page), "name": "run_diagnostics"})
    done = wait_for(lambda: db.execute("SELECT status FROM commands WHERE plant_id=? ORDER BY id DESC", (pid,)).fetchone()["status"] == "completato", 15, 0.1)
    check(done, f"run_diagnostics eseguito in {time.time() - t0:.1f}s (long-polling)")
    web.post(BASE + f"/impianti/{pid}/comandi", data={"csrf": csrf(page), "name": "ping"})
    check(wait_for(lambda: db.execute("SELECT status FROM commands WHERE plant_id=? ORDER BY id DESC", (pid,)).fetchone()["status"] == "completato", 15, 0.1), "ping eseguito")
    r = web.post(BASE + f"/impianti/{pid}/comandi", data={"csrf": csrf(page), "name": "rm_rf"})
    check("Comando non valido" in r.text, "comando fuori lista rifiutato dalla dashboard")
    # comando fuori lista iniettato direttamente nel DB: l'agente deve rifiutarlo
    db.execute("INSERT INTO commands(plant_id,name,args,created_at) VALUES(?,?,?,?)", (pid, "shell", "{}", "2026-01-01T00:00:00+00:00")); db.commit()
    check(wait_for(lambda: db.execute("SELECT status FROM commands WHERE name='shell'").fetchone()["status"] == "errore", 40, 0.2), "comando sconosciuto rifiutato dall'agente")

    # 6. pannello cliente + ticket
    panel = requests.get(PANEL)
    check(panel.status_code == 200 and "Rossi Impianti" in panel.text and "Collegato" in panel.text, "pannello cliente con marchio installatore")
    r = panel_post("ticket", {"title": "Tapparella soggiorno ferma", "description": "Da ieri non sale più", "contact": "333 9998877"})
    check("Richiesta inviata" in r.text, "cliente apre un ticket dal pannello")
    t = db.execute("SELECT * FROM tickets WHERE plant_id=?", (pid,)).fetchone()
    check(t and t["diagnostics"] and t["contact"] == "333 9998877", "ticket arrivato con diagnostica allegata")
    tp = web.get(BASE + f"/ticket/{t['id']}").text
    check("Da ieri non sale" in tp and "cover.soggiorno" in tp, "ticket visibile in dashboard con diagnostica")
    web.post(BASE + f"/ticket/{t['id']}/rispondi", data={"csrf": csrf(tp), "text": "Passo domani mattina alle 9", "status": "in_lavorazione", "assigned_to": "1"})
    panel = requests.get(PANEL).text
    check("Passo domani mattina" in panel and "In lavorazione" in panel, "il cliente vede risposta e stato nel pannello")
    panel_post("rispondi", {"tid": t["id"], "text": "Perfetto grazie"})
    check("Perfetto grazie" in web.get(BASE + f"/ticket/{t['id']}").text, "risposta del cliente arriva in dashboard")
    time.sleep(1)
    msgs = tg_messages()
    check(any("Nuovo ticket" in m and "Tapparella soggiorno ferma" in m and "Famiglia Bianchi" in m for m in msgs),
          "Telegram: notifica nuovo ticket")
    check(any("Risposta del cliente" in m and "Perfetto grazie" in m for m in msgs), "Telegram: notifica risposta del cliente")
    # offline: simulo l'ultimo contatto 10 minuti fa (il controllo gira ogni secondo nei test)
    n0 = len(tg_messages())
    db.execute("UPDATE plants SET last_seen=? WHERE id=?", ("2020-01-01T00:00:00+00:00", pid)); db.commit()
    check(wait_for(lambda: any("Impianto offline" in m for m in tg_messages()[n0:]), 10, 0.3), "Telegram: impianto offline")
    check(wait_for(lambda: any("Di nuovo online" in m for m in tg_messages()[n0:]), 40, 0.5), "Telegram: impianto di nuovo online")
    n1 = len(tg_messages())
    crit = json.dumps({"status": "critico", "summary": {}, "system": {}, "issues": [{"severity": "critico", "code": "disk_full", "message": "Disco quasi pieno (95%)"}]})
    db.execute("UPDATE plants SET last_diag=? WHERE id=?", (crit, pid)); db.commit()
    check(wait_for(lambda: any("Stato critico" in m and "Disco quasi pieno" in m for m in tg_messages()[n1:]), 10, 0.3),
          "Telegram: impianto critico, con il problema")
    time.sleep(2.5)
    check(sum("Stato critico" in m for m in tg_messages()[n1:]) == 1, "Telegram: nessun avviso ripetuto")
    # link a Home Assistant comunicato dall'agente (finto HA: external_url)
    page = web.get(BASE + f"/impianti/{pid}").text
    check("https://bianchi.duckdns.org" in page and "Apri Home Assistant" in page, "link a Home Assistant dall'agente")
    web.post(BASE + f"/impianti/{pid}/ha-url", data={"csrf": csrf(page), "ha_url_manual": "https://ha.bianchi.it/"})
    page = web.get(BASE + f"/impianti/{pid}").text
    check('href="https://ha.bianchi.it"' in page and "inserito a mano" in page, "indirizzo Home Assistant inserito a mano")
    r = web.post(BASE + f"/impianti/{pid}/ha-url", data={"csrf": csrf(page), "ha_url_manual": "ftp://x"})
    check("deve iniziare" in r.text, "indirizzo Home Assistant non valido rifiutato")

    # 7. consenso
    panel_post("consenso", {"enabled": "0"})
    check(wait_for(lambda: db.execute("SELECT consent FROM plants WHERE id=?", (pid,)).fetchone()["consent"] == 0, 10, 0.2), "il cliente sospende l'accesso: il server lo sa")
    page = web.get(BASE + f"/impianti/{pid}").text
    check("sospeso dal cliente" in page, "dashboard mostra accesso sospeso")
    r = web.post(BASE + f"/impianti/{pid}/comandi", data={"csrf": csrf(page), "name": "run_diagnostics"})
    check("comando non inviato" in r.text, "diagnostica bloccata con consenso sospeso")
    panel_post("consenso", {"enabled": "1", "durata": "4h"})
    check(wait_for(lambda: db.execute("SELECT consent_until FROM plants WHERE id=?", (pid,)).fetchone()["consent_until"], 10, 0.2),
          "accesso a tempo (4 ore): il server riceve la scadenza")
    check("Accesso remoto fino al" in web.get(BASE + f"/impianti/{pid}").text and "fino al" in requests.get(PANEL).text,
          "scadenza visibile in dashboard e nel pannello del cliente")
    panel_post("consenso", {"enabled": "0"})
    panel_post("consenso", {"enabled": "1", "durata": "sempre"})
    check(wait_for(lambda: (lambda r: r["consent"] == 1 and r["consent_until"] is None)(
          db.execute("SELECT consent, consent_until FROM plants WHERE id=?", (pid,)).fetchone()), 10, 0.2),
          "accesso senza scadenza")

    # 8. ruoli
    up = web.get(BASE + "/utenti").text
    web.post(BASE + "/utenti", data={"csrf": csrf(up), "username": "luca", "full_name": "Luca", "password": "passwordluca1", "role": "lettura"})
    ro = requests.Session(); lp = ro.get(BASE + "/login")
    ro.post(BASE + "/login", data={"csrf": csrf(lp.text), "username": "luca", "password": "passwordluca1"})
    check("Panoramica" in ro.get(BASE + "/").text, "utente sola lettura accede")
    pg = ro.get(BASE + f"/impianti/{pid}").text
    r = ro.post(BASE + f"/impianti/{pid}/comandi", data={"csrf": csrf(pg), "name": "ping"})
    check("Non hai i permessi" in r.text and '/comandi"' not in pg, "sola lettura non può inviare comandi")
    check("Non hai i permessi" in ro.get(BASE + "/utenti").text, "sola lettura non vede gestione utenti")
    ap = ro.get(BASE + "/account").text
    r = ro.post(BASE + "/account", data={"csrf": csrf(ap), "azione": "password", "current": "sbagliata1234",
                                         "password": "nuovapassword1", "password2": "nuovapassword1"})
    check("password attuale non è corretta" in r.text, "cambio password: password attuale verificata")
    r = ro.post(BASE + "/account", data={"csrf": csrf(ap), "azione": "password", "current": "passwordluca1",
                                         "password": "nuovapassword1", "password2": "nuovapassword1"})
    check("Password cambiata" in r.text, "cambio password dal proprio account (anche sola lettura)")
    ro2 = requests.Session(); lp = ro2.get(BASE + "/login")
    check("Panoramica" in ro2.post(BASE + "/login", data={"csrf": csrf(lp.text), "username": "luca", "password": "nuovapassword1"}).text,
          "accesso con la nuova password")
    up = web.get(BASE + "/utenti").text
    luca_id = db.execute("SELECT id FROM users WHERE username='luca'").fetchone()["id"]
    r = web.post(BASE + f"/utenti/{luca_id}/password", data={"csrf": csrf(up), "password": "corta"})
    check("almeno 10" in r.text, "reimpostazione: password troppo corta rifiutata")
    r = web.post(BASE + f"/utenti/{luca_id}/password", data={"csrf": csrf(up), "password": "resetdaladmin1"})
    check("reimpostata" in r.text, "l'amministratore reimposta la password di un utente")
    ro3 = requests.Session(); lp = ro3.get(BASE + "/login")
    check("Panoramica" in ro3.post(BASE + "/login", data={"csrf": csrf(lp.text), "username": "luca", "password": "resetdaladmin1"}).text,
          "accesso con la password reimpostata")
    r = ro3.post(BASE + f"/utenti/{luca_id}/password", data={"csrf": csrf(ro3.get(BASE + "/account").text), "password": "hackerata12345"})
    check("Non hai i permessi" in r.text, "un utente normale non può reimpostare password altrui")

    # 9. login errato e blocco
    bad = requests.Session()
    for _ in range(5):
        lp = bad.get(BASE + "/login"); bad.post(BASE + "/login", data={"csrf": csrf(lp.text), "username": "mario", "password": "x"})
    lp = bad.get(BASE + "/login")
    check("Troppi tentativi" in bad.post(BASE + "/login", data={"csrf": csrf(lp.text), "username": "mario", "password": "passwordsicura1"}).text, "blocco dopo 5 tentativi falliti")

    # 10. API agente: protocollo e token
    check(requests.post(BASE + "/api/agent/report", json={}, headers={"X-Agent-Protocol": "1", "Authorization": "Bearer falso"}).status_code == 401, "token falso -> 401")
    check(requests.post(BASE + "/api/agent/report", json={}, headers={"X-Agent-Protocol": "9"}).status_code == 426, "protocollo sbagliato -> 426")
    check(requests.post(BASE + "/api/agent/pair", json={"code": code}).status_code == 404, "codice già usato non riutilizzabile")

    # 11. revoca
    page = web.get(BASE + f"/impianti/{pid}").text
    web.post(BASE + f"/impianti/{pid}/revoca", data={"csrf": csrf(page)})
    check(wait_for(lambda: json.load(open(f"{S}/agentdata/state.json"))["connection"] == "revocato", 40, 0.5), "revoca rilevata dall'agente")
    check("Collegamento revocato" in requests.get(PANEL).text and "Chiedi assistenza" not in requests.get(PANEL).text, "il pannello mostra la revoca al cliente")
    st = json.load(open(f"{S}/agentdata/state.json"))
    check(st["token"] is None and st["connection"] == "revocato", "token cancellato dall'agente")
finally:
    agent.terminate()
print(f"\nTUTTI I {ok_count} CONTROLLI SUPERATI")

# Nexohm Assist — assistenza remota per installatori Home Assistant (MVP 0.2)

Assistenza remota per installatori domotici: ogni installatore ha la **sua dashboard sul suo server**,
ogni cliente ha un **add-on agente** sul proprio Home Assistant. Il cliente non apre porte e non serve VPN.

```
 SERVER INSTALLATORE (Docker)                         IMPIANTI CLIENTI
 ┌─────────────────────────────┐
 │ dashboard: clienti, stato,  │◄── HTTPS (l'agente chiama) ──  HA + add-on Agente Assistenza
 │ diagnostica, ticket, utenti │◄── HTTPS (l'agente chiama) ──  HA + add-on Agente Assistenza
 └─────────────────────────────┘
```

## Contenuto

| Cartella | Cosa | Diventerà il repo GitHub |
|---|---|---|
| `assist-server/` | Dashboard dell'installatore (Python, Starlette, SQLite, Docker) | `nexohm/assist-server` |
| `ha-addons/` | Repository add-on per Home Assistant con l'**Agente Assistenza** | `nexohm/ha-addons` |
| `docs/PROTOCOL.md` | Il contratto tra agente e server (leggerlo per primo) | dentro `nexohm/assist-server` |

`ha-addons/` deve essere **un repository a sé**: Home Assistant legge `repository.yaml` dalla radice del repo.

## Cosa fa già (versione 0.2)

**Server installatore**
- configurazione guidata al primo avvio (nome azienda, colore, primo amministratore), protetta da un
  **codice di installazione** che si legge solo sul server (`sudo ha-suite codice`)
- utenti con ruoli: amministratore, tecnico, sola lettura
- clienti e impianti, codice di abbinamento monouso valido 24 ore
- panoramica con impianti ordinati per gravità (offline → critici → da controllare)
- pagina impianto con diagnostica, comandi remoti e storico, aggiornamento automatico
- ticket con risposte, stato e assegnazione al tecnico, diagnostica allegata all'apertura
- revoca dell'accesso di un impianto e nuovo abbinamento
- sicurezza: password PBKDF2, token salvati solo come hash, protezione CSRF, blocco dei tentativi di accesso
  per IP e per utente, Content-Security-Policy e intestazioni anti-clickjacking, dati degli agenti
  sempre validati (vedi [docs/SECURITY.md](docs/SECURITY.md) e `tests/run_security.sh`)
- **distribuito come immagine Docker già pronta** (amd64, arm64, armv7), installazione con un comando
- copia di sicurezza automatica del database prima di ogni aggiornamento che ne cambia la struttura
- **notifiche Telegram** all'installatore: nuovi ticket, risposte dei clienti, impianti offline/di nuovo online, stato critico
- **"Il mio account"**: ognuno cambia la propria password; l'amministratore può reimpostarla agli altri
- **link all'Home Assistant del cliente**: comunicato dall'agente (indirizzo esterno o interno) o inserito a mano
- **avvisi di aggiornamento**: banner per gli amministratori quando esce una nuova versione del server, ed etichetta
  "agente da aggiornare" sugli impianti con un agente vecchio (controllo su GitHub ogni 6 ore, `UPDATE_CHECK=0` per disattivarlo)
- **conferme animate**: ogni salvataggio mostra un avviso con spunta (o una X rossa se qualcosa non va) e i
  pulsanti mostrano un indicatore mentre lavorano

**Agente (add-on)**
- nessuna dipendenza esterna: solo Python standard
- diagnostica: entità non disponibili, entità orfane, batterie scariche, aggiornamenti,
  automazioni spente, disco, età dell'ultimo backup
- esegue solo i comandi della lista chiusa (`ping`, `run_diagnostics`), rifiuta tutto il resto
- pannello **Assistenza** nella barra laterale di HA, con il nome e il colore dell'installatore:
  il cliente apre richieste, legge le risposte, sospende o attiva l'accesso remoto **a tempo**
  (1 ora, 4 ore, 24 ore, 7 giorni o senza scadenza: allo scadere si sospende da solo)
- **notifiche al cliente** quando l'installatore risponde o cambia lo stato di una richiesta: in Home Assistant
  e, a scelta, sul telefono (app companion o altri servizi `notify`), con apertura diretta del pannello
- **pulizia delle richieste chiuse** nel pannello (dopo 7/30/90 giorni o tenendo le ultime N); lo storico resta sul server
- **entità escluse**: il cliente decide cosa l'installatore non deve controllare (anche con `*`, es. `light.giardino_*`)

## Installazione del server (per l'installatore)

Un solo comando su un qualsiasi Linux con accesso a internet (mini PC, VPS, Raspberry Pi 4/5):

```bash
curl -fsSL https://raw.githubusercontent.com/nexohm/assist-server/main/install.sh | sudo bash
```

Lo script installa Docker se manca, chiede la modalità e il dominio, scarica l'immagine pronta
e avvia tutto in `/opt/ha-suite`. Due modalità:

| Modalità | Quando | Cosa serve |
|---|---|---|
| **HTTPS automatico** | macchina dedicata | un dominio che punta all'IP pubblico, porte 80 e 443 aperte |
| **Proxy esistente** | c'è già Nginx Proxy Manager, Traefik... | puntare il proxy a `http://<ip>:8080`, timeout di lettura ≥ 60 s |

Con un proxy esistente lo script chiede anche la **porta**: la trova da solo (la prima libera da 8080 in su)
oppure la sceglie l'installatore, e controlla che sia libera.

Senza domande: `... | sudo DOMAIN=assistenza.tuaditta.it bash` oppure `... | sudo MODE=proxy SUITE_PORT=8086 bash`.

**Gestione quotidiana** con il comando `ha-suite`:

| Comando | Cosa fa |
|---|---|
| `ha-suite stato` | il server è in funzione? |
| `ha-suite log` | log in tempo reale |
| `ha-suite aggiorna` | aggiorna anche lo script stesso, backup, scarica l'ultima versione, riavvia |
| `ha-suite versione` | versione del server in esecuzione |
| `ha-suite backup` | salva tutti i dati in `/opt/ha-suite/backup` (tiene gli ultimi 14) |
| `ha-suite ripristina <file>` | ripristina un backup |

Tutti i dati stanno nel volume Docker `ha-suite_suite_data`: database, chiave delle sessioni e copie
automatiche. Per fissare una versione invece di seguire gli aggiornamenti: `SUITE_TAG=0.2.0` in `/opt/ha-suite/.env`.

## Installazione dell'agente (presso il cliente)

1. Impostazioni → Add-on → Store → ⋮ → Repository → aggiungere `https://github.com/nexohm/ha-addons`
2. Installare **Agente Assistenza**
3. In *Configurazione*: indirizzo del server + codice di abbinamento dalla dashboard
4. Avviare: in pochi secondi l'impianto diventa verde nella dashboard

## Pubblicare su GitHub (una volta sola)

1. Creare l'organizzazione GitHub `nexohm` (già impostata in tutti i file)
2. Creare i repository pubblici `assist-server` (contenuto di `assist-server/`) e `ha-addons` (contenuto di `ha-addons/`)
3. Al primo push partono i workflow in `.github/workflows/`:
   - **assist-server**: esegue la prova completa, poi pubblica `ghcr.io/<org>/assist-server`.
     Un tag `v0.2.0` pubblica la versione `0.2.0` e aggiorna `latest`; ogni push su `main` pubblica `edge`.
   - **ha-addons**: compila l'add-on per amd64, aarch64 e armv7.
4. Su GitHub → organizzazione → Packages, rendere **pubblici** i pacchetti (altrimenti chiedono login)
5. Nell'add-on togliere il `#` dalla riga `image:` di `config.yaml`: da lì HA scarica l'add-on già compilato
6. Aggiungere `icon.png` (128×128) e `logo.png` all'add-on e scegliere la licenza

## Sviluppo e prove

```bash
cd assist-server
pip install -r requirements.txt requests
tests/run_e2e.sh                       # prova completa: server + agente + finto HA, 48 controlli
tests/run_security.sh                  # test di sicurezza: 39 attacchi a server e add-on

DATA_DIR=./data SECURE_COOKIES=0 python3 -m uvicorn app.main:app --port 8080   # solo dashboard

docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build       # immagine dal codice locale
```

## Prossimi passi (ordine consigliato)

1. Notifiche all'installatore per impianti offline e nuovi ticket (email / Telegram)
2. Report PDF per il cliente con il marchio dell'installatore
3. Nuovi comandi: `create_backup`, `reload_integration`, `apply_blueprint` (libreria di configurazioni pronte)
4. Integrazione HACS con sensori in HA del cliente (ticket aperti, stato assistenza)
5. Licenze per la versione commerciale del server
6. Backup automatico pianificato (oggi: `ha-suite backup` a mano o da cron)

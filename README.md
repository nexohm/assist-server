# Nexohm Assist — server di assistenza per installatori Home Assistant

🇮🇹 Italiano · [🇬🇧 English](README.en.md)

**Nexohm Assist** permette a un installatore domotico di seguire a distanza gli impianti Home Assistant dei
suoi clienti: vede se sono accesi, riceve un controllo automatico dello stato, risponde alle richieste di
assistenza e, solo se il cliente lo concede, entra come amministratore per qualche ora.

È fatto di due parti:

| Parte | Dove gira | Repository |
|---|---|---|
| **Server di assistenza** (questa) | sul server dell'installatore, in Docker | `nexohm/assist-server` |
| **Agente Assistenza** (add-on) | sull'Home Assistant di ogni cliente | [`nexohm/ha-addons`](https://github.com/nexohm/ha-addons) |

```
  SERVER DELL'INSTALLATORE (Docker)                    IMPIANTI DEI CLIENTI
 ┌──────────────────────────────────┐
 │ dashboard: clienti, impianti,    │◄── HTTPS ── Home Assistant + Agente Assistenza
 │ diagnostica, ticket, utenti,     │◄── HTTPS ── Home Assistant + Agente Assistenza
 │ aziende, notifiche               │◄── HTTPS ── Home Assistant + Agente Assistenza
 └──────────────────────────────────┘
          è sempre l'agente a chiamare il server: dal cliente non si apre nessuna porta
```

---

## Indice

1. [Come funziona](#come-funziona)
2. [Cosa fa](#cosa-fa)
3. [Requisiti](#requisiti)
4. [Installazione del server](#installazione-del-server)
5. [Primo avvio](#primo-avvio)
6. [Collegare l'impianto di un cliente](#collegare-limpianto-di-un-cliente)
7. [Utenti e ruoli](#utenti-e-ruoli)
8. [Più aziende sullo stesso server](#più-aziende-sullo-stesso-server)
9. [Notifiche Telegram](#notifiche-telegram)
10. [Password dimenticata ed email](#password-dimenticata-ed-email)
11. [Accesso amministratore temporaneo](#accesso-amministratore-temporaneo)
12. [Gestione quotidiana: il comando `ha-suite`](#gestione-quotidiana-il-comando-ha-suite)
13. [Aggiornamenti, backup e ripristino](#aggiornamenti-backup-e-ripristino)
14. [Sicurezza e privacy](#sicurezza-e-privacy)
15. [Sviluppo, prove e rilasci](#sviluppo-prove-e-rilasci)

---

## Come funziona

1. **L'installatore** installa il server su una sua macchina (mini PC, VPS, Raspberry Pi) raggiungibile da
   internet con un indirizzo HTTPS, per esempio `https://assistenza.rossiimpianti.it`.
2. Nella dashboard crea il **cliente** e il suo **impianto**: il server genera un **codice di abbinamento**
   monouso, valido 24 ore.
3. **Sull'Home Assistant del cliente** si installa l'add-on **Agente Assistenza** e si inseriscono indirizzo del
   server e codice. L'agente si abbina e da quel momento usa un token personale (il codice non serve più).
4. L'agente **chiama il server** a intervalli regolari: comunica che l'impianto è acceso, invia il controllo
   automatico (diagnostica) e ritira i comandi dell'installatore. Per la risposta rapida ai comandi tiene una
   connessione in attesa (long polling): l'installatore preme un pulsante e l'agente esegue in pochi secondi.
5. **Il cliente** ha nella barra laterale di Home Assistant il pannello **Assistenza**, con nome e colore
   dell'azienda dell'installatore: apre richieste, legge le risposte, decide cosa far controllare e può
   sospendere l'assistenza quando vuole.

Il cliente non apre porte e non serve VPN: tutto il traffico parte dal suo Home Assistant verso il server,
cifrato in HTTPS. L'agente esegue solo una lista chiusa di comandi e non può modificare la configurazione.

## Cosa fa

**Dashboard dell'installatore**
- **Panoramica** con gli impianti ordinati per gravità (offline → critici → da controllare) e caselle
  cliccabili; per ogni impianto: stato, se è **online** e se il cliente lo tiene **collegato**.
- **Clienti e impianti**: anagrafica, rinomina ed eliminazione (riscrivendo il nome), codice di abbinamento con
  pulsante *Copia*, revoca e nuovo abbinamento, collegamento all'Home Assistant del cliente.
- **Diagnostica**: entità non disponibili o orfane, batterie scariche, aggiornamenti disponibili, automazioni
  spente, spazio su disco, età dell'ultimo backup.
- **Comandi remoti**: diagnostica immediata e ping, con storico.
- **Ticket**: richieste dei clienti con diagnostica allegata, risposte, stato e assegnazione a un tecnico.
- **Accesso amministratore temporaneo** all'Home Assistant del cliente, solo se il cliente lo concede.
- **Utenti** con ruoli (amministratore, tecnico, sola lettura) e **più aziende** sullo stesso server.
- **Notifiche Telegram**: nuovi ticket, risposte, impianti offline o in stato critico.
- **Avvisi di aggiornamento** del server e degli agenti; conferme animate a ogni salvataggio.

**Pannello Assistenza del cliente** (nell'add-on, vedi [`nexohm/ha-addons`](https://github.com/nexohm/ha-addons))
- schede **Richieste**, **Impianto**, **Preferenze**;
- richieste di assistenza e risposte, con notifiche in Home Assistant o sul telefono;
- **accesso remoto a tempo** (1 ora, 4 ore, 24 ore, 7 giorni o senza scadenza) e sospensione in un clic;
- **entità escluse** dai controlli (anche con `*`) oppure controllo solo delle entità con certe **etichette**;
- concessione e chiusura dell'**accesso amministratore**.

## Requisiti

- Un computer Linux sempre acceso con **Docker** (lo script lo installa se manca): x86_64, arm64
  (Raspberry Pi 4/5) o armv7. Bastano 1 GB di RAM e pochi GB di disco.
- Un **indirizzo HTTPS** raggiungibile dagli impianti dei clienti, in uno di questi modi:
  - un **dominio** che punta all'IP pubblico della macchina, con le porte **80 e 443** aperte (HTTPS
    automatico, incluso nell'installazione);
  - un **reverse proxy** che hai già (Nginx Proxy Manager, Traefik, Caddy…) o un **tunnel Cloudflare**.
- Gli impianti dei clienti: Home Assistant **OS** o **Supervised** (servono gli add-on), su amd64, aarch64 o armv7.

## Installazione del server

Un solo comando:

```bash
curl -fsSL https://raw.githubusercontent.com/nexohm/assist-server/main/install.sh | sudo bash
```

Lo script installa Docker se manca, chiede la modalità, scarica l'immagine pronta e avvia tutto in
`/opt/ha-suite`. Le modalità sono due:

| Modalità | Quando sceglierla | Cosa serve |
|---|---|---|
| **1. HTTPS automatico** | macchina dedicata, raggiungibile direttamente | un dominio che punta all'IP pubblico, porte 80 e 443 aperte |
| **2. Proxy esistente** | hai già Nginx Proxy Manager, Traefik, un tunnel Cloudflare… | far puntare il proxy a `http://<ip-del-server>:<porta>` |

Con il proxy esistente lo script chiede la **porta** (propone la prima libera da 8080 in su) e controlla che sia
libera. Configurazione del proxy:
- **Nginx Proxy Manager**: *Proxy Host* con il tuo dominio → `http`, IP del server, porta scelta, e SSL con
  Let's Encrypt. Il timeout di lettura deve essere di almeno 60 secondi (è il valore predefinito): l'agente tiene
  connessioni in attesa fino a 25 secondi.
- **Tunnel Cloudflare**: aggiungi un *public hostname* (es. `assistenza.tuodominio.it`) verso
  `http://localhost:<porta>` o verso il tuo proxy.
- Se il proxy è su **un'altra macchina**, in `/opt/ha-suite/.env` metti `SUITE_BIND=0.0.0.0` e lancia
  `sudo ha-suite riavvia`.

Installazione senza domande (per script o installazioni in serie):

```bash
curl -fsSL .../install.sh | sudo DOMAIN=assistenza.tuaditta.it bash     # HTTPS automatico
curl -fsSL .../install.sh | sudo MODE=proxy SUITE_PORT=8086 bash        # proxy esistente, porta 8086
```

La configurazione sta in `/opt/ha-suite/.env` (vedi [`.env.example`](.env.example)); dopo una modifica:
`sudo ha-suite riavvia`.

## Primo avvio

1. Alla fine dell'installazione il terminale mostra un **codice di installazione** (per rivederlo:
   `sudo ha-suite codice`). Serve perché chi arriva per primo sull'indirizzo non possa prendersi il server.
2. Apri l'indirizzo del server nel browser e compila: codice di installazione, **nome dell'azienda** (lo
   vedranno i clienti), colore del marchio, nome utente e password del primo amministratore.
3. Questo primo utente è il **gestore del server**: amministra la propria azienda e in più può creare altre
   aziende, gestire l'indirizzo pubblico, la posta e gli aggiornamenti.
4. In **Impostazioni** controlla l'**indirizzo pubblico** (quello che i clienti inseriscono nell'add-on) e, se vuoi,
   configura Telegram e la posta.
5. In **Il mio account** inserisci il tuo nome completo (lo vedono i clienti nelle notifiche) e la tua email.

## Collegare l'impianto di un cliente

1. **Clienti → Nuovo cliente**, poi **Aggiungi impianto**: compare il **codice di abbinamento** (valido 24 ore).
2. Sull'Home Assistant del cliente: **Impostazioni → Componenti aggiuntivi → Store → ⋮ → Repository** e
   aggiungi `https://github.com/nexohm/ha-addons`.
3. Installa **Agente Assistenza**; nella scheda **Configurazione** inserisci l'indirizzo del server
   (es. `https://assistenza.rossiimpianti.it`) e il codice.
4. Avvia l'add-on: in pochi secondi l'impianto risulta **online** nella dashboard
   e nel pannello del cliente compare la voce **Assistenza**.

Se il codice scade o va perso: nella pagina dell'impianto, **Nuovo codice**. Per scollegare un impianto:
**Revoca accesso** (l'agente lo mostra al cliente e chiude un eventuale accesso amministratore).

## Utenti e ruoli

| Ruolo | Cosa può fare |
|---|---|
| **Amministratore** | tutto nella propria azienda: utenti, impostazioni, eliminazioni |
| **Tecnico** | clienti, impianti, ticket e comandi |
| **Sola lettura** | consultazione |

- Il nuovo utente riceve una **password iniziale** e al primo accesso deve sceglierne una sua.
- Ognuno cambia la propria password da **Il mio account**; cambiandola, le altre sessioni aperte si chiudono.
- Il **nome completo** dell'utente è quello che il cliente vede nelle notifiche («Mario Rossi di Rossi Impianti
  chiede l'accesso amministratore…»).
- Accesso protetto: password PBKDF2, blocco dei tentativi per indirizzo e per utente, protezione CSRF.

## Più aziende sullo stesso server

Un solo server, con un solo indirizzo, può ospitare **più aziende di installatori**:

- il **gestore** crea un'azienda da **Aziende**, con nome, colore, eventuali **limiti** di clienti e impianti e il
  primo amministratore (che al primo accesso sceglie la sua password);
- ogni azienda ha i propri utenti, clienti, impianti, ticket, nome, colore e bot Telegram e **non vede nulla delle
  altre**; i clienti vedono nome e colore dell'azienda a cui appartiene il loro impianto;
- il gestore vede delle altre aziende **solo i numeri**: quante sono, quanti clienti e impianti hanno;
- **limiti**: contano tutti gli impianti inseriti (anche non abbinati o revocati); non si possono abbassare sotto il
  numero attuale, l'azienda deve prima eliminarne;
- **sospensione**: gli utenti dell'azienda non entrano più (anche se erano collegati), gli impianti vanno in pausa
  e il cliente vede «Assistenza sospesa»; nessun dato viene perso e con **Riattiva** riparte tutto;
- **eliminazione**: chiede il nome dell'azienda e la password del gestore, fa prima una copia di sicurezza del
  database e cancella utenti, clienti, impianti e ticket dell'azienda;
- la propria azienda il gestore non può né sospenderla né eliminarla.

## Notifiche Telegram

Ogni azienda può ricevere su Telegram nuovi ticket, risposte dei clienti, impianti offline o di nuovo online e
stati critici. In **Impostazioni → Notifiche Telegram**:

1. su Telegram apri **@BotFather**, invia `/newbot` e scegli un nome: ricevi il **token**;
2. incollalo e salva;
3. apri il tuo bot, premi **Avvia**, poi **Rileva chat ID**;
4. **Invia prova**.

## Password dimenticata ed email

Il recupero funziona sempre con un **link monouso** (valido 30 minuti, una volta sola) che permette all'utente di
scegliere una nuova password. Chi può crearlo:

| Chi ha dimenticato la password | Come ottenere il link |
|---|---|
| un tecnico o un utente di sola lettura | il suo amministratore: **Utenti → Link di recupero password** |
| l'amministratore di un'azienda | il gestore: **Aziende → Password dimenticata da…** |
| il gestore del server | sul server: `sudo ha-suite password <nome-utente>` |

**Recupero via email (facoltativo).** Se il gestore lo attiva, nella pagina di accesso compare
**«Password dimenticata?»** e l'utente riceve il link nella casella indicata in *Il mio account*.
Serve un servizio di invio (SMTP): la casella del tuo dominio o un servizio come Brevo, SMTP2GO o Mailjet.
In **Impostazioni → Recupero della password via email** inserisci server SMTP, sicurezza (di solito STARTTLS
porta 587, oppure SSL/TLS porta 465), utente, password o chiave SMTP e mittente (es. `noreply@tuodominio.it`),
poi **Invia prova** e spunta **Attiva**.

Consigli:
- usa come mittente un indirizzo **del tuo dominio** e autentica il dominio presso il servizio (record SPF/DKIM
  nel DNS), altrimenti le email finiscono nello spam;
- il server **controlla ogni giorno** che la posta funzioni e **ogni 30 giorni** invia un'email di controllo al
  gestore: così le chiavi che scadono se inutilizzate (Brevo, per esempio, dopo 90 giorni) restano attive;
- se qualcosa smette di funzionare, il gestore vede un avviso in dashboard e, se ha Telegram, riceve un messaggio.

## Accesso amministratore temporaneo

Quando l'installatore deve correggere qualcosa nell'Home Assistant del cliente:

1. dalla pagina dell'impianto **chiede l'accesso amministratore**: il cliente riceve una notifica;
2. il cliente lo **concede** dal pannello Assistenza, scheda *Impianto*, scegliendo la durata
   (1 ora, 4 ore, 24 ore, 7 giorni o senza scadenza), oppure lo rifiuta;
3. l'installatore preme **Mostra password**: l'agente crea in Home Assistant un utente `assistenza_…` con una
   **password nuova**, mostrata una volta sola e mai salvata (sul server resta in memoria per 15 minuti);
4. ogni nuova password ricrea l'utente da zero; il cliente riceve una notifica a ogni ingresso e può chiudere
   l'accesso quando vuole;
5. alla scadenza l'utente viene **cancellato dall'agente**, anche se il server non è raggiungibile; anche
   all'arresto dell'add-on, alla revoca o alla sospensione dell'assistenza;
6. **senza scadenza**: l'utente viene cancellato dopo 30 giorni senza ingressi e il cliente riceve un promemoria
   ogni 30 giorni.

## Gestione quotidiana: il comando `ha-suite`

| Comando | Cosa fa |
|---|---|
| `sudo ha-suite stato` | mostra se il server è in funzione |
| `sudo ha-suite log` | segue i log in tempo reale |
| `sudo ha-suite aggiorna` | aggiorna anche lo script, fa un backup, scarica la nuova versione e riavvia |
| `sudo ha-suite versione` | versione del server in esecuzione |
| `sudo ha-suite backup` | salva tutti i dati in `/opt/ha-suite/backup` (tiene gli ultimi 14) |
| `sudo ha-suite ripristina backup/<file>.tar.gz` | ripristina un backup |
| `sudo ha-suite codice` | codice di installazione (solo prima della configurazione iniziale) |
| `sudo ha-suite password <utente>` | link per scegliere una nuova password (vale 30 minuti) |
| `sudo ha-suite riavvia` | applica le modifiche al file `.env` |
| `sudo ha-suite disinstalla` | rimuove i container (i dati restano) |

## Aggiornamenti, backup e ripristino

- **Aggiornare il server**: `sudo ha-suite aggiorna`. Il server controlla da solo su GitHub se c'è una nuova
  versione (ogni ora; *Impostazioni → Aggiornamenti → Controlla adesso*) e lo segnala al gestore con un avviso.
- **Aggiornare gli agenti**: dall'Home Assistant del cliente (*Impostazioni → Componenti aggiuntivi*); la dashboard
  segnala gli impianti con l'agente da aggiornare.
- **Versione fissa**: invece di seguire gli aggiornamenti, in `.env` metti per esempio `SUITE_TAG=0.2.7`.
- **Dati**: stanno tutti nel volume Docker `ha-suite_suite_data` (database, chiave delle sessioni, copie di
  sicurezza). Prima di ogni aggiornamento che cambia la struttura del database e prima di eliminare un'azienda il
  server fa da solo una copia in `/data/backups` (tiene le ultime 10).
- **Backup completo**: `sudo ha-suite backup`, da lanciare a mano o da cron, per esempio ogni notte:
  `0 3 * * * /opt/ha-suite/ha-suite backup`.
- **Disattivare il controllo degli aggiornamenti** (server senza internet): `UPDATE_CHECK=0` nel `.env`.

## Sicurezza e privacy

- Gli agenti si autenticano con un token personale, salvato sul server solo come hash; il codice di abbinamento è
  monouso e scade in 24 ore.
- Ogni dato in arrivo dagli agenti viene validato; le pagine sono protette con Content-Security-Policy, protezione
  contro clickjacking e CSRF.
- Ogni azienda vede solo i propri dati: il controllo è centralizzato e coperto da test.
- **Il cliente decide**: può escludere entità, far controllare solo certe etichette, sospendere l'assistenza e
  rifiutare o chiudere l'accesso amministratore. L'installatore non vede telecamere, posizione delle persone,
  cronologia né dati personali.

Dettagli e test: [docs/SECURITY.md](docs/SECURITY.md). Il protocollo tra agente e server:
[docs/PROTOCOL.md](docs/PROTOCOL.md).

## Sviluppo, prove e rilasci

```bash
git clone https://github.com/nexohm/assist-server && git clone https://github.com/nexohm/ha-addons
cd assist-server
pip install -r requirements.txt requests

SERVER_DIR=. AGENT_DIR=../ha-addons/installer_agent tests/run_e2e.sh        # prova completa: server + agente + finto HA
SERVER_DIR=. AGENT_DIR=../ha-addons/installer_agent tests/run_security.sh   # test di sicurezza di server e agente

DATA_DIR=./data SECURE_COOKIES=0 python3 -m uvicorn app.main:app --port 8080     # solo la dashboard
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build         # immagine dal codice locale
```

**Branch e versioni**
- Si lavora sul branch **`sviluppo`**: ogni push esegue i test e pubblica l'immagine `ghcr.io/nexohm/assist-server:sviluppo`
  (la versione mostrata è `sviluppo-<commit>`). Per provarla su un server: `SUITE_TAG=sviluppo` nel `.env`, poi
  `sudo ha-suite aggiorna`; finite le prove, `SUITE_TAG=latest`.
- **`main`** è protetto: si aggiorna solo con una pull request da `sviluppo` e con i test superati. Ogni push su
  `main` pubblica l'immagine `edge`.
- **Rilascio**: dopo il merge, un tag `vX.Y.Z` (es. `git tag v0.2.7 && git push origin v0.2.7`) pubblica
  `X.Y.Z`, `X.Y` e `latest`: da quel momento `ha-suite aggiorna` porta la nuova versione a tutti.

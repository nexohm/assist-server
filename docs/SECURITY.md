# Sicurezza — revisione e test (ottobre 2026)

Revisione del codice di `assist-server` e dell'add-on `installer_agent` (`ha-addons`), con attacchi
automatici contro un'istanza locale. I test sono in `tests/run_security.sh` e girano anche nella CI.

**Risultato:** prima delle correzioni 21 attacchi su 39 andavano a segno; dopo, 0 su 39.
La prova completa esistente (`tests/run_e2e.sh`) passa ancora: 48 controlli.

## Chi può attaccare cosa

| Chi | Cosa può raggiungere |
|---|---|
| Chiunque su internet | login, primo avvio (`/setup`), API di abbinamento degli agenti |
| Un utente della dashboard con meno permessi | le pagine per il suo ruolo |
| Un agente (Home Assistant del cliente, anche manomesso) | l'API agente con il suo token |
| Il server dell'installatore, se compromesso | ciò che l'agente mostra nel pannello del cliente |
| Un sito qualsiasi aperto dal browser dell'utente | richieste "a nome suo" (CSRF, clickjacking) |

L'ultimo punto della tabella è il più delicato: il pannello **Assistenza** gira nella stessa origine
di Home Assistant (ingress). Codice eseguito lì avrebbe accesso a tutta la casa del cliente.

## Problemi trovati e corretti

### Gravi

| # | Problema | Correzione |
|---|---|---|
| 1 | **Primo avvio aperto a tutti**: appena installato, chiunque raggiungesse il dominio prima dell'installatore poteva creare l'amministratore. I nuovi certificati HTTPS sono pubblici, i bot li scandagliano in pochi minuti. | Codice di installazione generato al primo avvio, leggibile solo sul server (fine di `install.sh`, log, `sudo ha-suite codice`). Eliminato dopo l'uso. |
| 2 | **XSS nel pannello del cliente**: alcuni campi in arrivo dal server (stato e id dei ticket) finivano nella pagina senza escape. Un server compromesso poteva eseguire codice nell'Home Assistant di tutti i clienti. | Escape di ogni campo, scarto dei dati con forma sbagliata, e Content-Security-Policy che vieta qualsiasi script nel pannello (doppia protezione). |
| 3 | **Token dell'impianto inviato ad altri indirizzi**: l'agente seguiva i reindirizzamenti HTTP portandosi dietro l'intestazione `Authorization`. | Reindirizzamenti disattivati: vengono segnalati come errore con indicazione di usare l'indirizzo finale. |

### Medi

| # | Problema | Correzione |
|---|---|---|
| 4 | Blocco dei tentativi di login aggirabile cambiando `X-Forwarded-For` (uvicorn si fidava di qualsiasi IP: `--forwarded-allow-ips "*"`). | Secondo contatore per nome utente (10 tentativi / 15 min); X-Forwarded-For accettato solo da reti private (Caddy, proxy di casa). |
| 5 | Moduli della dashboard accettati **senza token CSRF** in una sessione appena aperta (token vuoto = token vuoto). | Un token vuoto non è mai valido. |
| 6 | Pannello del cliente senza protezione CSRF: un sito esterno poteva riattivare l'accesso remoto o aprire ticket. | Token anti-CSRF in ogni modulo del pannello. |
| 7 | Un agente con dati strani (diagnostica malformata, date senza fuso, tipi sbagliati) mandava in errore 500 le pagine della dashboard o l'API. | Nuovo modulo `app/sanitize.py`: ogni dato dell'agente viene riportato alla forma prevista prima di salvarlo e prima di mostrarlo. |
| 8 | Richieste enormi caricate per intero in memoria prima del controllo (120 MB → +83 MB di RAM). | Limite letto a pezzi (413 subito) nell'API agente e 1 MB su tutte le richieste. |
| 9 | Agente: server `http://` accettato anche su internet (token e diagnostica in chiaro). | `https://` obbligatorio; `http://` solo per indirizzi di rete locale. |
| 10 | Nessuna intestazione di sicurezza: dashboard incorniciabile in altri siti (clickjacking). | CSP rigorosa (JavaScript spostato in `static/app.js`), `X-Frame-Options`, `nosniff`, `Referrer-Policy`, HSTS sotto HTTPS. |

### Lievi

| # | Problema | Correzione |
|---|---|---|
| 11 | Il tempo di risposta del login rivelava quali nomi utente esistono (5 ms contro 70 ms). | Verifica della password anche per utenti inesistenti. |
| 12 | Logout senza CSRF (un sito poteva far uscire l'utente). | Logout con token. |
| 13 | Colore del marchio non validato al primo avvio (iniezione di CSS). | Solo `#rrggbb`. |
| 14 | Il risultato di un comando già concluso poteva essere riscritto dall'agente. | Accettato solo per comandi in attesa o inviati (409 altrimenti). |
| 15 | Codice di abbinamento: controllo e consumo in due passaggi separati. | Consumo atomico: con due richieste contemporanee vince solo la prima. |
| 16 | Risposte a ticket inesistenti o con assegnatario non valido → errore 500. | Controlli e messaggio d'errore. |
| 17 | Accesso remoto a tempo scaduto: il server accodava ancora la diagnostica (l'agente la rifiutava comunque). | Il server controlla anche la scadenza. |
| 18 | Un agente poteva aprire ticket senza limite. | Massimo 20 ticket al giorno per impianto. |
| 19 | Pannello: `Content-Length` negativo bloccava la richiesta; risposte del server malformate o enormi facevano cadere pagina o agente. | Validazione dell'input e dimensione massima delle risposte (2 MB). |

## Cosa era già a posto

Ruoli rispettati su tutte le operazioni provate, nessun accesso ai dati di un altro impianto con il
proprio token, token salvati solo come hash, query SQL sempre parametrizzate, escape automatico
nei template della dashboard, reindirizzamento dopo il login limitato a percorsi interni, sessione
non più valida dopo la disattivazione dell'utente.

## Accesso amministratore temporaneo (dal server 0.2.7 e dall'agente 0.1.8)

È la funzione più delicata: un amministratore di Home Assistant può modificare tutto. Le difese:

- il permesso lo dà **solo il cliente** dal pannello, con la durata che sceglie; il server può solo chiederlo,
  e un comando `admin_password` senza permesso viene rifiutato dall'agente;
- l'utente HA viene **ricreato a ogni richiesta di password**: password nuova e sessioni precedenti chiuse;
- la password non viene mai salvata: né dall'agente, né nel database del server (solo in memoria, 15 minuti),
  e non è visibile al ruolo lettura;
- la **scadenza la controlla l'agente**, anche se il server è spento o compromesso; l'utente viene cancellato
  anche quando l'impianto viene scollegato e quando l'add-on viene arrestato o disinstallato;
- senza scadenza: utente cancellato dopo 30 giorni senza richieste di password e promemoria al cliente ogni 30 giorni;
- il cliente riceve una notifica a ogni ingresso dell'installatore.

Rischio che resta: chi controlla il server dell'installatore **mentre** un accesso è concesso può chiedere
una password e ottenere un amministratore su quell'impianto (il cliente ne riceve notifica). Per questo il
server va protetto come indicato sotto, e conviene concedere durate brevi.

## Più aziende sullo stesso server (dal server 0.2.7)

- Ogni utente e ogni cliente appartiene a un'azienda; impianti, ticket e comandi la seguono attraverso il cliente.
- Il controllo è in un punto solo: ogni id nell'indirizzo di una pagina (cliente, impianto, ticket, utente, comando)
  viene verificato contro l'azienda di chi è collegato prima di eseguire la pagina. Un id di un'altra azienda
  risponde «non trovato», come un id inesistente. Un indirizzo nuovo con un parametro non previsto fa errore
  invece di passare senza controllo.
- Gli elenchi (panoramica, clienti, ticket, utenti, tecnici da assegnare) filtrano per azienda.
- Il gestore del server vede delle altre aziende solo nome e numeri (clienti, impianti). Indirizzo pubblico e
  aggiornamenti sono suoi; un altro amministratore della sua azienda non può reimpostargli la password né disattivarlo.
- I nomi utente sono unici su tutto il server (un solo login per tutte le aziende).
- Bot Telegram e scelte delle notifiche sono per azienda.
- Test: `tests/run_security.sh`, sezione «più aziende» (letture e scritture incrociate, assegnazioni, Telegram, permessi).

## Da sapere / non coperto

- **Sessioni**: il cookie è firmato, non salvato sul server. Il logout cancella il cookie dal browser,
  ma un cookie rubato resta valido fino a 12 ore (salvo disattivare l'utente). Manca anche il cambio
  password dalla dashboard: utile da aggiungere insieme a una revoca di tutte le sessioni.
- **Modalità "ho già un reverse proxy"**: il server ascolta in http su tutte le interfacce. Tenere la
  porta chiusa verso internet sul router: deve passare solo dal proxy.
- **Server dell'installatore**: è il punto da proteggere di più, perché parla con tutti gli impianti.
  Aggiornamenti del sistema, accesso SSH solo con chiave, backup (`ha-suite backup`) fuori dalla macchina.
- Non sono stati fatti test sull'immagine Docker reale né dietro Cloudflare / Nginx Proxy Manager:
  conviene ripetere `tests/run_security.sh` e una prova manuale dopo il primo deploy vero.

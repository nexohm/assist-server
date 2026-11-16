# Protocollo agente ↔ server — versione 1

Questo documento è il "contratto" tra l'add-on agente (sull'Home Assistant del cliente)
e il server dell'installatore. Se cambia qualcosa di incompatibile, si alza la versione.

## Principi

- **La connessione la apre sempre l'agente.** Il cliente non apre porte, non serve VPN.
- Tutto passa in **HTTPS** con JSON. Niente WebSocket: funziona dietro qualsiasi proxy/firewall.
- I comandi dal server arrivano con **long-polling**: l'agente fa una richiesta, il server la
  tiene aperta fino a 25 secondi e risponde appena c'è un comando (o vuota allo scadere).
- **Lista chiusa di comandi.** L'agente esegue solo quelli elencati sotto; tutto il resto lo rifiuta.
- **Consenso del cliente.** Se il cliente sospende l'accesso remoto dal suo pannello,
  l'agente continua a mandare solo il battito (online/offline) e rifiuta ogni comando tranne `ping`.

## Intestazioni

Ogni richiesta dopo l'abbinamento porta:

```
Authorization: Bearer <token dell'impianto>
X-Agent-Protocol: 1
X-Agent-Version: 0.1.0
```

Il server salva solo l'hash SHA-256 del token: se il database viene rubato, i token non sono utilizzabili.
Il server può revocare un impianto in qualsiasi momento: da lì in poi riceve `401` e l'agente si ferma.

## 1. Abbinamento

L'installatore crea l'impianto nella dashboard e ottiene un **codice di abbinamento**
(8 caratteri, valido 24 ore, monouso). Il codice si inserisce nella configurazione dell'add-on.

`POST /api/agent/pair`

```json
{ "code": "K7Q2-M9XA", "ha_version": "2026.10.2", "agent_version": "0.1.0", "location_name": "Casa" }
```

Risposta `200`:

```json
{ "plant_id": 12, "token": "…48 caratteri casuali…", "server_name": "Rossi Impianti", "protocol": 1 }
```

Errori: `404` codice inesistente o già usato, `410` codice scaduto.

## 2. Rapporto (battito + diagnostica)

`POST /api/agent/report`

```json
{
  "consent": true,
  "consent_until": "2026-10-20T18:30:00+00:00",   // null = accesso senza scadenza
  "ha_urls": { "internal": "http://192.168.1.50:8123", "external": "https://casa.duckdns.org" },  // dall'agente 0.1.4
  "ha_version": "2026.10.2",
  "admin": { "active": true, "permanent": false, "until": "2026-10-20T19:53:00+00:00",   // dall'agente 0.1.8
             "username": "assistenza_3cf8e5", "last_reveal": "…", "declined": null },
  "diagnostics": { … }      // presente solo se il consenso è attivo e la diagnostica è stata eseguita
}
```

Risposta:

```json
{ "ok": true, "pending_commands": 0, "tickets_updated": "2026-10-20T14:05:00+00:00", "admin_requested": null,
  "notices": "3-17", "server_name": "Rossi Impianti", "brand_color": "#0f766e" }
```

Dall'agente 0.1.9 il rapporto può contenere anche:
- `notices_read`: `[{ "id": 17, "at": "<data>", "by": "Maria" }]` — comunicazioni viste nel pannello (la prima
  lettura non si riscrive, date future non accettate);
- `jobs`: ultimi 10 backup e aggiornamenti avviati dall'installatore, `[{ "id": <id del comando>, "kind": "backup|update",
  "title", "entity_id", "from", "to", "status": "in_corso|completato|errore", "message", "by", "started_at", "finished_at" }]`.

`notices` (dal server 0.2.8) è l'impronta delle comunicazioni dell'installatore per l'impianto (`null` se non
ce ne sono): quando cambia, l'agente le rilegge con `GET /api/agent/notices` (vedi sezione 5).

`tickets_updated` (dal server 0.2.6) è l'ultima modifica ai ticket dell'impianto: quando cambia,
l'agente rilegge i ticket e avvisa il cliente delle risposte dell'installatore.

L'agente lo invia ogni `heartbeat_seconds` (default 60). `consent_until` è la scadenza dell'accesso
remoto scelta dal cliente (campo facoltativo, aggiunto con l'agente 0.1.3): allo scadere l'agente
manda `consent: false` da solo. La diagnostica completa viaggia
ogni `diagnostics_minutes` (default 15) o quando richiesta da un comando.
Il server considera l'impianto **offline** se non riceve nulla da più di 3 minuti. L'avviso Telegram
all'installatore parte invece dopo i minuti scelti dall'azienda o per il singolo impianto (da 3 minuti a 24 ore).

`admin` (dall'agente 0.1.8) è lo stato dell'accesso amministratore concesso dal cliente: **non contiene
mai la password**. `admin_requested` (dal server 0.2.7) è la data della richiesta dell'installatore
ancora in attesa: l'agente la mostra al cliente, che può concedere l'accesso o rifiutarlo
(`declined` riporta la richiesta rifiutata, così il server la chiude).

### Formato della diagnostica

```json
{
  "collected_at": "2026-10-18T13:00:00+00:00",
  "status": "ok | attenzione | critico",
  "summary": { "entities": 412, "unavailable": 3, "low_battery": 1, "updates": 2, "automations_off": 0, "excluded": 4 },
  "system": { "ha_version": "…", "os_version": "…", "disk_used_gb": 12.1, "disk_total_gb": 58.0,
              "last_backup": "2026-10-17T03:00:00+00:00", "last_backup_age_days": 1 },
  "issues": [ { "severity": "critico|attenzione|info", "code": "unavailable_entities", "message": "…", "items": ["light.cucina", "…"],
                "names": { "light.cucina": "Luce cucina" } } ]
}
```

Dall'agente 0.1.9 la diagnostica contiene anche `updates_list` (aggiornamenti: `entity_id`, `title`, `installed`,
`latest`, `kind` = core|os|supervisor|addon|altro, `installable`, `in_progress`) e `backups` (`items`: nome, data,
`size_mb`, `kind` completo|parziale, `locations`, `automatic`, `protected`; `automatic`: `recurrence`, `days`, `time`,
`copies`, `retention_days`, `locations` e le date `last_attempted_automatic_backup`, `last_completed_automatic_backup`,
`next_automatic_backup`).

`names` (facoltativo, dall'agente 0.1.9) dà il nome leggibile delle entità elencate in `items`: la dashboard
le mostra raggruppate per tipo, col nome e l'id sotto.

## 3. Comandi (long-polling)

`GET /api/agent/commands?wait=25` → `{ "commands": [ { "id": 7, "name": "run_diagnostics", "args": {} } ] }`

Risultato: `POST /api/agent/commands/{id}/result`

```json
{ "ok": true, "result": { … } }        // oppure { "ok": false, "error": "consenso sospeso dal cliente" }
```

### Comandi permessi (v1)

| Nome | Cosa fa | Richiede consenso |
|---|---|---|
| `ping` | Risponde subito con versione agente e HA | no |
| `run_diagnostics` | Esegue la diagnostica completa e la restituisce | sì |
| `admin_password` | Ricrea l'utente amministratore temporaneo e restituisce `{username, password}` | accesso amministratore concesso dal cliente |
| `admin_close` | Chiude l'accesso amministratore e cancella l'utente | no |
| `create_backup` | Avvia un backup completo (agente 0.1.9) | sì |
| `install_update` | `{entity_id}`: backup completo, poi installazione dell'aggiornamento (agente 0.1.9) | sì |

`create_backup` e `install_update` rispondono subito `{"started": true}` e continuano in background: l'esito
arriva con i rapporti (`jobs`). Un comando rispedito con lo stesso id non avvia una seconda operazione e ne
gira una alla volta. Se il backup che precede un aggiornamento non riesce, l'aggiornamento non parte.

La password di `admin_password` il server la tiene **solo in memoria** per 15 minuti: nel database va
un segnaposto. Scadenza e cancellazione dell'utente le gestisce l'agente, non il server.

Nuovi comandi (es. `apply_blueprint`, `reload_integration`) si aggiungono
**prima all'agente** (che li dichiara in `capabilities` nel rapporto, dalla v2) e poi al server.

## 4. Ticket

Aperto dal cliente dal pannello dell'add-on:

`POST /api/agent/tickets`

```json
{ "title": "Le tapparelle del soggiorno non rispondono", "description": "…", "contact": "333 1234567", "diagnostics": { … },
  "user": { "id": "<id utente di Home Assistant, 32 caratteri esadecimali>", "name": "Maria" } }
```

La diagnostica viene allegata automaticamente se il consenso è attivo. `user` (facoltativo, dall'agente 0.1.9) è
l'utente di Home Assistant che scrive dal pannello, anche non amministratore: il server lo mostra all'installatore.

- `GET /api/agent/tickets` → ticket dell'impianto con i messaggi (così il cliente vede le risposte); dal server
  0.2.8 ogni ticket ha anche `opened_by` e `opened_by_id`, così l'agente mostra a un utente non amministratore
  solo le sue richieste
- `POST /api/agent/tickets/{id}/messages` → `{ "text": "…", "user": { … } }` risposta del cliente

## 5. Comunicazioni dell'installatore

`GET /api/agent/notices` (dal server 0.2.8) → le ultime 20 comunicazioni scritte dall'installatore per l'impianto:

```json
{ "key": "3-17", "notices": [ { "id": 17, "title": "…", "text": "…", "author_name": "Mario Rossi", "created_at": "…" } ] }
```

Il server segna come ricevute quelle restituite. L'agente le mostra in cima al pannello a tutti gli utenti di casa
e notifica il cliente una volta per ogni comunicazione nuova.

## Codici di risposta

| Codice | Significato | Cosa fa l'agente |
|---|---|---|
| 200 | ok | — |
| 401 | token non valido o impianto revocato | smette di contattare il server, mostra "abbinamento revocato" |
| 426 | versione di protocollo non supportata | mostra "aggiornare l'add-on" |
| 5xx / rete | server irraggiungibile | riprova con attesa crescente (max 5 minuti) |

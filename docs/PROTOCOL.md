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
  "ha_version": "2026.10.2",
  "diagnostics": { … }      // presente solo se il consenso è attivo e la diagnostica è stata eseguita
}
```

Risposta: `{ "ok": true, "pending_commands": 0 }`

L'agente lo invia ogni `heartbeat_seconds` (default 60). `consent_until` è la scadenza dell'accesso
remoto scelta dal cliente (campo facoltativo, aggiunto con l'agente 0.1.3): allo scadere l'agente
manda `consent: false` da solo. La diagnostica completa viaggia
ogni `diagnostics_minutes` (default 15) o quando richiesta da un comando.
Il server considera l'impianto **offline** se non riceve nulla da più di 3 minuti.

### Formato della diagnostica

```json
{
  "collected_at": "2026-10-18T13:00:00+00:00",
  "status": "ok | attenzione | critico",
  "summary": { "entities": 412, "unavailable": 3, "low_battery": 1, "updates": 2, "automations_off": 0 },
  "system": { "ha_version": "…", "os_version": "…", "disk_used_gb": 12.1, "disk_total_gb": 58.0,
              "last_backup": "2026-10-17T03:00:00+00:00", "last_backup_age_days": 1 },
  "issues": [ { "severity": "critico|attenzione|info", "code": "unavailable_entities", "message": "…", "items": ["light.cucina", "…"] } ]
}
```

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

Nuovi comandi (es. `apply_blueprint`, `reload_integration`, `create_backup`) si aggiungono
**prima all'agente** (che li dichiara in `capabilities` nel rapporto, dalla v2) e poi al server.

## 4. Ticket

Aperto dal cliente dal pannello dell'add-on:

`POST /api/agent/tickets`

```json
{ "title": "Le tapparelle del soggiorno non rispondono", "description": "…", "contact": "333 1234567", "diagnostics": { … } }
```

La diagnostica viene allegata automaticamente se il consenso è attivo.

- `GET /api/agent/tickets` → ticket dell'impianto con i messaggi (così il cliente vede le risposte)
- `POST /api/agent/tickets/{id}/messages` → `{ "text": "…" }` risposta del cliente

## Codici di risposta

| Codice | Significato | Cosa fa l'agente |
|---|---|---|
| 200 | ok | — |
| 401 | token non valido o impianto revocato | smette di contattare il server, mostra "abbinamento revocato" |
| 426 | versione di protocollo non supportata | mostra "aggiornare l'add-on" |
| 5xx / rete | server irraggiungibile | riprova con attesa crescente (max 5 minuti) |

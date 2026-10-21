# Nexohm Assist — support server for Home Assistant installers

[🇮🇹 Italiano](README.md) · 🇬🇧 English

**Nexohm Assist** lets a home-automation installer look after their customers' Home Assistant systems remotely:
see whether they are up, receive an automatic health check, answer support requests and, only when the customer
allows it, log in as an administrator for a limited time.

It has two parts:

| Part | Runs on | Repository |
|---|---|---|
| **Support server** (this one) | the installer's server, in Docker | `nexohm/assist-server` |
| **Support Agent** (add-on) | each customer's Home Assistant | [`nexohm/ha-addons`](https://github.com/nexohm/ha-addons) |

```
  INSTALLER'S SERVER (Docker)                          CUSTOMER SYSTEMS
 ┌──────────────────────────────────┐
 │ dashboard: customers, systems,   │◄── HTTPS ── Home Assistant + Support Agent
 │ diagnostics, tickets, users,     │◄── HTTPS ── Home Assistant + Support Agent
 │ companies, notifications         │◄── HTTPS ── Home Assistant + Support Agent
 └──────────────────────────────────┘
          the agent always calls the server: no port is opened at the customer's home
```

The user interface (dashboard and customer panel) is currently in **Italian**.

---

## Contents

1. [How it works](#how-it-works)
2. [Features](#features)
3. [Requirements](#requirements)
4. [Installing the server](#installing-the-server)
5. [First start](#first-start)
6. [Connecting a customer's system](#connecting-a-customers-system)
7. [Users and roles](#users-and-roles)
8. [Several companies on one server](#several-companies-on-one-server)
9. [Telegram notifications](#telegram-notifications)
10. [Forgotten passwords and email](#forgotten-passwords-and-email)
11. [Temporary administrator access](#temporary-administrator-access)
12. [Day-to-day management: the `ha-suite` command](#day-to-day-management-the-ha-suite-command)
13. [Updates, backup and restore](#updates-backup-and-restore)
14. [Security and privacy](#security-and-privacy)
15. [Development, testing and releases](#development-testing-and-releases)

---

## How it works

1. **The installer** installs the server on their own machine (mini PC, VPS, Raspberry Pi), reachable from the
   internet at an HTTPS address such as `https://support.example-installer.com`.
2. In the dashboard they create the **customer** and their **system**: the server generates a one-time
   **pairing code**, valid for 24 hours.
3. **On the customer's Home Assistant** the **Support Agent** add-on is installed with the server address and the
   code. The agent pairs and from then on uses a personal token (the code is no longer needed).
4. The agent **calls the server** at regular intervals: it reports that the system is up, sends the automatic
   health check (diagnostics) and picks up the installer's commands. It keeps a waiting connection open (long
   polling), so when the installer presses a button the agent acts within seconds.
5. **The customer** gets an **Assistenza** (Support) panel in the Home Assistant sidebar, with the installer
   company's name and colour: they open requests, read replies, choose what may be checked and can suspend
   support at any time.

The customer opens no ports and needs no VPN: all traffic goes from their Home Assistant to the server over
HTTPS. The agent only runs a closed list of commands and cannot change the configuration.

## Features

**Installer dashboard**
- **Overview** with systems sorted by severity (offline → critical → needs attention) and clickable tiles; for
  each system: status, whether it is **online** and whether the customer keeps it **connected**.
- **Customers and systems**: contact details, rename and delete (by retyping the name), pairing code with a
  *Copy* button, revoke and re-pair, link to the customer's Home Assistant.
- **Diagnostics**: unavailable or orphaned entities, low batteries, pending updates, disabled automations, disk
  space, age of the last backup.
- **Remote commands**: on-demand diagnostics and ping, with history.
- **Tickets**: customer requests with diagnostics attached, replies, status and assignment to a technician.
- **Temporary administrator access** to the customer's Home Assistant, only if the customer grants it.
- **Users** with roles (administrator, technician, read-only) and **several companies** on one server.
- **Telegram notifications**: new tickets, replies, systems offline or critical.
- **Update notices** for the server and the agents; animated confirmations on every save.

**Customer's Support panel** (in the add-on, see [`nexohm/ha-addons`](https://github.com/nexohm/ha-addons))
- **Requests**, **System** and **Preferences** tabs;
- support requests and replies, with notifications in Home Assistant or on the phone;
- **time-limited remote access** (1 hour, 4 hours, 24 hours, 7 days or no expiry) and one-click suspension;
- **excluded entities** (wildcards allowed) or checking only entities with given **labels**;
- granting and closing **administrator access**.

## Requirements

- An always-on Linux computer with **Docker** (the script installs it if missing): x86_64, arm64
  (Raspberry Pi 4/5) or armv7. 1 GB of RAM and a few GB of disk are enough.
- An **HTTPS address** reachable from the customers' systems, in one of these ways:
  - a **domain** pointing to the machine's public IP, with ports **80 and 443** open (automatic HTTPS, included);
  - a **reverse proxy** you already have (Nginx Proxy Manager, Traefik, Caddy…) or a **Cloudflare Tunnel**.
- Customer systems: Home Assistant **OS** or **Supervised** (add-ons are required), on amd64, aarch64 or armv7.

## Installing the server

One command:

```bash
curl -fsSL https://raw.githubusercontent.com/nexohm/assist-server/main/install.sh | sudo bash
```

The script installs Docker if needed, asks for the mode, downloads the ready-made image and starts everything in
`/opt/ha-suite`. There are two modes:

| Mode | When to choose it | What you need |
|---|---|---|
| **1. Automatic HTTPS** | dedicated machine, directly reachable | a domain pointing to the public IP, ports 80 and 443 open |
| **2. Existing proxy** | you already have Nginx Proxy Manager, Traefik, a Cloudflare Tunnel… | point the proxy to `http://<server-ip>:<port>` |

With an existing proxy the script asks for the **port** (it suggests the first free one from 8080 upwards) and
checks that it is free. Proxy setup:
- **Nginx Proxy Manager**: a *Proxy Host* with your domain → `http`, server IP, chosen port, and SSL with Let's
  Encrypt. The read timeout must be at least 60 seconds (the default): the agent keeps connections waiting for up
  to 25 seconds.
- **Cloudflare Tunnel**: add a *public hostname* (e.g. `support.yourdomain.com`) pointing to
  `http://localhost:<port>` or to your proxy.
- If the proxy runs on **another machine**, set `SUITE_BIND=0.0.0.0` in `/opt/ha-suite/.env` and run
  `sudo ha-suite riavvia`.

Unattended installation:

```bash
curl -fsSL .../install.sh | sudo DOMAIN=support.yourcompany.com bash      # automatic HTTPS
curl -fsSL .../install.sh | sudo MODE=proxy SUITE_PORT=8086 bash          # existing proxy, port 8086
```

The configuration lives in `/opt/ha-suite/.env` (see [`.env.example`](.env.example)); after a change run
`sudo ha-suite riavvia`.

## First start

1. At the end of the installation the terminal shows an **installation code** (to see it again:
   `sudo ha-suite codice`). It prevents whoever reaches the address first from taking over the server.
2. Open the server address in a browser and fill in: installation code, **company name** (customers will see it),
   brand colour, username and password of the first administrator.
3. This first user is the **server manager**: they run their own company and can also create other companies and
   manage the public address, email and updates.
4. In **Impostazioni** (Settings) check the **public address** (the one customers enter in the add-on) and, if you
   like, set up Telegram and email.
5. In **Il mio account** (My account) enter your full name (customers see it in notifications) and your email.

## Connecting a customer's system

1. **Clienti → Nuovo cliente** (Customers → New customer), then **Aggiungi impianto** (Add system): the
   **pairing code** appears (valid for 24 hours).
2. On the customer's Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories** and add
   `https://github.com/nexohm/ha-addons`.
3. Install **Agente Assistenza** (Support Agent); in the **Configuration** tab enter the server address
   (e.g. `https://support.example-installer.com`) and the code.
4. Start the add-on: within seconds the system shows as **online** in the dashboard and the customer gets the
   **Assistenza** panel.

If the code expires or gets lost: **Nuovo codice** (New code) on the system page. To disconnect a system:
**Revoca accesso** (Revoke access) — the agent tells the customer and closes any administrator access.

## Users and roles

| Role | What it can do |
|---|---|
| **Administrator** | everything within their own company: users, settings, deletions |
| **Technician** | customers, systems, tickets and commands |
| **Read-only** | viewing only |

- New users get an **initial password** and must choose their own at first login.
- Everyone changes their password in **Il mio account**; changing it closes the user's other open sessions.
- The user's **full name** is what customers see in notifications ("Mario Rossi of Rossi Impianti is asking for
  administrator access…").
- Login is protected: PBKDF2 passwords, rate limiting per address and per user, CSRF protection.

## Several companies on one server

One server, with a single address, can host **several installer companies**:

- the **manager** creates a company from **Aziende** (Companies), with name, colour, optional customer and system
  **limits** and the first administrator (who chooses their own password at first login);
- each company has its own users, customers, systems, tickets, name, colour and Telegram bot and **sees nothing
  of the others**; customers see the name and colour of the company their system belongs to;
- of the other companies the manager only sees **numbers**: how many there are and how many customers and systems
  each has;
- **limits**: every system counts, including unpaired or revoked ones; a limit cannot go below the current number,
  the company must delete some first;
- **suspension**: the company's users can no longer log in (even if already logged in), its systems pause and
  customers see "support suspended"; no data is lost and **Riattiva** (Reactivate) brings everything back;
- **deletion**: requires the company name and the manager's password, takes a database backup first and then
  deletes the company's users, customers, systems and tickets;
- the manager cannot suspend or delete their own company.

## Telegram notifications

Each company can receive new tickets, customer replies, systems going offline or back online and critical states
on Telegram. In **Impostazioni → Notifiche Telegram**:

1. in Telegram open **@BotFather**, send `/newbot` and choose a name: you get the **token**;
2. paste it and save;
3. open your bot, press **Start**, then **Rileva chat ID** (Detect chat ID);
4. **Invia prova** (Send test).

## Forgotten passwords and email

Recovery always works through a **one-time link** (valid for 30 minutes, usable once) that lets the user choose a
new password. Who can create it:

| Who forgot their password | How to get the link |
|---|---|
| a technician or read-only user | their administrator: **Utenti → Link di recupero password** |
| a company administrator | the manager: **Aziende → Password dimenticata da…** |
| the server manager | on the server: `sudo ha-suite password <username>` |

**Email recovery (optional).** When the manager enables it, a **"Password dimenticata?"** (Forgot password?) link
appears on the login page and the user receives the link at the address set in *Il mio account*. You need a
sending service (SMTP): your domain's mailbox or a service such as Brevo, SMTP2GO or Mailjet. In
**Impostazioni → Recupero della password via email** enter the SMTP server, security (usually STARTTLS on port
587, or SSL/TLS on port 465), username, SMTP password or key and sender (e.g. `noreply@yourdomain.com`), then
**Invia prova** and tick **Attiva**.

Tips:
- use a sender address **on your own domain** and authenticate the domain with the service (SPF/DKIM DNS records),
  otherwise emails end up in spam;
- the server **checks the mail login every day** and **every 30 days** sends a check email to the manager, so keys
  that expire when unused (Brevo, for instance, after 90 days) stay active;
- if something stops working the manager sees a warning in the dashboard and, with Telegram set up, gets a message.

## Temporary administrator access

When the installer needs to fix something in the customer's Home Assistant:

1. on the system page they **ask for administrator access**: the customer gets a notification;
2. the customer **grants** it from the Support panel, *Impianto* tab, choosing how long (1 hour, 4 hours,
   24 hours, 7 days or no expiry), or declines;
3. the installer presses **Mostra password** (Show password): the agent creates an `assistenza_…` user in Home
   Assistant with a **new password**, shown once and never stored (the server keeps it in memory for 15 minutes);
4. every new password recreates the user from scratch; the customer is notified at every login and can close the
   access at any time;
5. when the time is up the **agent deletes the user**, even if the server is unreachable; also when the add-on
   stops, when the system is revoked or when support is suspended;
6. **no expiry**: the user is deleted after 30 days without logins and the customer gets a reminder every 30 days.

## Day-to-day management: the `ha-suite` command

| Command | What it does |
|---|---|
| `sudo ha-suite stato` | shows whether the server is running |
| `sudo ha-suite log` | follows the logs |
| `sudo ha-suite aggiorna` | updates the script itself, takes a backup, downloads the new version and restarts |
| `sudo ha-suite versione` | running server version |
| `sudo ha-suite backup` | saves all data to `/opt/ha-suite/backup` (keeps the last 14) |
| `sudo ha-suite ripristina backup/<file>.tar.gz` | restores a backup |
| `sudo ha-suite codice` | installation code (only before the first configuration) |
| `sudo ha-suite password <user>` | link to choose a new password (valid for 30 minutes) |
| `sudo ha-suite riavvia` | applies changes to the `.env` file |
| `sudo ha-suite disinstalla` | removes the containers (data is kept) |

## Updates, backup and restore

- **Updating the server**: `sudo ha-suite aggiorna`. The server checks GitHub for new versions on its own (every
  hour; *Impostazioni → Aggiornamenti → Controlla adesso*) and shows the manager a notice.
- **Updating the agents**: from the customer's Home Assistant (*Settings → Add-ons*); the dashboard flags systems
  running an old agent.
- **Pinning a version**: instead of following updates, set e.g. `SUITE_TAG=0.2.7` in `.env`.
- **Data** lives in the Docker volume `ha-suite_suite_data` (database, session key, safety copies). Before every
  update that changes the database structure, and before deleting a company, the server automatically copies the
  database to `/data/backups` (keeping the last 10).
- **Full backup**: `sudo ha-suite backup`, by hand or from cron, e.g. every night:
  `0 3 * * * /opt/ha-suite/ha-suite backup`.
- **Disabling the update check** (servers without internet access): `UPDATE_CHECK=0` in `.env`.

## Security and privacy

- Agents authenticate with a personal token stored on the server only as a hash; the pairing code is single-use
  and expires after 24 hours.
- Everything coming from agents is validated; pages are protected with a Content-Security-Policy and against
  clickjacking and CSRF.
- Each company only sees its own data: the check is centralised and covered by tests.
- **The customer is in control**: they can exclude entities, allow checks only on certain labels, suspend support
  and decline or close administrator access. The installer never sees cameras, people's locations, history or
  personal data.

Details and tests: [docs/SECURITY.md](docs/SECURITY.md) (Italian). Agent ↔ server protocol:
[docs/PROTOCOL.md](docs/PROTOCOL.md).

## Development, testing and releases

```bash
git clone https://github.com/nexohm/assist-server && git clone https://github.com/nexohm/ha-addons
cd assist-server
pip install -r requirements.txt requests

SERVER_DIR=. AGENT_DIR=../ha-addons/installer_agent tests/run_e2e.sh        # full test: server + agent + fake HA
SERVER_DIR=. AGENT_DIR=../ha-addons/installer_agent tests/run_security.sh   # security tests for server and agent

DATA_DIR=./data SECURE_COOKIES=0 python3 -m uvicorn app.main:app --port 8080     # dashboard only
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build         # image from local code
```

**Branches and versions**
- Work happens on the **`sviluppo`** (development) branch: every push runs the tests and publishes the
  `ghcr.io/nexohm/assist-server:sviluppo` image (shown as version `sviluppo-<commit>`). To try it on a server:
  `SUITE_TAG=sviluppo` in `.env`, then `sudo ha-suite aggiorna`; when done, `SUITE_TAG=latest`.
- **`main`** is protected: it only changes through a pull request from `sviluppo` with passing tests. Every push
  to `main` publishes the `edge` image.
- **Release**: after merging, a `vX.Y.Z` tag (e.g. `git tag v0.2.7 && git push origin v0.2.7`) publishes `X.Y.Z`,
  `X.Y` and `latest`: from then on `ha-suite aggiorna` brings the new version to everyone.

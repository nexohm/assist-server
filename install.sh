#!/usr/bin/env bash
# Installazione del server di assistenza con l'immagine Docker già pronta.
#
#   curl -fsSL https://raw.githubusercontent.com/nexohm/assist-server/main/install.sh | sudo bash
#
# Senza domande (es. per script o installazioni in serie):
#   curl -fsSL .../install.sh | sudo DOMAIN=assistenza.tuaditta.it bash
#   curl -fsSL .../install.sh | sudo MODE=proxy bash
#   curl -fsSL .../install.sh | sudo MODE=proxy SUITE_PORT=8085 bash   (porta diversa da 8080)
set -euo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/nexohm/assist-server/main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/ha-suite}"
SUITE_IMAGE="${SUITE_IMAGE:-ghcr.io/nexohm/assist-server}"
SUITE_TAG="${SUITE_TAG:-latest}"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
fail() { printf '\n\033[31mErrore:\033[0m %s\n' "$*" >&2; exit 1; }
# Le domande leggono dal terminale anche quando lo script arriva da "curl | bash".
ask() {
  local prompt="$1" default="${2:-}" answer=""
  if [[ -r /dev/tty ]]; then read -rp "$prompt" answer < /dev/tty || true; fi
  printf '%s' "${answer:-$default}"
}

bold "Server di assistenza Home Assistant — installazione"

# ---------- requisiti ----------
[[ $EUID -eq 0 ]] || fail "avvia l'installazione come amministratore (con sudo)."

case "$(uname -m)" in
  x86_64|aarch64|arm64|armv7l) ;;
  *) fail "architettura $(uname -m) non supportata (servono x86_64, arm64 o armv7)." ;;
esac

if ! command -v docker >/dev/null; then
  ans=$(ask "Docker non è installato. Lo installo ora con lo script ufficiale? [S/n] " "s")
  [[ "${ans,,}" == "s" ]] || fail "Docker è necessario: https://docs.docker.com/engine/install/"
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || fail "serve Docker Compose v2 (comando 'docker compose')."
command -v curl >/dev/null || fail "serve curl."

# Una porta è occupata se qualcosa risponde in locale (funziona anche senza "ss").
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

# ---------- modalità ----------
MODE="${MODE:-}"
DOMAIN="${DOMAIN:-}"
if [[ -z "$MODE" ]]; then
  if [[ -n "$DOMAIN" ]]; then
    MODE=https
  else
    echo
    info "1) Installazione completa: HTTPS automatico (servono un dominio e le porte 80/443 aperte)"
    info "2) Ho già un reverse proxy con HTTPS (Nginx Proxy Manager, Traefik, Caddy...)"
    choice=$(ask "Scelta [1]: " "1")
    [[ "$choice" == "2" ]] && MODE=proxy || MODE=https
  fi
fi

if [[ "$MODE" == "https" ]]; then
  [[ -n "$DOMAIN" ]] || DOMAIN=$(ask "Dominio pubblico (es. assistenza.tuaditta.it): ")
  [[ -n "$DOMAIN" ]] || fail "il dominio è obbligatorio per l'HTTPS automatico."
  for port in 80 443; do
    if port_busy "$port"; then
      if ! docker ps --format '{{.Names}}' | grep -q '^ha-suite-caddy'; then
        fail "la porta $port è già occupata. Se hai già un reverse proxy scegli la modalità 2 (MODE=proxy)."
      fi
    fi
  done
  PROFILES=https; BIND=127.0.0.1
else
  # In ascolto su tutte le interfacce: il proxy (anche se gira in un altro container,
  # come Nginx Proxy Manager) lo raggiunge su http://<ip-di-questa-macchina>:<porta>.
  # I cookie di accesso sono "solo HTTPS": dal browser si entra comunque solo passando dal proxy.
  PROFILES=""; BIND=0.0.0.0
fi

# ---------- porta del server ----------
already_ours() { docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -q "^ha-suite-suite-1 .*:$1->"; }
first_free() { local p=$1; while port_busy "$p"; do p=$((p + 1)); done; echo "$p"; }
valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && (( $1 >= 1024 && $1 <= 65535 )); }

if [[ -f "$INSTALL_DIR/.env" ]] && grep -q '^SUITE_PORT=' "$INSTALL_DIR/.env"; then
  # Reinstallazione o aggiornamento: si tiene la porta già configurata nel proxy
  SUITE_PORT=$(grep '^SUITE_PORT=' "$INSTALL_DIR/.env" | cut -d= -f2)
  info "Porta del server: $SUITE_PORT (configurazione esistente)"
elif [[ -n "${SUITE_PORT:-}" ]]; then
  # Porta indicata nel comando (SUITE_PORT=8086): si usa quella o ci si ferma
  valid_port "$SUITE_PORT" || fail "porta non valida: $SUITE_PORT (scegline una tra 1024 e 65535)."
  port_busy "$SUITE_PORT" && ! already_ours "$SUITE_PORT" \
    && fail "la porta $SUITE_PORT è già occupata. Prova con: SUITE_PORT=$(first_free "$SUITE_PORT")"
elif [[ "$MODE" == "https" ]]; then
  # Con Caddy la porta resta interna alla macchina: basta che sia libera
  SUITE_PORT=$(first_free 8080)
else
  free=$(first_free 8080)
  echo
  info "Porta su cui il tuo reverse proxy raggiungerà il server:"
  info "1) Trovala tu: la prima libera da 8080 in su (ora: $free)"
  info "2) La scelgo io"
  choice=$(ask "Scelta [1]: " "1")
  if [[ "$choice" == "2" ]]; then
    while true; do
      SUITE_PORT=$(ask "Porta [$free]: " "$free")
      if ! valid_port "$SUITE_PORT"; then
        info "Porta non valida: scegline una tra 1024 e 65535."
      elif port_busy "$SUITE_PORT" && ! already_ours "$SUITE_PORT"; then
        info "La porta $SUITE_PORT è già usata da un altro servizio. Prima libera successiva: $(first_free "$SUITE_PORT")"
      else
        break
      fi
    done
  else
    SUITE_PORT=$free
  fi
  info "Userò la porta $SUITE_PORT"
fi

# ---------- file di installazione ----------
bold "Preparo $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
for f in docker-compose.yml Caddyfile ha-suite; do
  curl -fsSL "$REPO_RAW/$f" -o "$f.new" || fail "impossibile scaricare $f da $REPO_RAW"
  mv "$f.new" "$f"
done
chmod +x ha-suite
ln -sf "$INSTALL_DIR/ha-suite" /usr/local/bin/ha-suite

if [[ -f .env ]]; then
  info "Trovata una configurazione esistente (.env): la mantengo."
else
  cat > .env <<EOF
# Configurazione del server di assistenza. Dopo una modifica: ha-suite riavvia
DOMAIN=${DOMAIN:-}
COMPOSE_PROFILES=${PROFILES}
SUITE_BIND=${BIND}
SUITE_PORT=${SUITE_PORT}
SUITE_IMAGE=${SUITE_IMAGE}
SUITE_TAG=${SUITE_TAG}
EOF
  chmod 600 .env
fi

# ---------- avvio ----------
bold "Scarico l'immagine e avvio"
docker compose pull
docker compose up -d

printf '  Attendo che il server sia pronto'
for _ in $(seq 1 60); do
  if [[ "$(docker inspect -f '{{.State.Health.Status}}' ha-suite-suite-1 2>/dev/null)" == "healthy" ]]; then
    ready=1; break
  fi
  printf '.'; sleep 2
done
echo
[[ "${ready:-0}" == 1 ]] || fail "il server non risponde. Controlla con: ha-suite log"

bold "Installazione completata"
if [[ "$MODE" == "https" ]]; then
  info "Apri  https://$DOMAIN  per la configurazione iniziale."
  info "Il primo accesso può richiedere qualche secondo: Caddy sta ottenendo il certificato."
else
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  info "Il server ascolta su http://$ip:$SUITE_PORT"
  info "Configura il tuo reverse proxy verso questo indirizzo con HTTPS e"
  info "un timeout di lettura di almeno 60 secondi, poi apri il dominio nel browser."
fi
echo
info "Comandi utili:  ha-suite stato | log | aggiorna | backup"

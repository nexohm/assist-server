#!/usr/bin/env bash
# Test di sicurezza: server (attacchi alla dashboard e all'API agente) e add-on (server ostile).
#   pip install -r requirements.txt requests
#   tests/run_security.sh
# Come la prova e2e, cerca l'agente in ../ha-addons/installer_agent.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export SERVER_DIR="$(cd "${SERVER_DIR:-$HERE/..}" && pwd)"
export AGENT_DIR="$(cd "${AGENT_DIR:-$SERVER_DIR/../ha-addons/installer_agent}" && pwd)"
export NO_PROXY="localhost,127.0.0.1${NO_PROXY:+,$NO_PROXY}"
TMP="$(mktemp -d)"; mkdir -p "$TMP/srvdata" "$TMP/oidcdata"
cleanup() { kill $(jobs -p) 2>/dev/null || true; wait 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

# Stesse opzioni dell'immagine Docker; "*" = caso peggiore (proxy che inoltra X-Forwarded-For del client)
(cd "$SERVER_DIR" && exec env DATA_DIR="$TMP/srvdata" SECURE_COOKIES=0 SUITE_VERSION=test UPDATE_CHECK=0 \
   python3 -m uvicorn app.main:app --port 8080 --log-level warning --proxy-headers \
   --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}") &
SERVER_PID=$!
# Secondo server per l'accesso con il provider OpenID Connect, con un finto Authentik in http (solo prove)
python3 "$HERE/fake_oidc.py" 8125 &
(cd "$SERVER_DIR" && exec env DATA_DIR="$TMP/oidcdata" SECURE_COOKIES=0 SUITE_VERSION=test UPDATE_CHECK=0 OIDC_ALLOW_HTTP=1 \
   python3 -m uvicorn app.main:app --port 8081 --log-level warning) &
for _ in $(seq 1 30); do curl -fs http://127.0.0.1:8080/health >/dev/null 2>&1 && break; sleep 0.5; done
for _ in $(seq 1 60); do curl -fs http://127.0.0.1:8081/health >/dev/null 2>&1 \
  && curl -fs http://127.0.0.1:8125/application/o/assist/jwks/ >/dev/null 2>&1 && break; sleep 0.5; done

echo "== Server"
SERVER_PID=$SERVER_PID python3 "$HERE/security_server.py" "$TMP/srvdata"; s=$?
echo; echo "== Accesso con il provider (OpenID Connect)"
python3 "$HERE/security_oidc.py" "$TMP/oidcdata"; o=$?
echo; echo "== Add-on agente"
python3 "$HERE/security_agent.py"; a=$?
exit $(( s || o || a ))

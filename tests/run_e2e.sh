#!/usr/bin/env bash
# Prova completa in locale: server + agente + finto Home Assistant (44 controlli).
#   pip install -r requirements.txt requests
#   tests/run_e2e.sh
# Per default cerca l'agente in ../ha-addons/installer_agent (cartelle affiancate).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export SERVER_DIR="$(cd "${SERVER_DIR:-$HERE/..}" && pwd)"
export AGENT_DIR="$(cd "${AGENT_DIR:-$SERVER_DIR/../ha-addons/installer_agent}" && pwd)"
TMP="$(mktemp -d)"; mkdir -p "$TMP/srvdata" "$TMP/agentdata"
cleanup() { kill $(jobs -p) 2>/dev/null || true; wait 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

python3 "$HERE/fake_ha.py" &
(cd "$SERVER_DIR" && DATA_DIR="$TMP/srvdata" SECURE_COOKIES=0 SUITE_VERSION=test \
   python3 -m uvicorn app.main:app --port 8080 --log-level warning) &
for _ in $(seq 1 30); do curl -fs http://127.0.0.1:8080/health >/dev/null 2>&1 && break; sleep 0.5; done
python3 "$HERE/e2e.py" "$TMP"

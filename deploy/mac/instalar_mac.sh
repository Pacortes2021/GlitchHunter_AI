#!/bin/bash
# =============================================================================
# GlitchHunter AI - Radar del PC (macOS) como servicio, con túnel al servidor
#
# Deja dos servicios de launchd que arrancan al iniciar sesión y se reinician si se caen:
#   cl.glitchhunter.tunel  túnel SSH al servidor (puerto local 8090 -> servidor 127.0.0.1:8080)
#   cl.glitchhunter.radar  el radar (python3 run.py server) en la carpeta del repositorio
#
# Uso (desde la carpeta del repositorio):
#   bash deploy/mac/instalar_mac.sh ubuntu@IP_DEL_SERVIDOR
#   bash deploy/mac/instalar_mac.sh --desinstalar
#
# Requisitos: la llave SSH (~/.ssh/id_ed25519 o la indicada en SSH_KEY) entra al servidor sin
# contraseña, y el .env del PC tiene RADAR_ONLY_STORES / FORWARD_TO_URL=http://127.0.0.1:8090.
# Registros: ~/Library/Logs/glitchhunter-radar.log y glitchhunter-tunel.log
# =============================================================================
set -euo pipefail

AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs"
UID_N="$(id -u)"
REPO="${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"

unload() {
  for name in radar tunel; do
    launchctl bootout "gui/${UID_N}/cl.glitchhunter.${name}" 2>/dev/null || true
  done
}

if [ "${1:-}" = "--desinstalar" ]; then
  unload
  rm -f "${AGENTS}/cl.glitchhunter.radar.plist" "${AGENTS}/cl.glitchhunter.tunel.plist"
  echo "Servicios del radar y del túnel eliminados."
  exit 0
fi

SERVER="${1:?Uso: bash deploy/mac/instalar_mac.sh ubuntu@IP_DEL_SERVIDOR}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
PYTHON="${PYTHON:-$(command -v python3)}"
LOCAL_PORT="${LOCAL_PORT:-8090}"

[ -f "${REPO}/run.py" ] || { echo "No encuentro run.py en ${REPO}"; exit 1; }
[ -f "${SSH_KEY}" ] || { echo "No existe la llave ${SSH_KEY}"; exit 1; }
env -u SSH_AUTH_SOCK ssh -o BatchMode=yes -o IdentitiesOnly=yes -i "${SSH_KEY}" -o ConnectTimeout=10 "${SERVER}" true \
  || { echo "La llave no entra a ${SERVER} sin contraseña; el túnel no podría reconectarse solo."; exit 1; }
"${PYTHON}" -c "import fastapi, curl_cffi" 2>/dev/null \
  || { echo "${PYTHON} no tiene las dependencias: ${PYTHON} -m pip install -r requirements.txt"; exit 1; }

mkdir -p "${AGENTS}" "${LOGS}"
unload

cat > "${AGENTS}/cl.glitchhunter.tunel.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>cl.glitchhunter.tunel</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/ssh</string><string>-N</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-o</string><string>IdentitiesOnly=yes</string>
    <string>-i</string><string>${SSH_KEY}</string>
    <string>-o</string><string>ServerAliveInterval=30</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-L</string><string>${LOCAL_PORT}:127.0.0.1:8080</string>
    <string>${SERVER}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>${LOGS}/glitchhunter-tunel.log</string>
  <key>StandardErrorPath</key><string>${LOGS}/glitchhunter-tunel.log</string>
</dict>
</plist>
PLIST

cat > "${AGENTS}/cl.glitchhunter.radar.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>cl.glitchhunter.radar</string>
  <key>ProgramArguments</key>
  <array><string>${PYTHON}</string><string>run.py</string><string>server</string></array>
  <key>WorkingDirectory</key><string>${REPO}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>${LOGS}/glitchhunter-radar.log</string>
  <key>StandardErrorPath</key><string>${LOGS}/glitchhunter-radar.log</string>
</dict>
</plist>
PLIST

launchctl bootstrap "gui/${UID_N}" "${AGENTS}/cl.glitchhunter.tunel.plist"
launchctl bootstrap "gui/${UID_N}" "${AGENTS}/cl.glitchhunter.radar.plist"

echo "Esperando a que respondan el radar y el túnel…"
for _ in $(seq 1 45); do
  if curl -fsS -m 3 "http://127.0.0.1:8080/api/status" >/dev/null 2>&1 \
     && curl -fsS -m 3 "http://127.0.0.1:${LOCAL_PORT}/api/status" >/dev/null 2>&1; then
    echo "Radar del PC y túnel funcionando ✅"
    echo "  Panel del servidor: http://localhost:${LOCAL_PORT}   Panel del PC: http://localhost:8080"
    echo "  Registros: ${LOGS}/glitchhunter-radar.log y glitchhunter-tunel.log"
    exit 0
  fi
  sleep 2
done
echo "No respondieron en 90 s. Revisa: tail -50 ${LOGS}/glitchhunter-radar.log ${LOGS}/glitchhunter-tunel.log"
exit 1

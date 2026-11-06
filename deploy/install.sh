#!/usr/bin/env bash
# =============================================================================
# GlitchHunter AI - Instalador para un servidor Ubuntu (Oracle Cloud u otro VPS)
#
# Instala el radar como servicio (systemd): arranca solo al encender la máquina
# y se reinicia si se cae. Se puede volver a correr para ACTUALIZAR (git pull +
# dependencias + reinicio) sin tocar tu base de datos ni tu .env.
#
# Uso (en el servidor):
#   curl -fsSL https://raw.githubusercontent.com/Pacortes2021/GlitchHunter_AI/main/deploy/install.sh | sudo bash
#
# Variables opcionales:
#   REPO_URL     repositorio a clonar   (por defecto el oficial)
#   BRANCH       rama                   (main)
#   INSTALL_DIR  código                 (/opt/glitchhunter)
#   DATA_DIR     base de datos          (/var/lib/glitchhunter)
#   SOURCE_DIR   usar una copia local en vez de clonar (pruebas)
#   NO_START=1   instalar sin arrancar el servicio
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Pacortes2021/GlitchHunter_AI.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/glitchhunter}"
DATA_DIR="${DATA_DIR:-/var/lib/glitchhunter}"
BACKUP_DIR="${DATA_DIR}/respaldos"
SERVICE_USER="glitchhunter"
SERVICE_NAME="glitchhunter"

log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Ejecuta con sudo:  sudo bash install.sh"
command -v apt-get >/dev/null || die "Este instalador es para Ubuntu/Debian."

log "Instalando paquetes del sistema (python, git, sqlite)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git sqlite3 ca-certificates curl >/dev/null

log "Usuario del servicio: ${SERVICE_USER}"
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

log "Código en ${INSTALL_DIR}"
if [ -n "${SOURCE_DIR:-}" ]; then
  mkdir -p "${INSTALL_DIR}"
  cp -a "${SOURCE_DIR}/." "${INSTALL_DIR}/"
elif [ -d "${INSTALL_DIR}/.git" ]; then
  git config --global --add safe.directory "${INSTALL_DIR}" || true
  git -C "${INSTALL_DIR}" fetch --quiet origin "${BRANCH}"
  git -C "${INSTALL_DIR}" checkout --quiet "${BRANCH}"
  git -C "${INSTALL_DIR}" reset --quiet --hard "origin/${BRANCH}"
else
  git clone --quiet --branch "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
fi

log "Entorno Python y dependencias"
if [ ! -x "${INSTALL_DIR}/venv/bin/python" ]; then
  python3 -m venv "${INSTALL_DIR}/venv"
fi
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

log "Datos en ${DATA_DIR}"
mkdir -p "${DATA_DIR}" "${BACKUP_DIR}"

ENV_FILE="${INSTALL_DIR}/.env"
if [ ! -f "${ENV_FILE}" ]; then
  log "Creando ${ENV_FILE} (configuración inicial)"
  cp "${INSTALL_DIR}/.env.example" "${ENV_FILE}"
  # Solo accesible desde la propia máquina: se entra por túnel SSH o Tailscale (ver la guía)
  sed -i 's/^WEB_HOST=.*/WEB_HOST=127.0.0.1/' "${ENV_FILE}"
  # Clave para las acciones del panel (escanear, simular, watchlist)
  KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  if grep -q '^API_AUTH_KEY=' "${ENV_FILE}"; then
    sed -i "s|^API_AUTH_KEY=.*|API_AUTH_KEY=${KEY}|" "${ENV_FILE}"
  else
    printf '\nAPI_AUTH_KEY=%s\n' "${KEY}" >> "${ENV_FILE}"
  fi
  CREATED_KEY="${KEY}"
fi
grep -q '^GLITCHHUNTER_DATA_DIR=' "${ENV_FILE}" || printf '\nGLITCHHUNTER_DATA_DIR=%s\n' "${DATA_DIR}" >> "${ENV_FILE}"

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}" "${DATA_DIR}"
chmod 600 "${ENV_FILE}"

log "Servicio systemd"
sed -e "s|@INSTALL_DIR@|${INSTALL_DIR}|g" -e "s|@DATA_DIR@|${DATA_DIR}|g" -e "s|@USER@|${SERVICE_USER}|g" \
  "${INSTALL_DIR}/deploy/glitchhunter.service" > "/etc/systemd/system/${SERVICE_NAME}.service"

log "Respaldo diario de la base de datos (04:15, se guardan 7 días)"
install -m 755 "${INSTALL_DIR}/deploy/backup.sh" /usr/local/bin/glitchhunter-backup
cat > /etc/cron.d/glitchhunter-backup <<EOF
15 4 * * * ${SERVICE_USER} DATA_DIR=${DATA_DIR} /usr/local/bin/glitchhunter-backup >/dev/null 2>&1
EOF

systemctl daemon-reload
systemctl enable --quiet "${SERVICE_NAME}"
if [ "${NO_START:-0}" != "1" ]; then
  systemctl restart "${SERVICE_NAME}"
  PORT="$(grep -E '^WEB_PORT=' "${ENV_FILE}" | tail -1 | cut -d= -f2 | tr -d ' "' || true)"
  PORT="${PORT:-8080}"
  log "Esperando a que el radar responda en el puerto ${PORT}…"
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${PORT}/api/status" >/dev/null 2>&1; then
      READY=1; break
    fi
    sleep 2
  done
  if [ "${READY:-0}" = "1" ]; then
    log "Radar funcionando ✅"
  else
    systemctl --no-pager status "${SERVICE_NAME}" | tail -20 || true
    die "El radar no respondió en 2 minutos. Revisa: journalctl -u ${SERVICE_NAME} -n 100"
  fi
fi

cat <<EOF

=====================================================================
 GlitchHunter instalado
   Código:        ${INSTALL_DIR}
   Base de datos: ${DATA_DIR}/glitchhunter.db
   Respaldos:     ${BACKUP_DIR} (diarios, 7 días)
   Configuración: ${ENV_FILE}

 Comandos útiles:
   Ver el registro en vivo:  sudo journalctl -u ${SERVICE_NAME} -f
   Reiniciar:                sudo systemctl restart ${SERVICE_NAME}
   Detener:                  sudo systemctl stop ${SERVICE_NAME}
   Actualizar:               sudo bash ${INSTALL_DIR}/deploy/install.sh
=====================================================================
EOF
if [ -n "${CREATED_KEY:-}" ]; then
  echo " Clave del panel (API_AUTH_KEY), guárdala:  ${CREATED_KEY}"
  echo " (la pide el panel solo para escanear, simular o editar la watchlist)"
fi

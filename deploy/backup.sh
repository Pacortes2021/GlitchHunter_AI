#!/usr/bin/env bash
# Respaldo en caliente de la base de datos (sqlite3 .backup es seguro con el radar corriendo).
# Guarda un archivo comprimido por día y borra los de más de 7 días.
set -euo pipefail
DATA_DIR="${DATA_DIR:-/var/lib/glitchhunter}"
DB="${DATA_DIR}/glitchhunter.db"
DEST="${DATA_DIR}/respaldos"
KEEP_DAYS="${KEEP_DAYS:-7}"

[ -f "${DB}" ] || { echo "No existe ${DB}"; exit 0; }
mkdir -p "${DEST}"
OUT="${DEST}/glitchhunter-$(date +%Y%m%d-%H%M%S).db"
sqlite3 -cmd ".timeout 30000" "${DB}" ".backup '${OUT}'"
gzip -f "${OUT}"
find "${DEST}" -name 'glitchhunter-*.db.gz' -mtime +"${KEEP_DAYS}" -delete
echo "Respaldo listo: ${OUT}.gz"

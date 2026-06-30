#!/bin/bash
# Watcher для кнопки «Синхронизировать сейчас».
# Запускается launchd раз в ~60 секунд. Опрашивает api/sync.php на хостинге;
# если выставлен флаг requested=true — запускает полный прогон коннектора
# и снимает флаг.
#
# Переменные берутся из окружения (заданы в launchd plist):
#   CONNECTOR_TOKEN  — общий секрет (X-Connector-Token)
#   CONFIG_URL       — https://ХОСТ/b24-admin/api/config.php
#   STATUS_URL       — https://ХОСТ/b24-admin/api/status.php
#   SYNC_URL         — https://ХОСТ/b24-admin/api/sync.php
#   CONNECTOR_DIR    — каталог b24-connector
#   PYTHON           — путь к python3 (по умолчанию /usr/bin/python3)

set -euo pipefail

PYTHON="${PYTHON:-/usr/bin/python3}"
CONNECTOR_DIR="${CONNECTOR_DIR:-/Users/prise4you/b24-connector}"

# 1. Узнаём, есть ли запрос на синхронизацию
state="$(curl -fsS -H "X-Connector-Token: ${CONNECTOR_TOKEN}" "${SYNC_URL}" || echo '{}')"
requested="$(printf '%s' "$state" | "$PYTHON" -c 'import sys,json;
try: print(json.load(sys.stdin).get("requested"))
except Exception: print("None")')"

if [ "$requested" != "True" ]; then
  exit 0
fi

echo "[$(date '+%F %T')] sync_watcher: получен запрос на синхронизацию"

# 2. Сразу снимаем флаг, чтобы не запускать прогон дважды
curl -fsS -X POST -H "X-Connector-Token: ${CONNECTOR_TOKEN}" \
     -H "Content-Type: application/json" \
     -d '{"action":"clear"}' "${SYNC_URL}" >/dev/null || true

# 3. Полный прогон
cd "$CONNECTOR_DIR"
"$PYTHON" connector.py \
  --config-url "${CONFIG_URL}" \
  --status-url "${STATUS_URL}" \
  --connector-token "${CONNECTOR_TOKEN}"

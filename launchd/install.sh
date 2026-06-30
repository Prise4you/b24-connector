#!/bin/bash
# Установка автопополнения через launchd (Продукт A).
# 1) Регистрирует offline-события в Bitrix24
# 2) Ставит launchd-агент, который каждые 10 мин опрашивает очередь
set -e

DIR="/Users/prise4you/b24-connector"
PLIST="$DIR/launchd/com.anit.b24kb.poll.plist"
DEST="$HOME/Library/LaunchAgents/com.anit.b24kb.poll.plist"

echo "1. Регистрирую offline-события в Bitrix24..."
cd "$DIR"
python3 connector.py --bind

echo "2. Создаю logs/..."
mkdir -p "$DIR/logs"

echo "3. Устанавливаю launchd-агент..."
cp "$PLIST" "$DEST"
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"

echo "✅ Готово. Агент com.anit.b24kb.poll запускается каждые 10 минут."
echo "   Логи: $DIR/logs/poll.out.log"
echo "   Остановить: launchctl unload $DEST"

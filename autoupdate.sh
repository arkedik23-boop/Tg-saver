#!/usr/bin/env bash
# Проверяет GitHub и, если есть новые коммиты, обновляет бота и перезапускает.
cd /opt/tg_saver || exit 0
git fetch --quiet origin main || exit 0
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" != "$REMOTE" ]; then
    git pull --quiet
    systemctl restart tgsaver
    logger -t tgsaver-update "updated $LOCAL -> $REMOTE, restarted"
fi

#!/usr/bin/env bash
# Установка модуля исчезающих фото/видео (юзербот Telethon) как systemd-сервиса.
# Запускать от root из /opt/tg_saver:  bash install_ttl.sh
set -e
APP="$(cd "$(dirname "$0")" && pwd)"
cd "$APP"

# .env уже есть от основного бота (BOT_TOKEN, ADMIN_ID). Дописываем ключи Telethon.
touch .env
if ! grep -q '^API_ID=' .env; then
  read -rp "API_ID (с my.telegram.org): " AID </dev/tty
  read -rp "API_HASH: " AH </dev/tty
  printf 'API_ID=%s\nAPI_HASH=%s\n' "$AID" "$AH" >> .env
fi
if ! grep -q '^SESSIONS=' .env; then
  read -rp "Имена сессий через запятую (напр. acc1,acc2): " S </dev/tty
  printf 'SESSIONS=%s\n' "$S" >> .env
fi
chmod 600 .env

cat > /etc/systemd/system/tgttl.service <<EOF
[Unit]
Description=TG Saver — disappearing media (userbot)
After=network-online.target

[Service]
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
ExecStart=$APP/venv/bin/python userbot_ttl.py run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable tgttl >/dev/null 2>&1 || true
echo
echo "Готово. Теперь ВОЙДИ в каждый аккаунт (один раз):"
grep '^SESSIONS=' .env | cut -d= -f2 | tr ',' '\n' | while read -r s; do
  [ -n "$s" ] && echo "  set -a; . ./.env; set +a; venv/bin/python userbot_ttl.py login $s"
done
echo
echo "После всех входов запусти:  systemctl start tgttl"

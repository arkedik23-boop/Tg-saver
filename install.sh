#!/usr/bin/env bash
# Установка TG Saver как systemd-сервиса. Запускать от root из папки проекта:
#   bash install.sh
set -e
APP="$(cd "$(dirname "$0")" && pwd)"
cd "$APP"

apt-get update -y
apt-get install -y python3-venv git

python3 -m venv venv
venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  read -rsp "BOT_TOKEN (ввод скрыт): " T </dev/tty; echo
  read -rp "ADMIN_ID (твой числовой id): " A </dev/tty
  printf 'BOT_TOKEN=%s\nADMIN_ID=%s\n' "$T" "$A" > .env
  chmod 600 .env
fi

cat > /etc/systemd/system/tgsaver.service <<EOF
[Unit]
Description=TG Saver
After=network-online.target

[Service]
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
ExecStart=$APP/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now tgsaver
sleep 4
systemctl is-active tgsaver
journalctl -u tgsaver -n 15 --no-pager

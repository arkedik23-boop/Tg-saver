#!/usr/bin/env bash
# Ставит автообновление бота из GitHub каждые 2 минуты. Запускать от root:
#   bash /opt/tg_saver/install_autoupdate.sh
set -e
chmod +x /opt/tg_saver/autoupdate.sh

cat > /etc/systemd/system/tgsaver-update.service <<'EOF'
[Unit]
Description=TG Saver auto-update from GitHub

[Service]
Type=oneshot
ExecStart=/bin/bash /opt/tg_saver/autoupdate.sh
EOF

cat > /etc/systemd/system/tgsaver-update.timer <<'EOF'
[Unit]
Description=Run TG Saver auto-update every 2 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now tgsaver-update.timer
echo "Автообновление включено. Проверка каждые 2 минуты."
systemctl list-timers tgsaver-update.timer --no-pager

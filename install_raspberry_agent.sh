#!/usr/bin/env bash
set -euo pipefail
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/zarzis"
ENV_DIR="/etc/zarzis"
ENV_FILE="$ENV_DIR/agent.env"
SERVICE_FILE="/etc/systemd/system/zarzis-agent.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "Relance avec: sudo bash install_raspberry_agent.sh"
  exit 1
fi

if id zarzis >/dev/null 2>&1; then
  RUN_USER="zarzis"
elif [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
  RUN_USER="$SUDO_USER"
else
  RUN_USER="root"
fi

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3 python3-venv python3-pip ca-certificates
fi

mkdir -p "$APP_DIR" "$ENV_DIR"
cp "$SRC_DIR/zarzis_edge_agent.py" "$APP_DIR/zarzis_edge_agent.py"
cp "$SRC_DIR/requirements.txt" "$APP_DIR/requirements.txt"
cp "$SRC_DIR/terrain_check.py" "$APP_DIR/terrain_check.py"
cp "$SRC_DIR/stop_all_cloud.py" "$APP_DIR/stop_all_cloud.py"

if [ ! -f "$ENV_FILE" ]; then
  cp "$SRC_DIR/agent.env.terrain" "$ENV_FILE"
  echo "[INFO] Cree: $ENV_FILE"
  echo "[ACTION] Edite API_TOKEN et DR302_HOST si necessaire: sudo nano $ENV_FILE"
else
  cp "$ENV_FILE" "$ENV_FILE.backup.$(date +%Y%m%d-%H%M%S)"
  echo "[INFO] $ENV_FILE existe deja, conserve. Sauvegarde creee."
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR" || true

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Zarzis Edge Agent HTTP Push
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/zarzis_edge_agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable zarzis-agent
systemctl restart zarzis-agent

echo "[OK] Agent installe. Logs: journalctl -u zarzis-agent -f"
echo "[OK] Diagnostic: /opt/zarzis/.venv/bin/python /opt/zarzis/terrain_check.py"

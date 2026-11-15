#!/usr/bin/env bash
# One-time VPS setup (Ubuntu 22.04+). Run as root from the repo root:
#   EARSHOT_DOMAIN=earshot.example.com sudo -E bash deploy/install.sh
set -euo pipefail

ENV_FILE="${EARSHOT_ENV_FILE:-/etc/earshot.env}"
TURN_CONF="${EARSHOT_TURN_CONF:-/etc/turnserver.conf}"
CADDY_CONF="${EARSHOT_CADDY_CONF:-/etc/caddy/Caddyfile}"

apt-get update && apt-get install -y python3-venv coturn caddy npm openssl
id -u earshot &>/dev/null || useradd -r -m -d /opt/earshot earshot
rsync -a --exclude .git --exclude '*.env' . /opt/earshot/
cd /opt/earshot
python3 -m venv .venv && .venv/bin/pip install -r server/requirements.txt -r sim/requirements.txt
(cd client && npm install && npm run build)

# Bootstrap /etc/earshot.env outside git if missing (preserves existing rotated secrets)
if [ ! -f "$ENV_FILE" ]; then
    DOMAIN="${EARSHOT_DOMAIN:-earshot.example.com}"
    TURN_HOST="${EARSHOT_TURN_HOST:-turn:${DOMAIN}:3478}"
    TURN_SECRET="${EARSHOT_TURN_SECRET:-$(openssl rand -hex 32)}"
    (
        umask 077
        cat > "$ENV_FILE" <<EOF
EARSHOT_DOMAIN=${DOMAIN}
EARSHOT_TURN_HOST=${TURN_HOST}
EARSHOT_TURN_SECRET=${TURN_SECRET}
EOF
    )
    chown root:root "$ENV_FILE"
    chmod 600 "$ENV_FILE"
fi

# Render /etc/turnserver.conf from deploy/coturn.conf + /etc/earshot.env without exposing secrets on argv
python3 - "$ENV_FILE" deploy/coturn.conf "$TURN_CONF" <<'PY'
import pathlib, sys

env_path, tpl_path, out_path = map(pathlib.Path, sys.argv[1:4])
env = {}
for raw in env_path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip().strip("'\"")

secret = env.get("EARSHOT_TURN_SECRET", "")
domain = env.get("EARSHOT_DOMAIN", "earshot.example.com")
if not secret or secret.startswith("__") or secret == "CHANGE_ME_SHARED_SECRET":
    raise SystemExit(f"Refusing to render {out_path}: invalid EARSHOT_TURN_SECRET in {env_path}")

rendered = (
    tpl_path.read_text(encoding="utf-8")
    .replace("__EARSHOT_TURN_SECRET__", secret)
    .replace("__EARSHOT_DOMAIN__", domain)
)
out_path.write_text(rendered, encoding="utf-8")
PY

if getent group turnserver >/dev/null 2>&1; then
    chown root:turnserver "$TURN_CONF"
    chmod 640 "$TURN_CONF"
else
    chown root:root "$TURN_CONF"
    chmod 600 "$TURN_CONF"
fi

DOMAIN_FROM_ENV="$(awk -F= '/^EARSHOT_DOMAIN=/{print $2}' "$ENV_FILE" | tail -n1)"
sed "s/earshot\.example\.com/${DOMAIN_FROM_ENV:-earshot.example.com}/g" deploy/Caddyfile > "$CADDY_CONF"
cp deploy/earshot-coordinator.service /etc/systemd/system/
chown -R earshot:earshot /opt/earshot
systemctl daemon-reload
systemctl enable --now earshot-coordinator coturn caddy
echo "Done. Verify TURN credentials via: curl -fsS http://127.0.0.1:8700/ice"

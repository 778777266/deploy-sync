#!/usr/bin/env bash
set -euo pipefail

# ===== config =====
APP_DIR="${APP_DIR:-/opt/deploy-sync}"
SERVICE_NAME="${SERVICE_NAME:-deploy-sync}"
APP_PORT="${APP_PORT:-8000}"
APP_USER="${APP_USER:-fastapi}"
TOKEN_FILE="${TOKEN_FILE:-/root/deploy-sync-upload-token.txt}"

DOMAIN=""

usage() {
  echo "Usage: $0 --domain <your.domain>"
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: Please run as root (sudo)."
    exit 1
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --domain)
        DOMAIN="${2:-}"
        shift 2
        ;;
      *)
        echo "Unknown arg: $1"
        usage
        exit 1
        ;;
    esac
  done

  if [[ -z "${DOMAIN}" ]]; then
    echo "ERROR: --domain is required"
    usage
    exit 1
  fi
}

install_packages() {
  echo "==> Install system packages..."
  apt update -y
  apt install -y \
    ca-certificates \
    curl \
    git \
    nginx \
    python3 \
    python3-venv \
    python3-pip \
    ufw \
    openssl \
    iproute2 \
    certbot \
    python3-certbot-nginx
}

configure_firewall() {
  echo "==> Configure firewall (allow 22/80/443)..."
  ufw --force reset
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow 22/tcp
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable
}

create_app_user() {
  echo "==> Create service user..."
  if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
  fi
}

ensure_repo_files() {
  echo "==> Check repo files..."
  if [[ ! -f "${APP_DIR}/main.py" ]]; then
    echo "ERROR: ${APP_DIR}/main.py not found"
    exit 1
  fi
  if [[ ! -f "${APP_DIR}/requirements.txt" ]]; then
    echo "ERROR: ${APP_DIR}/requirements.txt not found"
    exit 1
  fi
}

setup_venv_and_deps() {
  echo "==> Create venv and install deps from requirements.txt..."
  mkdir -p "${APP_DIR}"
  chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

  sudo -u "${APP_USER}" -H bash -lc "python3 -m venv '${APP_DIR}/.venv'"
  sudo -u "${APP_USER}" -H bash -lc "source '${APP_DIR}/.venv/bin/activate' && python -m pip install --upgrade pip wheel && pip install -r '${APP_DIR}/requirements.txt'"
}

create_systemd_service() {
  echo "==> Create systemd service..."
  cat >/etc/systemd/system/${SERVICE_NAME}.service <<SERVICEEOF
[Unit]
Description=FastAPI app (${SERVICE_NAME})
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment="PATH=${APP_DIR}/.venv/bin"
ExecStart=${APP_DIR}/.venv/bin/uvicorn main:app --host 127.0.0.1 --port ${APP_PORT}
Restart=always
RestartSec=2
User=${APP_USER}
Group=${APP_USER}

[Install]
WantedBy=multi-user.target
SERVICEEOF

  systemctl daemon-reload
  systemctl enable --now "${SERVICE_NAME}.service"
}

rotate_token_and_set_env() {
  echo "==> Rotate UPLOAD_TOKEN and set systemd environment..."
  local token
  token="$(openssl rand -hex 32)"

  umask 077
  printf "%s\n" "${token}" > "${TOKEN_FILE}"

  mkdir -p "/etc/systemd/system/${SERVICE_NAME}.service.d"
  cat > "/etc/systemd/system/${SERVICE_NAME}.service.d/10-env.conf" <<EOF
[Service]
Environment="UPLOAD_TOKEN=${token}"
EOF

  systemctl daemon-reload
  systemctl restart "${SERVICE_NAME}.service"

  echo "==> UPLOAD_TOKEN: ${token}"
  echo "==> UPLOAD_TOKEN file: ${TOKEN_FILE}"
}

health_check_local() {
  echo "==> Wait for backend to listen on 127.0.0.1:${APP_PORT} (up to 30s)..."
  for _ in $(seq 1 30); do
    if ss -lnt "( sport = :${APP_PORT} )" | grep -q LISTEN; then
      break
    fi
    sleep 1
  done

  echo "==> Health check: http://127.0.0.1:${APP_PORT}/docs"
  curl -fsS -o /dev/null "http://127.0.0.1:${APP_PORT}/docs"
  echo "==> Backend is healthy."
}

issue_cert_http01_no_email() {
  echo "==> Stop nginx for initial cert issuance (standalone on 80)..."
  systemctl stop nginx || true

  if [[ -d "/etc/letsencrypt/live/${DOMAIN}" ]]; then
    echo "==> Existing certificate found for ${DOMAIN}, skip initial issuance."
    return
  fi

  echo "==> Issue Let's Encrypt cert via HTTP-01 (standalone, no email)..."
  certbot certonly \
    --standalone \
    --preferred-challenges http-01 \
    --agree-tos \
    --non-interactive \
    --register-unsafely-without-email \
    -d "${DOMAIN}"
}

configure_nginx() {
  echo "==> Configure nginx (80 redirect + 443 reverse proxy)..."
  rm -f /etc/nginx/sites-enabled/default || true

  cat >/etc/nginx/sites-available/${SERVICE_NAME} <<NGINXEOF
server {
    listen 80;
    server_name ${DOMAIN};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name ${DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    client_max_body_size 50m;
    proxy_read_timeout 300;
    proxy_send_timeout 300;

    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
NGINXEOF

  ln -sf /etc/nginx/sites-available/${SERVICE_NAME} /etc/nginx/sites-enabled/${SERVICE_NAME}
  nginx -t
  systemctl enable --now nginx
  systemctl reload nginx
}

fix_renew_to_nginx_plugin() {
  echo "==> Configure renewal to use nginx plugin (so it won't bind port 80)..."
  certbot renew --dry-run --nginx
}

final_notes() {
  echo ""
  echo "✅ Done!"
  echo "App:   systemctl status ${SERVICE_NAME}"
  echo "Nginx: systemctl status nginx"
  echo "UFW:   ufw status"
  echo "Open:  https://${DOMAIN}/docs"
  echo ""
  echo "UPLOAD_TOKEN file: ${TOKEN_FILE}"
  echo ""
  echo "UPLOAD_TOKEN (print again at end):"
  cat "${TOKEN_FILE}"
  echo ""
}

main() {
  require_root
  parse_args "$@"
  install_packages
  configure_firewall
  create_app_user
  ensure_repo_files
  setup_venv_and_deps
  create_systemd_service
  rotate_token_and_set_env
  health_check_local
  issue_cert_http01_no_email
  configure_nginx
  fix_renew_to_nginx_plugin
  final_notes
}

main "$@"

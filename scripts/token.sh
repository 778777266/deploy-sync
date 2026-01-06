#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="deploy-sync"
TOKEN_FILE="/root/deploy-sync-upload-token.txt"
DROPIN_DIR="/etc/systemd/system/${SERVICE_NAME}.service.d"
DROPIN_FILE="${DROPIN_DIR}/10-env.conf"

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: Please run as root (sudo)."
    exit 1
  fi
}

cmd="${1:-}"

print_token() {
  if [[ -f "${TOKEN_FILE}" ]]; then
    sudo cat "${TOKEN_FILE}"
  else
    echo "ERROR: token file not found: ${TOKEN_FILE}"
    exit 1
  fi
}

rotate_token() {
  local token
  token="$(openssl rand -hex 32)"

  umask 077
  printf "%s\n" "${token}" > "${TOKEN_FILE}"

  mkdir -p "${DROPIN_DIR}"
  cat > "${DROPIN_FILE}" <<EOF
[Service]
Environment="UPLOAD_TOKEN=${token}"
EOF

  systemctl daemon-reload
  systemctl restart "${SERVICE_NAME}.service"

  echo "${token}"
}

main() {
  require_root

  case "${cmd}" in
    print)
      print_token
      ;;
    rotate)
      rotate_token
      ;;
    *)
      echo "Usage: $0 {print|rotate}"
      exit 1
      ;;
  esac
}

main "$@"

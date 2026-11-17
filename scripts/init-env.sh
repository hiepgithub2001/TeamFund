#!/usr/bin/env bash
# Creates .env with fresh secrets. Usage:
#   scripts/init-env.sh <master bot token> <your telegram id> [public host]
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  echo ".env already exists; edit it instead (or delete it to start over)." >&2
  exit 1
fi

TOKEN="${1:-}"
ADMIN_ID="${2:-}"
HOST="${3:-teamfund.103-90-225-28.sslip.io}"
rand() { python3 -c "import secrets;print(secrets.token_hex($1))"; }
fernet() { python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }

umask 077
cat > .env <<EOF
# Public hostname that points to this server (HTTPS on port 443)
PUBLIC_HOST=${HOST}

# Master bot from @BotFather, and the Telegram user IDs who approve new teams (comma separated)
MASTER_BOT_TOKEN=${TOKEN}
MASTER_ADMIN_IDS=${ADMIN_ID}

# Default language for new teams and group messages: vi or en
DEFAULT_LANG=vi
TEAM_TIMEZONE=Asia/Ho_Chi_Minh

# webhook (needs HTTPS) or polling (for testing without HTTPS)
TELEGRAM_MODE=webhook

# Generated secrets. Keep TOKEN_ENC_KEY safe: it decrypts the stored team bot tokens.
POSTGRES_PASSWORD=$(rand 24)
MASTER_WEBHOOK_SECRET=$(rand 32)
TOKEN_ENC_KEY=$(fernet)
EOF
echo "Wrote .env"
[[ -z "$TOKEN" || -z "$ADMIN_ID" ]] && echo "Fill in MASTER_BOT_TOKEN and MASTER_ADMIN_IDS in .env before starting."
exit 0

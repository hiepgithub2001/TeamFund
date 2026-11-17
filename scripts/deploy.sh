#!/usr/bin/env bash
# Update the running app to the latest code on GitHub. Run on the VPS from the project folder.
#   scripts/deploy.sh            deploy origin/main
#   scripts/deploy.sh <git-ref>  deploy a specific commit or tag (rollback)
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "No .env. Run scripts/init-env.sh first." >&2; exit 1; }

if [[ $# -gt 0 ]]; then
  git fetch --tags origin
  git checkout --detach "$1"
else
  git checkout main
  git pull --ff-only origin main
fi
echo "Deploying $(git rev-parse --short HEAD): $(git log -1 --format=%s)"

docker compose build --pull
docker compose up -d --remove-orphans
docker image prune -f >/dev/null

echo "Waiting for the public health check…"
host=$(grep -E '^PUBLIC_HOST=' .env | cut -d= -f2)
for _ in $(seq 1 30); do
  if curl -fsS --max-time 5 "https://${host}/health" >/dev/null 2>&1; then
    echo "OK: https://${host}/health"
    docker compose ps
    exit 0
  fi
  sleep 2
done
echo "Health check failed. Check: docker compose logs --tail=100 caddy gateway core" >&2
docker compose ps
exit 1

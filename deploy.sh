#!/usr/bin/env bash
#
# deploy.sh — script de déploiement local sur le VPS.
#
# Enchaîne : git pull → docker compose down → build → up -d → tail des logs.
# À lancer depuis la racine du repo, sur le serveur de prod.
#
# Usage : ./deploy.sh [--no-pull] [--logs]

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

PULL=1
TAIL_LOGS=0
for arg in "$@"; do
  case "$arg" in
    --no-pull) PULL=0 ;;
    --logs)    TAIL_LOGS=1 ;;
    -h|--help)
      echo "Usage: $0 [--no-pull] [--logs]"
      exit 0
      ;;
    *)
      echo "Argument inconnu : $arg" >&2
      exit 2
      ;;
  esac
done

log() { printf '\033[1;34m▶\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m✓\033[0m %s\n' "$*"; }

# 1. Pré-vol : .env présent ?
if [[ ! -f .env ]]; then
  echo "✗ .env introuvable. Copie .env.example et renseigne ALBERT_API_KEY." >&2
  exit 1
fi

# 2. Pull
if [[ $PULL -eq 1 ]]; then
  log "git fetch + pull"
  git fetch --all --prune
  # On force le pull sur la branche courante depuis origin/<branche>.
  branch="$(git rev-parse --abbrev-ref HEAD)"
  git pull --ff-only origin "$branch"
  ok "code à jour ($(git rev-parse --short HEAD))"
else
  log "git pull ignoré (--no-pull)"
fi

# 3. Stop
log "docker compose down"
docker compose down

# 4. Build
log "docker compose build"
docker compose build

# 5. Up
log "docker compose up -d"
docker compose up -d

# 6. Statut
sleep 2
log "état des conteneurs"
docker compose ps

ok "déploiement terminé"

if [[ $TAIL_LOGS -eq 1 ]]; then
  log "tail des logs (Ctrl+C pour quitter)"
  docker compose logs -f
fi

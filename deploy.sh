#!/usr/bin/env bash
#
# deploy.sh — script de déploiement local sur le VPS.
#
# Enchaîne : git pull → docker compose down → build → (optionnel) génération
#            variations → up -d → tail des logs.
# À lancer depuis la racine du repo, sur le serveur de prod.
#
# Usage : ./deploy.sh [--no-pull] [--logs] [--generate-variations]
#
# Flags :
#   --no-pull               saute le git pull (utile en test local)
#   --logs                  tail les logs après le déploiement
#   --generate-variations   lance scripts/generate_variations.py dans un
#                           conteneur éphémère (docker compose run --rm).
#                           Nécessite ANTHROPIC_API_KEY dans .env — passée
#                           automatiquement via env_file.
#
# Schéma DB :
#   Les évolutions de schéma sont prises en charge automatiquement par
#   ``app.core.db.init_db()`` au démarrage du conteneur (cf. PR #48).
#   Pour les ajouts de colonnes, une migration additive ``ALTER TABLE``
#   est appliquée sans perte de données. Pour les divergences non
#   additives (rename, drop, type change), l'app retombe sur un drop &
#   recharge complet. Aucun flag à passer ici pour ça.
#
#   Si tu veux vraiment forcer un reset complet de la DB pour une raison
#   exceptionnelle (corruption, reset volontaire pour démo, etc.), fais-le
#   explicitement à la main AVANT le deploy :
#     docker compose down && rm -f data/app.db data/app.db-wal data/app.db-shm

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

PULL=1
TAIL_LOGS=0
GENERATE_VARIATIONS=0
for arg in "$@"; do
  case "$arg" in
    --no-pull)              PULL=0 ;;
    --logs)                 TAIL_LOGS=1 ;;
    --generate-variations)  GENERATE_VARIATIONS=1 ;;
    -h|--help)
      sed -n '3,31p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Argument inconnu : $arg" >&2
      exit 2
      ;;
  esac
done

log()  { printf '\033[1;34m▶\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m⚠\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; }

# 1. Pré-vol : .env présent ?
if [[ ! -f .env ]]; then
  err ".env introuvable. Copie .env.example et renseigne ALBERT_API_KEY."
  exit 1
fi

# 1bis. UID/GID du user hôte → injectés dans le build du conteneur pour
# que le user runtime du conteneur puisse écrire dans le bind-mount data/.
# Cf. Dockerfile (ARG UID / GID) et docker-compose.yml (build.args).
export APP_UID="$(id -u)"
export APP_GID="$(id -g)"

# 1ter. data/ et content/ writable par l'utilisateur courant ?
# Sans ça, le git pull ci-dessous peut échouer ("Permission denied") si des
# fichiers ont été créés en root par un ancien conteneur sans Dockerfile USER.
# On teste récursivement : content/ reçoit les variations générées par
# `docker compose run --rm app python -m scripts.generate_variations`, et
# data/ contient la SQLite runtime.
for dir in data content; do
  [[ -d "$dir" ]] || continue
  first_bad="$(find "$dir" -not -writable -print -quit 2>/dev/null || true)"
  if [[ -n "$first_bad" ]]; then
    warn "droits insuffisants sur $dir/ (ex: $first_bad)"
    if command -v sudo >/dev/null 2>&1; then
      log "tentative de chown -R $(id -un):$(id -gn) $dir/"
      sudo chown -R "$(id -u):$(id -g)" "$dir" || {
        err "chown a échoué. Lance manuellement :"
        err "  sudo chown -R \$(id -u):\$(id -g) $dir/"
        exit 1
      }
      ok "$dir/ rebasculé sur $(id -un):$(id -gn)"
    else
      err "sudo indisponible. Corrige les droits manuellement :"
      err "  chown -R \$(id -u):\$(id -g) $dir/"
      exit 1
    fi
  fi
done

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
# La gestion du schéma DB (migration additive ou drop legacy en dernier
# recours) se fait côté Python dans ``app.core.db.init_db()`` au démarrage
# du conteneur. Pas de pré-check shell ici : l'ancien pré-check était
# hardcodé sur la table ``subject`` uniquement, exigeait ``sqlite3``
# installé sur l'hôte (ce qui n'est pas garanti), et faisait silencieusement
# doublon avec le drop automatique de ``init_db()``. Voir PR #48.
log "docker compose down"
docker compose down

# 4. Build
log "docker compose build"
docker compose build

# 5. (Optionnel) Génération des variations via Opus, dans un conteneur éphémère
#
# On utilise `docker compose run --rm` plutôt qu'une venv hôte :
#   - pas besoin d'installer Python + anthropic sur le VPS
#   - le conteneur éphémère hérite de env_file: .env, donc ANTHROPIC_API_KEY
#     est injectée automatiquement (cf. docker-compose.yml)
#   - les volumes data/ et content/ sont montés pareil que le service principal,
#     donc les JSON atterrissent sur l'hôte dans
#     content/histoire-geo-emc/subjects/variations/.
#
# Important : cette étape tourne APRÈS le build (pour avoir la dernière version
# de scripts/generate_variations.py dans l'image) mais AVANT le up final
# (inutile d'avoir l'app en ligne pendant la génération).
if [[ $GENERATE_VARIATIONS -eq 1 ]]; then
  log "génération des variations (Opus, conteneur éphémère)"
  # --no-TTY pour que ça marche aussi en SSH non-interactif / CI.
  # On surcharge le CMD par la commande python souhaitée.
  docker compose run --rm --no-TTY app \
    python -m scripts.generate_variations
  ok "variations à jour dans content/histoire-geo-emc/subjects/variations/"
fi

# 6. Up
log "docker compose up -d"
docker compose up -d

# 7. Statut
sleep 2
log "état des conteneurs"
docker compose ps

ok "déploiement terminé"

if [[ $TAIL_LOGS -eq 1 ]]; then
  log "tail des logs (Ctrl+C pour quitter)"
  docker compose logs -f
fi

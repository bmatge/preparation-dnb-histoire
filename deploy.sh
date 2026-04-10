#!/usr/bin/env bash
#
# deploy.sh — script de déploiement local sur le VPS.
#
# Enchaîne : git pull → pré-check schéma DB → docker compose down → build
#            → (optionnel) génération variations → up -d → tail des logs.
# À lancer depuis la racine du repo, sur le serveur de prod.
#
# Usage : ./deploy.sh [--no-pull] [--logs] [--generate-variations] [--reset-db]
#
# Flags :
#   --no-pull               saute le git pull (utile en test local)
#   --logs                  tail les logs après le déploiement
#   --generate-variations   lance scripts/generate_variations.py dans un
#                           conteneur éphémère (docker compose run --rm).
#                           Nécessite ANTHROPIC_API_KEY dans .env — passée
#                           automatiquement via env_file.
#   --reset-db              supprime data/app.db avant de redémarrer. À utiliser
#                           quand le schéma Subject a changé (cf. HANDOFF §DB :
#                           pas de migrations Alembic, on assume les drops).
#                           DESTRUCTIF : perd les sessions élèves en cours.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

PULL=1
TAIL_LOGS=0
GENERATE_VARIATIONS=0
RESET_DB=0
for arg in "$@"; do
  case "$arg" in
    --no-pull)              PULL=0 ;;
    --logs)                 TAIL_LOGS=1 ;;
    --generate-variations)  GENERATE_VARIATIONS=1 ;;
    --reset-db)             RESET_DB=1 ;;
    -h|--help)
      sed -n '3,22p' "$0" | sed 's/^# \{0,1\}//'
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

# 3. Pré-check schéma DB — détecte la dérive avant de redémarrer
#
# L'app n'a pas de migrations Alembic (cf. app/db.py §Modèle). Si on ajoute une
# colonne au modèle Subject et qu'on redéploie sans dropper data/app.db, le
# conteneur va crasher en boucle sur un SELECT qui référence la nouvelle
# colonne. On détecte ça ici et on abort proprement (ou on drop si --reset-db).
#
# La liste des colonnes attendues est maintenue à la main. À mettre à jour
# quand le modèle Subject évolue.
EXPECTED_SUBJECT_COLS=(
  id source_file dc_index year serie session session_label discipline theme
  consigne verbe_cle bornes_chrono bornes_spatiales notions_attendues_json
  bareme_points is_variation
)

if [[ -f data/app.db ]]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    log "pré-check schéma DB (data/app.db)"
    existing_cols="$(sqlite3 data/app.db "PRAGMA table_info(subject);" 2>/dev/null | cut -d'|' -f2 || true)"
    missing=()
    for col in "${EXPECTED_SUBJECT_COLS[@]}"; do
      if ! grep -qx "$col" <<<"$existing_cols"; then
        missing+=("$col")
      fi
    done
    if (( ${#missing[@]} > 0 )); then
      warn "schéma Subject obsolète — colonne(s) manquante(s) : ${missing[*]}"
      if [[ $RESET_DB -eq 1 ]]; then
        warn "--reset-db : suppression de data/app.db (sessions perdues)"
        rm -f data/app.db data/app.db-journal
        ok "DB supprimée, sera recréée au démarrage"
      else
        err "Refuse de déployer avec un schéma incompatible."
        err "Relance avec --reset-db pour dropper data/app.db (destructif)."
        exit 1
      fi
    else
      ok "schéma DB à jour"
    fi
  else
    warn "sqlite3 non installé, pré-check schéma sauté"
  fi
elif [[ $RESET_DB -eq 1 ]]; then
  log "--reset-db sans DB existante, rien à supprimer"
fi

# 4. Stop
log "docker compose down"
docker compose down

# 5. Build
log "docker compose build"
docker compose build

# 6. (Optionnel) Génération des variations via Opus, dans un conteneur éphémère
#
# On utilise `docker compose run --rm` plutôt qu'une venv hôte :
#   - pas besoin d'installer Python + anthropic sur le VPS
#   - le conteneur éphémère hérite de env_file: .env, donc ANTHROPIC_API_KEY
#     est injectée automatiquement (cf. docker-compose.yml)
#   - les volumes data/ sont montés pareil que le service principal, donc les
#     JSON atterrissent sur l'hôte dans data/subjects/variations/.
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
  ok "variations à jour dans data/subjects/variations/"
fi

# 7. Up
log "docker compose up -d"
docker compose up -d

# 8. Statut
sleep 2
log "état des conteneurs"
docker compose ps

ok "déploiement terminé"

if [[ $TAIL_LOGS -eq 1 ]]; then
  log "tail des logs (Ctrl+C pour quitter)"
  docker compose logs -f
fi

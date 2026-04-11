#!/usr/bin/env bash
#
# Synchronise les annales DNB maths + corrigés depuis la bibliothèque locale
# vers content/mathematiques/annales/. Le contenu de ce dossier est gitignoré
# (cf .gitignore et content/mathematiques/annales/README.md).
#
# Usage : ./scripts/sync_math_annales.sh
#

set -euo pipefail

SRC_ROOT="${SRC_ROOT:-$HOME/Documents/Projets/RevisionDNB/Mathematiques}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)/content/mathematiques/annales"

if [ ! -d "$SRC_ROOT" ]; then
    echo "ERREUR : source introuvable : $SRC_ROOT" >&2
    echo "Définis SRC_ROOT pour pointer vers ta bibliothèque DNB maths." >&2
    exit 1
fi

if [ ! -d "$SRC_ROOT/annales" ] || [ ! -d "$SRC_ROOT/corrections" ]; then
    echo "ERREUR : sous-dossiers attendus absents dans $SRC_ROOT :" >&2
    echo "  - annales/" >&2
    echo "  - corrections/" >&2
    exit 1
fi

mkdir -p "$DST_ROOT" "$DST_ROOT/corrections"

echo "→ Synchronisation des annales depuis $SRC_ROOT/annales/"
rsync -av --include '*.pdf' --exclude '*' "$SRC_ROOT/annales/" "$DST_ROOT/"

echo "→ Synchronisation des corrigés depuis $SRC_ROOT/corrections/"
rsync -av --include '*.pdf' --exclude '*' "$SRC_ROOT/corrections/" "$DST_ROOT/corrections/"

n_annales=$(find "$DST_ROOT" -maxdepth 1 -name '*.pdf' | wc -l | tr -d ' ')
n_corriges=$(find "$DST_ROOT/corrections" -maxdepth 1 -name '*.pdf' | wc -l | tr -d ' ')

echo ""
echo "Synchronisation terminée :"
echo "  - $n_annales sujets dans content/mathematiques/annales/"
echo "  - $n_corriges corrigés dans content/mathematiques/annales/corrections/"
echo ""
echo "Étape suivante : ingestion Albert (cf README) :"
echo "  .venv/bin/python -m scripts.ingest --matiere mathematiques --collections math_annales --dry-run"

"""
Génération offline de variations de sujets DC via Claude Opus.

Ce script prend les sujets extraits par `scripts/extract_subjects.py`
(content/histoire-geo-emc/subjects/*.json) et, pour chacun, demande à Opus de produire N
variations qui restent dans le programme cycle 4 mais changent :
- le verbe-clé de la consigne (décrire ↔ expliquer ↔ montrer…),
- les bornes chronologiques ou spatiales,
- ou l'angle d'approche (acteurs, conséquences, causes…).

La discipline et le grand thème du programme sont TOUJOURS conservés.

Pourquoi ce script est offline (et pas du runtime) :
- Générer au clic demanderait un étage de validation coûteux (parsing JSON
  strict + vérification notions ⊂ programme + juge LLM oui/non) pour éviter
  les sujets farfelus. Cf. discussion archi : on a préféré la variation
  précalculée pour garantir la qualité.
- Opus est plus fiable qu'Albert large sur la génération structurée.
- Coût one-shot : ~23 sujets × 3 variations × 1 appel = ~70 appels, une fois.

Sortie :
    content/histoire-geo-emc/subjects/variations/<stem>_var.json

Format identique à celui produit par extract_subjects.py — ce qui permet à
`app.histoire_geo_emc.models.load_subjects_from_jsons()` de les charger avec la même logique,
simplement marqués `is_variation=True` parce qu'ils sont dans le sous-dossier
`variations/`.

Usage :
    source .env
    .venv/bin/python -m scripts.generate_variations                       # traite tous les sujets
    .venv/bin/python -m scripts.generate_variations --source content/histoire-geo-emc/subjects/22genhgemcan1pdf-98088.json
    .venv/bin/python -m scripts.generate_variations --count 5 --force

Options :
    --count N : nombre de variations par sujet original (défaut 3)
    --force   : retraite les fichiers dont la variation existe déjà
    --limit N : ne traite que les N premiers fichiers (utile pour tester)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

from anthropic import Anthropic

logger = logging.getLogger(__name__)

# ============================================================================
# Constantes
# ============================================================================

OPUS_MODEL = "claude-opus-4-6"

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBJECTS_DIR = REPO_ROOT / "content" / "histoire-geo-emc" / "subjects"
OUTPUT_DIR = SUBJECTS_DIR / "variations"


# ============================================================================
# Prompt de génération
# ============================================================================

VARIATION_PROMPT = """\
Tu es un concepteur de sujets du Diplôme National du Brevet (DNB) français en \
histoire-géographie-EMC. On te fournit un sujet "développement construit" \
réel, et tu dois en produire {count} variations plausibles pour qu'un·e \
élève de 3e puisse s'entraîner sur des sujets proches mais différents.

Règles STRICTES :
1. Chaque variation DOIT rester dans le programme officiel de cycle 4 \
(classe de 3e).
2. Chaque variation DOIT garder la même `discipline` et le même grand `theme` \
que l'original (par ex. « Un monde bipolaire au temps de la guerre froide »).
3. Chaque variation DOIT changer AU MOINS UN de ces éléments par rapport à \
l'original :
   - le verbe-clé de la consigne (« décrire » ↔ « expliquer » ↔ « montrer » ↔ \
« présenter » ↔ « raconter »),
   - les bornes chronologiques (en restant dans les bornes du thème),
   - les bornes spatiales (en restant dans les bornes du thème),
   - l'angle d'approche (acteurs concernés, causes plutôt que conséquences, \
une dimension particulière : politique, économique, sociale, culturelle).
4. N'INVENTE AUCUN fait hors programme. Si tu hésites sur une date, un \
acteur, un lieu : reste sur ce qui est dans l'original ou ce qui est dans le \
programme cycle 4 standard.
5. La formulation de la consigne doit ressembler à celles du vrai DNB : \
« Dans un développement construit d'une vingtaine de lignes, <verbe> ... »
6. Les `notions_attendues` doivent rester des notions du programme cycle 4. \
Maximum 6 notions, minimum 3.
7. Les variations doivent être DIFFÉRENTES entre elles : deux verbes-clés \
différents, ou deux angles différents. Ne produis pas deux variations quasi \
identiques.

Sujet original :
<original>
{original_json}
</original>

Ta réponse doit être UNIQUEMENT un tableau JSON valide (commençant par `[` \
et finissant par `]`), contenant exactement {count} objets, sans aucun texte \
avant ou après, sans balises markdown. Chaque objet a ces clés :

{{
  "consigne": "la consigne complète de la variation, formulée comme au vrai DNB",
  "discipline": "histoire" | "geographie" | "emc",
  "theme": "même thème que l'original, copié tel quel",
  "verbe_cle": "verbe principal à l'infinitif (décrire, expliquer, montrer...)",
  "bornes_chrono": "bornes temporelles si pertinentes, sinon null",
  "bornes_spatiales": "bornes géographiques si pertinentes, sinon null",
  "notions_attendues": ["3 à 6 notions du programme cycle 4"],
  "bareme_points": null,
  "variation_de": "courte phrase qui explique ce qui change vs l'original (ex: 'verbe-clé passé de décrire à expliquer', 'angle recentré sur les acteurs économiques')"
}}
"""


# ============================================================================
# Appel Opus
# ============================================================================


def call_opus_for_variations(
    client: Anthropic, original_dc: dict, count: int
) -> list[dict]:
    """Demande à Opus de produire `count` variations d'un sujet original."""
    original_json = json.dumps(original_dc, ensure_ascii=False, indent=2)
    prompt = VARIATION_PROMPT.format(count=count, original_json=original_json)

    response = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=4000,
        # Un peu de température pour avoir de la variété entre variations,
        # mais pas trop pour rester dans le programme.
        temperature=0.4,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()

    # Filet de sécurité si Opus a rajouté des fences markdown malgré la consigne
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Opus n'a pas renvoyé un JSON valide :\n{raw[:500]}\nErreur : {e}"
        )

    if not isinstance(data, list):
        raise RuntimeError(f"Attendu un tableau JSON, obtenu : {type(data).__name__}")

    # Garde-fous basiques — si Opus renvoie moins que demandé, on accepte
    # (mieux vaut 2 variations propres que 3 dont une bancale).
    validated: list[dict] = []
    for v in data:
        if not isinstance(v, dict):
            continue
        if not (v.get("consigne") and v.get("discipline") and v.get("theme")):
            logger.warning("  ↪ variation rejetée (champs requis manquants)")
            continue
        validated.append(v)
    return validated


# ============================================================================
# Pipeline par fichier
# ============================================================================


def process_file(
    json_path: Path,
    client: Anthropic,
    output_dir: Path,
    count: int,
    force: bool,
) -> dict | None:
    """Traite un fichier sujet d'annale : génère ses variations et les écrit."""
    stem = json_path.stem
    output_file = output_dir / f"{stem}_var.json"

    if output_file.exists() and not force:
        logger.info("  ↪ déjà traité, skip (--force pour retraiter)")
        return None

    try:
        original = json.loads(json_path.read_text())
    except json.JSONDecodeError as e:
        logger.error("  JSON invalide : %s", e)
        return None

    originals_dcs = original.get("developpements_construits", [])
    if not originals_dcs:
        logger.info("  ↪ aucun DC dans ce fichier, skip")
        return None

    # Une entrée du fichier original → `count` variations dans le fichier cible.
    # On applatit le tout dans une seule liste `developpements_construits`,
    # ce qui permet à load_subjects_from_jsons de les ingérer sans modification.
    all_variations: list[dict] = []
    for i, dc in enumerate(originals_dcs):
        logger.info("  → DC #%d : %s", i, (dc.get("consigne") or "")[:60])
        try:
            variations = call_opus_for_variations(client, dc, count)
        except Exception as e:
            logger.error("    ❌ Opus : %s", e)
            continue
        logger.info("    %d variation(s) générée(s)", len(variations))
        all_variations.extend(variations)

    if not all_variations:
        logger.warning("  ↪ aucune variation produite, on n'écrit rien")
        return None

    result = {
        # Préfixe pour que (source_file, dc_index) ne collisionne JAMAIS avec
        # les sujets originaux. Cf. contrainte d'idempotence dans app/db.py.
        "source_file": f"variation_{original.get('source_file') or stem}",
        "year": original.get("year"),
        "serie": original.get("serie"),
        "session": original.get("session"),
        "session_label": original.get("session_label"),
        "original_source": original.get("source_file") or stem,
        "developpements_construits": all_variations,
    }

    output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    logger.info("    écrit → %s", output_file)
    return result


# ============================================================================
# Main
# ============================================================================


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=SUBJECTS_DIR,
        help="Fichier JSON unique ou dossier contenant les sujets d'annales "
        "(défaut: content/histoire-geo-emc/subjects/)",
    )
    parser.add_argument(
        "--count", type=int, default=3, help="Variations par sujet original (défaut 3)"
    )
    parser.add_argument("--force", action="store_true", help="Retraiter même si existant")
    parser.add_argument("--limit", type=int, default=None, help="Ne traiter que N fichiers")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY manquant. Source ton .env avant de lancer.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    client = Anthropic()

    if args.source.is_file():
        json_paths = [args.source]
    elif args.source.is_dir():
        json_paths = [
            p
            for p in sorted(args.source.glob("*.json"))
            if p.name != "_all.json" and p.parent == args.source
        ]
    else:
        sys.exit(f"Source introuvable : {args.source}")

    if args.limit:
        json_paths = json_paths[: args.limit]

    logger.info("%d fichier(s) source à traiter", len(json_paths))

    processed = 0
    total_variations = 0
    errors = []
    for json_path in json_paths:
        logger.info("\n[%s]", json_path.name)
        try:
            result = process_file(
                json_path, client, args.output_dir, args.count, args.force
            )
            if result is not None:
                processed += 1
                total_variations += len(result["developpements_construits"])
        except Exception as e:
            logger.error("  ❌ erreur : %s", e)
            errors.append((json_path.name, str(e)))

    logger.info("\n" + "=" * 60)
    logger.info(
        "Terminé : %d fichier(s) traité(s), %d variation(s) générée(s)",
        processed,
        total_variations,
    )
    if errors:
        logger.warning("%d erreur(s) :", len(errors))
        for name, err in errors:
            logger.warning("  - %s : %s", name, err)

    logger.info(
        "\nProchaines étapes :\n"
        "  1. Supprime data/app.db (ou ajoute une colonne manuellement) pour que\n"
        "     init_db() recharge les sujets avec le flag is_variation.\n"
        "  2. (Optionnel) python -m scripts.ingest --only sujets pour pousser\n"
        "     aussi les variations dans la collection RAG dnb_sujets.\n"
    )


if __name__ == "__main__":
    main()

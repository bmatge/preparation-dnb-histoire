"""
Extraction des sujets "développement construit" depuis les PDF des annales DNB.

Ce script est un outil de DÉVELOPPEMENT exécuté OFFLINE par le mainteneur,
pas en production. Il utilise Claude Opus pour analyser finement chaque PDF
et extraire les consignes de DC sous forme structurée (JSON).

Pourquoi Claude Opus et pas Albert :
- Tâche one-shot, volume limité (~23 PDF), pas besoin de scaler
- Opus est plus précis sur l'extraction structurée + raisonnement
- Pas d'appel en prod → coût négligeable
- Albert n'a pas toujours un parser JSON strict fiable

Ce que le script produit :
1. Un fichier JSON par PDF : content/histoire-geo-emc/subjects/<stem>.json
2. Un fichier consolidé : content/histoire-geo-emc/subjects/_all.json (array de tous les sujets)

Ces fichiers seront ensuite consommés par scripts/ingest.py pour :
- Alimenter la table `subjects` de SQLite
- Pousser les sujets dans la collection Albert `dnb_sujets`

Usage :
    source .env
    .venv/bin/python -m scripts.extract_subjects content/histoire-geo-emc/annales/
    .venv/bin/python -m scripts.extract_subjects content/histoire-geo-emc/annales/18genhgemcan1pdf-80388.pdf

Options :
    --force : retraite les PDF même si le JSON existe déjà
    --limit N : ne traite que les N premiers PDF (utile pour tester)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pdfplumber
from anthropic import Anthropic

logger = logging.getLogger(__name__)

# ============================================================================
# Constantes
# ============================================================================

# Modèle Opus — extraction = tâche critique, on prend le plus capable
OPUS_MODEL = "claude-opus-4-6"

# Répertoire de sortie
OUTPUT_DIR = Path("content/histoire-geo-emc/subjects")

# Regex pour parser le nom de fichier type "22genhgemcan1pdf-98088.pdf"
# (YY)(gen|pro)(hgemc)(XX)(1)(pdf)-(NNN).pdf
FILENAME_RE = re.compile(
    r"(?P<yy>\d{2})(?P<serie>gen|pro)hgemc(?P<session>[a-z][a-z0-9])(?P<var>\d)pdf-.*\.pdf",
    re.IGNORECASE,
)

# Mapping des codes de session vers libellés lisibles
SESSION_LABELS = {
    "an": "Amérique du Nord",
    "ag": "Antilles-Guyane",
    "as": "Asie",
    "aa": "Afrique",
    "in": "Inde / Pondichéry",
    "me": "Métropole",
    "g1": "Métropole (1er groupe)",
    "nc": "Nouvelle-Calédonie",
}

# ============================================================================
# Prompt d'extraction
# ============================================================================

EXTRACTION_PROMPT = """\
Tu es un assistant qui analyse des sujets du Diplôme National du Brevet (DNB) \
français en histoire-géographie-EMC pour un outil pédagogique d'entraînement.

Ta mission : extraire depuis le texte du sujet ci-dessous **toutes les consignes \
de "développement construit"** qu'il contient, et UNIQUEMENT celles-ci.

Qu'est-ce qu'une consigne de développement construit dans ce contexte :
- Elle est explicitement appelée « développement construit » dans le sujet.
- Formulation type : « Rédigez un développement construit d'environ vingt lignes \
dans lequel vous… » puis un verbe de consigne (décrivez / expliquez / montrez / \
présentez…).
- Elle peut se trouver en histoire, en géographie ou en EMC.
- Il peut y en avoir 0, 1 ou plusieurs dans un même sujet.

Ce que tu dois IGNORER (ne pas extraire) :
- Les questions d'analyse de document (« Présentez le document… », « Relevez… »).
- Les exercices de cartographie / repères.
- Les « rédigez un texte de quelques lignes » ou « présentez en quelques lignes » \
qui ne s'appellent PAS explicitement « développement construit ».
- Tout ce qui n'est pas une consigne de DC au sens strict.

Pour chaque DC trouvé, tu produis un objet JSON avec exactement ces clés :

{
  "consigne": "texte EXACT de la consigne (la phrase complète avec « Rédigez… »)",
  "discipline": "histoire" | "geographie" | "emc",
  "theme": "le titre de thème annoncé juste au-dessus de la consigne (ex: 'Pourquoi et comment aménager le territoire ?', 'Un monde bipolaire au temps de la guerre froide'). Copie-le tel quel.",
  "verbe_cle": "le verbe principal de la consigne à l'infinitif (décrire, expliquer, montrer, présenter, raconter...)",
  "bornes_chrono": "bornes temporelles si mentionnées explicitement dans la consigne, sinon null",
  "bornes_spatiales": "bornes géographiques si mentionnées explicitement dans la consigne, sinon null",
  "notions_attendues": ["liste de 3 à 6 notions clés du programme de 3e que l'examinateur s'attend à voir traitées pour ce sujet. Base-toi sur ta connaissance du programme officiel cycle 4."],
  "bareme_points": nombre de points sur lequel le DC est noté si indiqué entre parenthèses, sinon null
}

Règles IMPORTANTES :
- Ta réponse doit être UNIQUEMENT un tableau JSON valide (commençant par `[` et finissant par `]`), sans aucun texte avant ou après, sans balises markdown.
- Si le sujet ne contient aucun DC, réponds `[]`.
- Le champ `consigne` doit être le texte EXACT, sans reformulation, sans ajout.
- Pour `notions_attendues`, reste dans le programme officiel de 3e (cycle 4). Ne liste pas de notions hors-programme.
- La `discipline` doit être inférée depuis le contexte (le titre de l'exercice, le thème annoncé) : « HISTOIRE » → "histoire", « GEOGRAPHIE » → "geographie", « ENSEIGNEMENT MORAL ET CIVIQUE » → "emc".

Texte du sujet :
<sujet>
{pdf_text}
</sujet>
"""


# ============================================================================
# Types
# ============================================================================


@dataclass
class FilenameMeta:
    year: int | None  # 2018, 2022...
    serie: str | None  # "generale" | "professionnelle"
    session: str | None  # code ex "an"
    session_label: str | None  # "Amérique du Nord"
    variant: str | None  # "1"

    @classmethod
    def from_filename(cls, filename: str) -> "FilenameMeta":
        m = FILENAME_RE.match(filename)
        if not m:
            return cls(None, None, None, None, None)
        yy = int(m.group("yy"))
        year = 2000 + yy if yy < 80 else 1900 + yy
        serie = {"gen": "generale", "pro": "professionnelle"}.get(m.group("serie"))
        session = m.group("session").lower()
        return cls(
            year=year,
            serie=serie,
            session=session,
            session_label=SESSION_LABELS.get(session, session),
            variant=m.group("var"),
        )


# ============================================================================
# Extraction PDF
# ============================================================================


def extract_pdf_text(pdf_path: Path) -> str:
    """Extrait tout le texte d'un PDF avec pdfplumber."""
    with pdfplumber.open(pdf_path) as pdf:
        pages = []
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            pages.append(f"--- page {i} ---\n{text}")
    return "\n\n".join(pages)


# ============================================================================
# Appel Opus
# ============================================================================


def call_opus_for_extraction(client: Anthropic, pdf_text: str) -> list[dict]:
    """Appelle Claude Opus pour extraire les DC du texte du sujet."""
    prompt = EXTRACTION_PROMPT.replace("{pdf_text}", pdf_text)
    response = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=4000,
        temperature=0,  # extraction déterministe
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()

    # Nettoyage : si Opus a ajouté des fences markdown malgré la consigne
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

    return data


# ============================================================================
# Pipeline par fichier
# ============================================================================


def process_pdf(
    pdf_path: Path,
    client: Anthropic,
    output_dir: Path,
    force: bool = False,
) -> dict:
    """Traite un PDF : extraction texte → appel Opus → écriture JSON."""
    stem = pdf_path.stem
    output_file = output_dir / f"{stem}.json"

    if output_file.exists() and not force:
        logger.info("  ↪ déjà traité, skip (--force pour retraiter)")
        return json.loads(output_file.read_text())

    logger.info("  → extraction PDF (pdfplumber)...")
    pdf_text = extract_pdf_text(pdf_path)
    logger.info("    %d caractères extraits", len(pdf_text))

    logger.info("  → appel Claude Opus...")
    dcs = call_opus_for_extraction(client, pdf_text)
    logger.info("    %d développement(s) construit(s) trouvé(s)", len(dcs))

    meta = FilenameMeta.from_filename(pdf_path.name)
    result = {
        "source_file": pdf_path.name,
        "year": meta.year,
        "serie": meta.serie,
        "session": meta.session,
        "session_label": meta.session_label,
        "variant": meta.variant,
        "developpements_construits": dcs,
    }

    output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    logger.info("    écrit → %s", output_file)
    return result


# ============================================================================
# Main
# ============================================================================


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        type=Path,
        help="Chemin vers un PDF ou un dossier de PDF (ex: content/histoire-geo-emc/annales/)",
    )
    parser.add_argument("--force", action="store_true", help="Retraiter même si JSON existant")
    parser.add_argument("--limit", type=int, default=None, help="Ne traiter que N PDF")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY manquant. Source ton .env avant de lancer.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    client = Anthropic()

    # Rassembler la liste des PDF à traiter
    if args.path.is_file():
        pdf_paths = [args.path]
    elif args.path.is_dir():
        pdf_paths = sorted(args.path.glob("*.pdf"))
    else:
        sys.exit(f"Chemin introuvable : {args.path}")

    if args.limit:
        pdf_paths = pdf_paths[: args.limit]

    logger.info("%d PDF à traiter", len(pdf_paths))

    all_results = []
    errors = []
    total_dcs = 0
    for pdf_path in pdf_paths:
        logger.info("\n[%s]", pdf_path.name)
        try:
            result = process_pdf(pdf_path, client, args.output_dir, force=args.force)
            all_results.append(result)
            total_dcs += len(result["developpements_construits"])
        except Exception as e:
            logger.error("  ❌ erreur : %s", e)
            errors.append((pdf_path.name, str(e)))

    # Écrit le consolidé
    consolidated = args.output_dir / "_all.json"
    consolidated.write_text(json.dumps(all_results, ensure_ascii=False, indent=2))
    logger.info("\n" + "=" * 60)
    logger.info("Terminé : %d PDF traités, %d DC extraits au total", len(all_results), total_dcs)
    logger.info("Consolidé → %s", consolidated)
    if errors:
        logger.warning("%d erreurs :", len(errors))
        for name, err in errors:
            logger.warning("  - %s : %s", name, err)


if __name__ == "__main__":
    main()

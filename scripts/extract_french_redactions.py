"""
Extraction des sujets de « Rédaction » depuis les PDF des annales DNB français.

Ce script est un outil de DÉVELOPPEMENT exécuté OFFLINE par le mainteneur,
pas en production. Il utilise Claude Opus en mode multimodal (PDF natif) pour
analyser chaque sujet de rédaction et produire un JSON structuré contenant
les deux options proposées à l'élève (imagination et réflexion).

Pourquoi Opus multimodal plutôt qu'un extracteur PDF + LLM texte :
- Les sujets de rédaction contiennent régulièrement des citations en italique
  à conserver, voire un court paratexte ou une amorce en exergue qu'un extracteur
  texte nu risque de désaligner.
- Certains sujets renvoient explicitement au texte support de l'épreuve de
  compréhension (« en t'appuyant sur le texte de X, lignes Y à Z »), et il
  faut préserver cette référence.
- Opus est déterministe à température 0 et gère nativement les PDF DNB.

Ce que le script produit :
1. Un fichier JSON par PDF → content/francais/redaction/subjects/<stem>.json
2. Un fichier consolidé → content/francais/redaction/subjects/_all.json

Ces JSON sont ensuite chargés en base par l'app via
``app/francais/redaction/loader.py`` et peuvent être poussés dans une
collection Albert dédiée par ``scripts/ingest.py`` (à ajouter dans la
prochaine itération, cf. issue #6).

Usage :
    source .env
    .venv/bin/python -m scripts.extract_french_redactions content/francais/redaction/annales/
    .venv/bin/python -m scripts.extract_french_redactions content/francais/redaction/annales/2023_Metropole_francais_redaction.pdf
    .venv/bin/python -m scripts.extract_french_redactions content/francais/redaction/annales/ --limit 1

Options :
    --force      : retraite les PDF même si le JSON existe déjà
    --limit N    : ne traite que les N premiers PDF (utile pour un dry-run)
    --output-dir : répertoire de sortie alternatif
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from anthropic import Anthropic
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

# ============================================================================
# Constantes
# ============================================================================

OPUS_MODEL = "claude-opus-4-6"
# Un sujet de rédaction est plus court qu'un sujet de compréhension : pas de
# texte support à recopier ligne par ligne, seulement les deux consignes et
# leurs contraintes. 4k tokens de sortie suffisent très largement.
OPUS_MAX_TOKENS = 4000

OUTPUT_DIR = Path("content/francais/redaction/subjects")

# Regex pour parser un nom type "2023_Metropole_francais_redaction.pdf"
# ou "2024_Antilles-Guyane_francais_redaction_2.pdf"
FILENAME_RE = re.compile(
    r"^(?P<year>\d{4})_(?P<centre>[A-Za-z0-9-]+)_francais_redaction"
    r"(?:_(?P<variant>\d+))?\.pdf$",
    re.IGNORECASE,
)


# ============================================================================
# Prompt d'extraction
# ============================================================================

EXTRACTION_PROMPT = """\
Tu es un assistant qui analyse des sujets du Diplôme National du Brevet (DNB) \
français pour alimenter un outil pédagogique d'entraînement destiné à des \
élèves de 3e.

Le PDF joint est un sujet officiel de l'épreuve "Rédaction" (durée 1h30, \
40 points). Cette épreuve propose TOUJOURS **deux sujets au choix** :
- un **sujet d'imagination** (écrit d'invention à partir d'une amorce, souvent \
en lien avec le texte littéraire étudié en compréhension)
- un **sujet de réflexion** (argumentation sur une question, éventuellement en \
lien avec le texte ou avec un thème plus large)

L'élève en choisit un seul.

Ta mission est de produire un objet JSON structuré fidèle au sujet, sans \
reformulation des énoncés ni invention de contenu.

## Schéma JSON attendu

{
  "id": "<à laisser vide, sera rempli par le script>",
  "source": {
    "annee": <int, ex 2023>,
    "session": "<juin | septembre | inconnu>",
    "centre": "<ex: 'Métropole', 'Amérique du Nord', 'Antilles-Guyane'...>",
    "code_sujet": "<code imprimé sur la page, ex '23GENFRRDME1', null si absent>"
  },
  "epreuve": {
    "intitule": "Rédaction",
    "duree_minutes": 90,
    "points_total": 40
  },
  "texte_support_ref": "<courte phrase qui rappelle le texte support de l'épreuve de compréhension SI le sujet y renvoie explicitement, ex 'texte de Colette, Sido'. Null si le sujet de rédaction ne mentionne pas le texte support.>",
  "sujet_imagination": {
    "type": "imagination",
    "numero": "<numéro ou étiquette du sujet tel qu'imprimé, ex 'Sujet 1', 'Sujet A', 'Sujet d'imagination'>",
    "amorce": "<courte phrase d'amorce / d'accroche du sujet si elle est distincte de la consigne (ex. une phrase en italique avant l'énoncé). Null si la consigne et l'amorce sont confondues.>",
    "consigne": "<texte intégral de l'énoncé principal de rédaction, mot pour mot, sans le barème entre parenthèses. Conserve la ponctuation et les guillemets français « ».>",
    "contraintes": [<liste des contraintes explicitement formulées dans le sujet : longueur minimale, point de vue, registre, personnages imposés, temps verbaux, éléments obligatoires. Une chaîne par contrainte. [] si aucune contrainte n'est formulée.>],
    "longueur_min_lignes": <int ou null ; si le sujet dit "deux pages minimum" mets ~50, si "une page" mets ~25, si "X lignes" reprends X, sinon null>,
    "reference_texte_support": "<si le sujet renvoie explicitement à un passage du texte support ('en t'appuyant sur les lignes 15 à 20', 'à la manière de l'auteur du texte'...), copie ici l'extrait littéral de la consigne qui fait ce renvoi. Null sinon.>"
  },
  "sujet_reflexion": {
    "type": "reflexion",
    "numero": "<numéro ou étiquette, ex 'Sujet 2', 'Sujet B', 'Sujet de réflexion'>",
    "amorce": "<amorce éventuelle, null sinon>",
    "consigne": "<texte intégral de l'énoncé, mot pour mot>",
    "contraintes": [<liste de contraintes explicites, [] sinon>],
    "longueur_min_lignes": <int ou null>,
    "reference_texte_support": "<référence explicite au texte support, null sinon>"
  }
}

## Règles d'extraction IMPORTANTES

1. **Deux sujets obligatoires** : chaque sujet DNB rédaction propose deux \
options. Si tu n'en vois qu'une, relis le PDF — il y en a forcément deux, \
sauf cas très rare de sujet adapté. Distingue-les par leur nature : un sujet \
propose d'imaginer / inventer / raconter (imagination), l'autre pose une \
question à discuter / argumenter (réflexion).

2. **Numéro / étiquette** : préserve strictement l'étiquetage officiel du \
sujet ("Sujet 1", "Sujet A", "Sujet d'imagination", "Premier sujet"...). Ne \
renumérote pas.

3. **Consigne littérale** : recopie la consigne EXACTEMENT telle qu'elle \
apparaît, ponctuation comprise. N'ajoute pas de reformulation. Supprime \
uniquement le barème entre parenthèses s'il est collé à la fin ("(40 points)").

4. **Contraintes** : liste chaque contrainte explicite comme une entrée \
courte et autonome dans le tableau `contraintes`. Exemples : "récit à la \
première personne", "intégrer un dialogue", "au moins deux personnages", \
"registre fantastique", "longueur : une cinquantaine de lignes", "utiliser \
le passé simple". N'invente pas de contraintes qui ne sont pas dans le \
sujet.

5. **Longueur minimale** : si le sujet précise un nombre de lignes, de \
pages ou de mots, traduis-le en nombre approximatif de LIGNES dans \
`longueur_min_lignes` (1 page ≈ 25 lignes, 2 pages ≈ 50 lignes, 300 mots ≈ \
20 lignes). Si aucune indication de longueur : null.

6. **Référence au texte support** : si la consigne du sujet renvoie \
explicitement au texte support de l'épreuve de compréhension (par exemple \
"dans la continuité du texte", "en imaginant la suite du récit de X", "à \
la manière de l'auteur", "en t'appuyant sur les lignes 15 à 20"), copie \
l'extrait littéral de la consigne qui contient ce renvoi dans \
`reference_texte_support`. Sinon : null. En parallèle, remplis \
`texte_support_ref` au niveau racine avec une courte étiquette identifiant \
le texte ("texte de Colette, Sido") uniquement si c'est clair depuis le \
sujet seul ; sinon null.

7. **Sortie** : ta réponse doit être UNIQUEMENT l'objet JSON demandé, sans \
aucun texte avant ou après, sans balises markdown, sans commentaires. \
L'objet doit être valide et parsable.
"""


# ============================================================================
# Types pour validation pydantic
# ============================================================================


class Source(BaseModel):
    annee: int
    session: str
    centre: str
    code_sujet: str | None = None


class Epreuve(BaseModel):
    intitule: str
    duree_minutes: int
    points_total: int


class SujetOption(BaseModel):
    type: Literal["imagination", "reflexion"]
    numero: str
    amorce: str | None = None
    consigne: str
    contraintes: list[str] = Field(default_factory=list)
    longueur_min_lignes: int | None = None
    reference_texte_support: str | None = None


class RedactionSubject(BaseModel):
    id: str
    source: Source
    epreuve: Epreuve
    texte_support_ref: str | None = None
    sujet_imagination: SujetOption
    sujet_reflexion: SujetOption


# ============================================================================
# Filename parsing
# ============================================================================


@dataclass
class FilenameMeta:
    year: int | None
    centre: str | None
    variant: int | None

    @classmethod
    def from_filename(cls, filename: str) -> "FilenameMeta":
        m = FILENAME_RE.match(filename)
        if not m:
            return cls(None, None, None)
        return cls(
            year=int(m.group("year")),
            centre=m.group("centre").replace("-", " "),
            variant=int(m.group("variant")) if m.group("variant") else None,
        )

    def make_id(self) -> str:
        if self.year is None or self.centre is None:
            return "unknown"
        slug = self.centre.lower().replace(" ", "-")
        base = f"{self.year}_{slug}"
        if self.variant:
            base += f"_{self.variant}"
        return base


# ============================================================================
# Appel Opus multimodal
# ============================================================================


def call_opus_multimodal(client: Anthropic, pdf_path: Path) -> dict:
    pdf_bytes = pdf_path.read_bytes()
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    response = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=OPUS_MAX_TOKENS,
        temperature=0,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    )
    raw = response.content[0].text.strip()

    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        debug_path = Path("/tmp") / f"opus_raw_{pdf_path.stem}.txt"
        debug_path.write_text(raw)
        raise RuntimeError(
            f"Opus n'a pas renvoyé un JSON valide. Sortie brute sauvegardée → {debug_path}\n"
            f"Erreur : {e}"
        )

    if not isinstance(data, dict):
        raise RuntimeError(f"Attendu un objet JSON, obtenu : {type(data).__name__}")

    return data


# ============================================================================
# Validation et contrôles sémantiques
# ============================================================================


def validate_subject(data: dict, expected_id: str) -> RedactionSubject:
    data["id"] = expected_id
    try:
        subj = RedactionSubject.model_validate(data)
    except ValidationError as e:
        raise RuntimeError(f"Validation pydantic échouée :\n{e}")

    warnings: list[str] = []
    if subj.sujet_imagination.type != "imagination":
        warnings.append("sujet_imagination.type incorrect")
    if subj.sujet_reflexion.type != "reflexion":
        warnings.append("sujet_reflexion.type incorrect")
    if len(subj.sujet_imagination.consigne) < 20:
        warnings.append("consigne imagination anormalement courte")
    if len(subj.sujet_reflexion.consigne) < 20:
        warnings.append("consigne réflexion anormalement courte")

    if warnings:
        logger.warning("  ⚠ avertissements :")
        for w in warnings:
            logger.warning("    - %s", w)

    return subj


# ============================================================================
# Pipeline par fichier
# ============================================================================


def process_pdf(
    pdf_path: Path,
    client: Anthropic,
    output_dir: Path,
    force: bool = False,
) -> dict:
    stem = pdf_path.stem
    output_file = output_dir / f"{stem}.json"

    if output_file.exists() and not force:
        logger.info("  ↪ déjà traité, skip (--force pour retraiter)")
        return json.loads(output_file.read_text())

    meta = FilenameMeta.from_filename(pdf_path.name)
    expected_id = meta.make_id()
    logger.info("  → id calculé : %s", expected_id)

    logger.info("  → appel Claude Opus (multimodal, PDF natif)...")
    raw_data = call_opus_multimodal(client, pdf_path)
    logger.info("  → validation pydantic...")
    subj = validate_subject(raw_data, expected_id)

    result = subj.model_dump()
    result["source_file"] = pdf_path.name

    output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    logger.info("  ✓ écrit → %s", output_file)
    return result


# ============================================================================
# Main
# ============================================================================


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", type=Path, help="Chemin vers un PDF ou un dossier de PDF"
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY manquant. Source ton .env avant de lancer.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    client = Anthropic()

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
    errors: list[tuple[str, str]] = []
    for pdf_path in pdf_paths:
        logger.info("\n[%s]", pdf_path.name)
        try:
            result = process_pdf(pdf_path, client, args.output_dir, force=args.force)
            all_results.append(result)
        except Exception as e:
            logger.error("  ❌ erreur : %s", e)
            errors.append((pdf_path.name, str(e)))

    consolidated = args.output_dir / "_all.json"
    consolidated.write_text(json.dumps(all_results, ensure_ascii=False, indent=2))
    logger.info("\n" + "=" * 60)
    logger.info("Terminé : %d/%d PDF traités", len(all_results), len(pdf_paths))
    logger.info("Consolidé → %s", consolidated)
    if errors:
        logger.warning("\n%d erreurs :", len(errors))
        for name, err in errors:
            logger.warning("  - %s : %s", name, err)
        sys.exit(1)


if __name__ == "__main__":
    main()

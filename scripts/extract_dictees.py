"""Extraction des dictées DNB français depuis les PDF d'annales.

Script offline, exécuté par le mainteneur. Utilise Claude Opus en mode
multimodal pour parser chaque PDF de dictée et produire un JSON structuré
qui sera ensuite consommé par :
- `scripts/generate_dictee_audio.py` pour synthétiser les MP3 phrase par phrase
- le runtime `app/francais/dictee/` pour la pédagogie et l'évaluation

Hors scope : les dictées aménagées (gros caractères, version oralisée).
Le script ne traite que les fichiers `*_dictee.pdf`, pas `*_dicteeamenagee.pdf`.

Usage :
    source .env
    .venv/bin/python -m scripts.extract_dictees content/francais/dictee/annales/
    .venv/bin/python -m scripts.extract_dictees content/francais/dictee/annales/2023_Metropole_francais_dictee.pdf
    .venv/bin/python -m scripts.extract_dictees content/francais/dictee/annales/ --limit 2

Options :
    --force      : retraite les PDF même si le JSON existe déjà
    --limit N    : ne traite que les N premiers PDF (utile pour tester)
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

OPUS_MODEL = "claude-opus-4-6"
OPUS_MAX_TOKENS = 8000

OUTPUT_DIR = Path("content/francais/dictee/exercises")

# Format attendu : "2023_Metropole_francais_dictee.pdf"
FILENAME_RE = re.compile(
    r"^(?P<year>\d{4})_(?P<centre>[A-Za-z0-9-]+)_francais_dictee\.pdf$",
    re.IGNORECASE,
)


# ============================================================================
# Prompt d'extraction
# ============================================================================

EXTRACTION_PROMPT = """\
Tu es un assistant qui analyse des sujets du Diplôme National du Brevet (DNB) \
français pour alimenter un outil pédagogique d'entraînement à la dictée \
destiné à des élèves de 3e.

Le PDF joint est un sujet officiel d'une dictée DNB (épreuve de \
« Dictée », 10 points sur 50). Il contient typiquement :
- Une page de garde avec les métadonnées de l'épreuve.
- Un titre de dictée (donné par l'examinateur, ex. « L'arrivée à la ferme »).
- Le texte intégral à dicter à l'élève (généralement 100 à 180 mots).
- La référence bibliographique (auteur, œuvre, parfois année).
- Parfois une note de bas de page (mot rare, nom propre à écrire au tableau, \
contexte historique).

Ta mission est de produire un objet JSON structuré conforme au schéma \
ci-dessous, fidèle au sujet, sans reformulation ni invention.

## Schéma JSON attendu

{
  "id": "<à laisser vide, sera rempli par le script>",
  "source": {
    "annee": <int, ex 2023>,
    "session": "<juin | septembre | inconnu>",
    "centre": "<ex: 'Métropole', 'Amérique du Nord', 'Antilles-Guyane'...>",
    "code_sujet": "<code imprimé sur la page si présent, sinon null>"
  },
  "titre": "<titre de la dictée tel qu'il apparaît dans le sujet, sans guillemets. Si aucun titre explicite, mets null.>",
  "reference": {
    "auteur": "<nom de l'auteur tel qu'il apparaît>",
    "oeuvre": "<titre de l'œuvre>",
    "annee_publication": <int ou null>
  },
  "texte_complet": "<le texte intégral à dicter, en une seule chaîne, ponctuation et accents EXACTS, sans saut de ligne au milieu d'un paragraphe. Garde les guillemets français « » et les apostrophes typographiques ' tels qu'ils apparaissent.>",
  "phrases": [
    {
      "ordre": 1,
      "texte": "<première phrase complète, ponctuation finale incluse>",
      "difficultes": [
        {
          "type": "<lexicale | accord | conjugaison | homophone | trait_union | majuscule | apostrophe | autre>",
          "mot": "<le mot ou groupe concerné, écrit correctement>",
          "explication": "<courte explication d'un piège anticipé pour un élève de 3e (1 phrase max)>"
        }
      ]
    },
    ...
  ],
  "notes_examinateur": [
    "<éventuelles instructions au correcteur ou mots à écrire au tableau, telles qu'elles apparaissent dans le PDF. [] si aucune.>"
  ]
}

## Règles d'extraction IMPORTANTES

1. **Texte exact** : recopie le texte EXACTEMENT comme il apparaît dans le \
PDF, sans corriger, sans normaliser. Conserve les accents, les cédilles, les \
trémas, les ligatures (œ, æ), les guillemets français (« »), les tirets \
demi-cadratins (–) si présents. C'est ce texte qui servira de référence \
pour l'évaluation lettre-à-lettre des copies élève.

2. **Découpage en phrases** : segmente le texte en phrases complètes, \
délimitées par . ! ? Une phrase qui contient un point d'exclamation au \
milieu d'un dialogue (ex. « N'avance pas ! » dit-il en reculant.) reste \
UNE seule phrase. Une phrase peut contenir des incises, des subordonnées, \
des dialogues. Si une phrase dépasse 30 mots et contient une rupture \
naturelle (deux-points, point-virgule, conjonction de coordination forte), \
tu peux la couper en deux phrases — c'est utile pour la dictée parce \
que chaque phrase devient un fichier audio.

3. **Difficultés anticipées** : pour chaque phrase, identifie 1 à 4 pièges \
orthographiques qu'un élève de 3e risque de manquer. Ne tague PAS tous les \
mots — uniquement ceux qui demandent une réflexion (accord du participe \
passé, homophone à/a, nom composé, mot rare, conjugaison piège, élision \
inhabituelle). Reste sobre.

4. **Titre** : récupère le titre de la dictée s'il est explicitement donné \
dans le sujet (souvent une seule phrase en haut, en italique ou en gras, \
ex. « Les vacances à la mer », « L'orage », « La leçon »). Si le sujet ne \
donne pas de titre explicite, mets null — ne fabrique pas un titre.

5. **Référence biblio** : extraite telle qu'elle apparaît, généralement à \
la fin du texte ou en bas. Format type « Marcel Pagnol, *La Gloire de mon \
père*, 1957 ». Récupère auteur, œuvre et année si présents ; mets null \
sinon.

6. **Notes examinateur** : si le sujet contient des instructions adressées \
à l'examinateur (du type « Mots à écrire au tableau : Jordanie, Aqaba » ou \
« Lire le titre, puis lire le texte une première fois en entier »), \
recopie-les dans `notes_examinateur`. Sinon `[]`.

7. **Sortie** : ta réponse doit être UNIQUEMENT l'objet JSON demandé, sans \
aucun texte avant ou après, sans balises markdown, sans commentaires. \
L'objet doit être valide et parsable.
"""


# ============================================================================
# Schéma pydantic
# ============================================================================


class Source(BaseModel):
    annee: int
    session: str
    centre: str
    code_sujet: str | None = None


class Reference(BaseModel):
    auteur: str
    oeuvre: str
    annee_publication: int | None = None


class Difficulte(BaseModel):
    type: Literal[
        "lexicale",
        "accord",
        "conjugaison",
        "homophone",
        "trait_union",
        "majuscule",
        "apostrophe",
        "autre",
    ]
    mot: str
    explication: str


class Phrase(BaseModel):
    ordre: int
    texte: str
    difficultes: list[Difficulte] = Field(default_factory=list)


class Dictee(BaseModel):
    id: str
    source: Source
    titre: str | None = None
    reference: Reference
    texte_complet: str
    phrases: list[Phrase]
    notes_examinateur: list[str] = Field(default_factory=list)


# ============================================================================
# Filename parsing
# ============================================================================


@dataclass
class FilenameMeta:
    year: int | None
    centre: str | None

    @classmethod
    def from_filename(cls, filename: str) -> "FilenameMeta":
        m = FILENAME_RE.match(filename)
        if not m:
            return cls(None, None)
        return cls(
            year=int(m.group("year")),
            centre=m.group("centre").replace("-", " "),
        )

    def make_id(self) -> str:
        if self.year is None or self.centre is None:
            return "unknown"
        slug = self.centre.lower().replace(" ", "-")
        return f"{self.year}_{slug}"


# ============================================================================
# Appel Opus multimodal
# ============================================================================


def call_opus_multimodal(client: Anthropic, pdf_path: Path) -> dict:
    pdf_b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode("utf-8")
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
        debug_path = Path("/tmp") / f"opus_dictee_raw_{pdf_path.stem}.txt"
        debug_path.write_text(raw)
        raise RuntimeError(
            f"Opus n'a pas renvoyé un JSON valide. Sortie brute → {debug_path}\n"
            f"Erreur : {e}"
        )
    if not isinstance(data, dict):
        raise RuntimeError(f"Attendu un objet JSON, obtenu : {type(data).__name__}")
    return data


# ============================================================================
# Validation
# ============================================================================


def validate_dictee(data: dict, expected_id: str) -> Dictee:
    data["id"] = expected_id
    try:
        dictee = Dictee.model_validate(data)
    except ValidationError as e:
        raise RuntimeError(f"Validation pydantic échouée :\n{e}")

    warnings: list[str] = []

    # Continuité de l'ordre des phrases
    for i, p in enumerate(dictee.phrases, start=1):
        if p.ordre != i:
            warnings.append(f"phrase ordre {p.ordre}, attendu {i}")
            break

    # La concaténation des phrases doit retomber sur le texte complet
    # (à des espaces près)
    rejointes = " ".join(p.texte.strip() for p in dictee.phrases)
    norm_a = re.sub(r"\s+", " ", rejointes).strip()
    norm_b = re.sub(r"\s+", " ", dictee.texte_complet).strip()
    if norm_a != norm_b:
        warnings.append(
            "concaténation des phrases ≠ texte_complet "
            f"(diff de {abs(len(norm_a) - len(norm_b))} caractères)"
        )

    # Au moins une phrase
    if not dictee.phrases:
        warnings.append("aucune phrase extraite")

    # Texte non vide
    if not dictee.texte_complet.strip():
        warnings.append("texte_complet vide")

    if warnings:
        logger.warning("  avertissements :")
        for w in warnings:
            logger.warning("    - %s", w)

    return dictee


# ============================================================================
# Pipeline par fichier
# ============================================================================


def process_pdf(
    pdf_path: Path,
    client: Anthropic,
    output_dir: Path,
    *,
    force: bool = False,
) -> dict:
    stem = pdf_path.stem
    output_file = output_dir / f"{stem}.json"

    if output_file.exists() and not force:
        logger.info("  deja traite, skip (--force pour retraiter)")
        return json.loads(output_file.read_text())

    meta = FilenameMeta.from_filename(pdf_path.name)
    expected_id = meta.make_id()
    logger.info("  id calcule : %s", expected_id)

    logger.info("  appel Claude Opus (multimodal, PDF natif)...")
    raw_data = call_opus_multimodal(client, pdf_path)
    logger.info("  validation pydantic...")
    dictee = validate_dictee(raw_data, expected_id)

    result = dictee.model_dump()
    result["source_file"] = pdf_path.name

    output_file.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "  ok -> %s (%d phrases, %d caracteres)",
        output_file.name,
        len(dictee.phrases),
        len(dictee.texte_complet),
    )
    return result


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="PDF unique ou dossier de PDF")
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
        # On filtre strictement les dictées standard, pas les aménagées
        pdf_paths = sorted(
            p for p in args.path.glob("*.pdf") if "amenagee" not in p.name.lower()
        )
    else:
        sys.exit(f"Chemin introuvable : {args.path}")

    if args.limit:
        pdf_paths = pdf_paths[: args.limit]

    logger.info("%d PDF a traiter", len(pdf_paths))

    all_results = []
    errors: list[tuple[str, str]] = []
    for pdf_path in pdf_paths:
        logger.info("\n[%s]", pdf_path.name)
        try:
            result = process_pdf(pdf_path, client, args.output_dir, force=args.force)
            all_results.append(result)
        except Exception as e:
            logger.error("  erreur : %s", e)
            errors.append((pdf_path.name, str(e)))

    consolidated = args.output_dir / "_all.json"
    consolidated.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("\n" + "=" * 60)
    logger.info("Termine : %d/%d PDF traites", len(all_results), len(pdf_paths))
    logger.info("Consolide -> %s", consolidated)
    if errors:
        logger.warning("\n%d erreurs :", len(errors))
        for name, err in errors:
            logger.warning("  - %s : %s", name, err[:200])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

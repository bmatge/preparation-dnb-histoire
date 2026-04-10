"""
Extraction des sujets "Compréhension et compétences d'interprétation" depuis
les PDF des annales DNB français.

Ce script est un outil de DÉVELOPPEMENT exécuté OFFLINE par le mainteneur,
pas en production. Il utilise Claude Opus en mode multimodal (PDF natif) pour
analyser finement chaque sujet et produire un JSON structuré couvrant le
paratexte, le texte littéraire ligne par ligne, les notes, l'image, et les
questions des deux parties (compréhension et grammaire).

Pourquoi Opus multimodal plutôt qu'un extracteur PDF + LLM texte :
- Les énoncés contiennent des mots soulignés, des italiques, des passages en
  vers — autant d'informations visuelles que pdfplumber perd.
- L'image (photographie, photogramme, tableau) doit être décrite pour servir
  d'indice en runtime à un élève qui ne la voit pas (ou pour la RAG).
- Opus est déterministe à température 0 et gère nativement les PDF DNB.

Ce que le script produit :
1. Un fichier JSON par PDF : content/francais/comprehension/exercises/<stem>.json
2. Un fichier consolidé : content/francais/comprehension/exercises/_all.json

Ces JSON seront ensuite chargés en base par l'app via app/francais/comprehension/
et poussés dans les collections Albert dnb_francais_* par scripts/ingest.py.

Usage :
    source .env
    .venv/bin/python -m scripts.extract_french_exercises content/francais/comprehension/annales/
    .venv/bin/python -m scripts.extract_french_exercises content/francais/comprehension/annales/2023_Metropole_francais_questions-grammaire-comp.pdf
    .venv/bin/python -m scripts.extract_french_exercises content/francais/comprehension/annales/ --limit 2

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

# ============================================================================
# Constantes
# ============================================================================

# Modèle Opus — extraction = tâche critique, température 0
OPUS_MODEL = "claude-opus-4-6"

# Budget tokens généreux : un sujet complet avec texte ligne par ligne,
# toutes les questions et la description d'image peut facilement tourner
# autour de 6-8k tokens de sortie.
OPUS_MAX_TOKENS = 12000

# Répertoire de sortie par défaut
OUTPUT_DIR = Path("content/francais/comprehension/exercises")

# Regex pour parser un nom type "2023_Metropole_francais_questions-grammaire-comp.pdf"
# ou "2024_Antilles-Guyane_francais_questions-grammaire-comp_2.pdf"
FILENAME_RE = re.compile(
    r"^(?P<year>\d{4})_(?P<centre>[A-Za-z-]+)_francais_questions-grammaire-comp"
    r"(?:_(?P<variant>\d+))?\.pdf$",
    re.IGNORECASE,
)

# Liste fermée des compétences (cf décision validée avec le mainteneur)
ALLOWED_COMPETENCES = {
    "reperage_explicite",
    "comprehension_implicite",
    "selection_pertinente",
    "champ_lexical",
    "interpretation",
    "texte_image",
    "structure_narrative",
    "fonctions_grammaticales",
    "propositions_subordonnees",
    "lexique_etymologie",
    "reecriture",
    "conjugaison",
    "classes_grammaticales",
    "discours_rapporte",
}

# ============================================================================
# Prompt d'extraction
# ============================================================================

EXTRACTION_PROMPT = """\
Tu es un assistant qui analyse des sujets du Diplôme National du Brevet (DNB) \
français pour alimenter un outil pédagogique d'entraînement destiné à des \
élèves de 3e.

Le PDF joint est un sujet officiel de l'épreuve "Grammaire et compétences \
linguistiques – Compréhension et compétences d'interprétation" (durée 1h10, \
50 points). Il contient :
- Une page de garde (métadonnées épreuve, à ignorer sauf pour le code sujet).
- Un paratexte introductif en italique (1 à 3 lignes qui situent l'extrait).
- Un texte littéraire avec numérotation des lignes en pas de 5 dans la marge \
gauche (lignes 5, 10, 15, 20, 25, 30, 35…).
- Des notes de bas de texte numérotées (lexique ou contexte).
- Une référence bibliographique (auteur, œuvre, année).
- Une image (photographie, photogramme, tableau, illustration) avec légende.
- Deux blocs de questions :
    I. Compréhension et compétences d'interprétation (32 points)
    II. Grammaire et compétences linguistiques (18 points)

Ta mission est de produire un objet JSON structuré conforme au schéma \
ci-dessous, fidèle au sujet, sans reformulation des énoncés ni invention de \
contenu.

## Schéma JSON attendu

{
  "id": "<à laisser vide, sera rempli par le script>",
  "source": {
    "annee": <int, ex 2023>,
    "session": "<juin | septembre | inconnu>",
    "centre": "<ex: 'Métropole', 'Amérique du Nord', 'Antilles-Guyane'...>",
    "code_sujet": "<code imprimé sur la page, ex '23GENFRQGCME1'>"
  },
  "epreuve": {
    "intitule": "Grammaire et compétences linguistiques – Compréhension et compétences d'interprétation",
    "duree_minutes": 70,
    "points_total": 50,
    "points_comprehension": 32,
    "points_grammaire": 18
  },
  "paratexte": "<le court texte en italique qui précède le texte littéraire, sans les guillemets éventuels. Null si absent.>",
  "texte_support": {
    "auteur": "<nom de l'auteur tel qu'il apparaît>",
    "auteur_note": "<pseudonyme ou précision entre crochets si présente, sinon null>",
    "oeuvre": "<titre de l'œuvre>",
    "partie": "<partie/chapitre si indiqué, sinon null>",
    "annee_publication": <int ou null>,
    "genre": "<roman | autobiographie | nouvelle | theatre | poesie | recit | essai | conte | memoires>",
    "lignes": [
      { "n": 1, "texte": "<contenu exact de la ligne 1 du sujet>" },
      { "n": 2, "texte": "<contenu exact de la ligne 2>" },
      ...
    ]
  },
  "notes_texte": [
    { "n": 1, "terme": "<mot ou expression noté>", "definition": "<définition donnée>" },
    ...
  ],
  "image": {
    "type": "<photographie | photogramme | peinture | dessin | gravure | affiche | sculpture | illustration>",
    "auteur": "<nom>",
    "titre": "<titre>",
    "annee": <int ou null>,
    "description_visuelle": "<description neutre et précise de ce que montre l'image, 2 à 4 phrases, comme si tu la décrivais à quelqu'un qui ne la voit pas. N'interprète pas, décris.>"
  },
  "questions": [
    {
      "numero": "<numéro officiel du sujet, garde-le tel quel, ex: '1', '4', '7'>",
      "partie": "comprehension" ou "grammaire",
      "type": "<standard | reecriture>",
      "enonce": "<texte intégral de l'énoncé, sans le barème entre parenthèses. Si la question a des sous-questions, mets ici la formulation principale/introductive.>",
      "citation": "<si l'énoncé commence par citer un extrait du texte avec guillemets français « », copie-le ici. Sinon null.>",
      "mots_soulignes": [<liste des mots ou groupes soulignés dans la citation, copiés exactement. Identifie les soulignements en regardant visuellement le PDF. [] si aucun.>],
      "lignes_ciblees": [
        { "start": <int>, "end": <int> }
      ],
      "passage_a_reecrire": "<pour les questions de réécriture uniquement, le passage exact à transformer. Null sinon.>",
      "contraintes": [<pour les questions de réécriture, liste des contraintes imposées ('ne pas modifier X', 'mettre au passé simple'...). [] sinon.>],
      "sous_questions": [
        {
          "lettre": "a",
          "enonce": "<texte de la sous-question a>",
          "points": <int ou float>,
          "competence": "<une valeur de la liste ci-dessous>"
        },
        ...
      ],
      "points": <int ou float, le barème total de la question, même si elle a des sous-questions>,
      "competence": "<une valeur de la liste ci-dessous ; à utiliser si la question n'a pas de sous-questions, sinon mets null et tague les sous-questions>",
      "necessite_image": <true si la question porte sur l'image, false sinon>
    },
    ...
  ]
}

## Liste fermée des valeurs autorisées pour "competence"

- reperage_explicite : identifier une info explicite (qui, où, quand)
- comprehension_implicite : déduire une info non-dite du texte
- selection_pertinente : relever X éléments / X citations pertinents
- champ_lexical : identifier un champ lexical et relever des mots
- interpretation : interpréter un passage, analyser une intention
- texte_image : mettre en relation le texte et l'image
- structure_narrative : analyser la construction du récit
- fonctions_grammaticales : identifier / justifier une fonction (sujet, COD…)
- propositions_subordonnees : identifier / nommer une subordonnée
- lexique_etymologie : étymologie, sens enrichi par l'origine
- reecriture : transformer un passage selon des contraintes
- conjugaison : changer un temps, un mode, une personne
- classes_grammaticales : identifier la nature (nom, adjectif…)
- discours_rapporte : passer du direct à l'indirect ou inverse

## Règles d'extraction IMPORTANTES

1. **Texte littéraire** : découpe ligne par ligne en respectant la \
numérotation exacte imprimée dans le PDF (pas par phrases, par lignes \
d'affichage du sujet DNB). Une ligne = une ligne visible dans le sujet. Les \
lignes 5, 10, 15… portent un numéro dans la marge ; les autres n'en portent \
pas mais comptent quand même. Recopie le texte EXACTEMENT (ponctuation, \
accents, guillemets français « »). Conserve les citations en italique \
(ex. vers de tragédie) comme du texte normal, elles comptent comme des \
lignes du sujet.

2. **Mots soulignés** : examine visuellement le PDF. Dans les questions de \
grammaire, certains groupes de mots sont soulignés. Identifie-les en \
regardant la typographie (trait horizontal sous le texte) et liste-les \
exactement dans le champ `mots_soulignes` de la question concernée.

3. **Numérotation des questions** : préserve strictement la numérotation \
officielle du sujet. Si le sujet numérote 1, 2, 3, 4a, 4b, 5, 6 puis 7, 8, \
9a, 9b, 10, reproduis cette numérotation. Ne renumérote pas.

4. **Sous-questions** : si une question principale a des sous-questions \
(a, b, c), mets-les dans `sous_questions`. Dans ce cas :
   - Le champ `enonce` de la question principale contient l'introduction \
commune éventuelle (souvent la citation ou le contexte) ou l'énoncé de la \
question principale si elle est unifiée.
   - `points` de la question principale = somme des points des sous-questions.
   - Chaque sous-question a son propre barème et sa propre compétence.
   - Si la question n'a pas de sous-questions, `sous_questions` vaut `[]` et \
tu tagues la `competence` au niveau de la question.

5. **Question de réécriture** : c'est toujours une question à ~10 points en \
partie grammaire. Elle demande de réécrire un passage selon des contraintes \
(changer un pronom, un temps, un mode, passer au discours indirect…). \
Marque `type: "reecriture"`, remplis `passage_a_reecrire` avec le texte \
exact et `contraintes` avec la liste des instructions.

6. **Question sur l'image** : marque `necessite_image: true` dès que la \
question mentionne explicitement l'image (photographie, photogramme, \
tableau…) ou demande une mise en relation texte/image.

7. **Lignes ciblées** : si l'énoncé mentionne "ligne(s) X à Y", remplis \
`lignes_ciblees: [{"start": X, "end": Y}]`. Pour une ligne unique "ligne X", \
mets `{"start": X, "end": X}`. Si plusieurs plages sont mentionnées, mets \
plusieurs entrées. Si aucune ligne n'est ciblée, mets `[]`.

8. **Barème** : extrais les points de chaque question/sous-question depuis \
les parenthèses "(X points)". Garde les décimales quand elles existent \
(ex. 1,5 point → 1.5).

9. **Image - description_visuelle** : décris objectivement la composition, \
les personnages, le décor, la technique visuelle. N'interprète PAS le sens \
artistique, ne dis pas ce que l'image "exprime". Juste ce qu'on voit. 2 à 4 \
phrases suffisent.

10. **Sortie** : ta réponse doit être UNIQUEMENT l'objet JSON demandé, sans \
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
    points_comprehension: int
    points_grammaire: int


class Ligne(BaseModel):
    n: int
    texte: str


class TexteSupport(BaseModel):
    auteur: str
    auteur_note: str | None = None
    oeuvre: str
    partie: str | None = None
    annee_publication: int | None = None
    genre: str
    lignes: list[Ligne]


class NoteTexte(BaseModel):
    n: int
    terme: str
    definition: str


class Image(BaseModel):
    type: str
    auteur: str | None = None
    titre: str | None = None
    annee: int | None = None
    description_visuelle: str


class LignesCiblees(BaseModel):
    start: int
    end: int


class SousQuestion(BaseModel):
    lettre: str
    enonce: str
    points: float
    competence: str | None = None


class Question(BaseModel):
    numero: str
    partie: Literal["comprehension", "grammaire"]
    type: Literal["standard", "reecriture"] = "standard"
    enonce: str
    citation: str | None = None
    mots_soulignes: list[str] = Field(default_factory=list)
    lignes_ciblees: list[LignesCiblees] = Field(default_factory=list)
    passage_a_reecrire: str | None = None
    contraintes: list[str] = Field(default_factory=list)
    sous_questions: list[SousQuestion] = Field(default_factory=list)
    points: float
    competence: str | None = None
    necessite_image: bool = False


class Exercise(BaseModel):
    id: str
    source: Source
    epreuve: Epreuve
    paratexte: str | None = None
    texte_support: TexteSupport
    notes_texte: list[NoteTexte] = Field(default_factory=list)
    image: Image | None = None
    questions: list[Question]


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
    """Appelle Claude Opus en mode multimodal avec le PDF natif."""
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

    # Nettoyage défensif : fences markdown éventuelles
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        # Sauvegarde la sortie brute pour debug
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


def validate_exercise(data: dict, expected_id: str) -> Exercise:
    """Valide la structure JSON et applique des contrôles sémantiques."""
    data["id"] = expected_id

    try:
        exo = Exercise.model_validate(data)
    except ValidationError as e:
        raise RuntimeError(f"Validation pydantic échouée :\n{e}")

    # Contrôles métier
    warnings: list[str] = []

    # Compétences dans la liste fermée
    for q in exo.questions:
        if q.competence and q.competence not in ALLOWED_COMPETENCES:
            warnings.append(f"Q{q.numero} compétence inconnue : {q.competence}")
        for sq in q.sous_questions:
            if sq.competence and sq.competence not in ALLOWED_COMPETENCES:
                warnings.append(
                    f"Q{q.numero}{sq.lettre} compétence inconnue : {sq.competence}"
                )

    # Somme des points doit matcher les totaux de partie (tolérance 0.5)
    total_compr = sum(q.points for q in exo.questions if q.partie == "comprehension")
    total_gram = sum(q.points for q in exo.questions if q.partie == "grammaire")
    if abs(total_compr - exo.epreuve.points_comprehension) > 0.5:
        warnings.append(
            f"Somme compréhension ({total_compr}) ≠ barème ({exo.epreuve.points_comprehension})"
        )
    if abs(total_gram - exo.epreuve.points_grammaire) > 0.5:
        warnings.append(
            f"Somme grammaire ({total_gram}) ≠ barème ({exo.epreuve.points_grammaire})"
        )

    # Numérotation des lignes continue
    expected_line = 1
    for ligne in exo.texte_support.lignes:
        if ligne.n != expected_line:
            warnings.append(
                f"Trou dans la numérotation lignes : attendu {expected_line}, trouvé {ligne.n}"
            )
            break
        expected_line += 1

    if warnings:
        logger.warning("  ⚠ avertissements :")
        for w in warnings:
            logger.warning("    - %s", w)

    return exo


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
    exo = validate_exercise(raw_data, expected_id)

    result = exo.model_dump()
    result["source_file"] = pdf_path.name

    output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    logger.info(
        "  ✓ écrit → %s (%d questions, %d lignes)",
        output_file,
        len(exo.questions),
        len(exo.texte_support.lignes),
    )
    return result


# ============================================================================
# Main
# ============================================================================


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        type=Path,
        help="Chemin vers un PDF ou un dossier de PDF",
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
    logger.info(
        "Terminé : %d/%d PDF traités", len(all_results), len(pdf_paths)
    )
    logger.info("Consolidé → %s", consolidated)
    if errors:
        logger.warning("\n%d erreurs :", len(errors))
        for name, err in errors:
            logger.warning("  - %s : %s", name, err)
        sys.exit(1)


if __name__ == "__main__":
    main()

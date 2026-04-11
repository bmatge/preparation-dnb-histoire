"""Extraction des automatismes (Partie 1) depuis les annales DNB maths.

Outil OFFLINE de développement, exécuté manuellement par le mainteneur. Utilise
Claude Opus vision pour analyser chaque PDF d'annale et en extraire les
questions de Partie 1 « automatismes / tâches simples » qui correspondent
strictement au référentiel officiel 2026 (`content/mathematiques/automatismes/
_liste_officielle.json`).

Pourquoi Opus vision plutôt qu'extraction texte :
- Beaucoup de questions des annales reposent sur une figure (Thalès, lecture
  graphique, droite graduée, schéma Scratch). L'extraction texte seule rate
  les valeurs numériques et la nature de la question.
- Opus vision lit les PDFs directement (bloc `document`) et restitue à la
  fois l'énoncé et la nature des éléments visuels.

Pourquoi un référentiel strict :
- L'épreuve 2026 a un périmètre d'automatismes calé sur la liste indicative
  d'octobre 2025. Les questions de Partie 1 des annales 2017-2025 ne sont
  pas toutes dans ce périmètre (certaines ont migré, d'autres ont disparu).
  On rejette systématiquement les questions hors-référentiel pour ne pas
  diluer la banque cible 2026.

Sortie :
- `content/mathematiques/automatismes/questions/annales_2023_2025.json` (par
  défaut) au format pydantic `Question` de `app/mathematiques/automatismes/
  models.py` (id stable, source, theme, competence, enonce, scoring,
  optionnellement figure et options).
- Journal de rejets : `scripts/.logs/extract_math_auto_<timestamp>.json`
  (raisons de rejet par PDF/question pour audit).
- Figures : extraites à part via `pdfimages` et déposées dans
  `content/mathematiques/figures/` avec un nom stable
  `auto_annale_<year>_<serie_slug>_q<N>.png`.

Usage :
    source .env
    .venv/bin/python -m scripts.extract_math_automatismes \\
        --glob "content/mathematiques/annales/202[345]_*Metropole*.pdf" \\
        --dry-run

    .venv/bin/python -m scripts.extract_math_automatismes \\
        --pdfs content/mathematiques/annales/2024_BrevetMetropolejuin2024.pdf \\
        --output content/mathematiques/automatismes/questions/annales_2023_2025.json
"""

from __future__ import annotations

import argparse
import base64
import glob as glob_lib
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env", override=True)

# Import après load_dotenv pour préserver l'idempotence des clés.
sys.path.insert(0, str(REPO_ROOT))
from app.mathematiques.automatismes.models import Question  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

OPUS_MODEL = "claude-opus-4-6"
REFERENTIEL_PATH = (
    REPO_ROOT
    / "content"
    / "mathematiques"
    / "automatismes"
    / "_liste_officielle.json"
)
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "content"
    / "mathematiques"
    / "automatismes"
    / "questions"
    / "annales_2023_2025.json"
)
FIGURES_DIR = REPO_ROOT / "content" / "mathematiques" / "figures"
LOGS_DIR = REPO_ROOT / "scripts" / ".logs"


# ============================================================================
# Référentiel : chargement et formatage pour le prompt
# ============================================================================


def load_referentiel() -> tuple[list[dict], set[str], dict[str, dict]]:
    """Charge le référentiel officiel 2026.

    Retourne (rubriques, set des item_ids valides, index par id).
    """
    data = json.loads(REFERENTIEL_PATH.read_text(encoding="utf-8"))
    items_by_id: dict[str, dict] = {}
    for rubrique in data.get("rubriques", []):
        for item in rubrique.get("items", []):
            items_by_id[item["id"]] = {**item, "rubrique": rubrique["code"]}
    return data["rubriques"], set(items_by_id.keys()), items_by_id


def referentiel_to_prompt(rubriques: list[dict]) -> str:
    """Formate le référentiel en bullets pour le prompt système."""
    lines: list[str] = []
    for rub in rubriques:
        lines.append(f"\n## {rub['titre']} ({rub['code']})")
        for item in rub["items"]:
            themes = ", ".join(item.get("themes", []))
            lines.append(f"- **{item['id']}** [{themes}] : {item['libelle']}")
    return "\n".join(lines)


# ============================================================================
# Métadonnées par fichier (année, série, source)
# ============================================================================


YEAR_RE = re.compile(r"(20\d{2})")
SERIE_RE = re.compile(
    r"(Metropole|MetropoleAntilles|AmeriqueNord|AmeriqueSud|Asie|Polynesie|"
    r"Caledonie|NlleCaledo|Antilles|AntillesGuyane|Liban|Pondichery|"
    r"centresetrangers|Centresetrangers|etrangers|Madagascar|Wallis)",
    re.IGNORECASE,
)
SESSION_RE = re.compile(
    r"(juin|septembre|sept|mars|mai|nov|novembre|decembre|dec|avril)", re.IGNORECASE
)


@dataclass
class PdfMeta:
    path: Path
    year: int | None
    serie_slug: str | None
    session: str | None

    @classmethod
    def from_path(cls, path: Path) -> "PdfMeta":
        name = path.stem
        year_match = YEAR_RE.search(name)
        serie_match = SERIE_RE.search(name)
        session_match = SESSION_RE.search(name)
        return cls(
            path=path,
            year=int(year_match.group(1)) if year_match else None,
            serie_slug=serie_match.group(1).lower() if serie_match else None,
            session=session_match.group(1).lower() if session_match else None,
        )

    def slug(self) -> str:
        parts = [
            str(self.year or "xxxx"),
            self.serie_slug or "unknown",
            self.session or "",
        ]
        return "_".join(p for p in parts if p)


# ============================================================================
# Prompt Opus
# ============================================================================


SYSTEM_PROMPT_TPL = """\
Tu es un expert du référentiel d'automatismes du DNB mathématiques 2026 (édition \
française, niveau 3e). Ton rôle est d'extraire depuis une annale de DNB \
mathématiques (sujet officiel, format PDF) toutes les questions courtes \
qui correspondent **strictement** à un item de la liste officielle 2026 \
ci-dessous, et UNIQUEMENT celles-ci.

## IMPORTANT : ce que tu cherches

Les annales pré-2026 n'ont pas toujours une « Partie 1 — automatismes » \
explicite ; le format strict « Partie 1 + Partie 2 » est propre à la réforme \
2026. **Tu dois quand même extraire les questions courtes qui s'apparentent à \
des automatismes**, où qu'elles se trouvent dans le PDF :

- Les blocs de QCM « sans justification » (qu'ils soient en début ou au milieu \
du sujet)
- Les sous-questions ponctuelles d'exercices qui demandent UNIQUEMENT un calcul \
court ou une réponse numérique (ex. « Calculer la moyenne de cette série », \
« Quelle est la mesure de l'angle ABC ? », « Quel est le PGCD de 91 et 77 ? »)
- Les questions de type « Vrai/Faux » avec justification courte

Ce que tu N'extrais PAS :
- Les sous-questions qui demandent une démonstration, une justification \
rédigée, un raisonnement étape par étape
- Les sous-questions d'analyse de figure qui requièrent plusieurs étapes \
combinées
- Les sous-questions qui s'appuient sur une question précédente (chaînage)

## Liste officielle des items 2026

Chaque item est identifié par un code (ex. `nc_04`, `eg_11`) et listé sous une \
rubrique. Tu dois :

1. Identifier toutes les questions courtes du PDF qui pourraient être des \
automatismes au sens du référentiel 2026.
2. Pour chaque question, juger si elle correspond à un item de la liste \
ci-dessous. Si **oui**, l'extraire avec le code de l'item. Si **non**, la rejeter \
en expliquant pourquoi (item hors référentiel, question trop complexe, \
chaînage requis, etc.).

{referentiel}

## Format de sortie

Tu réponds UNIQUEMENT par un JSON strict (pas de markdown, pas de commentaire) \
avec deux clés au top-level :

```
{{
  "extracted": [
    {{
      "numero_question": <int>,
      "item_id": "<code de l'item référentiel, ex: nc_04>",
      "theme": "<un thème parmi : calcul_numerique, calcul_litteral, fractions, pourcentages_proportionnalite, stats_probas, grandeurs_mesures, geometrie_numerique, programmes_calcul>",
      "competence": "<libellé court de la compétence (max 100 caractères), terminé par '(item <code>)'>",
      "enonce": "<énoncé EXACT de la question telle qu'elle apparaît dans le PDF, sans reformulation. Inclure les valeurs numériques visibles dans la figure SI ELLES SONT NÉCESSAIRES pour répondre>",
      "type_reponse": "<un type parmi : entier, decimal, fraction, pourcentage, texte_court, qcm>",
      "reponse_canonique": "<la réponse correcte attendue. Pour qcm, l'identifiant de l'option (ex: 'C')>",
      "unite": "<unité éventuelle, ex: 'cm', 'h', '°'. null si pas d'unité>",
      "options": [
        {{"id": "A", "texte": "<libellé option A>"}},
        {{"id": "B", "texte": "<libellé option B>"}},
        {{"id": "C", "texte": "<libellé option C>"}},
        {{"id": "D", "texte": "<libellé option D>"}}
      ],
      "has_figure": <true|false>,
      "figure_page": <numéro de page 1-indexed où se trouve la figure, ou null>,
      "figure_description": "<description courte de la figure, ou null>"
    }}
  ],
  "rejected": [
    {{
      "numero_question": <int>,
      "raison": "<courte raison du rejet>"
    }}
  ]
}}
```

Règles strictes :

- `options` n'est rempli QUE si `type_reponse == "qcm"`. Sinon, omettre cette clé \
ou la mettre à `null`.
- `enonce` doit être l'énoncé EXACT du PDF, en français, sans paraphrase. Si la \
figure contient des valeurs numériques nécessaires (longueurs, angles, abscisses), \
les ajouter entre parenthèses dans l'énoncé textuel pour qu'il soit auto-suffisant.
- `reponse_canonique` doit être déterministe et calculable depuis l'énoncé. \
Pour les QCM, c'est l'identifiant de l'option correcte (« A », « B », « C », « D »).
- Si tu n'es PAS sûr à 100 % qu'une question correspond à un item, REJETTE-la.
- Si tu n'arrives pas à déterminer la bonne réponse, REJETTE-la.
- Si l'énoncé est ambigu sans la figure et que la figure n'est pas auto-suffisante, \
REJETTE-la.
- Tu peux extraire 0, 1 ou plusieurs questions par PDF.
- Ignore complètement la Partie 2 (problèmes, raisonnement, justification).
"""


def build_system_prompt() -> str:
    rubriques, _, _ = load_referentiel()
    return SYSTEM_PROMPT_TPL.format(referentiel=referentiel_to_prompt(rubriques))


# ============================================================================
# Appel Opus vision (PDF en base64)
# ============================================================================


def call_opus_extract(
    client: Anthropic, pdf_path: Path, system_prompt: str
) -> dict[str, Any]:
    pdf_bytes = pdf_path.read_bytes()
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")

    response = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=8000,
        temperature=0,
        system=system_prompt,
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
                    {
                        "type": "text",
                        "text": (
                            "Analyse cette annale et extrais les automatismes "
                            "Partie 1 selon le format demandé."
                        ),
                    },
                ],
            }
        ],
    )

    raw = response.content[0].text.strip()
    # Opus a tendance à raisonner à voix haute avant de produire le JSON,
    # parfois dans un bloc ```json … ```. On extrait robustement l'objet JSON :
    # 1) chercher un bloc ```json … ``` (ou ``` … ```)
    # 2) à défaut, chercher le premier '{' jusqu'au dernier '}' équilibrant
    fence_match = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL
    )
    if fence_match:
        raw = fence_match.group(1)
    else:
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1 and last > first:
            raw = raw[first : last + 1]
    return json.loads(raw)


# ============================================================================
# Validation pydantic + écriture
# ============================================================================


def to_question_dict(
    pdf_meta: PdfMeta, raw: dict[str, Any], valid_item_ids: set[str]
) -> tuple[dict | None, str | None]:
    """Convertit la sortie Opus en dict question conforme au schéma. Retourne
    (question_dict, raison_rejet)."""

    item_id = raw.get("item_id")
    if item_id not in valid_item_ids:
        return None, f"item_id hors référentiel: {item_id}"

    type_reponse = raw.get("type_reponse")
    if type_reponse not in (
        "entier",
        "decimal",
        "fraction",
        "pourcentage",
        "texte_court",
        "qcm",
    ):
        return None, f"type_reponse invalide: {type_reponse}"

    theme = raw.get("theme")
    valid_themes = {
        "calcul_numerique",
        "calcul_litteral",
        "fractions",
        "pourcentages_proportionnalite",
        "stats_probas",
        "grandeurs_mesures",
        "geometrie_numerique",
        "programmes_calcul",
    }
    if theme not in valid_themes:
        return None, f"theme invalide: {theme}"

    enonce = (raw.get("enonce") or "").strip()
    if not enonce:
        return None, "enonce vide"

    reponse = raw.get("reponse_canonique")
    if reponse is None or str(reponse).strip() == "":
        return None, "reponse_canonique vide"

    numero = raw.get("numero_question")
    qid = f"auto_annale_{pdf_meta.slug()}_q{numero}"

    scoring: dict[str, Any] = {
        "mode": "python",
        "type_reponse": type_reponse,
        "reponse_canonique": str(reponse),
    }
    if raw.get("unite"):
        scoring["unite"] = raw["unite"]

    out: dict[str, Any] = {
        "id": qid,
        "source": {
            "type": "annale_dnb",
            "document": pdf_meta.path.name,
            "numero_question": numero,
            "item_liste": item_id,
        },
        "theme": theme,
        "competence": (raw.get("competence") or "")[:200],
        "enonce": enonce,
        "scoring": scoring,
    }

    if type_reponse == "qcm":
        opts = raw.get("options") or []
        if not opts or len(opts) < 2:
            return None, "qcm sans options exploitables"
        out["options"] = [
            {"id": str(o["id"]), "texte": str(o["texte"])} for o in opts
        ]

    if raw.get("has_figure"):
        figure_filename = (
            f"auto_annale_{pdf_meta.slug()}_q{numero}.png"
        )
        out["_figure_extract_request"] = {
            "page": raw.get("figure_page"),
            "filename": figure_filename,
            "description": raw.get("figure_description"),
        }

    # Validation pydantic finale (sans le _figure_extract_request)
    pydantic_payload = {k: v for k, v in out.items() if not k.startswith("_")}
    try:
        Question.model_validate(pydantic_payload)
    except Exception as exc:
        return None, f"validation pydantic: {exc}"

    return out, None


# ============================================================================
# Extraction des figures via pdfimages
# ============================================================================


def extract_figure(pdf_path: Path, page: int, target_filename: str) -> bool:
    """Extrait toutes les images d'une page via pdfimages et conserve la
    plus grosse (heuristique : la figure du sujet est presque toujours la plus
    volumineuse). Renvoie True si succès."""
    if page is None or page < 1:
        return False

    tmp_dir = Path("/tmp") / f"extract_{pdf_path.stem}_{page}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    prefix = tmp_dir / "fig"
    try:
        subprocess.run(
            [
                "pdfimages",
                "-png",
                "-p",
                "-f",
                str(page),
                "-l",
                str(page),
                str(pdf_path),
                str(prefix),
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning("pdfimages a échoué pour %s p%d : %s", pdf_path.name, page, exc)
        return False

    candidates = sorted(tmp_dir.glob("fig*.png"), key=lambda p: p.stat().st_size)
    if not candidates:
        return False

    largest = candidates[-1]
    target = FIGURES_DIR / target_filename
    target.write_bytes(largest.read_bytes())
    logger.info("  figure extraite → %s (%d octets)", target.name, target.stat().st_size)
    return True


# ============================================================================
# Pipeline
# ============================================================================


def collect_pdfs(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] = []
    if args.pdfs:
        paths.extend(Path(p) for p in args.pdfs)
    if args.glob:
        paths.extend(Path(p) for p in glob_lib.glob(args.glob, recursive=True))
    paths = [p for p in paths if p.is_file() and p.suffix.lower() == ".pdf"]
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(p)
    return sorted(unique)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdfs", nargs="*", help="Liste explicite de PDFs")
    parser.add_argument("--glob", help="Glob pattern (ex: 'content/.../202[345]_*Metropole*.pdf')")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="JSON de sortie")
    parser.add_argument("--dry-run", action="store_true", help="N'écrit rien sur disque")
    parser.add_argument("--limit", type=int, help="Ne traiter que N PDFs (debug)")
    args = parser.parse_args()

    pdfs = collect_pdfs(args)
    if args.limit:
        pdfs = pdfs[: args.limit]
    if not pdfs:
        logger.error("Aucun PDF à traiter (vérifie --pdfs ou --glob)")
        return 1

    logger.info("→ %d PDF(s) à analyser", len(pdfs))
    for p in pdfs:
        logger.info("    - %s", p.name)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY manquante")
        return 2

    client = Anthropic(api_key=api_key)
    _, valid_item_ids, _ = load_referentiel()
    system_prompt = build_system_prompt()

    all_questions: list[dict] = []
    journal: list[dict] = []
    n_extracted = n_rejected = n_errors = 0

    for pdf_path in pdfs:
        meta = PdfMeta.from_path(pdf_path)
        logger.info("\n[%s] année=%s série=%s", pdf_path.name, meta.year, meta.serie_slug)

        try:
            opus_out = call_opus_extract(client, pdf_path, system_prompt)
        except Exception as exc:
            logger.error("  Opus a échoué : %s", exc)
            n_errors += 1
            journal.append(
                {"pdf": pdf_path.name, "error": str(exc)[:300]}
            )
            continue

        raw_extracted = opus_out.get("extracted") or []
        raw_rejected = opus_out.get("rejected") or []

        for raw in raw_extracted:
            q, reason = to_question_dict(meta, raw, valid_item_ids)
            if q is None:
                logger.info("  ✗ rejet validation : %s", reason)
                n_rejected += 1
                journal.append(
                    {
                        "pdf": pdf_path.name,
                        "numero_question": raw.get("numero_question"),
                        "rejet_local": reason,
                        "raw": raw,
                    }
                )
                continue
            # Extraction figure si demandée
            fig_req = q.pop("_figure_extract_request", None)
            if fig_req and not args.dry_run:
                ok = extract_figure(pdf_path, fig_req.get("page"), fig_req["filename"])
                if ok:
                    q["figure"] = fig_req["filename"]
            all_questions.append(q)
            n_extracted += 1
            logger.info("  ✓ extrait q%s (item %s)", raw.get("numero_question"), raw.get("item_id"))

        for raw in raw_rejected:
            n_rejected += 1
            journal.append(
                {
                    "pdf": pdf_path.name,
                    "numero_question": raw.get("numero_question"),
                    "raison_opus": raw.get("raison"),
                }
            )
            logger.info(
                "  · Opus a rejeté q%s : %s",
                raw.get("numero_question"),
                raw.get("raison"),
            )

    logger.info(
        "\n=== Résumé ===\n  extraits : %d\n  rejetés : %d\n  erreurs : %d",
        n_extracted,
        n_rejected,
        n_errors,
    )

    if args.dry_run:
        logger.info("(dry-run) rien n'est écrit. Sortie type :")
        sample = {"_comment": "DRY-RUN", "questions": all_questions[:3]}
        print(json.dumps(sample, ensure_ascii=False, indent=2))
        return 0

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": (
            f"Automatismes extraits depuis les annales DNB maths via "
            f"scripts/extract_math_automatismes.py le {datetime.now().isoformat(timespec='seconds')}. "
            f"Sources : {', '.join(p.name for p in pdfs)}. "
            f"{n_extracted} extraits, {n_rejected} rejetés, {n_errors} erreurs."
        ),
        "questions": all_questions,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("→ %s écrit (%d questions)", output_path, n_extracted)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"extract_math_auto_{datetime.now():%Y%m%d_%H%M%S}.json"
    log_path.write_text(
        json.dumps(
            {
                "summary": {
                    "n_pdfs": len(pdfs),
                    "n_extracted": n_extracted,
                    "n_rejected": n_rejected,
                    "n_errors": n_errors,
                },
                "journal": journal,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("→ journal : %s", log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

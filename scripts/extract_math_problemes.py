"""Extraction des exercices Partie 2 (raisonnement et résolution de problèmes)
depuis les annales DNB maths.

Outil OFFLINE de développement, exécuté manuellement par le mainteneur. Utilise
Claude Opus vision pour analyser les PDFs d'annales et en extraire chaque
exercice de Partie 2 avec son contexte, ses sous-questions et ses figures.

Différence avec extract_math_automatismes.py :
- Cible la **Partie 2** (problèmes), pas la Partie 1 (automatismes).
- Pas de référentiel strict : tous les exercices sont conservés tant qu'ils
  produisent au moins une sous-question évaluable.
- Skip systématique des sous-questions de pure construction graphique
  (« représenter graphiquement », « tracer », « placer le point ») et des
  lectures graphiques sur une construction de l'élève.
- Garde les sous-questions qui peuvent être évaluées en mode `python`
  (entier, decimal, fraction, pourcentage, texte_court) ou `albert`
  (justification courte) — fallback `albert` si Opus ne sait pas.
- Extraction automatique de la figure de contexte de chaque exercice via
  `pdfimages` (heuristique : la plus grosse image de la première page
  contenant l'exercice).

Sortie :
- `content/mathematiques/problemes/exercices/annales_2020_2025.json`
- Figures dans `content/mathematiques/figures/` au nom
  `prob_annale_<year>_<serie_slug>_ex<N>.png`
- Journal dans `scripts/.logs/extract_math_prob_<timestamp>.json`

Usage :
    source .env
    .venv/bin/python -m scripts.extract_math_problemes \\
        --glob "content/mathematiques/annales/202[012345]_*Metropole*.pdf" \\
        --dry-run

    .venv/bin/python -m scripts.extract_math_problemes \\
        --pdfs content/mathematiques/annales/2024_BrevetMetropolejuin2024.pdf
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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env", override=True)

sys.path.insert(0, str(REPO_ROOT))
from app.mathematiques.problemes.models import ProblemExerciseSchema  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

OPUS_MODEL = "claude-opus-4-6"
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "content"
    / "mathematiques"
    / "problemes"
    / "exercices"
    / "annales_2020_2025.json"
)
FIGURES_DIR = REPO_ROOT / "content" / "mathematiques" / "figures"
LOGS_DIR = REPO_ROOT / "scripts" / ".logs"

VALID_THEMES = {
    "statistiques",
    "probabilites",
    "fonctions",
    "geometrie",
    "arithmetique",
    "grandeurs_mesures",
    "programmes_calcul",
}


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
        return cls(
            path=path,
            year=int(YEAR_RE.search(name).group(1)) if YEAR_RE.search(name) else None,
            serie_slug=(SERIE_RE.search(name).group(1).lower() if SERIE_RE.search(name) else None),
            session=(SESSION_RE.search(name).group(1).lower() if SESSION_RE.search(name) else None),
        )

    def slug(self) -> str:
        parts = [str(self.year or "xxxx"), self.serie_slug or "unknown", self.session or ""]
        return "_".join(p for p in parts if p)


SYSTEM_PROMPT = """\
Tu es un expert du DNB mathématiques 2026 (épreuve de fin de 3e en France). \
Ton rôle est d'extraire depuis une annale officielle (PDF) tous les exercices \
de **Partie 2 — raisonnement et résolution de problèmes**, avec leurs \
sous-questions évaluables.

## Ce que tu extrais

Pour chaque exercice de la Partie 2 :

1. Le **contexte** complet (énoncé général + données chiffrées + tableaux \
en markdown si nécessaire)
2. La liste des **sous-questions** numérotées (1, 2.a, 2.b, etc.)
3. Pour chaque sous-question, sa **réponse attendue calculable** quand \
c'est possible

## Ce que tu ignores ou rejettes

- Les sous-questions de **pure construction graphique** par l'élève : \
« représenter graphiquement la fonction », « tracer la médiatrice », \
« placer le point M ». Skip-les.
- Les sous-questions qui demandent une **lecture graphique sur une figure \
construite par l'élève** au cours de l'exercice (ex. « par lecture graphique, \
trouve l'abscisse du point d'intersection » qui suit un « représenter \
graphiquement »). Skip-les.
- Les sous-questions de pure rédaction libre sans réponse calculable \
(« commente », « propose une stratégie ») : à la rigueur, garde-les en \
mode `albert` avec un `reponse_modele` court.

Attention : tu ne dois PAS toucher à la Partie 1 (automatismes), traitée par \
un autre script.

## Format de sortie

Réponse JSON STRICT (pas de markdown, pas de commentaire). Format :

```
{
  "exercices": [
    {
      "numero_exercice": <int>,
      "theme": "<un thème parmi : statistiques, probabilites, fonctions, geometrie, arithmetique, grandeurs_mesures, programmes_calcul>",
      "titre": "<titre court de l'exercice (max 80 caractères)>",
      "competence_principale": "<compétence principale travaillée (max 150 caractères)>",
      "points_total": <float, ex: 3.0>,
      "contexte": "<énoncé global EXACT, conservé tel quel, avec tableaux en markdown si présents>",
      "has_figure": <true|false>,
      "figure_page": <numéro de page 1-indexed où apparaît la figure principale, ou null>,
      "figure_description": "<description courte ou null>",
      "sous_questions": [
        {
          "numero": "<libellé court : '1', '2.a', '2.b (i)', etc.>",
          "texte": "<énoncé EXACT de la sous-question>",
          "scoring_mode": "<python|albert>",
          "type_reponse": "<entier|decimal|fraction|pourcentage|texte_court>  // requis si scoring_mode=python",
          "reponse_canonique": "<réponse correcte, ex: '42', '3.14', '2/3'>  // requis si scoring_mode=python",
          "tolerance_abs": <float ou null, ex: 0.01 pour decimal>,
          "unite": "<ex: 'cm', 'kg', '°' ou null>",
          "formes_acceptees": ["<chaînes alternatives acceptées>"],
          "reponse_modele": "<texte réponse modèle si scoring_mode=albert>",
          "criteres_validation": ["<critères pour scoring_mode=albert>"],
          "skip_reason": "<si tu skip cette sous-question : 'construction graphique', 'lecture sur construction élève', etc.>"
        }
      ]
    }
  ],
  "rejected_exercises": [
    {"numero_exercice": <int>, "raison": "<courte raison>"}
  ]
}
```

Règles strictes :

- `contexte` doit être l'énoncé EXACT, sans paraphrase, en français.
- Si une sous-question est skippée, mets `skip_reason` et N'inclus PAS \
les autres champs (texte, scoring, etc.).
- Tu peux extraire 0, 1 ou plusieurs exercices par PDF.
- Ne réécris JAMAIS un énoncé pour le « simplifier » : on veut la forme du \
sujet officiel.
- Si un exercice a 0 sous-question évaluable après filtrage, rejette-le \
dans `rejected_exercises`.
"""


# ============================================================================
# Opus call
# ============================================================================


def call_opus_extract(client: Anthropic, pdf_path: Path) -> dict[str, Any]:
    pdf_b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode("ascii")
    response = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=12000,
        temperature=0,
        system=SYSTEM_PROMPT,
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
                            "Analyse cette annale et extrais les exercices "
                            "de la Partie 2 selon le format demandé."
                        ),
                    },
                ],
            }
        ],
    )
    raw = response.content[0].text.strip()
    # Opus a tendance à raisonner à voix haute avant de produire le JSON,
    # parfois dans un bloc ```json … ```. Extraction robuste :
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
# Construction du dict ProblemExerciseSchema
# ============================================================================


def to_exercise_dict(
    pdf_meta: PdfMeta, raw: dict[str, Any]
) -> tuple[dict | None, str | None]:
    theme = raw.get("theme")
    if theme not in VALID_THEMES:
        return None, f"theme invalide: {theme}"

    contexte = (raw.get("contexte") or "").strip()
    if not contexte:
        return None, "contexte vide"

    numero_ex = raw.get("numero_exercice")
    ex_id = f"prob_annale_{pdf_meta.slug()}_ex{numero_ex}"

    sub_qs: list[dict] = []
    for raw_sq in raw.get("sous_questions") or []:
        if raw_sq.get("skip_reason"):
            continue
        scoring_mode = raw_sq.get("scoring_mode")
        sq_dict: dict[str, Any] = {
            "id": f"{ex_id}_q{len(sub_qs) + 1}",
            "numero": str(raw_sq.get("numero", str(len(sub_qs) + 1))),
            "texte": (raw_sq.get("texte") or "").strip(),
        }
        if not sq_dict["texte"]:
            continue

        if scoring_mode == "python":
            type_rep = raw_sq.get("type_reponse")
            rep = raw_sq.get("reponse_canonique")
            if not type_rep or rep is None:
                continue
            sc: dict[str, Any] = {
                "mode": "python",
                "type_reponse": type_rep,
                "reponse_canonique": str(rep),
            }
            if raw_sq.get("tolerance_abs") is not None:
                sc["tolerances"] = {"abs": float(raw_sq["tolerance_abs"])}
            if raw_sq.get("unite"):
                sc["unite"] = raw_sq["unite"]
            if raw_sq.get("formes_acceptees"):
                sc["formes_acceptees"] = list(raw_sq["formes_acceptees"])
            sq_dict["scoring"] = sc
        elif scoring_mode == "albert":
            modele = raw_sq.get("reponse_modele") or ""
            criteres = raw_sq.get("criteres_validation") or []
            if not modele:
                continue
            sq_dict["scoring"] = {
                "mode": "albert",
                "reponse_modele": modele,
                "criteres_validation": list(criteres),
            }
        else:
            continue

        sub_qs.append(sq_dict)

    if not sub_qs:
        return None, "aucune sous-question évaluable après filtrage"

    out: dict[str, Any] = {
        "id": ex_id,
        "source": {
            "type": "annale_dnb",
            "document": pdf_meta.path.name,
            "exercice": numero_ex,
        },
        "theme": theme,
        "titre": (raw.get("titre") or f"Exercice {numero_ex}")[:200],
        "competence_principale": (raw.get("competence_principale") or "")[:300],
        "points_total": float(raw.get("points_total") or 0.0),
        "contexte": contexte,
        "sous_questions": sub_qs,
    }

    if raw.get("has_figure"):
        out["_figure_extract_request"] = {
            "page": raw.get("figure_page"),
            "filename": f"prob_annale_{pdf_meta.slug()}_ex{numero_ex}.png",
        }

    pydantic_payload = {k: v for k, v in out.items() if not k.startswith("_")}
    try:
        ProblemExerciseSchema.model_validate(pydantic_payload)
    except Exception as exc:
        return None, f"validation pydantic: {str(exc)[:300]}"

    return out, None


# ============================================================================
# Extraction figures
# ============================================================================


def extract_figure(pdf_path: Path, page: int, target_filename: str) -> bool:
    if page is None or page < 1:
        return False
    tmp_dir = Path("/tmp") / f"extract_prob_{pdf_path.stem}_{page}"
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
    parser.add_argument("--glob", help="Glob pattern")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    pdfs = collect_pdfs(args)
    if args.limit:
        pdfs = pdfs[: args.limit]
    if not pdfs:
        logger.error("Aucun PDF (vérifie --pdfs ou --glob)")
        return 1

    logger.info("→ %d PDF(s) à analyser", len(pdfs))
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY manquante")
        return 2

    client = Anthropic(api_key=api_key)

    all_exercises: list[dict] = []
    journal: list[dict] = []
    n_extracted = n_rejected = n_errors = 0

    for pdf_path in pdfs:
        meta = PdfMeta.from_path(pdf_path)
        logger.info("\n[%s] année=%s série=%s", pdf_path.name, meta.year, meta.serie_slug)

        try:
            opus_out = call_opus_extract(client, pdf_path)
        except Exception as exc:
            logger.error("  Opus a échoué : %s", exc)
            n_errors += 1
            journal.append({"pdf": pdf_path.name, "error": str(exc)[:300]})
            continue

        for raw in opus_out.get("exercices") or []:
            ex, reason = to_exercise_dict(meta, raw)
            if ex is None:
                n_rejected += 1
                journal.append(
                    {
                        "pdf": pdf_path.name,
                        "numero_exercice": raw.get("numero_exercice"),
                        "rejet_local": reason,
                    }
                )
                logger.info("  ✗ rejet ex%s : %s", raw.get("numero_exercice"), reason)
                continue
            fig_req = ex.pop("_figure_extract_request", None)
            if fig_req and not args.dry_run:
                ok = extract_figure(pdf_path, fig_req.get("page"), fig_req["filename"])
                if ok:
                    ex["figure"] = fig_req["filename"]
            all_exercises.append(ex)
            n_extracted += 1
            logger.info("  ✓ extrait ex%s (%d sous-questions)", raw.get("numero_exercice"), len(ex["sous_questions"]))

        for raw in opus_out.get("rejected_exercises") or []:
            n_rejected += 1
            journal.append(
                {
                    "pdf": pdf_path.name,
                    "numero_exercice": raw.get("numero_exercice"),
                    "raison_opus": raw.get("raison"),
                }
            )

    logger.info(
        "\n=== Résumé ===\n  extraits : %d\n  rejetés : %d\n  erreurs : %d",
        n_extracted,
        n_rejected,
        n_errors,
    )

    if args.dry_run:
        logger.info("(dry-run) sortie type :")
        sample = {"_comment": "DRY-RUN", "exercices": all_exercises[:2]}
        print(json.dumps(sample, ensure_ascii=False, indent=2))
        return 0

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": (
            f"Exercices Partie 2 extraits depuis les annales DNB maths via "
            f"scripts/extract_math_problemes.py le {datetime.now().isoformat(timespec='seconds')}. "
            f"Sources : {', '.join(p.name for p in pdfs)}. "
            f"{n_extracted} extraits, {n_rejected} rejetés, {n_errors} erreurs."
        ),
        "exercices": all_exercises,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("→ %s écrit (%d exercices)", output_path, n_extracted)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"extract_math_prob_{datetime.now():%Y%m%d_%H%M%S}.json"
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

"""
Extraction des repères chronologiques et spatiaux officiels depuis les PDF
du programme HG-EMC (cycle 4 / fin de 3e).

Ce script est un outil de DÉVELOPPEMENT exécuté OFFLINE par le mainteneur,
pas en production. Il utilise Claude Opus pour analyser finement chaque PDF
de programme et en extraire les repères officiels sous forme structurée
(JSON).

Pourquoi Claude Opus et pas Albert (même logique que `extract_subjects.py`) :
- tâche one-shot, volume limité (< 10 PDF)
- Opus est plus précis sur l'extraction structurée
- pas d'appel en prod → coût négligeable
- Albert n'a pas toujours un parser JSON strict fiable

**Règle cardinale** : ne pas inventer, ne pas déduire, ne pas extrapoler.
Seuls les repères **explicitement listés** dans les programmes officiels ou
les attendus de fin de 3e sont extraits. Qualité > quantité.

Le script produit :
  content/histoire-geo-emc/reperes/_all.json     (liste consolidée)

Schéma d'un repère (cf. modèles dans app/histoire_geo_emc/reperes/models.py) :
  {
    "id": "histoire-1914-debut-premiere-guerre-mondiale",
    "discipline": "histoire" | "geographie" | "emc",
    "type": "date" | "evenement" | "personnage" | "lieu" | "notion" | "definition",
    "theme": "...",
    "libelle": "...",
    "annee": int | null,
    "annee_fin": int | null,
    "periode": "XXe siècle" | null,
    "notions_associees": ["...", "..."],
    "source": "PROGRAMME ... - thème X",
    "niveau_requis": "3e"
  }

Idempotence : SHA256 des fichiers sources stocké dans data/ingest_state.db
(table `reperes_sources`), pour ne re-traiter que les PDF modifiés.

Usage :
    source .env
    .venv/bin/python -m scripts.extract_reperes
    .venv/bin/python -m scripts.extract_reperes --force
    .venv/bin/python -m scripts.extract_reperes --limit 2

Note : la toute première extraction a été faite directement dans une
session Claude Code (lecture PDF via l'outil Read, écriture JSON via Write)
pour éviter un appel API payant. Ce script sert pour les itérations
ultérieures quand on veut refaire tourner l'extraction proprement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pdfplumber
from anthropic import Anthropic

logger = logging.getLogger(__name__)

# ============================================================================
# Constantes
# ============================================================================

OPUS_MODEL = "claude-opus-4-6"

REPO_ROOT = Path(__file__).resolve().parent.parent
PROGRAMME_DIR = REPO_ROOT / "content" / "histoire-geo-emc" / "programme"
METHODO_DIR = REPO_ROOT / "content" / "histoire-geo-emc" / "methodologie"
OUTPUT_DIR = REPO_ROOT / "content" / "histoire-geo-emc" / "reperes"
OUTPUT_FILE = OUTPUT_DIR / "_all.json"

STATE_DB = REPO_ROOT / "data" / "ingest_state.db"

# PDF sources candidats, par ordre de priorité
PRIORITY_SOURCES = [
    "PROGRAMME 4776_annexe1_280567.pdf",
    "PROGRAMME hstoire-geographie-emc-3eme.pdf",
    "PROGRAMME ra16c3c4sereperertemps819126pdf-77163.pdf",
    "PROGRAMME ra16c3c4higeserepererespace819122pdf-77166.pdf",
]


# ============================================================================
# Prompt d'extraction (règle cardinale encodée dans les consignes)
# ============================================================================

EXTRACTION_SYSTEM = """Tu es un expert du programme scolaire français d'histoire-géographie-EMC du cycle 4 (collège, classes de 5e à 3e).

Ta mission : extraire les **repères chronologiques et spatiaux officiels** explicitement listés dans un document de programme ou d'attendus officiels, et rien d'autre.

RÈGLES ABSOLUES :
1. Tu n'extrais QUE les repères EXPLICITEMENT LISTÉS dans le document fourni. Si un repère n'est pas explicitement désigné comme "repère à connaître", "repère de fin de cycle", "repère historique", "repère spatial", ou équivalent, tu l'IGNORES.
2. Tu n'inventes RIEN. Tu ne déduis RIEN. Tu n'extrapoles RIEN.
3. Tu conserves la formulation du document au plus près.
4. Tu réponds uniquement avec un objet JSON valide, rien d'autre — pas de texte avant ou après, pas de ```json``` markdown.
"""


EXTRACTION_USER_TEMPLATE = """Voici le texte extrait d'un PDF de programme ou d'attendus officiels HG-EMC cycle 4 :

<document source="{source_name}">
{text}
</document>

Extrais tous les repères officiels explicitement listés dans ce document et retourne-les sous la forme d'un objet JSON :

{{
  "reperes": [
    {{
      "discipline": "histoire" | "geographie" | "emc",
      "type": "date" | "evenement" | "personnage" | "lieu" | "notion" | "definition",
      "theme": "titre du thème du programme",
      "libelle": "nom du repère tel qu'il sera demandé à un élève",
      "annee": null ou entier,
      "annee_fin": null ou entier,
      "periode": null ou chaîne libre (ex: "XVIIIe siècle", "1945-1989"),
      "notions_associees": [],
      "source_detail": "section du document d'où provient le repère"
    }}
  ]
}}

Règles de remplissage :
- "date" : un point dans le temps. `annee` obligatoire.
- "evenement" : un événement, souvent sur une plage. `annee` et/ou `annee_fin`.
- "personnage" : un nom. `annee`/`annee_fin` = dates de vie ou mandat si connues.
- "lieu" : métropole, fleuve, façade, région, grand ensemble géographique.
- "notion" : un concept (laïcité, mondialisation, métropolisation…).
- "definition" : définition courte d'un terme.
- "libelle" doit être concis et autonome (ex : "Début de la Révolution française", "Paris", "Jean Jaurès", "Laïcité").
- "notions_associees" : 0 à 3 mots-clés utiles pour formuler des indices.

Si le document ne contient AUCUN repère explicitement listé (par exemple parce que c'est une ressource pédagogique méthodologique), retourne `{{"reperes": []}}`.
"""


# ============================================================================
# Idempotence : SHA256 des PDF sources
# ============================================================================


def _ensure_state_db() -> None:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(STATE_DB) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS reperes_sources (
                path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                processed_at TEXT NOT NULL
            )"""
        )


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _already_processed(path: Path) -> bool:
    sha = _file_sha256(path)
    with sqlite3.connect(STATE_DB) as conn:
        row = conn.execute(
            "SELECT sha256 FROM reperes_sources WHERE path = ?", (str(path),)
        ).fetchone()
    return row is not None and row[0] == sha


def _mark_processed(path: Path) -> None:
    sha = _file_sha256(path)
    with sqlite3.connect(STATE_DB) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO reperes_sources (path, sha256, processed_at) "
            "VALUES (?, ?, ?)",
            (str(path), sha, datetime.utcnow().isoformat()),
        )


# ============================================================================
# Extraction
# ============================================================================


def _extract_pdf_text(path: Path) -> str:
    """Concatène le texte de toutes les pages d'un PDF."""
    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            txt = page.extract_text() or ""
            parts.append(f"--- page {i} ---\n{txt}")
    return "\n\n".join(parts)


def _call_opus(client: Anthropic, source_name: str, text: str) -> list[dict]:
    """Appelle Opus en extraction et retourne la liste brute des repères."""
    user_msg = EXTRACTION_USER_TEMPLATE.format(source_name=source_name, text=text)
    resp = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=8000,
        temperature=0,
        system=EXTRACTION_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = resp.content[0].text.strip()
    # Tolère un préambule markdown éventuel au cas où
    if raw.startswith("```"):
        raw = raw.strip("`").split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[: -3]
    data = json.loads(raw)
    return data.get("reperes", [])


def _slugify(text: str) -> str:
    """Slug ascii minuscules + tirets, pour les ids."""
    import re
    import unicodedata

    nfkd = unicodedata.normalize("NFKD", text)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    return slug[:80]  # cap length


def _build_id(repere: dict) -> str:
    disc = repere["discipline"]
    libelle_slug = _slugify(repere["libelle"])
    annee = repere.get("annee")
    if annee is not None:
        return f"{disc}-{annee}-{libelle_slug}"
    return f"{disc}-{repere['type']}-{libelle_slug}"


def _enrich_repere(repere: dict, pdf_name: str) -> dict:
    """Complète les champs manquants et pose l'id."""
    out = {
        "id": _build_id(repere),
        "discipline": repere["discipline"],
        "type": repere["type"],
        "theme": repere.get("theme", ""),
        "libelle": repere["libelle"],
        "annee": repere.get("annee"),
        "annee_fin": repere.get("annee_fin"),
        "periode": repere.get("periode"),
        "notions_associees": repere.get("notions_associees", []) or [],
        "source": f"{pdf_name} - {repere.get('source_detail', '')}".strip(" -"),
        "niveau_requis": "3e",
    }
    return out


# ============================================================================
# Pilotage
# ============================================================================


def _list_sources() -> list[Path]:
    sources: list[Path] = []
    # Priorité explicite
    for name in PRIORITY_SOURCES:
        p = PROGRAMME_DIR / name
        if p.exists():
            sources.append(p)
    # Puis le reste du dossier programme
    for p in sorted(PROGRAMME_DIR.glob("*.pdf")):
        if p not in sources:
            sources.append(p)
    return sources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="retraite les PDF déjà vus"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="ne traite que N PDF"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if "ANTHROPIC_API_KEY" not in os.environ:
        logger.error("ANTHROPIC_API_KEY manquante — exécute `source .env` avant")
        return 2

    _ensure_state_db()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    client = Anthropic()
    sources = _list_sources()
    if args.limit:
        sources = sources[: args.limit]

    all_reperes: list[dict] = []
    seen_ids: set[str] = set()
    used_sources: list[str] = []

    for pdf in sources:
        if not args.force and _already_processed(pdf):
            logger.info("skip (déjà traité, même sha256) : %s", pdf.name)
            continue

        logger.info("extraction : %s", pdf.name)
        try:
            text = _extract_pdf_text(pdf)
            raw_reperes = _call_opus(client, pdf.name, text)
        except Exception as exc:  # pragma: no cover
            logger.error("échec sur %s : %s", pdf.name, exc)
            continue

        added = 0
        for r in raw_reperes:
            enriched = _enrich_repere(r, pdf.name)
            if enriched["id"] in seen_ids:
                continue
            seen_ids.add(enriched["id"])
            all_reperes.append(enriched)
            added += 1
        logger.info("  %d repères ajoutés (sur %d bruts)", added, len(raw_reperes))

        used_sources.append(str(pdf.relative_to(REPO_ROOT)))
        _mark_processed(pdf)

    output = {
        "generated_from": used_sources,
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d"),
        "extraction_method": "scripts/extract_reperes.py (Claude Opus offline)",
        "total": len(all_reperes),
        "reperes": all_reperes,
    }

    OUTPUT_FILE.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("écrit %d repères dans %s", len(all_reperes), OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())

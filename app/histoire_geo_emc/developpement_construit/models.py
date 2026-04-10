"""
Modèle et chargement des sujets « développement construit » (histoire-géo-EMC).

Ce module est spécifique à la matière histoire-géo-EMC — il définit :
- le modèle SQLModel `Subject` (un DC extrait d'une annale ou une variation) ;
- le chargement idempotent depuis `content/histoire-geo-emc/subjects/*.json`
  au démarrage de l'app ;
- les helpers de tirage (`random_subject`, `get_subject`).

Côté persistance, on partage la même base SQLite que les autres matières
(cf. `app/core/db.py`). La table est actuellement nommée `subject` (comportement
par défaut de SQLModel sur la classe `Subject`). Une éventuelle évolution vers
`SubjectHGEMC` est repoussée à une étape ultérieure du refacto.
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path

from sqlmodel import Field, Session as DBSession, SQLModel, select

from app.core.db import get_engine

logger = logging.getLogger(__name__)

# app/histoire_geo_emc/developpement_construit/models.py → racine = 4 parents.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SUBJECTS_DIR = REPO_ROOT / "content" / "histoire-geo-emc" / "subjects"
# Sujets générés offline par scripts/generate_variations.py — format JSON
# identique mais marqués is_variation=True en base.
VARIATIONS_DIR = SUBJECTS_DIR / "variations"


# ============================================================================
# Modèle
# ============================================================================


class Subject(SQLModel, table=True):
    """Un développement construit extrait d'une annale DNB histoire-géo-EMC."""

    id: int | None = Field(default=None, primary_key=True)
    source_file: str  # ex: "18genhgemcan1pdf-80388.pdf"
    dc_index: int  # index du DC dans le fichier (0, 1, ...)
    year: int | None = None
    serie: str | None = None
    session: str | None = None
    session_label: str | None = None
    discipline: str  # "histoire" | "geographie" | "emc"
    theme: str
    consigne: str
    verbe_cle: str | None = None
    bornes_chrono: str | None = None
    bornes_spatiales: str | None = None
    notions_attendues_json: str = "[]"  # JSON-encoded list[str]
    bareme_points: int | None = None
    # True pour les sujets produits offline par scripts/generate_variations.py
    # (variations Opus de sujets réels). False pour les sujets issus des annales.
    is_variation: bool = Field(default=False)

    @property
    def notions_attendues(self) -> list[str]:
        try:
            return json.loads(self.notions_attendues_json)
        except json.JSONDecodeError:
            return []


# ============================================================================
# Chargement des sujets depuis content/histoire-geo-emc/subjects/*.json
# ============================================================================


def init_hgemc_subjects() -> int:
    """Charge les sujets DC à chaque startup. Idempotent via `(source_file, dc_index)`."""
    with DBSession(get_engine()) as s:
        n = load_subjects_from_jsons(s)
        if n > 0:
            logger.info("Chargé %d nouveaux sujets DC depuis %s", n, SUBJECTS_DIR)
        return n


def load_subjects_from_jsons(s: DBSession) -> int:
    """Insère tous les DC présents dans content/histoire-geo-emc/subjects/*.json.
    Idempotent.

    Le format des JSON est celui produit par scripts/extract_subjects.py :
    chaque fichier contient une liste `developpements_construits` avec un ou
    plusieurs DC. On en fait un Subject par entrée.

    Les fichiers situés dans `content/histoire-geo-emc/subjects/variations/`
    sont chargés avec le flag `is_variation=True` (générés offline par
    scripts/generate_variations.py).
    """
    inserted = 0
    # 1. Sujets réels d'annales
    if SUBJECTS_DIR.exists():
        for json_path in sorted(SUBJECTS_DIR.glob("*.json")):
            if json_path.name == "_all.json":
                continue
            inserted += _load_subject_file(s, json_path, is_variation=False)
    else:
        logger.warning("Répertoire de sujets introuvable : %s", SUBJECTS_DIR)

    # 2. Variations générées offline
    if VARIATIONS_DIR.exists():
        for json_path in sorted(VARIATIONS_DIR.glob("*.json")):
            if json_path.name == "_all.json":
                continue
            inserted += _load_subject_file(s, json_path, is_variation=True)

    s.commit()
    return inserted


def _load_subject_file(
    s: DBSession, json_path: Path, is_variation: bool
) -> int:
    """Charge un fichier JSON de sujets. Renvoie le nombre de DC insérés."""
    try:
        data = json.loads(json_path.read_text())
    except json.JSONDecodeError as e:
        logger.error("JSON invalide %s : %s", json_path.name, e)
        return 0

    source_file = data.get("source_file") or json_path.stem
    inserted = 0
    for idx, dc in enumerate(data.get("developpements_construits", [])):
        consigne = (dc.get("consigne") or "").strip()
        theme = (dc.get("theme") or "").strip()
        discipline = (dc.get("discipline") or "").strip()
        if not (consigne and theme and discipline):
            continue
        # Idempotence : on saute si (source_file, dc_index) existe déjà.
        already = s.exec(
            select(Subject).where(
                Subject.source_file == source_file,
                Subject.dc_index == idx,
            )
        ).first()
        if already is not None:
            continue

        subj = Subject(
            source_file=source_file,
            dc_index=idx,
            year=data.get("year"),
            serie=data.get("serie"),
            session=data.get("session"),
            session_label=data.get("session_label"),
            discipline=discipline,
            theme=theme,
            consigne=consigne,
            verbe_cle=dc.get("verbe_cle"),
            bornes_chrono=dc.get("bornes_chrono"),
            bornes_spatiales=dc.get("bornes_spatiales"),
            notions_attendues_json=json.dumps(
                dc.get("notions_attendues") or [], ensure_ascii=False
            ),
            bareme_points=dc.get("bareme_points"),
            is_variation=is_variation,
        )
        s.add(subj)
        inserted += 1
    return inserted


# ============================================================================
# Helpers de tirage
# ============================================================================


def random_subject(
    s: DBSession,
    discipline: str | None = None,
    is_variation: bool | None = None,
) -> Subject | None:
    """Tire un sujet au hasard. Filtrable par discipline et par origine.

    - `is_variation=False` : uniquement les sujets d'annales réelles.
    - `is_variation=True`  : uniquement les variations générées offline.
    - `is_variation=None`  : tous confondus.
    """
    stmt = select(Subject)
    if discipline:
        stmt = stmt.where(Subject.discipline == discipline)
    if is_variation is not None:
        stmt = stmt.where(Subject.is_variation == is_variation)
    rows = list(s.exec(stmt).all())
    if not rows:
        return None
    return random.choice(rows)


def get_subject(s: DBSession, subject_id: int) -> Subject | None:
    return s.get(Subject, subject_id)


__all__ = [
    "Subject",
    "SUBJECTS_DIR",
    "VARIATIONS_DIR",
    "init_hgemc_subjects",
    "load_subjects_from_jsons",
    "random_subject",
    "get_subject",
]

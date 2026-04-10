"""Chargement du catalogue des sujets de rédaction depuis les JSON.

À appeler depuis ``app.core.main.on_startup`` après ``init_db()``. Idempotent :
met à jour un sujet existant (upsert sur ``slug``) ou en crée un nouveau. Ne
supprime jamais un sujet absent des JSON.

Si le dossier ``content/francais/redaction/subjects/`` est vide ou absent
(cas tant que l'extraction Opus n'a pas encore tourné), la fonction loggue un
avertissement et renvoie 0 sans échouer — le reste de l'app démarre
normalement.
"""

from __future__ import annotations

import json
import logging
import random
import unicodedata
from pathlib import Path

from sqlmodel import Session as DBSession, select

from app.core.db import get_engine
from app.francais.comprehension.loader import get_exercise_by_slug
from app.francais.redaction.models import (
    FrenchRedactionSubject,
    RedactionSubject,
)

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parent.parent.parent.parent
SUBJECTS_DIR = REPO_ROOT / "content" / "francais" / "redaction" / "subjects"


def _slugify_centre(centre: str) -> str:
    """Normalise un nom de centre en slug ASCII minuscules tirets.

    Doit être cohérent avec la fonction ``FilenameMeta.make_id`` de
    ``scripts/extract_french_exercises.py`` qui produit les slugs des
    ``FrenchExercise`` à partir des noms de fichier d'annales (où les
    centres sont déjà sans accents : ``Metropole``, ``Antilles-Guyane``…).
    Côté rédaction, le label en DB est en revanche le centre humain
    (``Métropole``, ``Antilles-Guyane``) — d'où l'étape de désaccentuation.
    """
    # NFD décompose ``é`` en ``e`` + accent combinant ; on retire les
    # accents combinants puis on lowercase et on remplace les espaces.
    nfd = unicodedata.normalize("NFD", centre)
    no_accent = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return no_accent.lower().replace(" ", "-")


def _best_effort_comprehension_slug(
    session: DBSession, annee: int, centre: str
) -> str | None:
    """Tente d'associer un sujet de rédaction à l'exercice de compréhension
    de la même année et du même centre.

    Les slugs de ``FrenchExercise`` suivent le pattern ``{annee}_{centre-slug}``
    ou ``{annee}_{centre-slug}_{variant}``. On normalise le centre (minuscules,
    tirets, sans accents) et on teste le slug canonique. Si on ne trouve
    rien, on renvoie ``None`` — la feature fonctionne sans lien.
    """
    slug_base = f"{annee}_{_slugify_centre(centre)}"
    row = get_exercise_by_slug(session, slug_base)
    if row is not None:
        return row.slug
    return None


def load_redaction_subjects(
    session: DBSession, *, subjects_dir: Path = SUBJECTS_DIR
) -> int:
    if not subjects_dir.exists():
        logger.warning(
            "Dossier sujets rédaction introuvable : %s (extraction Opus pas encore lancée ?)",
            subjects_dir,
        )
        return 0

    json_files = sorted(
        p for p in subjects_dir.glob("*.json") if p.name != "_all.json"
    )
    if not json_files:
        logger.info("Aucun sujet rédaction à charger dans %s", subjects_dir)
        return 0

    count = 0
    errors: list[tuple[str, str]] = []

    for path in json_files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            subj = RedactionSubject.model_validate(raw)
        except Exception as e:
            errors.append((path.name, str(e)))
            continue

        texte_support_ref = subj.texte_support_ref or _best_effort_comprehension_slug(
            session, subj.source.annee, subj.source.centre
        )

        data_json = json.dumps(raw, ensure_ascii=False)

        existing = session.exec(
            select(FrenchRedactionSubject).where(
                FrenchRedactionSubject.slug == subj.id
            )
        ).first()

        if existing is None:
            row = FrenchRedactionSubject(
                slug=subj.id,
                source_file=subj.source_file or path.name,
                annee=subj.source.annee,
                centre=subj.source.centre,
                texte_support_ref=texte_support_ref,
                data_json=data_json,
            )
            session.add(row)
        else:
            existing.source_file = subj.source_file or path.name
            existing.annee = subj.source.annee
            existing.centre = subj.source.centre
            existing.texte_support_ref = texte_support_ref
            existing.data_json = data_json
            session.add(existing)

        count += 1

    session.commit()

    logger.info(
        "Français rédaction : %d sujets chargés (%d erreurs)",
        count,
        len(errors),
    )
    for name, err in errors:
        logger.warning("  - %s : %s", name, err[:200])

    return count


def init_french_redaction() -> int:
    """Charge les sujets rédaction si la table est vide. Idempotent."""
    with DBSession(get_engine()) as s:
        existing = s.exec(select(FrenchRedactionSubject).limit(1)).first()
        if existing is not None:
            return 0
        n = load_redaction_subjects(s)
        logger.info("Chargé %d sujets de rédaction français", n)
        return n


def pick_subject(
    session: DBSession, *, annee: int | None = None
) -> FrenchRedactionSubject | None:
    stmt = select(FrenchRedactionSubject)
    if annee is not None:
        stmt = stmt.where(FrenchRedactionSubject.annee == annee)
    rows = list(session.exec(stmt).all())
    if not rows:
        return None
    return random.choice(rows)


def get_subject(
    session: DBSession, subject_id: int
) -> FrenchRedactionSubject | None:
    return session.get(FrenchRedactionSubject, subject_id)


def list_subjects(session: DBSession) -> list[FrenchRedactionSubject]:
    return list(
        session.exec(
            select(FrenchRedactionSubject).order_by(
                FrenchRedactionSubject.annee.desc()  # type: ignore[attr-defined]
            )
        ).all()
    )


__all__ = [
    "SUBJECTS_DIR",
    "load_redaction_subjects",
    "init_french_redaction",
    "pick_subject",
    "get_subject",
    "list_subjects",
]

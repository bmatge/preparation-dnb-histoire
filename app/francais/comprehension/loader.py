"""Chargement du catalogue d'exercices de compréhension depuis les JSON.

À appeler depuis `app.core.main.on_startup` après `init_db()`. Idempotent :
met à jour un exercice existant (upsert sur `slug`) ou en crée un nouveau.
Ne supprime jamais un exercice absent des JSON (si on retire un PDF du
dossier, le vieux row reste en base tant qu'on ne drop pas explicitement).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlmodel import Session as DBSession, select

from app.core.db import get_engine
from app.francais.comprehension.models import (
    ComprehensionExercise,
    FrenchExercise,
)

logger = logging.getLogger(__name__)

# Racine du repo : app/francais/comprehension/loader.py → ../../..
_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parent.parent.parent.parent
EXERCISES_DIR = REPO_ROOT / "content" / "francais" / "comprehension" / "exercises"


def load_exercises(session: DBSession, *, exercises_dir: Path = EXERCISES_DIR) -> int:
    """Charge (ou met à jour) tous les exercices depuis `exercises_dir`.

    Retourne le nombre d'exercices chargés avec succès. Les fichiers qui
    échouent à la validation Pydantic sont loggés et ignorés individuellement.
    """
    if not exercises_dir.exists():
        logger.warning(
            "Dossier exercices français introuvable : %s", exercises_dir
        )
        return 0

    json_files = sorted(
        p for p in exercises_dir.glob("*.json") if p.name != "_all.json"
    )
    if not json_files:
        logger.info("Aucun exercice français à charger dans %s", exercises_dir)
        return 0

    count = 0
    errors: list[tuple[str, str]] = []

    for path in json_files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            exo = ComprehensionExercise.model_validate(raw)
        except Exception as e:
            errors.append((path.name, str(e)))
            continue

        existing = session.exec(
            select(FrenchExercise).where(FrenchExercise.slug == exo.id)
        ).first()

        data_json = json.dumps(raw, ensure_ascii=False)

        if existing is None:
            row = FrenchExercise(
                slug=exo.id,
                source_file=exo.source_file or path.name,
                annee=exo.source.annee,
                centre=exo.source.centre,
                data_json=data_json,
            )
            session.add(row)
        else:
            existing.source_file = exo.source_file or path.name
            existing.annee = exo.source.annee
            existing.centre = exo.source.centre
            existing.data_json = data_json
            session.add(existing)

        count += 1

    session.commit()

    logger.info(
        "Français compréhension : %d exercices chargés (%d erreurs)",
        count,
        len(errors),
    )
    for name, err in errors:
        logger.warning("  ✗ %s : %s", name, err[:200])

    return count


def init_french_comprehension() -> int:
    """Charge les exercices français de compréhension si la table est vide.

    À appeler depuis `app.core.main.on_startup` après `core.db.init_db()`.
    Idempotent : ne fait rien si la table `french_exercise` a déjà des
    lignes (mirroir du pattern `init_hgemc_subjects` / `init_reperes`).
    Retourne le nombre d'exercices insérés (0 si déjà peuplée).
    """
    with DBSession(get_engine()) as s:
        existing = s.exec(select(FrenchExercise).limit(1)).first()
        if existing is not None:
            return 0
        n = load_exercises(s)
        logger.info("Chargé %d exercices français compréhension", n)
        return n


def pick_exercise(
    session: DBSession,
    *,
    annee: int | None = None,
    exclude_ids: list[int] | None = None,
) -> FrenchExercise | None:
    """Tire un exercice (optionnellement filtré par année) au hasard."""
    import random

    stmt = select(FrenchExercise)
    if annee is not None:
        stmt = stmt.where(FrenchExercise.annee == annee)
    if exclude_ids:
        stmt = stmt.where(~FrenchExercise.id.in_(exclude_ids))  # type: ignore[attr-defined]
    rows = list(session.exec(stmt).all())
    if not rows:
        return None
    return random.choice(rows)


def get_exercise(session: DBSession, exercise_id: int) -> FrenchExercise | None:
    return session.get(FrenchExercise, exercise_id)


def get_exercise_by_slug(
    session: DBSession, slug: str
) -> FrenchExercise | None:
    return session.exec(
        select(FrenchExercise).where(FrenchExercise.slug == slug)
    ).first()


def list_exercises(session: DBSession) -> list[FrenchExercise]:
    return list(
        session.exec(select(FrenchExercise).order_by(FrenchExercise.annee.desc())).all()  # type: ignore[attr-defined]
    )


__all__ = [
    "EXERCISES_DIR",
    "load_exercises",
    "init_french_comprehension",
    "pick_exercise",
    "get_exercise",
    "get_exercise_by_slug",
    "list_exercises",
]

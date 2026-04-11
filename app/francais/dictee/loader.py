"""Chargement du catalogue de dictées depuis les JSON.

À appeler depuis `app.core.main.on_startup` après `init_db()`. Idempotent :
upsert sur `slug`. Ne supprime jamais une dictée absente des JSON.
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path

from sqlmodel import Session as DBSession, select

from app.core.db import get_engine
from app.francais.dictee.models import Dictee, FrenchDictee

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parent.parent.parent.parent
EXERCISES_DIR = REPO_ROOT / "content" / "francais" / "dictee" / "exercises"
AUDIO_DIR = REPO_ROOT / "content" / "francais" / "dictee" / "audio"


def load_dictees(session: DBSession, *, exercises_dir: Path = EXERCISES_DIR) -> int:
    if not exercises_dir.exists():
        logger.warning("Dossier dictées introuvable : %s", exercises_dir)
        return 0

    json_files = sorted(
        p for p in exercises_dir.glob("*.json") if p.name != "_all.json"
    )
    if not json_files:
        logger.info("Aucune dictée à charger dans %s", exercises_dir)
        return 0

    count = 0
    errors: list[tuple[str, str]] = []

    for path in json_files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            dictee = Dictee.model_validate(raw)
        except Exception as e:
            errors.append((path.name, str(e)))
            continue

        data_json = json.dumps(raw, ensure_ascii=False)
        existing = session.exec(
            select(FrenchDictee).where(FrenchDictee.slug == dictee.id)
        ).first()

        if existing is None:
            row = FrenchDictee(
                slug=dictee.id,
                source_file=dictee.source_file or path.name,
                annee=dictee.source.annee,
                centre=dictee.source.centre,
                data_json=data_json,
            )
            session.add(row)
        else:
            existing.source_file = dictee.source_file or path.name
            existing.annee = dictee.source.annee
            existing.centre = dictee.source.centre
            existing.data_json = data_json
            session.add(existing)
        count += 1

    session.commit()
    logger.info("Français dictée : %d dictées chargées (%d erreurs)", count, len(errors))
    for name, err in errors:
        logger.warning("  - %s : %s", name, err[:200])
    return count


def init_french_dictee() -> int:
    """Charge les dictées à chaque startup, idempotent via upsert sur slug."""
    with DBSession(get_engine()) as s:
        n = load_dictees(s)
        if n > 0:
            logger.info("Chargé %d dictées français", n)
        return n


def pick_dictee(
    session: DBSession,
    *,
    annee: int | None = None,
    exclude_ids: list[int] | None = None,
) -> FrenchDictee | None:
    stmt = select(FrenchDictee)
    if annee is not None:
        stmt = stmt.where(FrenchDictee.annee == annee)
    if exclude_ids:
        stmt = stmt.where(~FrenchDictee.id.in_(exclude_ids))  # type: ignore[attr-defined]
    rows = list(session.exec(stmt).all())
    if not rows:
        return None
    return random.choice(rows)


def get_dictee(session: DBSession, dictee_id: int) -> FrenchDictee | None:
    return session.get(FrenchDictee, dictee_id)


def get_dictee_by_slug(session: DBSession, slug: str) -> FrenchDictee | None:
    return session.exec(
        select(FrenchDictee).where(FrenchDictee.slug == slug)
    ).first()


def list_dictees(session: DBSession) -> list[FrenchDictee]:
    return list(
        session.exec(
            select(FrenchDictee).order_by(FrenchDictee.annee.desc())  # type: ignore[attr-defined]
        ).all()
    )


__all__ = [
    "EXERCISES_DIR",
    "AUDIO_DIR",
    "load_dictees",
    "init_french_dictee",
    "pick_dictee",
    "get_dictee",
    "get_dictee_by_slug",
    "list_dictees",
]

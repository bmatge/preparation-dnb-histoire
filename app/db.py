"""
Persistance locale — sessions élèves, tours de conversation, sujets DC.

Stack : SQLModel sur SQLite (data/app.db). Une seule base, gérée en mémoire
côté FastAPI via un engine global. On ne fait PAS de migrations Alembic à ce
stade : l'app est jeune, les schémas évoluent, on assume les drops manuels.

Modèle :
- `Subject` : un développement construit extrait des annales (un sujet par
  ligne, plusieurs sujets par fichier d'annale possible). Chargé au démarrage
  depuis `content/histoire-geo-emc/subjects/*.json` (voir `load_subjects_from_jsons`). Idempotent.
- `Session` : une session élève (mode + sujet tiré + état d'avancement).
- `Turn` : un échange dans la session (étape + rôle + contenu).

Helpers principaux :
- `init_db()` : crée les tables et charge les sujets si nécessaire.
- `random_subject(discipline=None)` : tire un sujet au hasard.
- `create_session(subject_id, mode)` / `add_turn(...)` / `get_turns(session_id)`.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Iterator

from sqlmodel import Field, Session as DBSession, SQLModel, create_engine, select

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "app.db"
SUBJECTS_DIR = REPO_ROOT / "content" / "histoire-geo-emc" / "subjects"
# Sujets générés offline par scripts/generate_variations.py — format JSON
# identique mais marqués is_variation=True en base.
VARIATIONS_DIR = SUBJECTS_DIR / "variations"


# ============================================================================
# Modèles
# ============================================================================


class Subject(SQLModel, table=True):
    """Un développement construit extrait d'une annale DNB."""

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


class Session(SQLModel, table=True):
    """Une session de travail d'un·e élève."""

    id: int | None = Field(default=None, primary_key=True)
    subject_id: int = Field(foreign_key="subject.id")
    mode: str  # "semi_assiste" pour le MVP
    current_step: int = 1  # 1..7
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Turn(SQLModel, table=True):
    """Un message dans une session (input élève ou réponse Albert)."""

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="session.id", index=True)
    step: int  # 1..7
    role: str  # "user" | "assistant"
    content: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# Engine + init
# ============================================================================

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
)


def get_engine():
    return _engine


def db_session() -> Iterator[DBSession]:
    """Dependency FastAPI : ouvre/ferme une session SQLModel par requête."""
    with DBSession(_engine) as session:
        yield session


def init_db() -> None:
    """Crée les tables et charge les sujets s'il n'y en a pas encore."""
    SQLModel.metadata.create_all(_engine)
    with DBSession(_engine) as s:
        existing = s.exec(select(Subject).limit(1)).first()
        if existing is None:
            n = load_subjects_from_jsons(s)
            logger.info("Chargé %d sujets DC depuis %s", n, SUBJECTS_DIR)


# ============================================================================
# Chargement des sujets depuis content/histoire-geo-emc/subjects/*.json
# ============================================================================


def load_subjects_from_jsons(s: DBSession) -> int:
    """Insère tous les DC présents dans content/histoire-geo-emc/subjects/*.json. Idempotent.

    Le format des JSON est celui produit par scripts/extract_subjects.py :
    chaque fichier contient une liste `developpements_construits` avec un ou
    plusieurs DC. On en fait un Subject par entrée.

    Les fichiers situés dans `content/histoire-geo-emc/subjects/variations/` sont chargés avec le
    flag `is_variation=True` (générés offline par scripts/generate_variations.py).
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
# Helpers métier
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


def create_session(
    s: DBSession, subject_id: int, mode: str = "semi_assiste"
) -> Session:
    sess = Session(subject_id=subject_id, mode=mode)
    s.add(sess)
    s.commit()
    s.refresh(sess)
    return sess


def get_session(s: DBSession, session_id: int) -> Session | None:
    return s.get(Session, session_id)


def update_session_step(s: DBSession, session_id: int, step: int) -> None:
    sess = s.get(Session, session_id)
    if sess is None:
        return
    sess.current_step = step
    s.add(sess)
    s.commit()


def add_turn(
    s: DBSession, session_id: int, step: int, role: str, content: str
) -> Turn:
    turn = Turn(session_id=session_id, step=step, role=role, content=content)
    s.add(turn)
    s.commit()
    s.refresh(turn)
    return turn


def get_turns(s: DBSession, session_id: int) -> list[Turn]:
    rows = s.exec(
        select(Turn).where(Turn.session_id == session_id).order_by(Turn.id)
    ).all()
    return list(rows)


def get_turns_by_step(s: DBSession, session_id: int, step: int) -> list[Turn]:
    rows = s.exec(
        select(Turn)
        .where(Turn.session_id == session_id, Turn.step == step)
        .order_by(Turn.id)
    ).all()
    return list(rows)


def get_last_user_turn(
    s: DBSession, session_id: int, step: int
) -> Turn | None:
    """Renvoie la dernière contribution élève pour une étape donnée."""
    rows = s.exec(
        select(Turn)
        .where(
            Turn.session_id == session_id,
            Turn.step == step,
            Turn.role == "user",
        )
        .order_by(Turn.id.desc())
    ).all()
    return rows[0] if rows else None


__all__ = [
    "Subject",
    "Session",
    "Turn",
    "init_db",
    "get_engine",
    "db_session",
    "load_subjects_from_jsons",
    "random_subject",
    "get_subject",
    "create_session",
    "get_session",
    "update_session_step",
    "add_turn",
    "get_turns",
    "get_turns_by_step",
    "get_last_user_turn",
]

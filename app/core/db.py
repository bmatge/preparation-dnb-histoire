"""
Persistance locale partagée — sessions élèves et tours de conversation.

Ce module est **mutualisé entre toutes les matières** de la plateforme. Il
fournit l'engine SQLite, la dépendance FastAPI `db_session`, et les deux
modèles partagés :
- `Session` : une session élève (matière + sujet tiré + étape).
- `Turn`    : un échange dans la session (étape + rôle + contenu).

Le modèle `Subject` et les helpers de tirage **ne vivent pas ici** — ils
sont spécifiques à chaque matière (cf. `app/histoire_geo_emc/models.py`
pour le DC histoire-géo-EMC). SQLModel enregistre toutes les tables dans
la même métadata, il suffit donc que le sous-module soit importé avant
`init_db()` pour que la table `subject` soit créée.

Stack : SQLModel sur SQLite (data/app.db). Pas de migrations Alembic à ce
stade : l'app est jeune, les schémas évoluent, on assume les drops manuels.

Helpers principaux :
- `init_db()` : crée les tables enregistrées dans SQLModel.metadata.
- `create_session(subject_id, mode)` / `add_turn(...)` / `get_turns(...)`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Iterator

from sqlmodel import Field, Session as DBSession, SQLModel, create_engine, select

logger = logging.getLogger(__name__)

# app/core/db.py → remonter de 3 niveaux pour atteindre la racine du repo.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = REPO_ROOT / "data" / "app.db"


# ============================================================================
# Modèles partagés
# ============================================================================


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
    """Crée les tables SQLModel.

    Le chargement des contenus métier (sujets DC, etc.) est la responsabilité
    de chaque sous-module de matière, appelé depuis `app.core.main.on_startup`
    après cet `init_db`.
    """
    SQLModel.metadata.create_all(_engine)


# ============================================================================
# Helpers métier partagés (session / turns)
# ============================================================================


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
    "Session",
    "Turn",
    "init_db",
    "get_engine",
    "db_session",
    "create_session",
    "get_session",
    "update_session_step",
    "add_turn",
    "get_turns",
    "get_turns_by_step",
    "get_last_user_turn",
    "DB_PATH",
]

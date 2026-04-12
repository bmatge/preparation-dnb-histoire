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

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Session as DBSession, SQLModel, create_engine, select

logger = logging.getLogger(__name__)

# app/core/db.py → remonter de 3 niveaux pour atteindre la racine du repo.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = REPO_ROOT / "data" / "app.db"


# ============================================================================
# Modèles partagés
# ============================================================================


class Session(SQLModel, table=True):
    """Une session de travail d'un·e élève.

    `subject_kind` identifie l'épreuve — plusieurs valeurs coexistent au
    sein d'une même matière. Pour HG-EMC on a :
      - "hgemc_dc"      : développement construit (7 étapes, pointe un
                          `Subject` tiré des annales via `subject_id`).
      - "hgemc_reperes" : quiz de repères chronologiques et spatiaux
                          (pas de Subject — `subject_id` reste NULL).

    Les autres matières (français, etc.) définissent leurs propres valeurs.
    Pas d'enum : chaque sous-package matière est libre de sa nomenclature.

    `subject_id` est donc nullable : certaines épreuves (comme les repères)
    tirent leur contenu d'une banque dédiée sans passer par `Subject`.
    """

    id: int | None = Field(default=None, primary_key=True)
    user_key: str | None = Field(default=None, index=True)
    subject_kind: str = Field(default="hgemc_dc", index=True)
    subject_id: int | None = Field(default=None, foreign_key="subject.id")
    mode: str = "semi_assiste"  # "semi_assiste" pour le MVP
    current_step: int = 1  # signification propre à chaque épreuve
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Turn(SQLModel, table=True):
    """Un message dans une session (input élève ou réponse Albert)."""

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="session.id", index=True)
    step: int  # 1..7
    role: str  # "user" | "assistant"
    content: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UserProgress(SQLModel, table=True):
    """Progression d'un eleve sur une question/item.

    Table unifiee pour toutes les matieres, identifiee par le triplet
    (user_key, subject_kind, item_id). Alimentee en ecriture double a cote
    des tables d'attempts existantes (RepereAttempt, AutoAttempt, etc.).
    """

    __tablename__ = "userprogress"
    __table_args__ = (
        UniqueConstraint("user_key", "subject_kind", "item_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_key: str = Field(index=True)
    subject_kind: str = Field(index=True)
    item_id: str = Field(index=True)
    status: str  # "reussi" | "rate" | "en_cours"
    attempts: int = 1
    first_seen_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)


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


def _schema_matches() -> bool:
    """Vérifie que les colonnes SQL correspondent au schéma SQLModel déclaré.

    Compare les colonnes de chaque table SQLModel existante en DB avec celles
    déclarées en Python. Retourne False dès qu'une colonne manque. N'utilise
    pas Alembic : on reste sur le principe drop & recharge (cf. CLAUDE.md).
    """
    import sqlite3

    if not DB_PATH.exists():
        return True  # la DB va être créée de zéro, pas de divergence possible

    conn = sqlite3.connect(DB_PATH)
    try:
        for table in SQLModel.metadata.sorted_tables:
            cursor = conn.execute(f"PRAGMA table_info('{table.name}')")
            db_cols = {row[1] for row in cursor.fetchall()}
            if not db_cols:
                continue  # table pas encore créée — create_all la gèrera
            model_cols = {col.name for col in table.columns}
            missing = model_cols - db_cols
            if missing:
                logger.warning(
                    "Colonnes manquantes dans %s : %s — drop & recreate de la DB",
                    table.name,
                    missing,
                )
                return False
    finally:
        conn.close()
    return True


def init_db() -> None:
    """Crée les tables SQLModel, en supprimant la DB si le schéma a divergé.

    Le chargement des contenus métier (sujets DC, etc.) est la responsabilité
    de chaque sous-module de matière, appelé depuis `app.core.main.on_startup`
    après cet `init_db`.

    Si des colonnes sont manquantes dans une table existante (schéma Python
    a évolué entre deux déploiements), la DB est supprimée puis recréée. Les
    données runtime (sessions, attempts) sont perdues mais les contenus métier
    sont idempotents (rechargés au startup suivant). Pas de migration Alembic.
    """
    if not _schema_matches():
        logger.info("Suppression de %s (schéma obsolète)", DB_PATH)
        DB_PATH.unlink(missing_ok=True)
        # Recréer l'engine pour pointer vers le fichier frais (SQLite ouvre à
        # la première connexion, le fichier sera recréé par create_all).
        global _engine
        _engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
    SQLModel.metadata.create_all(_engine)


# ============================================================================
# Helpers métier partagés (session / turns)
# ============================================================================


def create_session(
    s: DBSession,
    subject_id: int | None = None,
    mode: str = "semi_assiste",
    subject_kind: str = "hgemc_dc",
    user_key: str | None = None,
) -> Session:
    """Crée une nouvelle session élève.

    `subject_kind` identifie l'épreuve (ex. "hgemc_dc", "hgemc_reperes").
    `subject_id` est optionnel — il ne sert qu'aux épreuves qui pointent
    vers une ligne `Subject` (cf. DC). Les épreuves type quiz le laissent
    à None.
    `user_key` relie la session à un élève identifié par sa clé localStorage.
    """
    sess = Session(
        subject_kind=subject_kind,
        subject_id=subject_id,
        mode=mode,
        user_key=user_key,
    )
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


# ============================================================================
# Helpers progression élève (UserProgress)
# ============================================================================


def record_progress(
    s: DBSession,
    user_key: str,
    subject_kind: str,
    item_id: str,
    success: bool,
) -> UserProgress:
    """Upsert de la progression d'un élève sur un item.

    La réussite est un état absorbant : une fois « reussi », le statut ne
    repasse jamais à « rate ». Le compteur d'attempts est incrémenté à chaque
    appel.
    """
    row = s.exec(
        select(UserProgress).where(
            UserProgress.user_key == user_key,
            UserProgress.subject_kind == subject_kind,
            UserProgress.item_id == item_id,
        )
    ).first()

    now = datetime.utcnow()
    if row is None:
        row = UserProgress(
            user_key=user_key,
            subject_kind=subject_kind,
            item_id=item_id,
            status="reussi" if success else "rate",
            attempts=1,
            first_seen_at=now,
            last_seen_at=now,
        )
    else:
        row.attempts += 1
        row.last_seen_at = now
        if success:
            row.status = "reussi"
        # Si déjà « reussi », on ne repasse pas à « rate » (absorbant).
        # Si « rate » et encore raté, on reste « rate ».

    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def get_progress_counts(
    s: DBSession,
    user_key: str,
    subject_kind: str,
) -> dict[str, int]:
    """Compte les items par statut pour un élève et une épreuve."""
    rows = s.exec(
        select(UserProgress.status).where(
            UserProgress.user_key == user_key,
            UserProgress.subject_kind == subject_kind,
        )
    ).all()
    counts: dict[str, int] = {"reussi": 0, "rate": 0, "en_cours": 0}
    for status in rows:
        if status in counts:
            counts[status] += 1
    return counts


def get_item_ids_by_status(
    s: DBSession,
    user_key: str,
    subject_kind: str,
    status: str,
) -> list[str]:
    """Renvoie les item_id ayant le statut demandé pour un élève et une épreuve."""
    rows = s.exec(
        select(UserProgress.item_id).where(
            UserProgress.user_key == user_key,
            UserProgress.subject_kind == subject_kind,
            UserProgress.status == status,
        )
    ).all()
    return list(rows)


__all__ = [
    "Session",
    "Turn",
    "UserProgress",
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
    "record_progress",
    "get_progress_counts",
    "get_item_ids_by_status",
    "DB_PATH",
]

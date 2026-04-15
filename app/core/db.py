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
from datetime import datetime, timedelta
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


def _missing_columns_per_table() -> dict[str, list]:
    """Retourne, pour chaque table SQLModel existante en DB, la liste des
    colonnes déclarées en Python mais absentes du schéma SQLite.

    Les tables pas encore créées (create_all les fera) et celles sans
    divergence n'apparaissent pas dans le dict retourné.
    """
    import sqlite3

    missing_by_table: dict[str, list] = {}
    if not DB_PATH.exists():
        return missing_by_table

    conn = sqlite3.connect(DB_PATH)
    try:
        for table in SQLModel.metadata.sorted_tables:
            cursor = conn.execute(f"PRAGMA table_info('{table.name}')")
            db_cols = {row[1] for row in cursor.fetchall()}
            if not db_cols:
                continue  # table pas encore créée — create_all la gèrera
            missing = [col for col in table.columns if col.name not in db_cols]
            if missing:
                missing_by_table[table.name] = missing
    finally:
        conn.close()
    return missing_by_table


def _additive_migration_clause(col) -> str | None:
    """Construit la clause SQL d'un ``ALTER TABLE ... ADD COLUMN`` pour un
    champ SQLModel, ou retourne ``None`` si la migration additive n'est pas
    possible (colonne NOT NULL sans default exploitable).

    Les defaults sont dérivés directement depuis le modèle SQLAlchemy :
    - ``nullable=True`` → colonne ajoutée sans NOT NULL, valeur NULL pour
      les lignes existantes, c'est toujours valide.
    - ``nullable=False`` + default scalaire (``Field(default=0)`` ou
      ``Field(default="juin")``) → ``NOT NULL DEFAULT <valeur>``.
    - ``nullable=False`` sans default ou avec un default callable
      (ex. ``default_factory=datetime.utcnow``) → on ne peut pas produire
      une clause valide pour les lignes existantes, on retourne ``None``
      et on tombera sur le drop legacy.
    """
    type_sql = col.type.compile(dialect=_engine.dialect)
    nullable = col.nullable

    if nullable:
        return f'ALTER TABLE "{col.table.name}" ADD COLUMN "{col.name}" {type_sql}'

    default = col.default
    if default is None or not getattr(default, "is_scalar", False):
        return None

    raw = default.arg
    if isinstance(raw, bool):
        literal = "1" if raw else "0"
    elif isinstance(raw, (int, float)):
        literal = str(raw)
    elif isinstance(raw, str):
        literal = "'" + raw.replace("'", "''") + "'"
    else:
        return None

    return (
        f'ALTER TABLE "{col.table.name}" ADD COLUMN "{col.name}" '
        f"{type_sql} NOT NULL DEFAULT {literal}"
    )


def _apply_additive_migrations() -> list[str]:
    """Tente de combler les divergences additives via ``ALTER TABLE ADD COLUMN``.

    Stratégie de migration sans Alembic, inspirée du choix de projet « drop
    & recharge » mais en version conservatrice : on préserve les données
    chaque fois que c'est techniquement possible. Dérive automatiquement
    les clauses depuis ``SQLModel.metadata`` — aucune liste hardcodée à
    maintenir, chaque nouveau champ ``Field(...)`` avec un default scalaire
    est pris en charge tout seul au prochain démarrage.

    Retourne la liste des colonnes qui n'ont **pas** pu être migrées
    additivement (défaut non-scalaire, NOT NULL sans default). Si cette
    liste est non vide, l'appelant doit tomber sur le drop legacy. Si
    elle est vide et que le dict initial l'était aussi, rien n'a été fait.
    """
    import sqlite3

    missing_by_table = _missing_columns_per_table()
    unmigratable: list[str] = []
    if not missing_by_table:
        return unmigratable

    conn = sqlite3.connect(DB_PATH)
    try:
        for table_name, cols in missing_by_table.items():
            for col in cols:
                clause = _additive_migration_clause(col)
                if clause is None:
                    unmigratable.append(f"{table_name}.{col.name}")
                    continue
                logger.info(
                    "Migration additive : %s.%s (ALTER TABLE)",
                    table_name,
                    col.name,
                )
                conn.execute(clause)
        conn.commit()
    finally:
        conn.close()
    return unmigratable


def init_db() -> None:
    """Crée les tables SQLModel, migre additivement le schéma si possible,
    et en dernier recours supprime la DB quand la divergence n'est pas
    récupérable.

    Ordre de traitement :
    1. On liste les colonnes déclarées en Python mais absentes en DB.
    2. Pour chaque colonne manquante, on tente un ``ALTER TABLE ADD COLUMN``
       avec un default dérivé du champ SQLModel (cf. ``_apply_additive_migrations``).
       Les données runtime (sessions, attempts, progressions élève) sont
       conservées.
    3. S'il reste des divergences non-additives (renommage, drop column,
       changement de type, NOT NULL sans default exploitable), on retombe
       sur le drop & recharge historique pour rester cohérent avec le
       schéma SQLModel. Un log explicite liste ce qui a forcé le drop.

    Le chargement des contenus métier (sujets DC, exos maths, etc.) reste
    la responsabilité de chaque sous-module matière, appelé après cet
    ``init_db`` dans ``app.core.main.on_startup``.
    """
    global _engine
    unmigratable = _apply_additive_migrations()
    needs_drop = bool(unmigratable) or bool(_missing_columns_per_table())
    if needs_drop:
        logger.warning(
            "Schéma non récupérable par migration additive : %s — drop & recharge",
            unmigratable or "(divergence post-migration)",
        )
        # Fermer toutes les connexions du pool de l'ancien engine AVANT de
        # supprimer le fichier, sinon des connexions orphelines peuvent rester
        # pointées vers le fichier supprimé et retourner « readonly database ».
        _engine.dispose()
        DB_PATH.unlink(missing_ok=True)
        # Supprimer aussi les fichiers journal SQLite résiduels.
        for suffix in ("-wal", "-shm", "-journal"):
            DB_PATH.with_name(DB_PATH.name + suffix).unlink(missing_ok=True)
        # Recréer l'engine pour pointer vers le fichier frais (SQLite ouvre à
        # la première connexion, le fichier sera recréé par create_all).
        _engine = create_engine(
            f"sqlite:///{DB_PATH}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
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


def get_user_stats(s: DBSession) -> dict[str, int]:
    """Compte les utilisateurs uniques (user_key distinctes).

    Renvoie total, nouveaux aujourd'hui, nouveaux cette semaine.
    Un utilisateur est "nouveau" a la date de sa premiere activite, calculee
    comme le minimum entre sa premiere Session (si user_key renseigne) et sa
    premiere ligne UserProgress. L'union sur UserProgress est indispensable
    pour retrouver les utilisateurs anciens dont les Sessions avaient ete
    ecrites avec user_key=None avant le fix.
    """
    from sqlalchemy import func, case, union_all

    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    # Union des activites connues : sessions (post-fix) + progression (pre/post fix).
    sessions_q = select(
        Session.user_key.label("user_key"),
        Session.created_at.label("ts"),
    ).where(Session.user_key.isnot(None))
    progress_q = select(
        UserProgress.user_key.label("user_key"),
        UserProgress.first_seen_at.label("ts"),
    )
    activity = union_all(sessions_q, progress_q).subquery()
    sub = (
        select(
            activity.c.user_key,
            func.min(activity.c.ts).label("first_seen"),
        )
        .group_by(activity.c.user_key)
        .subquery()
    )
    row = s.exec(
        select(
            func.count().label("total"),
            func.sum(
                case((sub.c.first_seen >= today_start, 1), else_=0)
            ).label("today"),
            func.sum(
                case((sub.c.first_seen >= week_start, 1), else_=0)
            ).label("week"),
        ).select_from(sub)
    ).one()
    return {
        "total": row.total or 0,
        "today": row.today or 0,
        "week": row.week or 0,
    }


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
    "get_user_stats",
    "DB_PATH",
]

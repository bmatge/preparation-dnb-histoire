"""Modèles SQLModel + Pydantic de l'épreuve « Raisonnement et résolution
de problèmes » (mathématiques DNB 2026).

Schéma des contenus JSON committés dans
``content/mathematiques/problemes/exercices/`` :

- ``ProblemScoringPython`` / ``ProblemScoringAlbert`` : identiques aux
  scorings d'automatismes (on ne duplique pas, on importe).
- ``ProblemSubquestion`` : une sous-question dans un exercice (texte,
  scoring, indices gradués, explication de révélation).
- ``ProblemExercise`` : un exercice complet (contexte, sous-questions,
  thème, source).

Deux tables SQLModel :

- ``ProblemExercise`` (banque d'exercices, chargée au startup). Idempotent
  via la clé primaire ``id``.
- ``ProblemAttempt`` : trace analytique d'une tentative élève sur une
  sous-question donnée dans une session.

Convention des sessions : ``Session.subject_kind = "math_problemes"``,
``Session.subject_id = None`` (pas de table ``Subject`` côté maths).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field as PydField
from sqlmodel import Field, Session as DBSession, SQLModel, select

from app.core.db import get_engine
from app.mathematiques.automatismes.models import (
    QuestionIndices,
    ScoringAlbert as ProblemScoringAlbert,
    ScoringPython as ProblemScoringPython,
    ScoringTolerances,
)

logger = logging.getLogger(__name__)

# app/mathematiques/problemes/models.py → racine = 4 parents.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
EXERCISES_DIR = (
    REPO_ROOT / "content" / "mathematiques" / "problemes" / "exercices"
)


# ============================================================================
# Constantes métier
# ============================================================================


ALLOWED_THEMES: tuple[str, ...] = (
    "statistiques",
    "probabilites",
    "fonctions",
    "geometrie",
    "arithmetique",
    "grandeurs_mesures",
    "programmes_calcul",
)


# ============================================================================
# Schémas Pydantic — format JSON committé
# ============================================================================


class ProblemSource(BaseModel):
    """Origine d'un exercice de problèmes.

    Schéma distinct de ``QuestionSource`` (automatismes) parce que les
    clés discriminantes d'un problème (série, numéro d'exercice, note
    libre) n'ont pas d'équivalent côté automatismes.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    serie: str | None = None  # "A", "B" pour les sujets zéro 2026
    exercice: int | None = None  # numéro d'exercice dans le sujet
    document: str | None = None  # nom du PDF d'origine (optionnel)
    note: str | None = None  # commentaire libre (ex. "sous-questions retirées")


class ProblemSubquestion(BaseModel):
    """Une sous-question dans un exercice."""

    model_config = ConfigDict(extra="forbid")

    id: str
    numero: str  # ex. "1", "2.a", "2.b (i)" — libellé affiché à l'élève
    texte: str
    scoring: ProblemScoringPython | ProblemScoringAlbert
    indices: QuestionIndices = PydField(default_factory=QuestionIndices)
    reveal_explication: str | None = None


class ProblemExerciseSchema(BaseModel):
    """Un exercice complet au format JSON committé.

    Nom distinct de la table SQLModel ``ProblemExercise`` pour éviter
    l'ambiguïté dans les imports ; ce schéma ne sert qu'à valider le JSON.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    source: ProblemSource
    theme: str
    titre: str
    competence_principale: str
    points_total: float
    contexte: str
    sous_questions: list[ProblemSubquestion]


# ============================================================================
# Tables SQLModel
# ============================================================================


class ProblemExercise(SQLModel, table=True):
    """Un exercice de la Partie 2 du DNB maths, PK = slug JSON."""

    id: str = Field(primary_key=True)
    theme: str = Field(index=True)
    titre: str
    competence_principale: str
    points_total: float

    contexte: str

    # Liste des sous-questions sérialisée en JSON (plus simple qu'une
    # table séparée et évite une jointure à chaque affichage).
    sous_questions_json: str

    # Source : sérialisée en JSON (plusieurs champs optionnels).
    source_json: str

    @property
    def sous_questions(self) -> list[dict]:
        try:
            return json.loads(self.sous_questions_json) or []
        except (json.JSONDecodeError, TypeError):
            return []

    @property
    def source(self) -> dict:
        try:
            return json.loads(self.source_json) or {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def get_subquestion(self, subquestion_id: str) -> dict | None:
        """Renvoie la sous-question correspondant à l'id (ou None)."""
        for sq in self.sous_questions:
            if sq.get("id") == subquestion_id:
                return sq
        return None


class ProblemAttempt(SQLModel, table=True):
    """Tentative élève sur une sous-question d'un exercice."""

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="session.id", index=True)
    exercise_id: str = Field(foreign_key="problemexercise.id", index=True)
    subquestion_id: str = Field(index=True)

    student_answer: str
    is_correct: bool
    hints_used: int = 0

    # "python" ou "albert" — utile pour les analytics.
    scoring_mode: str

    created_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# Loader : JSON → DB, idempotent
# ============================================================================


def _load_exercises_from_json() -> list[dict]:
    """Charge l'union de tous les fichiers ``*.json`` du dossier ``exercices/``.

    Convention : un fichier par batch (``sujets_zero_2026.json``, etc.)
    qui contient une clé ``exercices: [...]``. Les fichiers dont le nom
    commence par ``_`` sont ignorés (réservés aux agrégats legacy).
    """
    if not EXERCISES_DIR.exists():
        logger.warning(
            "Dossier des exercices problèmes absent : %s", EXERCISES_DIR
        )
        return []
    all_ex: list[dict] = []
    for json_path in sorted(EXERCISES_DIR.glob("*.json")):
        if json_path.name.startswith("_"):
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error(
                "JSON exercices problèmes invalide : %s (%s)", json_path, exc
            )
            continue
        all_ex.extend(data.get("exercices", []))
    return all_ex


def init_problemes() -> int:
    """Charge les exercices dans la table ``ProblemExercise``. Idempotent.

    Retourne le nombre total d'exercices présents après chargement.
    Appelé depuis ``app.core.main.on_startup`` après ``core_db.init_db()``.
    """
    raw_list = _load_exercises_from_json()
    if not raw_list:
        return 0

    engine = get_engine()
    n_loaded = 0
    n_invalid = 0
    with DBSession(engine) as s:
        for raw in raw_list:
            try:
                ex = ProblemExerciseSchema.model_validate(raw)
            except Exception as exc:
                n_invalid += 1
                logger.warning(
                    "Exercice problème invalide ignoré (id=%s) : %s",
                    raw.get("id", "?"),
                    exc,
                )
                continue

            existing = s.get(ProblemExercise, ex.id)
            sq_payload = json.dumps(
                [sq.model_dump() for sq in ex.sous_questions],
                ensure_ascii=False,
            )
            source_payload = json.dumps(
                ex.source.model_dump(), ensure_ascii=False
            )

            if existing is None:
                s.add(
                    ProblemExercise(
                        id=ex.id,
                        theme=ex.theme,
                        titre=ex.titre,
                        competence_principale=ex.competence_principale,
                        points_total=ex.points_total,
                        contexte=ex.contexte,
                        sous_questions_json=sq_payload,
                        source_json=source_payload,
                    )
                )
            else:
                existing.theme = ex.theme
                existing.titre = ex.titre
                existing.competence_principale = ex.competence_principale
                existing.points_total = ex.points_total
                existing.contexte = ex.contexte
                existing.sous_questions_json = sq_payload
                existing.source_json = source_payload
                s.add(existing)
            n_loaded += 1
        s.commit()
    if n_invalid:
        logger.warning(
            "%d exercice(s) problèmes ignorés par init_problemes (schéma invalide)",
            n_invalid,
        )
    logger.info(
        "Chargé %d exercices problèmes depuis %s", n_loaded, EXERCISES_DIR
    )
    return n_loaded


# ============================================================================
# Helpers de requête
# ============================================================================


def list_themes(s: DBSession) -> list[str]:
    """Liste les thèmes effectivement présents dans la banque, triés selon
    l'ordre canonique d'``ALLOWED_THEMES``."""
    rows = s.exec(select(ProblemExercise.theme).distinct()).all()
    present = {r for r in rows if r}
    return [t for t in ALLOWED_THEMES if t in present]


def get_exercise(
    s: DBSession, exercise_id: str
) -> ProblemExercise | None:
    return s.get(ProblemExercise, exercise_id)


def list_exercises(
    s: DBSession, theme: str | None = None
) -> list[ProblemExercise]:
    """Liste tous les exercices, optionnellement filtrés par thème.

    Ordre stable par id pour que l'affichage sur la page d'index soit
    déterministe (utile pour que l'élève retrouve ses exercices en cours
    d'une session à l'autre).
    """
    q = select(ProblemExercise).order_by(ProblemExercise.id)
    if theme:
        q = q.where(ProblemExercise.theme == theme)
    return list(s.exec(q).all())


def add_attempt(
    s: DBSession,
    session_id: int,
    exercise_id: str,
    subquestion_id: str,
    student_answer: str,
    is_correct: bool,
    hints_used: int,
    scoring_mode: str,
) -> ProblemAttempt:
    row = ProblemAttempt(
        session_id=session_id,
        exercise_id=exercise_id,
        subquestion_id=subquestion_id,
        student_answer=student_answer,
        is_correct=is_correct,
        hints_used=hints_used,
        scoring_mode=scoring_mode,
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


__all__ = [
    "ALLOWED_THEMES",
    "ProblemScoringPython",
    "ProblemScoringAlbert",
    "ScoringTolerances",
    "ProblemSource",
    "ProblemSubquestion",
    "ProblemExerciseSchema",
    "ProblemExercise",
    "ProblemAttempt",
    "init_problemes",
    "list_themes",
    "get_exercise",
    "list_exercises",
    "add_attempt",
]

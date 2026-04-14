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
import re
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


# Trois types d'exercices dans la Partie 2 du DNB maths, présentés
# différemment dans l'UI :
#
# - "probleme_multi" : un énoncé (contexte, figure) partagé par plusieurs
#   sous-questions qui s'enchaînent logiquement. Rendu : contexte en
#   disclose au-dessus de chaque sous-question (ouvert sur la 1re,
#   replié ensuite).
# - "qcm" : plusieurs questions courtes indépendantes regroupées en un
#   « quiz » façon automatismes. Pas de contexte partagé — chaque
#   sous-question est autonome, avec son énoncé et son scoring.
# - "question_simple" : une unique question autonome (1 sous-question),
#   sans contexte ni progression multi-étapes.
ALLOWED_EXERCISE_TYPES: tuple[str, ...] = (
    "probleme_multi",
    "qcm",
    "question_simple",
)


# Centres d'examen connus. L'ordre ici sert de tri stable dans les
# filtres de la page d'accueil ; la valeur "metropole" couvre aussi les
# sujets conjoints Métropole/Antilles (ex. 2024_Brevet_19_09_2024_Metro_Ant).
# Les autres centres n'ont pas encore d'annales extraites : ils sont
# listés pour que l'ajout futur ne nécessite pas de toucher ce fichier.
ALLOWED_CENTRES: tuple[str, ...] = (
    "metropole",
    "antilles-guyane",
    "amerique-nord",
    "amerique-sud",
    "asie",
    "polynesie",
    "nouvelle-caledonie",
    "centres-etrangers",
)


# Session d'examen. "sujet_zero" regroupe les deux séries A/B des
# sujets zéro officiels 2026 (pas d'année d'examen réelle).
ALLOWED_SESSIONS: tuple[str, ...] = (
    "juin",
    "septembre",
    "sujet_zero",
)


# ============================================================================
# Schémas Pydantic — format JSON committé
# ============================================================================


class ProblemSource(BaseModel):
    """Origine d'un exercice de problèmes.

    Schéma distinct de ``QuestionSource`` (automatismes) parce que les
    clés discriminantes d'un problème (série, numéro d'exercice, note
    libre) n'ont pas d'équivalent côté automatismes.

    Les champs ``annee`` / ``centre`` / ``session`` sont facultatifs :
    s'ils sont absents du JSON source, ils sont résolus par
    ``resolve_source_metadata`` à partir de ``document``, ``type`` et
    ``serie``. Laisser ces champs vides dans les JSON existants évite
    une migration de masse ; on peut les pré-renseigner à la main quand
    on ajoute une nouvelle annale dont le nom de fichier est ambigu.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    serie: str | None = None  # "A", "B" pour les sujets zéro 2026
    exercice: int | None = None  # numéro d'exercice dans le sujet
    document: str | None = None  # nom du PDF d'origine (optionnel)
    note: str | None = None  # commentaire libre (ex. "sous-questions retirées")
    annee: int | None = None
    centre: str | None = None
    session: str | None = None


def resolve_source_metadata(
    source: ProblemSource,
) -> tuple[int | None, str | None, str | None]:
    """Détermine (année, centre, session) d'un exercice.

    Utilise les champs explicites de ``source`` en priorité, et retombe
    sur des heuristiques basées sur ``source.document`` et
    ``source.type`` pour les JSON historiques qui n'ont pas ces champs.

    - Sujet zéro DNB 2026 → année 2026, centre None, session "sujet_zero"
    - Document "YYYY_..." → année extraite du préfixe
    - Document contenant "sept" → session "septembre" (sinon "juin")
    - Document contenant "metro" (ou rien d'identifiable) → "metropole"
    """
    annee = source.annee
    centre = source.centre
    session = source.session

    if "sujet_zero" in (source.type or ""):
        if annee is None:
            # Les sujets zéro actuels sont ceux du DNB 2026.
            annee = 2026
        if session is None:
            session = "sujet_zero"
        return annee, centre, session

    doc = (source.document or "").lower()

    if annee is None:
        m = re.search(r"(20\d{2})", doc)
        if m:
            annee = int(m.group(1))

    if session is None:
        # On s'appuie sur "sept" (septembre) qui est présent dans tous
        # les noms de fichier d'annales de rattrapage observés. Par
        # défaut, on suppose juin (session principale du DNB).
        if "sept" in doc:
            session = "septembre"
        elif doc:
            session = "juin"

    if centre is None and doc:
        if "metro" in doc:
            centre = "metropole"
        elif "antilles" in doc or "guyane" in doc:
            centre = "antilles-guyane"
        elif "polynesie" in doc:
            centre = "polynesie"
        elif "asie" in doc:
            centre = "asie"

    return annee, centre, session


class ProblemSubquestion(BaseModel):
    """Une sous-question dans un exercice."""

    model_config = ConfigDict(extra="forbid")

    id: str
    numero: str  # ex. "1", "2.a", "2.b (i)" — libellé affiché à l'élève
    texte: str
    scoring: ProblemScoringPython | ProblemScoringAlbert
    indices: QuestionIndices = PydField(default_factory=QuestionIndices)
    reveal_explication: str | None = None
    # Figure spécifique à la sous-question (nom de fichier dans
    # content/mathematiques/figures/). Null si la figure est au niveau exercice.
    figure: str | None = None


class ProblemExerciseSchema(BaseModel):
    """Un exercice complet au format JSON committé.

    Nom distinct de la table SQLModel ``ProblemExercise`` pour éviter
    l'ambiguïté dans les imports ; ce schéma ne sert qu'à valider le JSON.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    source: ProblemSource
    # Type de rendu UI (cf. ALLOWED_EXERCISE_TYPES). Défaut `probleme_multi`
    # pour la rétro-compatibilité avec les JSON committés avant
    # l'introduction de ce champ.
    type: Literal["probleme_multi", "qcm", "question_simple"] = "probleme_multi"
    theme: str
    titre: str
    competence_principale: str
    points_total: float
    # Énoncé partagé par les sous-questions. Vide (`""`) pour les
    # exercices de type `qcm` ou `question_simple` où chaque ligne est
    # autonome.
    contexte: str = ""
    sous_questions: list[ProblemSubquestion]
    # Figure au niveau exercice (contexte) — nom de fichier dans
    # content/mathematiques/figures/, servi sur /math-figures.
    figure: str | None = None


# ============================================================================
# Tables SQLModel
# ============================================================================


class ProblemExercise(SQLModel, table=True):
    """Un exercice de la Partie 2 du DNB maths, PK = slug JSON."""

    id: str = Field(primary_key=True)
    # Type de rendu : "probleme_multi" (défaut, contexte partagé), "qcm"
    # (quiz de questions courtes indépendantes), "question_simple" (une
    # seule question autonome). Cf. ALLOWED_EXERCISE_TYPES.
    type: str = Field(default="probleme_multi", index=True)
    theme: str = Field(index=True)
    titre: str
    competence_principale: str
    points_total: float

    contexte: str = ""

    # Liste des sous-questions sérialisée en JSON (plus simple qu'une
    # table séparée et évite une jointure à chaque affichage).
    sous_questions_json: str

    # Source : sérialisée en JSON (plusieurs champs optionnels).
    source_json: str

    # Figure au niveau exercice (nom de fichier dans content/mathematiques/figures/).
    figure: str | None = None

    # Métadonnées de source dénormalisées pour filtrage rapide sur la
    # page d'accueil. Calculées par ``resolve_source_metadata`` au
    # moment du chargement JSON → DB, donc rechargées à chaque drop &
    # recharge sans intervention manuelle.
    annee: int | None = Field(default=None, index=True)
    centre: str | None = Field(default=None, index=True)
    session: str | None = Field(default=None, index=True)

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
            annee, centre, session = resolve_source_metadata(ex.source)

            if existing is None:
                s.add(
                    ProblemExercise(
                        id=ex.id,
                        type=ex.type,
                        theme=ex.theme,
                        titre=ex.titre,
                        competence_principale=ex.competence_principale,
                        points_total=ex.points_total,
                        contexte=ex.contexte,
                        sous_questions_json=sq_payload,
                        source_json=source_payload,
                        figure=ex.figure,
                        annee=annee,
                        centre=centre,
                        session=session,
                    )
                )
            else:
                existing.type = ex.type
                existing.theme = ex.theme
                existing.titre = ex.titre
                existing.competence_principale = ex.competence_principale
                existing.points_total = ex.points_total
                existing.contexte = ex.contexte
                existing.sous_questions_json = sq_payload
                existing.source_json = source_payload
                existing.figure = ex.figure
                existing.annee = annee
                existing.centre = centre
                existing.session = session
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


def list_annees(s: DBSession) -> list[int]:
    """Liste les années présentes, triées décroissantes (plus récentes
    d'abord — ce que l'élève veut voir en premier)."""
    rows = s.exec(select(ProblemExercise.annee).distinct()).all()
    return sorted({r for r in rows if r is not None}, reverse=True)


def list_centres(s: DBSession) -> list[str]:
    """Liste les centres présents, ordonnés selon ``ALLOWED_CENTRES``."""
    rows = s.exec(select(ProblemExercise.centre).distinct()).all()
    present = {r for r in rows if r}
    return [c for c in ALLOWED_CENTRES if c in present]


def list_sessions(s: DBSession) -> list[str]:
    """Liste les sessions présentes, ordonnées selon ``ALLOWED_SESSIONS``."""
    rows = s.exec(select(ProblemExercise.session).distinct()).all()
    present = {r for r in rows if r}
    return [s_ for s_ in ALLOWED_SESSIONS if s_ in present]


def get_exercise(
    s: DBSession, exercise_id: str
) -> ProblemExercise | None:
    return s.get(ProblemExercise, exercise_id)


def list_exercises(
    s: DBSession,
    theme: str | None = None,
    annee: int | None = None,
    centre: str | None = None,
    session: str | None = None,
) -> list[ProblemExercise]:
    """Liste tous les exercices, optionnellement filtrés.

    Ordre stable par id pour que l'affichage sur la page d'index soit
    déterministe (utile pour que l'élève retrouve ses exercices en cours
    d'une session à l'autre).
    """
    q = select(ProblemExercise).order_by(ProblemExercise.id)
    if theme:
        q = q.where(ProblemExercise.theme == theme)
    if annee is not None:
        q = q.where(ProblemExercise.annee == annee)
    if centre:
        q = q.where(ProblemExercise.centre == centre)
    if session:
        q = q.where(ProblemExercise.session == session)
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
    "ALLOWED_EXERCISE_TYPES",
    "ALLOWED_CENTRES",
    "ALLOWED_SESSIONS",
    "ProblemScoringPython",
    "ProblemScoringAlbert",
    "ScoringTolerances",
    "ProblemSource",
    "ProblemSubquestion",
    "ProblemExerciseSchema",
    "ProblemExercise",
    "ProblemAttempt",
    "resolve_source_metadata",
    "init_problemes",
    "list_themes",
    "list_annees",
    "list_centres",
    "list_sessions",
    "get_exercise",
    "list_exercises",
    "add_attempt",
]

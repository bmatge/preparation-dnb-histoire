"""Modèles SQLModel + Pydantic de l'épreuve « Automatismes » (mathématiques DNB).

Deux modèles Pydantic décrivent le format JSON committé dans
`content/mathematiques/automatismes/questions/` :

- `ScoringPython`  : scoring déterministe Python (réponses numériques).
- `ScoringAlbert`  : scoring par Albert (questions courtes ouvertes,
                     ex. ordre de grandeur, encadrement).
- `Question`       : une question d'automatismes.

Deux tables SQLModel :

- `AutoQuestion`   : la banque de questions, chargée au startup depuis
                     `_all.json`. Idempotent via la clé primaire `id`.
- `AutoAttempt`    : trace analytique d'une tentative élève sur une
                     question donnée dans une session.

Convention de nommage des thèmes (8, courte et stable) :
- calcul_numerique
- calcul_litteral
- fractions
- pourcentages_proportionnalite
- stats_probas
- grandeurs_mesures
- geometrie_numerique
- programmes_calcul

Les sessions automatismes ont `Session.subject_kind="math_automatismes"`
et `Session.subject_id=None` (pas de table `Subject` côté maths).
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

logger = logging.getLogger(__name__)

# app/mathematiques/automatismes/models.py → racine = 4 parents.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
QUESTIONS_DIR = (
    REPO_ROOT / "content" / "mathematiques" / "automatismes" / "questions"
)


# ============================================================================
# Constantes métier
# ============================================================================


ALLOWED_THEMES: tuple[str, ...] = (
    "calcul_numerique",
    "calcul_litteral",
    "fractions",
    "pourcentages_proportionnalite",
    "stats_probas",
    "grandeurs_mesures",
    "geometrie_numerique",
    "programmes_calcul",
)

ALLOWED_TYPE_REPONSE: tuple[str, ...] = (
    "entier",
    "decimal",
    "fraction",
    "pourcentage",
    "texte_court",
)


# ============================================================================
# Schémas Pydantic — format JSON committé
# ============================================================================


class ScoringTolerances(BaseModel):
    model_config = ConfigDict(extra="forbid")
    abs: float = 0.0
    rel: float = 0.0


class ScoringPython(BaseModel):
    """Scoring déterministe : on compare la réponse normalisée à une
    valeur canonique (entier, décimal, fraction, pourcentage)."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["python"] = "python"
    type_reponse: str
    reponse_canonique: str
    tolerances: ScoringTolerances = PydField(default_factory=ScoringTolerances)
    formes_acceptees: list[str] = PydField(default_factory=list)
    unite: str | None = None


class ScoringAlbert(BaseModel):
    """Scoring par Albert : pour les questions ouvertes courtes (ordre
    de grandeur, encadrement, justification d'une étape) où une
    comparaison Python ne suffit pas."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["albert"] = "albert"
    reponse_modele: str
    criteres_validation: list[str] = PydField(default_factory=list)


class QuestionSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    document: str | None = None
    numero_question: int | None = None
    item_liste: str | None = None


class QuestionIndices(BaseModel):
    model_config = ConfigDict(extra="forbid")
    niveau_1: str | None = None
    niveau_2: str | None = None
    niveau_3: str | None = None


class Question(BaseModel):
    """Une question d'automatismes au format JSON committé."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source: QuestionSource
    theme: str
    competence: str
    enonce: str
    scoring: ScoringPython | ScoringAlbert
    indices: QuestionIndices = PydField(default_factory=QuestionIndices)
    reveal_explication: str | None = None
    # Chemin relatif à content/mathematiques/figures/, p. ex.
    # "sujets_zero/serie_A/A-001-000.png". Servi par StaticFiles sous
    # /maths-figures/ (cf. app/core/main.py).
    image: str | None = None


# ============================================================================
# Tables SQLModel
# ============================================================================


class AutoQuestion(SQLModel, table=True):
    """Une question d'automatismes en banque (PK stable = slug du JSON)."""

    id: str = Field(primary_key=True)
    theme: str = Field(index=True)
    competence: str
    enonce: str

    # Mode de scoring : "python" ou "albert".
    scoring_mode: str = Field(index=True)

    # Le scoring complet est stocké en JSON pour rester ouvert (Python ou
    # Albert) sans avoir à étendre le schéma SQL à chaque évolution.
    scoring_json: str

    # Source : sérialisée en JSON pour la même raison (3 champs optionnels).
    source_json: str

    # Indices pré-calculés (souvent vides : on les génère via Albert au
    # runtime). Sérialisés en JSON pour préserver les niveaux 1/2/3 nullables.
    indices_json: str = "{}"

    reveal_explication: str | None = None

    # Chemin relatif optionnel vers une figure (cf. schéma Pydantic `Question`).
    image: str | None = None

    @property
    def scoring(self) -> dict:
        try:
            return json.loads(self.scoring_json)
        except (json.JSONDecodeError, TypeError):
            return {}

    @property
    def source(self) -> dict:
        try:
            return json.loads(self.source_json)
        except (json.JSONDecodeError, TypeError):
            return {}

    @property
    def indices(self) -> dict:
        try:
            return json.loads(self.indices_json)
        except (json.JSONDecodeError, TypeError):
            return {}


class AutoAttempt(SQLModel, table=True):
    """Tentative élève sur une question d'automatismes."""

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="session.id", index=True)
    question_id: str = Field(foreign_key="autoquestion.id", index=True)

    student_answer: str
    is_correct: bool
    hints_used: int = 0

    # "python" ou "albert" — utile pour les analytics (taux de réussite par mode).
    scoring_mode: str

    created_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# Loader : JSON → DB, idempotent
# ============================================================================


def _load_questions_from_json() -> list[dict]:
    """Charge l'union de tous les fichiers `*.json` du dossier `questions/`.

    Convention : un fichier par batch thématique (sujets_zero, calcul_numerique,
    etc.) qui contient une clé `questions: [...]`. Le fichier optionnel
    `_all.json` (legacy / aggrégat manuel) est ignoré pour éviter de doubler
    les questions s'il était laissé en place.
    """
    if not QUESTIONS_DIR.exists():
        logger.warning(
            "Dossier des questions automatismes absent : %s", QUESTIONS_DIR
        )
        return []
    all_qs: list[dict] = []
    for json_path in sorted(QUESTIONS_DIR.glob("*.json")):
        if json_path.name.startswith("_"):
            # `_all.json` (legacy) ou tout autre fichier privé.
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error(
                "JSON automatismes invalide : %s (%s)", json_path, exc
            )
            continue
        all_qs.extend(data.get("questions", []))
    return all_qs


def init_automatismes() -> int:
    """Charge `_all.json` dans la table `AutoQuestion`. Idempotent via PK.

    Retourne le nombre total de questions présentes dans le fichier (insérées
    + mises à jour). Appelé depuis `app.core.main.on_startup` après
    `core_db.init_db()`.
    """
    raw_list = _load_questions_from_json()
    if not raw_list:
        return 0

    engine = get_engine()
    n_loaded = 0
    n_invalid = 0
    with DBSession(engine) as s:
        for raw in raw_list:
            try:
                q = Question.model_validate(raw)
            except Exception as exc:
                n_invalid += 1
                logger.warning(
                    "Question invalide ignorée (id=%s) : %s",
                    raw.get("id", "?"),
                    exc,
                )
                continue

            existing = s.get(AutoQuestion, q.id)
            scoring_payload = json.dumps(
                q.scoring.model_dump(), ensure_ascii=False
            )
            source_payload = json.dumps(
                q.source.model_dump(), ensure_ascii=False
            )
            indices_payload = json.dumps(
                q.indices.model_dump(), ensure_ascii=False
            )

            if existing is None:
                s.add(
                    AutoQuestion(
                        id=q.id,
                        theme=q.theme,
                        competence=q.competence,
                        enonce=q.enonce,
                        scoring_mode=q.scoring.mode,
                        scoring_json=scoring_payload,
                        source_json=source_payload,
                        indices_json=indices_payload,
                        reveal_explication=q.reveal_explication,
                        image=q.image,
                    )
                )
            else:
                existing.theme = q.theme
                existing.competence = q.competence
                existing.enonce = q.enonce
                existing.scoring_mode = q.scoring.mode
                existing.scoring_json = scoring_payload
                existing.source_json = source_payload
                existing.indices_json = indices_payload
                existing.reveal_explication = q.reveal_explication
                existing.image = q.image
                s.add(existing)
            n_loaded += 1
        s.commit()
    if n_invalid:
        logger.warning(
            "%d question(s) ignorées par init_automatismes (schéma invalide)",
            n_invalid,
        )
    logger.info(
        "Chargé %d questions automatismes depuis %s",
        n_loaded,
        QUESTIONS_DIR,
    )
    return n_loaded


# ============================================================================
# Helpers de requête
# ============================================================================


def list_themes(s: DBSession) -> list[str]:
    """Liste les thèmes effectivement présents dans la banque (triés selon
    l'ordre canonique d'`ALLOWED_THEMES`)."""
    rows = s.exec(select(AutoQuestion.theme).distinct()).all()
    present = {r for r in rows if r}
    return [t for t in ALLOWED_THEMES if t in present]


def get_question(s: DBSession, question_id: str) -> AutoQuestion | None:
    return s.get(AutoQuestion, question_id)


def random_questions_by_theme(
    s: DBSession,
    n: int,
    theme: str | None = None,
) -> list[AutoQuestion]:
    """Tire N questions pseudo-aléatoires, filtrées éventuellement par thème."""
    import random

    q = select(AutoQuestion)
    if theme:
        q = q.where(AutoQuestion.theme == theme)
    rows = list(s.exec(q).all())
    random.shuffle(rows)
    return rows[:n]


def add_attempt(
    s: DBSession,
    session_id: int,
    question_id: str,
    student_answer: str,
    is_correct: bool,
    hints_used: int,
    scoring_mode: str,
) -> AutoAttempt:
    row = AutoAttempt(
        session_id=session_id,
        question_id=question_id,
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
    "ALLOWED_TYPE_REPONSE",
    "ScoringTolerances",
    "ScoringPython",
    "ScoringAlbert",
    "QuestionSource",
    "QuestionIndices",
    "Question",
    "AutoQuestion",
    "AutoAttempt",
    "init_automatismes",
    "list_themes",
    "get_question",
    "random_questions_by_theme",
    "add_attempt",
]

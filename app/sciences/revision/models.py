"""Modèles SQLModel + Pydantic de l'épreuve « Révision » sciences.

Format JSON committé dans `content/sciences/revision/questions/` : un
fichier par discipline × thème (ex. `physique_chimie_mouvements_energie.json`),
chaque fichier contient `{"questions": [...]}`.

Deux modèles Pydantic décrivent le format JSON :

- `SciencesScoringPython` : scoring déterministe (réponses numériques
                            ou texte court normalisé).
- `SciencesScoringAlbert` : scoring par Albert (questions ouvertes
                            courtes où une comparaison exacte ne suffit
                            pas, ex. justification d'un classement).
- `SciencesQuestion`      : une question de révision sciences.

Deux tables SQLModel :

- `SciencesQuestionRow`   : la banque de questions (PK = slug du JSON),
                            chargée au startup par `init_sciences_revision`.
- `SciencesAttempt`       : trace analytique d'une tentative élève.

Les sessions de révision sciences ont
`Session.subject_kind="sciences_revision"` et `Session.subject_id=None`
(pas de table `Subject` côté sciences — la banque de questions est
chargée en dur depuis `content/`).
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

# app/sciences/revision/models.py → racine du repo = 4 parents.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
QUESTIONS_DIR = REPO_ROOT / "content" / "sciences" / "revision" / "questions"


# ============================================================================
# Constantes métier
# ============================================================================


ALLOWED_DISCIPLINES: tuple[str, ...] = (
    "physique_chimie",
    "svt",
    "technologie",
)


# Chaque discipline possède ses propres thèmes. Les identifiants sont uniques
# globalement (pas de chevauchement entre disciplines) pour que le RAG et la
# DB puissent traiter `theme` comme une clé autonome. Les identifiants suivent
# la nomenclature des 8 fiches méthode + un éclatement pour la technologie
# (sous-représentée en une seule fiche mais explicitée dans le programme
# cycle 4).
ALLOWED_THEMES_PAR_DISCIPLINE: dict[str, tuple[str, ...]] = {
    "physique_chimie": (
        "organisation_matiere",
        "mouvements_energie",
        "electricite_signaux",
        "univers_melanges",
    ),
    "svt": (
        "corps_sante",
        "terre_evolution",
        "genetique",
    ),
    "technologie": (
        "objets_techniques",
        "materiaux_innovation",
        "programmation_robotique",
        "chaine_energie",
    ),
}


# Union aplatie pour validation rapide.
ALLOWED_THEMES: tuple[str, ...] = tuple(
    t for themes in ALLOWED_THEMES_PAR_DISCIPLINE.values() for t in themes
)


# Mapping inverse theme → discipline (sanity check + helper).
THEME_TO_DISCIPLINE: dict[str, str] = {
    theme: discipline
    for discipline, themes in ALLOWED_THEMES_PAR_DISCIPLINE.items()
    for theme in themes
}


ALLOWED_TYPE_REPONSE: tuple[str, ...] = (
    "entier",
    "decimal",
    "pourcentage",
    "texte_court",
    "qcm",
    "vrai_faux",
)


# ============================================================================
# Schémas Pydantic — format JSON committé
# ============================================================================


class ScoringTolerances(BaseModel):
    model_config = ConfigDict(extra="forbid")
    abs: float = 0.0
    rel: float = 0.0


class SciencesScoringPython(BaseModel):
    """Scoring déterministe : comparaison normalisée à une valeur canonique.

    Types supportés :
    - `entier` / `decimal` / `pourcentage` : comparaison numérique avec
      tolérance (voir `scoring.check`)
    - `texte_court` : comparaison lex-normalisée (lower, strip, accents,
      ponctuation)
    - `qcm` : la réponse canonique est un identifiant court (ex. « P2 »,
      « C », « 3 ») ; la comparaison est lex-normalisée
    - `vrai_faux` : comparaison lex-normalisée à « vrai » ou « faux »
      (synonymes courants acceptés par le scoring)
    """

    model_config = ConfigDict(extra="forbid")
    mode: Literal["python"] = "python"
    type_reponse: str
    reponse_canonique: str
    tolerances: ScoringTolerances = PydField(default_factory=ScoringTolerances)
    formes_acceptees: list[str] = PydField(default_factory=list)
    unite: str | None = None


class SciencesScoringAlbert(BaseModel):
    """Scoring par Albert pour les questions ouvertes courtes.

    Ex. « justifie en une phrase pourquoi on utilise l'aluminium plutôt
    que l'acier pour la coque de l'ISS ». La réponse modèle sert de
    référence, les critères aident le modèle à calibrer sa décision.
    """

    model_config = ConfigDict(extra="forbid")
    mode: Literal["albert"] = "albert"
    reponse_modele: str
    criteres_validation: list[str] = PydField(default_factory=list)


class SciencesQuestionSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Type de source : "fiche_methode", "programme", "annale", "sujet_zero".
    type: str
    # Document d'origine (nom de fichier ou identifiant libre).
    document: str | None = None
    # Numéro de la question dans le document (pour traçabilité).
    numero_question: str | None = None


class SciencesQuestionIndices(BaseModel):
    model_config = ConfigDict(extra="forbid")
    niveau_1: str | None = None
    niveau_2: str | None = None
    niveau_3: str | None = None


class SciencesQuestion(BaseModel):
    """Une question de révision sciences au format JSON committé."""

    model_config = ConfigDict(extra="forbid")

    id: str
    discipline: str
    theme: str
    competence: str
    source: SciencesQuestionSource
    enonce: str
    scoring: SciencesScoringPython | SciencesScoringAlbert
    indices: SciencesQuestionIndices = PydField(
        default_factory=SciencesQuestionIndices
    )
    reveal_explication: str | None = None


# ============================================================================
# Tables SQLModel
# ============================================================================


class SciencesQuestionRow(SQLModel, table=True):
    """Une question de révision sciences en banque (PK = slug du JSON)."""

    id: str = Field(primary_key=True)

    # Discipline (physique_chimie / svt / technologie) et thème sont
    # indexés pour que la sélection par filtre reste rapide sur plusieurs
    # centaines de questions.
    discipline: str = Field(index=True)
    theme: str = Field(index=True)

    competence: str
    enonce: str

    # Mode de scoring : "python" ou "albert".
    scoring_mode: str = Field(index=True)

    # Les structures complexes restent en JSON pour éviter un schéma SQL
    # rigide — même pattern que côté automatismes maths.
    scoring_json: str
    source_json: str
    indices_json: str = "{}"

    reveal_explication: str | None = None

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


class SciencesAttempt(SQLModel, table=True):
    """Tentative élève sur une question de révision sciences."""

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="session.id", index=True)
    question_id: str = Field(foreign_key="sciencesquestionrow.id", index=True)

    student_answer: str
    is_correct: bool
    hints_used: int = 0
    scoring_mode: str

    created_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# Loader : JSON → DB, idempotent
# ============================================================================


def _load_questions_from_json() -> list[dict]:
    """Charge l'union de tous les fichiers `*.json` du dossier `questions/`.

    Convention : un fichier par batch, nommé librement (ex.
    `physique_chimie_organisation_matiere.json`), contenant une clé
    `questions: [...]`. Les fichiers dont le nom commence par `_` sont
    ignorés (réservés aux méta-fichiers / agrégats legacy).
    """
    if not QUESTIONS_DIR.exists():
        logger.warning(
            "Dossier des questions sciences absent : %s", QUESTIONS_DIR
        )
        return []
    all_qs: list[dict] = []
    for json_path in sorted(QUESTIONS_DIR.glob("*.json")):
        if json_path.name.startswith("_"):
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error(
                "JSON sciences invalide : %s (%s)", json_path, exc
            )
            continue
        all_qs.extend(data.get("questions", []))
    return all_qs


def init_sciences_revision() -> int:
    """Charge les questions de révision sciences en DB. Idempotent via PK.

    Retourne le nombre total de questions insérées/mises à jour. Appelé
    depuis `app.core.main.on_startup` après `core_db.init_db()`.

    Les questions dont la discipline ou le thème ne sont pas dans la
    liste autorisée (ou qui ne matchent pas le couple discipline/thème)
    sont rejetées avec un warning, pas de crash.
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
                q = SciencesQuestion.model_validate(raw)
            except Exception as exc:
                n_invalid += 1
                logger.warning(
                    "Question sciences invalide ignorée (id=%s) : %s",
                    raw.get("id", "?"),
                    exc,
                )
                continue

            if q.discipline not in ALLOWED_DISCIPLINES:
                n_invalid += 1
                logger.warning(
                    "Discipline inconnue pour %s : %r", q.id, q.discipline
                )
                continue
            expected_discipline = THEME_TO_DISCIPLINE.get(q.theme)
            if expected_discipline is None:
                n_invalid += 1
                logger.warning(
                    "Thème inconnu pour %s : %r", q.id, q.theme
                )
                continue
            if expected_discipline != q.discipline:
                n_invalid += 1
                logger.warning(
                    "Thème %r n'appartient pas à la discipline %r (attendu %r) pour %s",
                    q.theme,
                    q.discipline,
                    expected_discipline,
                    q.id,
                )
                continue

            existing = s.get(SciencesQuestionRow, q.id)
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
                    SciencesQuestionRow(
                        id=q.id,
                        discipline=q.discipline,
                        theme=q.theme,
                        competence=q.competence,
                        enonce=q.enonce,
                        scoring_mode=q.scoring.mode,
                        scoring_json=scoring_payload,
                        source_json=source_payload,
                        indices_json=indices_payload,
                        reveal_explication=q.reveal_explication,
                    )
                )
            else:
                existing.discipline = q.discipline
                existing.theme = q.theme
                existing.competence = q.competence
                existing.enonce = q.enonce
                existing.scoring_mode = q.scoring.mode
                existing.scoring_json = scoring_payload
                existing.source_json = source_payload
                existing.indices_json = indices_payload
                existing.reveal_explication = q.reveal_explication
                s.add(existing)
            n_loaded += 1
        s.commit()
    if n_invalid:
        logger.warning(
            "%d question(s) sciences ignorées par init_sciences_revision",
            n_invalid,
        )
    logger.info(
        "Chargé %d questions révision sciences depuis %s",
        n_loaded,
        QUESTIONS_DIR,
    )
    return n_loaded


# ============================================================================
# Helpers de requête
# ============================================================================


def list_themes_for_discipline(
    s: DBSession, discipline: str
) -> list[str]:
    """Liste les thèmes effectivement présents en banque pour une discipline,
    triés selon l'ordre canonique d'`ALLOWED_THEMES_PAR_DISCIPLINE[discipline]`.
    """
    if discipline not in ALLOWED_THEMES_PAR_DISCIPLINE:
        return []
    rows = s.exec(
        select(SciencesQuestionRow.theme)
        .where(SciencesQuestionRow.discipline == discipline)
        .distinct()
    ).all()
    present = {r for r in rows if r}
    return [t for t in ALLOWED_THEMES_PAR_DISCIPLINE[discipline] if t in present]


def get_question(
    s: DBSession, question_id: str
) -> SciencesQuestionRow | None:
    return s.get(SciencesQuestionRow, question_id)


def random_questions(
    s: DBSession,
    n: int,
    discipline: str,
    theme: str | None = None,
) -> list[SciencesQuestionRow]:
    """Tire N questions pseudo-aléatoires pour une discipline donnée,
    optionnellement filtrées par thème."""
    import random

    q = select(SciencesQuestionRow).where(
        SciencesQuestionRow.discipline == discipline
    )
    if theme:
        q = q.where(SciencesQuestionRow.theme == theme)
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
) -> SciencesAttempt:
    row = SciencesAttempt(
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
    "ALLOWED_DISCIPLINES",
    "ALLOWED_THEMES",
    "ALLOWED_THEMES_PAR_DISCIPLINE",
    "ALLOWED_TYPE_REPONSE",
    "THEME_TO_DISCIPLINE",
    "ScoringTolerances",
    "SciencesScoringPython",
    "SciencesScoringAlbert",
    "SciencesQuestionSource",
    "SciencesQuestionIndices",
    "SciencesQuestion",
    "SciencesQuestionRow",
    "SciencesAttempt",
    "init_sciences_revision",
    "list_themes_for_discipline",
    "get_question",
    "random_questions",
    "add_attempt",
]

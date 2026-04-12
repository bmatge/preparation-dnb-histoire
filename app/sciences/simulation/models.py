"""Modeles SQLModel + Pydantic de l'epreuve « Simulation » sciences.

Format JSON committe dans ``content/sciences/simulation/sujets/`` :
un fichier par sujet d'annale, chaque fichier decrit un sujet complet
(2 disciplines, questions structurees, references aux captures PNG).

Deux tables SQLModel :

- ``SimulationSujet`` : le catalogue des sujets (PK = id string),
                        charge au startup par ``init_sciences_simulation``.
- ``SimulationAttempt`` : trace analytique d'une tentative eleve.

Les sessions de simulation ont
``Session.subject_kind="sciences_simulation"`` et ``Session.subject_id=None``.
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

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SUJETS_DIR = REPO_ROOT / "content" / "sciences" / "simulation" / "sujets"


# ============================================================================
# Constantes metier
# ============================================================================


ALLOWED_DISCIPLINES: tuple[str, ...] = (
    "physique_chimie",
    "svt",
    "technologie",
)


ALLOWED_TYPE_REPONSE: tuple[str, ...] = (
    "entier",
    "decimal",
    "pourcentage",
    "texte_court",
    "qcm",
    "vrai_faux",
)


# ============================================================================
# Schemas Pydantic — format JSON committe
# ============================================================================


class ScoringTolerances(BaseModel):
    model_config = ConfigDict(extra="forbid")
    abs: float = 0.0
    rel: float = 0.0


class SimScoringPython(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["python"] = "python"
    type_reponse: str
    reponse_canonique: str
    tolerances: ScoringTolerances = PydField(default_factory=ScoringTolerances)
    formes_acceptees: list[str] = PydField(default_factory=list)
    unite: str | None = None


class SimScoringAlbert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["albert"] = "albert"
    reponse_modele: str
    criteres_validation: list[str] = PydField(default_factory=list)


class SimQuestionIndices(BaseModel):
    model_config = ConfigDict(extra="forbid")
    niveau_1: str | None = None
    niveau_2: str | None = None
    niveau_3: str | None = None


class SimDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str
    capture: str


class SimQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    numero: str
    texte: str
    points: float = 0.0
    documents_ref: list[str] = PydField(default_factory=list)
    scoring: SimScoringPython | SimScoringAlbert
    indices: SimQuestionIndices = PydField(default_factory=SimQuestionIndices)
    reveal_explication: str | None = None


class SimDiscipline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    discipline: str
    theme_titre: str
    points: float = 25.0
    documents: list[SimDocument] = PydField(default_factory=list)
    questions: list[SimQuestion] = PydField(default_factory=list)


class SimSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = "annale"
    annee: int = 0
    centre: str = ""
    document: str | None = None
    corrige: str | None = None


class SimSujetSchema(BaseModel):
    """Un sujet de simulation complet au format JSON committe."""
    model_config = ConfigDict(extra="forbid")
    id: str
    source: SimSource
    points_total: float = 50.0
    disciplines: list[SimDiscipline]


# ============================================================================
# Tables SQLModel
# ============================================================================


class SimulationSujet(SQLModel, table=True):
    """Un sujet de simulation en catalogue (PK = id du JSON)."""

    id: str = Field(primary_key=True)
    annee: int = Field(index=True)
    centre: str
    points_total: float

    disciplines_json: str
    source_json: str

    @property
    def disciplines(self) -> list[dict]:
        try:
            return json.loads(self.disciplines_json)
        except (json.JSONDecodeError, TypeError):
            return []

    @property
    def source(self) -> dict:
        try:
            return json.loads(self.source_json)
        except (json.JSONDecodeError, TypeError):
            return {}

    @property
    def discipline_names(self) -> list[str]:
        return [d.get("discipline", "") for d in self.disciplines]

    @property
    def discipline_labels(self) -> list[str]:
        from app.sciences.simulation.loader import DISCIPLINE_LABELS
        return [
            DISCIPLINE_LABELS.get(d.get("discipline", ""), d.get("discipline", ""))
            for d in self.disciplines
        ]

    def get_discipline(self, disc_idx: int) -> dict:
        discs = self.disciplines
        if 0 <= disc_idx < len(discs):
            return discs[disc_idx]
        return {}

    def get_question(self, disc_idx: int, q_idx: int) -> dict:
        disc = self.get_discipline(disc_idx)
        questions = disc.get("questions", [])
        if 0 <= q_idx < len(questions):
            return questions[q_idx]
        return {}

    def total_questions(self) -> int:
        return sum(
            len(d.get("questions", [])) for d in self.disciplines
        )


class SimulationAttempt(SQLModel, table=True):
    """Tentative eleve sur une question de simulation sciences."""

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="session.id", index=True)
    sujet_id: str = Field(index=True)
    discipline_id: str = Field(index=True)
    question_id: str = Field(index=True)

    student_answer: str
    is_correct: bool
    hints_used: int = 0
    scoring_mode: str

    created_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# Loader : JSON -> DB, idempotent
# ============================================================================


def _load_sujets_from_json() -> list[dict]:
    if not SUJETS_DIR.exists():
        logger.warning(
            "Dossier des sujets simulation sciences absent : %s", SUJETS_DIR
        )
        return []
    all_sujets: list[dict] = []
    for json_path in sorted(SUJETS_DIR.glob("*.json")):
        if json_path.name.startswith("_"):
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error(
                "JSON simulation sciences invalide : %s (%s)", json_path, exc
            )
            continue
        all_sujets.append(data)
    return all_sujets


def init_sciences_simulation() -> int:
    """Charge les sujets de simulation sciences en DB. Idempotent via PK.

    Retourne le nombre de sujets charges. Appele depuis
    ``app.core.main.on_startup`` apres ``core_db.init_db()``.
    """
    raw_list = _load_sujets_from_json()
    if not raw_list:
        return 0

    engine = get_engine()
    n_loaded = 0
    n_invalid = 0
    with DBSession(engine) as s:
        for raw in raw_list:
            try:
                sujet = SimSujetSchema.model_validate(raw)
            except Exception as exc:
                n_invalid += 1
                logger.warning(
                    "Sujet simulation sciences invalide (id=%s) : %s",
                    raw.get("id", "?"),
                    exc,
                )
                continue

            if len(sujet.disciplines) != 2:
                n_invalid += 1
                logger.warning(
                    "Sujet %s a %d disciplines (attendu 2)",
                    sujet.id,
                    len(sujet.disciplines),
                )
                continue

            for disc in sujet.disciplines:
                if disc.discipline not in ALLOWED_DISCIPLINES:
                    n_invalid += 1
                    logger.warning(
                        "Discipline inconnue dans sujet %s : %r",
                        sujet.id,
                        disc.discipline,
                    )
                    break
            else:
                disciplines_payload = json.dumps(
                    [d.model_dump() for d in sujet.disciplines],
                    ensure_ascii=False,
                )
                source_payload = json.dumps(
                    sujet.source.model_dump(), ensure_ascii=False
                )

                existing = s.get(SimulationSujet, sujet.id)
                if existing is None:
                    s.add(
                        SimulationSujet(
                            id=sujet.id,
                            annee=sujet.source.annee,
                            centre=sujet.source.centre,
                            points_total=sujet.points_total,
                            disciplines_json=disciplines_payload,
                            source_json=source_payload,
                        )
                    )
                else:
                    existing.annee = sujet.source.annee
                    existing.centre = sujet.source.centre
                    existing.points_total = sujet.points_total
                    existing.disciplines_json = disciplines_payload
                    existing.source_json = source_payload
                    s.add(existing)
                n_loaded += 1
        s.commit()
    if n_invalid:
        logger.warning(
            "%d sujet(s) simulation sciences ignores par init_sciences_simulation",
            n_invalid,
        )
    logger.info(
        "Charge %d sujets simulation sciences depuis %s",
        n_loaded,
        SUJETS_DIR,
    )
    return n_loaded


# ============================================================================
# Helpers de requete
# ============================================================================


def get_sujet(s: DBSession, sujet_id: str) -> SimulationSujet | None:
    return s.get(SimulationSujet, sujet_id)


def list_sujets(s: DBSession) -> list[SimulationSujet]:
    return list(s.exec(
        select(SimulationSujet).order_by(
            SimulationSujet.annee.desc(),  # type: ignore[union-attr]
            SimulationSujet.centre,
        )
    ).all())


def add_attempt(
    s: DBSession,
    session_id: int,
    sujet_id: str,
    discipline_id: str,
    question_id: str,
    student_answer: str,
    is_correct: bool,
    hints_used: int,
    scoring_mode: str,
) -> SimulationAttempt:
    row = SimulationAttempt(
        session_id=session_id,
        sujet_id=sujet_id,
        discipline_id=discipline_id,
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
    "ALLOWED_TYPE_REPONSE",
    "ScoringTolerances",
    "SimScoringPython",
    "SimScoringAlbert",
    "SimQuestionIndices",
    "SimDocument",
    "SimQuestion",
    "SimDiscipline",
    "SimSource",
    "SimSujetSchema",
    "SimulationSujet",
    "SimulationAttempt",
    "init_sciences_simulation",
    "get_sujet",
    "list_sujets",
    "add_attempt",
]

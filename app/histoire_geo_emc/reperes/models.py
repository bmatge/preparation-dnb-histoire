"""
Modèles SQLModel de l'épreuve « Repères chronologiques et spatiaux ».

Deux tables :

- `Repere` : la banque de repères officiels, chargée une fois au startup
  depuis `content/histoire-geo-emc/reperes/_all.json`. Idempotent via la
  clé primaire `id` (slug stable, cf. scripts/extract_reperes.py).

- `RepereAttempt` : trace analytique d'une tentative élève sur un repère
  donné dans le cadre d'une session. On la garde au-delà de la fin de la
  session courante — l'état « en cours » du quiz lui vit dans le cookie
  Starlette (cf. routes.py), pas en DB.

Les repères ne sont pas liés à une ligne `Subject` : l'épreuve n'a pas
de « sujet tiré d'une annale » au sens DC. Les sessions repères ont
`Session.subject_kind="hgemc_reperes"` et `Session.subject_id=None`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from sqlmodel import Field, Session as DBSession, SQLModel, select

from app.core.db import get_engine

logger = logging.getLogger(__name__)

# app/histoire_geo_emc/reperes/models.py → racine = 4 parents.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
REPERES_JSON = REPO_ROOT / "content" / "histoire-geo-emc" / "reperes" / "_all.json"


# ============================================================================
# Tables
# ============================================================================


class Repere(SQLModel, table=True):
    """Un repère chronologique ou spatial à connaître pour le DNB."""

    # Clé primaire stable = slug construit par le script d'extraction
    # (cf. scripts/extract_reperes.py :: _build_id).
    id: str = Field(primary_key=True)

    # histoire | geographie | emc
    discipline: str = Field(index=True)

    # date | evenement | personnage | lieu | notion | definition
    type: str

    # Titre du thème du programme tel que listé dans le document source.
    theme: str

    # Nom du repère tel qu'il est demandé à l'élève (concis, autonome).
    libelle: str

    annee: int | None = None
    annee_fin: int | None = None

    # Qualification d'époque libre (« XVIIIe siècle », « 1945-1989 »…).
    periode: str | None = None

    # Liste JSON-encodée de 0 à 3 mots-clés, utilisée pour formuler des
    # indices. SQLModel ne gère pas nativement les list[str], on sérialise.
    notions_associees_json: str = "[]"

    # Chaîne courte désignant le document d'origine (traçabilité).
    source: str

    # "3e" pour le MVP — tous les repères testables au DNB.
    niveau_requis: str = "3e"

    @property
    def notions_associees(self) -> list[str]:
        try:
            return json.loads(self.notions_associees_json)
        except (json.JSONDecodeError, TypeError):
            return []


class RepereAttempt(SQLModel, table=True):
    """Une tentative élève sur un repère donné."""

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="session.id", index=True)
    repere_id: str = Field(foreign_key="repere.id", index=True)

    # Énoncé de la question reformulée par Albert, pour pouvoir
    # reconstituer le contexte en analytics ultérieur.
    question_asked: str

    # Dernière réponse saisie par l'élève (celle qui a validé ou épuisé
    # les indices). On ne garde pas toutes les réponses intermédiaires
    # pour ne pas faire exploser la table.
    student_answer: str

    is_correct: bool

    # 0 = trouvé du premier coup, 3 = a épuisé tous les indices et la
    # réponse a été révélée.
    hints_used: int = 0

    created_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# Loader : JSON → DB, idempotent
# ============================================================================


def _load_reperes_from_json() -> list[dict]:
    if not REPERES_JSON.exists():
        logger.warning("Fichier de repères absent : %s", REPERES_JSON)
        return []
    try:
        data = json.loads(REPERES_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.error("JSON de repères invalide : %s", exc)
        return []
    return data.get("reperes", [])


def init_reperes() -> int:
    """Charge `_all.json` dans la table Repere. Idempotent via PK.

    Retourne le nombre de repères insérés (nouveaux ou mis à jour).
    Appelé depuis `app.core.main.on_startup` après `core_db.init_db()`.
    """
    reperes = _load_reperes_from_json()
    if not reperes:
        return 0

    engine = get_engine()
    n = 0
    with DBSession(engine) as s:
        for r in reperes:
            rid = r.get("id")
            if not rid:
                continue
            existing = s.get(Repere, rid)
            notions_json = json.dumps(
                r.get("notions_associees") or [], ensure_ascii=False
            )
            if existing is None:
                row = Repere(
                    id=rid,
                    discipline=r["discipline"],
                    type=r["type"],
                    theme=r.get("theme", ""),
                    libelle=r["libelle"],
                    annee=r.get("annee"),
                    annee_fin=r.get("annee_fin"),
                    periode=r.get("periode"),
                    notions_associees_json=notions_json,
                    source=r.get("source", ""),
                    niveau_requis=r.get("niveau_requis", "3e"),
                )
                s.add(row)
                n += 1
            else:
                # Mise à jour des champs modifiables si le JSON a été
                # régénéré — le slug `id` reste stable donc les tentatives
                # historiques restent rattachées.
                existing.libelle = r["libelle"]
                existing.theme = r.get("theme", "")
                existing.annee = r.get("annee")
                existing.annee_fin = r.get("annee_fin")
                existing.periode = r.get("periode")
                existing.notions_associees_json = notions_json
                existing.source = r.get("source", "")
                s.add(existing)
        s.commit()
    logger.info("Chargé %d repères depuis %s", len(reperes), REPERES_JSON)
    return len(reperes)


# ============================================================================
# Helpers de requête
# ============================================================================


def list_themes(s: DBSession, discipline: str | None = None) -> list[str]:
    """Renvoie la liste des thèmes disponibles (optionnellement filtrée)."""
    q = select(Repere.theme).distinct()
    if discipline:
        q = q.where(Repere.discipline == discipline)
    return sorted([row for row in s.exec(q).all() if row])


def random_reperes(
    s: DBSession,
    n: int = 15,
    discipline: str | None = None,
    theme: str | None = None,
    exclude_ids: list[str] | None = None,
    only_ids: list[str] | None = None,
) -> list[Repere]:
    """Tire N repères pseudo-aléatoires, filtrés optionnellement."""
    import random

    q = select(Repere)
    if discipline:
        q = q.where(Repere.discipline == discipline)
    if theme:
        q = q.where(Repere.theme == theme)
    if exclude_ids:
        q = q.where(Repere.id.not_in(exclude_ids))  # type: ignore[attr-defined]
    if only_ids:
        q = q.where(Repere.id.in_(only_ids))  # type: ignore[attr-defined]
    all_rows = list(s.exec(q).all())
    random.shuffle(all_rows)
    return all_rows[:n]


def get_repere(s: DBSession, repere_id: str) -> Repere | None:
    return s.get(Repere, repere_id)


def add_attempt(
    s: DBSession,
    session_id: int,
    repere_id: str,
    question_asked: str,
    student_answer: str,
    is_correct: bool,
    hints_used: int,
) -> RepereAttempt:
    row = RepereAttempt(
        session_id=session_id,
        repere_id=repere_id,
        question_asked=question_asked,
        student_answer=student_answer,
        is_correct=is_correct,
        hints_used=hints_used,
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


__all__ = [
    "Repere",
    "RepereAttempt",
    "init_reperes",
    "list_themes",
    "random_reperes",
    "get_repere",
    "add_attempt",
]

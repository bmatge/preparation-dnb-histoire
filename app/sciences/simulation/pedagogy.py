"""Orchestration pedagogique de l'epreuve Simulation sciences.

Trois fonctions publiques :

- ``evaluate_answer`` : dispatch deterministe vs Albert.
- ``generate_hint``   : indice gradue via Albert + fallback deterministe.
- ``reveal_answer``   : revelation via Albert + fallback deterministe.

Adapte de ``app/sciences/revision/pedagogy.py`` avec ajout du contexte
discipline/theme du sujet de simulation.
"""

from __future__ import annotations

import json
import logging
import re

from app.core.albert_client import AlbertClient, AlbertError, Task
from app.core.rag import get_default_rag_client
from app.sciences.simulation import scoring as sim_scoring
from app.sciences.simulation.prompts import (
    build_hint_prompt,
    build_open_eval_prompt,
    build_reveal_prompt,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Singleton client Albert
# ============================================================================


_albert_client: AlbertClient | None = None


def get_albert_client() -> AlbertClient:
    global _albert_client
    if _albert_client is None:
        _albert_client = AlbertClient()
    return _albert_client


# ============================================================================
# Wrapper safe_chat
# ============================================================================


GENERIC_ERROR_MSG = (
    "Desole, j'ai eu un petit souci pour te repondre. Reessaie dans quelques secondes."
)


def _safe_chat(task: Task, messages: list[dict], fallback: str) -> str:
    try:
        client = get_albert_client()
        result = client.chat(task, messages, retry_on_missing_citations=False)
        return (result.content or "").strip() or fallback
    except AlbertError as exc:
        logger.warning("Albert a renvoye une erreur (%s) : %s", task, exc)
        return fallback
    except Exception:
        logger.exception("Erreur inattendue lors de l'appel Albert (%s)", task)
        return fallback


# ============================================================================
# 1. Evaluation d'une reponse
# ============================================================================


def evaluate_answer(
    question: dict,
    student_answer: str,
    discipline: str = "",
    theme_titre: str = "",
) -> bool:
    if not student_answer or not student_answer.strip():
        return False

    scoring = question.get("scoring") or {}
    mode = scoring.get("mode")

    if mode == "python":
        return sim_scoring.check(scoring, student_answer)

    if mode == "albert":
        return _evaluate_open(question, student_answer, discipline, theme_titre)

    logger.warning(
        "Mode de scoring inconnu pour question=%s : %r",
        question.get("id", "?"),
        mode,
    )
    return False


_JSON_BLOCK = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _evaluate_open(
    question: dict,
    student_answer: str,
    discipline: str,
    theme_titre: str,
) -> bool:
    rag_passages: list = []
    try:
        rag = get_default_rag_client()
        rag_passages = rag.search_for_task(
            subject_kind="sciences",
            task=Task.SCIENCES_SIM_EVAL_OPEN,
            query=question.get("texte", ""),
            limit=3,
        )
    except Exception:
        logger.exception("RAG indisponible pour eval ouverte (simulation sciences)")

    messages = build_open_eval_prompt(
        question, discipline, theme_titre, student_answer, rag_passages
    )
    raw = _safe_chat(Task.SCIENCES_SIM_EVAL_OPEN, messages, fallback="")
    if not raw:
        return False

    parsed = _try_parse_eval_json(raw)
    if parsed is None:
        logger.warning(
            "Reponse Albert non parsable pour eval ouverte simulation sciences "
            "(question=%s) : %r",
            question.get("id", "?"),
            raw[:200],
        )
        return False
    return bool(parsed.get("correct"))


def _try_parse_eval_json(raw: str) -> dict | None:
    txt = raw.strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK.search(txt)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ============================================================================
# 2. Indice gradue
# ============================================================================


def generate_hint(
    question: dict,
    discipline: str,
    theme_titre: str,
    hint_level: int,
    previous_answers: list[str],
) -> str:
    messages = build_hint_prompt(
        question, discipline, theme_titre, hint_level, previous_answers
    )
    return _safe_chat(
        Task.SCIENCES_SIM_HINT,
        messages,
        fallback=_fallback_hint(question, hint_level),
    )


def _fallback_hint(question: dict, hint_level: int) -> str:
    indices = question.get("indices") or {}
    pre = indices.get(f"niveau_{hint_level}")
    if pre:
        return pre

    scoring = question.get("scoring") or {}

    if hint_level == 1:
        return "Relis bien les documents et l'enonce de la question."

    if hint_level == 2:
        type_rep = scoring.get("type_reponse")
        if type_rep == "qcm":
            return "Relis les propositions et elimine celles qui sont incoherentes."
        if type_rep == "vrai_faux":
            return "Reprends l'enonce et cherche le point qui contredit (ou confirme) l'affirmation."
        if type_rep in ("pourcentage", "entier", "decimal"):
            return "La reponse est un nombre. Verifie tes calculs."
        return "Reprends la definition precise de la notion en jeu."

    rep = scoring.get("reponse_canonique") or scoring.get("reponse_modele") or ""
    rep_str = str(rep).strip()
    if rep_str:
        return f"Elle commence par << {rep_str[:1]} >>."
    return "Tu y es presque, retente."


# ============================================================================
# 3. Revelation de la reponse
# ============================================================================


def reveal_answer(
    question: dict,
    discipline: str = "",
    theme_titre: str = "",
) -> str:
    messages = build_reveal_prompt(question, discipline, theme_titre)
    return _safe_chat(
        Task.SCIENCES_SIM_REVEAL,
        messages,
        fallback=_fallback_reveal(question),
    )


def _fallback_reveal(question: dict) -> str:
    scoring = question.get("scoring") or {}
    rep = scoring.get("reponse_canonique") or scoring.get("reponse_modele") or "?"
    unite = scoring.get("unite")
    rep_label = f"{rep} {unite}" if unite else rep
    explication = question.get("reveal_explication")
    if explication:
        return (
            f"Pas grave. La bonne reponse etait : **{rep_label}**. "
            f"{explication}"
        )
    return f"Pas grave. La bonne reponse etait : **{rep_label}**."


__all__ = [
    "evaluate_answer",
    "generate_hint",
    "reveal_answer",
]

"""Orchestration pédagogique de l'épreuve Révision sciences.

Trois fonctions publiques :

- `evaluate_answer(question, student_answer)` : dispatch selon le mode
  de scoring. `python` → `scoring.check` déterministe ; `albert` →
  `_safe_chat(SCIENCES_REV_EVAL_OPEN, ...)` qui renvoie `True/False`
  après parsing JSON, avec fallback `False` si Albert plante.

- `generate_hint(question, hint_level, previous_answers)` : indice
  gradué via Albert (Mistral-Small). Fallback déterministe basé sur le
  type de réponse / les premiers caractères de la réponse canonique si
  Albert est indisponible.

- `reveal_answer(question)` : message de révélation via Albert. Fallback
  déterministe avec la réponse canonique brute + l'explication pré-écrite
  si Albert plante.

Toutes les erreurs Albert sont attrapées ici via `_safe_chat`. L'app
n'expose jamais de stack trace à l'élève.
"""

from __future__ import annotations

import json
import logging
import re

from app.core.albert_client import AlbertClient, AlbertError, Task
from app.core.rag import get_default_rag_client
from app.sciences.revision import models as science_models
from app.sciences.revision import scoring as science_scoring
from app.sciences.revision.prompts import (
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
    "Désolé, j'ai eu un petit souci pour te répondre. Réessaie dans quelques secondes."
)


def _safe_chat(task: Task, messages: list[dict], fallback: str) -> str:
    """Wrapper unique pour les appels Albert côté révision sciences."""
    try:
        client = get_albert_client()
        result = client.chat(task, messages, retry_on_missing_citations=False)
        return (result.content or "").strip() or fallback
    except AlbertError as exc:
        logger.warning("Albert a renvoyé une erreur (%s) : %s", task, exc)
        return fallback
    except Exception:  # pragma: no cover
        logger.exception("Erreur inattendue lors de l'appel Albert (%s)", task)
        return fallback


# ============================================================================
# 1. Évaluation d'une réponse
# ============================================================================


def evaluate_answer(
    question: science_models.SciencesQuestionRow, student_answer: str
) -> bool:
    """Dispatch déterministe vs Albert selon le mode de scoring."""
    if not student_answer or not student_answer.strip():
        return False

    scoring = question.scoring or {}
    mode = scoring.get("mode")

    if mode == "python":
        return science_scoring.check(scoring, student_answer)

    if mode == "albert":
        return _evaluate_open(question, student_answer)

    logger.warning(
        "Mode de scoring inconnu pour question=%s : %r", question.id, mode
    )
    return False


_JSON_BLOCK = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _evaluate_open(
    question: science_models.SciencesQuestionRow, student_answer: str
) -> bool:
    """Évalue une réponse ouverte via Albert (gpt-oss-120b).

    Le prompt force un JSON strict. On parse, on retombe sur `False` en
    cas de souci. On injecte un peu de RAG (méthodo + programme) pour
    permettre au modèle d'ancrer son feedback sur une source citable.
    """
    rag_passages: list = []
    try:
        rag = get_default_rag_client()
        rag_passages = rag.search_for_task(
            subject_kind="sciences",
            task=Task.SCIENCES_REV_EVAL_OPEN,
            query=question.enonce,
            limit=3,
        )
    except Exception:
        logger.exception("RAG indisponible pour eval ouverte (sciences)")

    messages = build_open_eval_prompt(question, student_answer, rag_passages)
    raw = _safe_chat(
        Task.SCIENCES_REV_EVAL_OPEN,
        messages,
        fallback="",
    )
    if not raw:
        return False

    parsed = _try_parse_eval_json(raw)
    if parsed is None:
        logger.warning(
            "Réponse Albert non parsable pour eval ouverte sciences "
            "(question=%s) : %r",
            question.id,
            raw[:200],
        )
        return False
    return bool(parsed.get("correct"))


def _try_parse_eval_json(raw: str) -> dict | None:
    """Tente de parser un JSON `{"correct": ..., "feedback_court": ...}`."""
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
# 2. Indice gradué
# ============================================================================


def generate_hint(
    question: science_models.SciencesQuestionRow,
    hint_level: int,
    previous_answers: list[str],
) -> str:
    """Indice gradué via Albert (Mistral-Small) + fallback déterministe."""
    messages = build_hint_prompt(question, hint_level, previous_answers)
    return _safe_chat(
        Task.SCIENCES_REV_HINT,
        messages,
        fallback=_fallback_hint(question, hint_level),
    )


def _fallback_hint(
    question: science_models.SciencesQuestionRow, hint_level: int
) -> str:
    """Indice déterministe utilisé quand Albert est indisponible."""
    indices = question.indices or {}
    pre = indices.get(f"niveau_{hint_level}")
    if pre:
        return pre

    scoring = question.scoring or {}

    if hint_level == 1:
        theme = question.theme.replace("_", " ")
        return f"Pense au thème : {theme}."

    if hint_level == 2:
        type_rep = scoring.get("type_reponse")
        if type_rep == "qcm":
            return "Relis les propositions et élimine celles qui sont incohérentes."
        if type_rep == "vrai_faux":
            return "Reprends l'énoncé et cherche le point qui contredit (ou confirme) l'affirmation."
        if type_rep == "pourcentage":
            return "La réponse est un pourcentage."
        if type_rep == "entier":
            return "La réponse est un nombre entier."
        if type_rep == "decimal":
            return "La réponse est un nombre décimal."
        return "Reprends la définition précise de la notion en jeu."

    # niveau 3 : on regarde la réponse canonique
    rep = scoring.get("reponse_canonique") or scoring.get("reponse_modele") or ""
    rep_str = str(rep).strip()
    if rep_str:
        return f"Elle commence par « {rep_str[:1]} »."
    return "Tu y es presque, retente."


# ============================================================================
# 3. Révélation de la réponse
# ============================================================================


def reveal_answer(
    question: science_models.SciencesQuestionRow,
) -> str:
    messages = build_reveal_prompt(question)
    return _safe_chat(
        Task.SCIENCES_REV_REVEAL,
        messages,
        fallback=_fallback_reveal(question),
    )


def _fallback_reveal(
    question: science_models.SciencesQuestionRow,
) -> str:
    scoring = question.scoring or {}
    rep = scoring.get("reponse_canonique") or scoring.get("reponse_modele") or "?"
    unite = scoring.get("unite")
    rep_label = f"{rep} {unite}" if unite else rep
    if question.reveal_explication:
        return (
            f"Pas grave. La bonne réponse était : **{rep_label}**. "
            f"{question.reveal_explication}"
        )
    return f"Pas grave. La bonne réponse était : **{rep_label}**."


__all__ = [
    "evaluate_answer",
    "generate_hint",
    "reveal_answer",
]

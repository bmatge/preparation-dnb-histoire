"""Orchestration pédagogique de la sous-épreuve « problèmes ».

Trois fonctions publiques, strictement symétriques à celles des
automatismes mais adaptées à la structure « exercice + sous-question » :

- ``evaluate_answer(exercise, subquestion, student_answer)`` : dispatch
  sur le mode de scoring. ``python`` → ``scoring.check`` (déterministe).
  ``albert`` → évaluation ouverte via ``Task.MATH_PROB_EVAL_OPEN`` qui
  retourne ``True/False`` après parsing JSON, avec fallback sur
  ``False`` si Albert plante.

- ``generate_hint(exercise, subquestion, hint_level, previous_answers)``
  : indice gradué via Albert (Mistral-Small), fallback déterministe
  basé sur les indices pré-calculés dans le JSON.

- ``reveal_answer(exercise, subquestion)`` : message de révélation via
  Albert, fallback déterministe avec la réponse canonique brute et
  l'explication pré-calculée.

Toutes les erreurs Albert sont attrapées ici via ``_safe_chat`` :
l'app n'expose jamais de stack trace à l'élève.
"""

from __future__ import annotations

import json
import logging
import re

from app.core.albert_client import AlbertClient, AlbertError, Task
from app.core.rag import get_default_rag_client
from app.mathematiques.problemes import models as prob_models
from app.mathematiques.problemes import scoring as prob_scoring
from app.mathematiques.problemes.prompts import (
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
    """Wrapper unique pour les appels Albert côté problèmes."""
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
    exercise: prob_models.ProblemExercise,
    subquestion: dict,
    student_answer: str,
) -> bool:
    """Dispatch déterministe vs Albert selon le mode de scoring."""
    if not student_answer or not student_answer.strip():
        return False

    scoring = (subquestion or {}).get("scoring") or {}
    mode = scoring.get("mode")

    if mode == "python":
        return prob_scoring.check(scoring, student_answer)

    if mode == "albert":
        return _evaluate_open(exercise, subquestion, student_answer)

    logger.warning(
        "Mode de scoring inconnu pour sous-question=%s : %r",
        subquestion.get("id"),
        mode,
    )
    return False


_JSON_BLOCK = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _evaluate_open(
    exercise: prob_models.ProblemExercise,
    subquestion: dict,
    student_answer: str,
) -> bool:
    """Évalue une justification ouverte via Albert (gpt-oss-120b).

    Le prompt force un JSON strict ; on parse, on retombe sur ``False``
    en cas de souci. On injecte un peu de RAG (méthodo + programme)
    pour permettre au modèle de citer une source dans son feedback.
    """
    rag_passages: list = []
    try:
        rag = get_default_rag_client()
        rag_passages = rag.search_for_task(
            subject_kind="mathematiques",
            task=Task.MATH_PROB_EVAL_OPEN,
            query=subquestion.get("texte") or "",
            limit=3,
        )
    except Exception:
        logger.exception("RAG indisponible pour eval ouverte problème")

    messages = build_open_eval_prompt(
        exercise, subquestion, student_answer, rag_passages
    )
    raw = _safe_chat(
        Task.MATH_PROB_EVAL_OPEN,
        messages,
        fallback="",
    )
    if not raw:
        return False

    parsed = _try_parse_eval_json(raw)
    if parsed is None:
        logger.warning(
            "Réponse Albert non parsable pour eval ouverte problème "
            "(sous_question=%s) : %r",
            subquestion.get("id"),
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
# 2. Indice gradué
# ============================================================================


def generate_hint(
    exercise: prob_models.ProblemExercise,
    subquestion: dict,
    hint_level: int,
    previous_answers: list[str],
) -> str:
    """Indice gradué via Albert (Mistral-Small) + fallback déterministe."""
    messages = build_hint_prompt(
        exercise, subquestion, hint_level, previous_answers
    )
    return _safe_chat(
        Task.MATH_PROB_HINT,
        messages,
        fallback=_fallback_hint(subquestion, hint_level),
    )


def _fallback_hint(subquestion: dict, hint_level: int) -> str:
    """Indice déterministe utilisé quand Albert est indisponible.

    Niveau 1 : indice pré-calculé niveau_1 si présent, sinon rappel du type.
    Niveau 2 : indice pré-calculé niveau_2 si présent.
    Niveau 3 : indice pré-calculé niveau_3 si présent, sinon premier
               caractère de la réponse canonique.
    """
    indices = subquestion.get("indices") or {}
    pre = indices.get(f"niveau_{hint_level}")
    if pre:
        return pre

    scoring = subquestion.get("scoring") or {}

    if hint_level == 1:
        return "Reprends l'énoncé et identifie ce qu'on te demande de calculer."

    if hint_level == 2:
        type_rep = scoring.get("type_reponse")
        if type_rep == "fraction":
            return "La réponse se met sous la forme d'une fraction a/b."
        if type_rep == "pourcentage":
            return "La réponse est un pourcentage."
        if type_rep == "entier":
            return "La réponse est un entier — pas besoin de virgule."
        if type_rep == "decimal":
            return "La réponse est un nombre décimal."
        return "Reformule l'étape clé et applique la propriété qui va avec."

    # niveau 3 : on regarde la réponse canonique
    rep = (
        scoring.get("reponse_canonique")
        or scoring.get("reponse_modele")
        or ""
    )
    rep_str = str(rep).strip()
    if rep_str:
        return f"Elle commence par « {rep_str[:1]} »."
    return "Tu y es presque, retente."


# ============================================================================
# 3. Révélation de la réponse
# ============================================================================


def reveal_answer(
    exercise: prob_models.ProblemExercise, subquestion: dict
) -> str:
    messages = build_reveal_prompt(exercise, subquestion)
    return _safe_chat(
        Task.MATH_PROB_REVEAL,
        messages,
        fallback=_fallback_reveal(subquestion),
    )


def _fallback_reveal(subquestion: dict) -> str:
    scoring = subquestion.get("scoring") or {}
    rep = (
        scoring.get("reponse_canonique")
        or scoring.get("reponse_modele")
        or "?"
    )
    unite = scoring.get("unite")
    rep_label = f"{rep} {unite}" if unite else rep
    reveal_explication = subquestion.get("reveal_explication")
    if reveal_explication:
        return (
            f"Pas grave. La bonne réponse était : **{rep_label}**. "
            f"{reveal_explication}"
        )
    return f"Pas grave. La bonne réponse était : **{rep_label}**."


__all__ = [
    "evaluate_answer",
    "generate_hint",
    "reveal_answer",
]

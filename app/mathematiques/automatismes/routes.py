"""Routes FastAPI de l'épreuve « Automatismes » (mathématiques DNB).

Ce router est inclus par `app.mathematiques.routes` (router racine
matière) sous le préfixe `/automatismes`. URLs finales :

  GET  /mathematiques/automatismes/                accueil (choix thème + start)
  POST /mathematiques/automatismes/quiz/new        création d'un quiz N questions
  GET  /mathematiques/automatismes/quiz            affiche la question courante
  POST /mathematiques/automatismes/quiz/answer     évalue, renvoie partial HTMX
  POST /mathematiques/automatismes/quiz/hint       demande un indice gradué
  POST /mathematiques/automatismes/quiz/reveal     révèle la réponse (« je sèche »)
  GET  /mathematiques/automatismes/quiz/next       passe à la question suivante
  GET  /mathematiques/automatismes/quiz/synthese   écran de fin de partie
  GET  /mathematiques/automatismes/restart         efface le quiz courant

L'état du quiz (liste des questions tirées, index courant, indices
consommés…) vit dans le cookie Starlette
(`request.session["math_auto_quiz"]`). Les tentatives sont aussi tracées
en DB (`AutoAttempt`) pour analytics.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlmodel import Session as DBSession

from app.core import db as core_db
from app.core.db import db_session
from app.core.formatting import render_eval_markdown
from app.mathematiques.automatismes import models as auto_models
from app.mathematiques.automatismes import loader as auto_loader
from app.mathematiques.automatismes.pedagogy import (
    evaluate_answer,
    generate_hint,
    reveal_answer,
)
from app.mathematiques.automatismes.prompts import random_positive_feedback

logger = logging.getLogger(__name__)

PREFIX = "/mathematiques/automatismes"

router = APIRouter(tags=["mathematiques / automatismes"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_MATH_TEMPLATES = _HERE.parent / "templates"  # pour _maths_base.html + _tools_fab.html
_AUTO_TEMPLATES = _HERE / "templates"

templates = Jinja2Templates(
    directory=[str(_AUTO_TEMPLATES), str(_MATH_TEMPLATES), str(_CORE_TEMPLATES)]
)
templates.env.filters["eval_md"] = lambda txt: Markup(render_eval_markdown(txt or ""))


# ============================================================================
# Constantes
# ============================================================================

DEFAULT_QUIZ_LENGTH = 10
ALLOWED_QUIZ_LENGTHS = (5, 10)
THEME_LABELS = auto_loader.THEME_LABELS  # ré-export pour les templates
SUBJECT_KIND = "math_automatismes"


# ============================================================================
# Helpers d'état (cookie Starlette)
# ============================================================================


def _get_quiz_state(request: Request) -> dict | None:
    state = request.session.get("math_auto_quiz")
    if not isinstance(state, dict):
        return None
    return state


def _set_quiz_state(request: Request, state: dict) -> None:
    request.session["math_auto_quiz"] = state


def _clear_quiz_state(request: Request) -> None:
    request.session.pop("math_auto_quiz", None)


def _current_question(
    s: DBSession, state: dict
) -> auto_models.AutoQuestion | None:
    ids = state.get("question_ids") or []
    idx = state.get("current_index", 0)
    if idx >= len(ids):
        return None
    return auto_models.get_question(s, ids[idx])


def _advance(state: dict) -> None:
    state["current_index"] = state.get("current_index", 0) + 1
    state["current_hints"] = 0
    state["previous_answers"] = []
    state["revealed"] = False
    state["completed"] = False


def _already_completed_feedback(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_partials/feedback.html",
        {
            "kind": "error",
            "message": (
                "Tu as déjà validé cette question. Clique sur « Question "
                "suivante » pour continuer."
            ),
            "show_next": True,
        },
    )


# ============================================================================
# Accueil de l'épreuve
# ============================================================================


@router.get("/", response_class=HTMLResponse)
def automatismes_home(
    request: Request,
    s: DBSession = Depends(db_session),
):
    """Accueil : choix du thème, longueur du quiz, bouton « Commencer »."""
    themes = auto_models.list_themes(s)
    available = [(t, THEME_LABELS.get(t, t)) for t in themes]
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "available_themes": available,
            "allowed_lengths": ALLOWED_QUIZ_LENGTHS,
            "default_length": DEFAULT_QUIZ_LENGTH,
        },
    )


# ============================================================================
# Création d'un quiz
# ============================================================================


@router.post("/quiz/new")
def quiz_new(
    request: Request,
    theme: str = Form(default=""),
    length: int = Form(default=DEFAULT_QUIZ_LENGTH),
    mode: str = Form(default="tout"),
    user_key: str = Form(default=""),
    s: DBSession = Depends(db_session),
):
    """Crée un quiz de N questions et démarre la session."""
    if length not in ALLOWED_QUIZ_LENGTHS:
        length = DEFAULT_QUIZ_LENGTH

    exclude_ids: list[str] | None = None
    only_ids: list[str] | None = None
    user_key = user_key or request.headers.get("x-user-key") or ""
    if user_key and mode == "skip-reussies":
        exclude_ids = core_db.get_item_ids_by_status(s, user_key, SUBJECT_KIND, "reussi")
    elif user_key and mode == "refaire-echecs":
        only_ids = core_db.get_item_ids_by_status(s, user_key, SUBJECT_KIND, "rate")
        if not only_ids:
            only_ids = None  # fallback tirage normal

    questions = auto_loader.pick_for_quiz(
        s, n=length, theme=theme or None,
        exclude_ids=exclude_ids, only_ids=only_ids,
    )
    if not questions:
        return RedirectResponse(
            url=f"{PREFIX}/?erreur=aucune_question", status_code=303
        )

    new_sess = core_db.create_session(
        s,
        subject_kind=SUBJECT_KIND,
        subject_id=None,
        mode="semi_assiste",
    )
    state = {
        "db_session_id": new_sess.id,
        "question_ids": [q.id for q in questions],
        "current_index": 0,
        "current_hints": 0,
        "previous_answers": [],
        "revealed": False,
        "completed": False,
        "missed_ids": [],
        "score": 0,
        "filter_theme": theme or None,
    }
    _set_quiz_state(request, state)
    return RedirectResponse(url=f"{PREFIX}/quiz", status_code=303)


# ============================================================================
# Affichage de la question courante
# ============================================================================


@router.get("/quiz", response_class=HTMLResponse)
def quiz_page(
    request: Request,
    s: DBSession = Depends(db_session),
):
    """Affiche la question courante du quiz en cours."""
    state = _get_quiz_state(request)
    if state is None:
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    question = _current_question(s, state)
    if question is None:
        return RedirectResponse(url=f"{PREFIX}/quiz/synthese", status_code=303)

    return templates.TemplateResponse(
        request,
        "quiz.html",
        {
            "question": question,
            "theme_label": THEME_LABELS.get(question.theme, question.theme),
            "position": state["current_index"] + 1,
            "total": len(state["question_ids"]),
            "score": state.get("score", 0),
            "hints_used": state.get("current_hints", 0),
            "revealed": state.get("revealed", False),
            "session_id": state["db_session_id"],
        },
    )


# ============================================================================
# Soumission d'une réponse
# ============================================================================


@router.post("/quiz/answer", response_class=HTMLResponse)
def quiz_answer(
    request: Request,
    answer: str = Form(...),
    s: DBSession = Depends(db_session),
):
    """Évalue une réponse et renvoie un fragment HTMX."""
    state = _get_quiz_state(request)
    if state is None:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    if state.get("completed"):
        return _already_completed_feedback(request)

    question = _current_question(s, state)
    if question is None:
        return RedirectResponse(url=f"{PREFIX}/quiz/synthese", status_code=303)

    answer = (answer or "").strip()
    if not answer:
        return templates.TemplateResponse(
            request,
            "_partials/feedback.html",
            {
                "kind": "error",
                "message": "Écris une réponse avant de valider.",
                "show_next": False,
            },
        )

    is_correct = evaluate_answer(question, answer)
    hints_used = state.get("current_hints", 0)

    auto_models.add_attempt(
        s,
        session_id=state["db_session_id"],
        question_id=question.id,
        student_answer=answer,
        is_correct=is_correct,
        hints_used=hints_used,
        scoring_mode=question.scoring_mode,
    )
    user_key = request.headers.get("x-user-key")
    if user_key:
        core_db.record_progress(s, user_key, SUBJECT_KIND, str(question.id), is_correct)

    if is_correct:
        if hints_used == 0:
            state["score"] = state.get("score", 0) + 1
        state["completed"] = True
        _set_quiz_state(request, state)
        return templates.TemplateResponse(
            request,
            "_partials/feedback.html",
            {
                "kind": "correct",
                "message": random_positive_feedback(),
                "show_next": True,
            },
        )

    previous = state.get("previous_answers") or []
    previous.append(answer)
    state["previous_answers"] = previous
    _set_quiz_state(request, state)
    return templates.TemplateResponse(
        request,
        "_partials/feedback.html",
        {
            "kind": "incorrect",
            "message": "Ce n'est pas ça. Tu peux retenter, demander un indice, ou demander la réponse.",
            "hints_used": hints_used,
            "max_hints": 3,
            "show_next": False,
        },
    )


# ============================================================================
# Indice
# ============================================================================


@router.post("/quiz/hint", response_class=HTMLResponse)
def quiz_hint(
    request: Request,
    s: DBSession = Depends(db_session),
):
    """Renvoie un indice gradué (niveau 1, 2 ou 3)."""
    state = _get_quiz_state(request)
    if state is None:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    if state.get("completed"):
        return _already_completed_feedback(request)

    question = _current_question(s, state)
    if question is None:
        return RedirectResponse(url=f"{PREFIX}/quiz/synthese", status_code=303)

    hints_used = state.get("current_hints", 0)
    if hints_used >= 3:
        return templates.TemplateResponse(
            request,
            "_partials/feedback.html",
            {
                "kind": "error",
                "message": "Tu as déjà eu les 3 indices. Tu peux demander la réponse.",
                "show_next": False,
            },
        )

    next_level = hints_used + 1
    hint_text = generate_hint(
        question, next_level, state.get("previous_answers") or []
    )
    state["current_hints"] = next_level
    _set_quiz_state(request, state)
    return templates.TemplateResponse(
        request,
        "_partials/feedback.html",
        {
            "kind": "hint",
            "message": hint_text,
            "hint_level": next_level,
            "max_hints": 3,
            "show_next": False,
        },
    )


# ============================================================================
# Révélation explicite (« je sèche »)
# ============================================================================


@router.post("/quiz/reveal", response_class=HTMLResponse)
def quiz_reveal(
    request: Request,
    s: DBSession = Depends(db_session),
):
    """Révèle la bonne réponse et marque la question comme manquée."""
    state = _get_quiz_state(request)
    if state is None:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    if state.get("completed"):
        return _already_completed_feedback(request)

    question = _current_question(s, state)
    if question is None:
        return RedirectResponse(url=f"{PREFIX}/quiz/synthese", status_code=303)

    reveal_text = reveal_answer(question)
    auto_models.add_attempt(
        s,
        session_id=state["db_session_id"],
        question_id=question.id,
        student_answer="",
        is_correct=False,
        hints_used=state.get("current_hints", 0),
        scoring_mode=question.scoring_mode,
    )
    user_key = request.headers.get("x-user-key")
    if user_key:
        core_db.record_progress(s, user_key, SUBJECT_KIND, str(question.id), False)
    missed = state.get("missed_ids") or []
    if question.id not in missed:
        missed.append(question.id)
    state["missed_ids"] = missed
    state["revealed"] = True
    state["completed"] = True
    _set_quiz_state(request, state)
    return templates.TemplateResponse(
        request,
        "_partials/feedback.html",
        {
            "kind": "revealed",
            "message": reveal_text,
            "show_next": True,
        },
    )


# ============================================================================
# Synthèse de fin de quiz
# ============================================================================


@router.get("/quiz/synthese", response_class=HTMLResponse)
def quiz_synthese(
    request: Request,
    s: DBSession = Depends(db_session),
):
    state = _get_quiz_state(request)
    if state is None:
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    missed_ids: list[str] = state.get("missed_ids") or []
    missed_questions = [
        q
        for q in (auto_models.get_question(s, qid) for qid in missed_ids)
        if q
    ]
    total = len(state.get("question_ids") or [])
    score = state.get("score", 0)

    return templates.TemplateResponse(
        request,
        "synthese.html",
        {
            "score": score,
            "total": total,
            "missed_questions": missed_questions,
            "has_missed": bool(missed_questions),
            "theme_labels": THEME_LABELS,
        },
    )


@router.get("/quiz/next")
def quiz_next(request: Request):
    """Avance à la question suivante après validation ou révélation.

    L'avancement est découplé de la validation/révélation pour éviter
    une désynchronisation si l'élève reclique sur « Valider » ou
    « Indice » alors que la question affichée est déjà résolue côté
    serveur. Tant que cet endpoint n'a pas été appelé, l'état pointe
    sur la question que le navigateur affiche.
    """
    state = _get_quiz_state(request)
    if state is None:
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)
    _advance(state)
    _set_quiz_state(request, state)
    return RedirectResponse(url=f"{PREFIX}/quiz", status_code=303)


@router.get("/restart")
def automatismes_restart(request: Request):
    _clear_quiz_state(request)
    return RedirectResponse(url=f"{PREFIX}/", status_code=303)


__all__ = ["router", "PREFIX"]

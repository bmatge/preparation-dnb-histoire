"""Routes FastAPI de l'épreuve « Révision » sciences.

Monté par `app.sciences.routes` (router racine matière) sous le préfixe
`/revision`. URLs finales :

  GET  /sciences/revision/                               index épreuve (3 cartes disciplines)
  GET  /sciences/revision/{discipline_slug}/             accueil discipline (choix thème + start)
  POST /sciences/revision/{discipline_slug}/quiz/new     création d'un quiz N questions
  GET  /sciences/revision/quiz                           affiche la question courante
  POST /sciences/revision/quiz/answer                    évalue, renvoie partial HTMX
  POST /sciences/revision/quiz/hint                      demande un indice gradué
  POST /sciences/revision/quiz/reveal                    révèle la réponse
  GET  /sciences/revision/quiz/synthese                  écran de fin de quiz
  GET  /sciences/revision/restart                        efface le quiz courant

L'état du quiz (liste des questions tirées, index courant, indices
consommés, discipline choisie…) vit dans le cookie Starlette
(`request.session["sciences_rev_quiz"]`). Les tentatives sont aussi
tracées en DB (`SciencesAttempt`) pour les analytics.

Les slugs de discipline dans l'URL (`physique-chimie`, `svt`,
`technologie`) sont convertis en identifiants internes
(`physique_chimie`, `svt`, `technologie`) via `DISCIPLINE_FROM_SLUG`.
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
from app.sciences.revision import models as science_models
from app.sciences.revision import loader as science_loader
from app.sciences.revision.loader import (
    DISCIPLINE_FROM_SLUG,
    DISCIPLINE_LABELS,
    DISCIPLINE_SLUGS,
    THEME_LABELS,
)
from app.sciences.revision.pedagogy import (
    evaluate_answer,
    generate_hint,
    reveal_answer,
)
from app.sciences.revision.prompts import random_positive_feedback

logger = logging.getLogger(__name__)

PREFIX = "/sciences/revision"

router = APIRouter(tags=["sciences / revision"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_SCIENCES_TEMPLATES = _HERE.parent / "templates"  # pour _sciences_base.html
_REV_TEMPLATES = _HERE / "templates"

templates = Jinja2Templates(
    directory=[
        str(_REV_TEMPLATES),
        str(_SCIENCES_TEMPLATES),
        str(_CORE_TEMPLATES),
    ]
)
templates.env.filters["eval_md"] = lambda txt: Markup(render_eval_markdown(txt or ""))


# ============================================================================
# Constantes
# ============================================================================

DEFAULT_QUIZ_LENGTH = 10
ALLOWED_QUIZ_LENGTHS = (5, 10)
SUBJECT_KIND = "sciences_revision"


# ============================================================================
# Helpers discipline
# ============================================================================


def _resolve_discipline_slug(slug: str) -> str:
    """Convertit un slug d'URL en identifiant interne ou lève 404."""
    discipline = DISCIPLINE_FROM_SLUG.get(slug)
    if discipline is None:
        raise HTTPException(status_code=404, detail="Discipline inconnue")
    return discipline


# ============================================================================
# Helpers d'état (cookie Starlette)
# ============================================================================


def _get_quiz_state(request: Request) -> dict | None:
    state = request.session.get("sciences_rev_quiz")
    if not isinstance(state, dict):
        return None
    return state


def _set_quiz_state(request: Request, state: dict) -> None:
    request.session["sciences_rev_quiz"] = state


def _clear_quiz_state(request: Request) -> None:
    request.session.pop("sciences_rev_quiz", None)


def _current_question(
    s: DBSession, state: dict
) -> science_models.SciencesQuestionRow | None:
    ids = state.get("question_ids") or []
    idx = state.get("current_index", 0)
    if idx >= len(ids):
        return None
    return science_models.get_question(s, ids[idx])


def _advance(state: dict) -> None:
    state["current_index"] = state.get("current_index", 0) + 1
    state["current_hints"] = 0
    state["previous_answers"] = []
    state["revealed"] = False


# ============================================================================
# Index de l'épreuve (3 cartes disciplines)
# ============================================================================


@router.get("/", response_class=HTMLResponse)
def revision_index(request: Request):
    """Page d'index de l'épreuve : liste des 3 disciplines."""
    disciplines = [
        (DISCIPLINE_SLUGS[d], DISCIPLINE_LABELS[d])
        for d in science_models.ALLOWED_DISCIPLINES
    ]
    return templates.TemplateResponse(
        request,
        "index.html",
        {"disciplines": disciplines},
    )


# ============================================================================
# Accueil d'une discipline (choix thème + longueur)
# ============================================================================


@router.get("/{discipline_slug}/", response_class=HTMLResponse)
def revision_home(
    request: Request,
    discipline_slug: str,
    s: DBSession = Depends(db_session),
):
    """Accueil d'une discipline : choix du thème + longueur + bouton start."""
    discipline = _resolve_discipline_slug(discipline_slug)
    themes = science_models.list_themes_for_discipline(s, discipline)
    available = [(t, THEME_LABELS.get(t, t)) for t in themes]
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "discipline": discipline,
            "discipline_slug": discipline_slug,
            "discipline_label": DISCIPLINE_LABELS.get(discipline, discipline),
            "available_themes": available,
            "allowed_lengths": ALLOWED_QUIZ_LENGTHS,
            "default_length": DEFAULT_QUIZ_LENGTH,
        },
    )


# ============================================================================
# Création d'un quiz
# ============================================================================


@router.post("/{discipline_slug}/quiz/new")
def quiz_new(
    request: Request,
    discipline_slug: str,
    theme: str = Form(default=""),
    length: int = Form(default=DEFAULT_QUIZ_LENGTH),
    s: DBSession = Depends(db_session),
):
    """Crée un quiz de N questions pour une discipline donnée et démarre."""
    discipline = _resolve_discipline_slug(discipline_slug)
    if length not in ALLOWED_QUIZ_LENGTHS:
        length = DEFAULT_QUIZ_LENGTH
    questions = science_loader.pick_for_quiz(
        s, n=length, discipline=discipline, theme=theme or None
    )
    if not questions:
        return RedirectResponse(
            url=f"{PREFIX}/{discipline_slug}/?erreur=aucune_question",
            status_code=303,
        )

    new_sess = core_db.create_session(
        s,
        subject_kind=SUBJECT_KIND,
        subject_id=None,
        mode="semi_assiste",
    )
    state = {
        "db_session_id": new_sess.id,
        "discipline": discipline,
        "discipline_slug": discipline_slug,
        "question_ids": [q.id for q in questions],
        "current_index": 0,
        "current_hints": 0,
        "previous_answers": [],
        "revealed": False,
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

    discipline = state.get("discipline") or question.discipline
    return templates.TemplateResponse(
        request,
        "quiz.html",
        {
            "question": question,
            "discipline_label": DISCIPLINE_LABELS.get(discipline, discipline),
            "discipline_slug": state.get("discipline_slug")
            or DISCIPLINE_SLUGS.get(discipline, discipline),
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

    science_models.add_attempt(
        s,
        session_id=state["db_session_id"],
        question_id=question.id,
        student_answer=answer,
        is_correct=is_correct,
        hints_used=hints_used,
        scoring_mode=question.scoring_mode,
    )

    if is_correct:
        if hints_used == 0:
            state["score"] = state.get("score", 0) + 1
        _advance(state)
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
# Révélation explicite
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

    question = _current_question(s, state)
    if question is None:
        return RedirectResponse(url=f"{PREFIX}/quiz/synthese", status_code=303)

    reveal_text = reveal_answer(question)
    science_models.add_attempt(
        s,
        session_id=state["db_session_id"],
        question_id=question.id,
        student_answer="",
        is_correct=False,
        hints_used=state.get("current_hints", 0),
        scoring_mode=question.scoring_mode,
    )
    missed = state.get("missed_ids") or []
    if question.id not in missed:
        missed.append(question.id)
    state["missed_ids"] = missed
    state["revealed"] = True
    _advance(state)
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
        for q in (science_models.get_question(s, qid) for qid in missed_ids)
        if q
    ]
    total = len(state.get("question_ids") or [])
    score = state.get("score", 0)
    discipline = state.get("discipline") or "?"

    return templates.TemplateResponse(
        request,
        "synthese.html",
        {
            "score": score,
            "total": total,
            "missed_questions": missed_questions,
            "has_missed": bool(missed_questions),
            "theme_labels": THEME_LABELS,
            "discipline": discipline,
            "discipline_slug": state.get("discipline_slug")
            or DISCIPLINE_SLUGS.get(discipline, discipline),
            "discipline_label": DISCIPLINE_LABELS.get(discipline, discipline),
        },
    )


@router.get("/restart")
def revision_restart(request: Request):
    _clear_quiz_state(request)
    return RedirectResponse(url=f"{PREFIX}/", status_code=303)


__all__ = ["router", "PREFIX"]

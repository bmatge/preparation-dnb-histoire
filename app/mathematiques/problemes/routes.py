"""Routes FastAPI de la sous-épreuve « Raisonnement et résolution de
problèmes » (mathématiques DNB 2026).

Ce router est inclus par ``app.mathematiques.routes`` (router racine
matière) sous le préfixe ``/problemes``. URLs finales :

  GET  /mathematiques/problemes/                      accueil : liste d'exercices
  GET  /mathematiques/problemes/start/{exercise_id}   démarre un exercice
  GET  /mathematiques/problemes/travail                 affiche la sous-question courante
  POST /mathematiques/problemes/travail/answer          évalue, renvoie partial HTMX
  POST /mathematiques/problemes/travail/hint            indice gradué
  POST /mathematiques/problemes/travail/reveal          révèle la sous-question
  GET  /mathematiques/problemes/travail/synthese        bilan de l'exercice
  GET  /mathematiques/problemes/restart                 efface l'exercice en cours

L'état vit dans le cookie Starlette (``request.session["math_prob"]``) :
id de l'exercice, index de la sous-question courante, indices utilisés
par sous-question, réponses déjà tentées, score. Les tentatives sont
aussi tracées en DB (``ProblemAttempt``) pour analytics.
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
from app.mathematiques.problemes import models as prob_models
from app.mathematiques.problemes import loader as prob_loader
from app.mathematiques.problemes.pedagogy import (
    evaluate_answer,
    generate_hint,
    reveal_answer,
)
from app.mathematiques.problemes.prompts import random_positive_feedback

logger = logging.getLogger(__name__)

PREFIX = "/mathematiques/problemes"

router = APIRouter(tags=["mathematiques / problemes"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_MATH_TEMPLATES = _HERE.parent / "templates"  # pour _maths_base.html + _tools_fab.html
_PROB_TEMPLATES = _HERE / "templates"

templates = Jinja2Templates(
    directory=[str(_PROB_TEMPLATES), str(_MATH_TEMPLATES), str(_CORE_TEMPLATES)]
)
templates.env.filters["eval_md"] = lambda txt: Markup(render_eval_markdown(txt or ""))


# ============================================================================
# Constantes
# ============================================================================

THEME_LABELS = prob_loader.THEME_LABELS  # ré-export pour les templates
SUBJECT_KIND = "math_problemes"
MAX_HINTS_PER_SUBQUESTION = 3


# ============================================================================
# Helpers d'état (cookie Starlette)
# ============================================================================


def _get_state(request: Request) -> dict | None:
    state = request.session.get("math_prob")
    if not isinstance(state, dict):
        return None
    return state


def _set_state(request: Request, state: dict) -> None:
    request.session["math_prob"] = state


def _clear_state(request: Request) -> None:
    request.session.pop("math_prob", None)


def _current_exercise(
    s: DBSession, state: dict
) -> prob_models.ProblemExercise | None:
    exercise_id = state.get("exercise_id")
    if not exercise_id:
        return None
    return prob_models.get_exercise(s, exercise_id)


def _current_subquestion(
    exercise: prob_models.ProblemExercise, state: dict
) -> dict | None:
    sous_questions = exercise.sous_questions
    idx = state.get("current_index", 0)
    if idx >= len(sous_questions):
        return None
    return sous_questions[idx]


def _advance(state: dict) -> None:
    state["current_index"] = state.get("current_index", 0) + 1
    state["current_hints"] = 0
    state["previous_answers"] = []
    state["revealed"] = False


def _push_history(
    state: dict, sq_id: str, status: str, answer: str, hints_used: int
) -> None:
    """Trace une sous-question terminée dans l'historique du cookie.

    Statuts utilisés :
    - ``correct_first_try`` : bonne réponse du premier coup, sans indice
    - ``correct_with_hints`` : bonne réponse après 1+ indice
    - ``revealed`` : l'élève a demandé la révélation

    Historique utilisé pour la barre de progression cliquable et la
    synthèse finale. On garde ``answer`` pour que l'élève puisse revoir
    sa propre réponse sur les sous-questions terminées.
    """
    history = state.get("history") or []
    history.append(
        {
            "sq_id": sq_id,
            "status": status,
            "answer": answer,
            "hints_used": hints_used,
        }
    )
    state["history"] = history


def _build_progress(
    exercise: prob_models.ProblemExercise, state: dict, viewing_index: int
) -> list[dict]:
    """Construit la liste des pastilles de progression pour le template.

    ``viewing_index`` est la sous-question actuellement visible dans la
    page (soit l'index courant en mode normal, soit un index historique
    en mode révision). Permet de mettre en valeur la pastille visible
    sans confondre avec l'index de reprise.
    """
    history = state.get("history") or []
    current_index = state.get("current_index", 0)
    progress: list[dict] = []
    for i, sq in enumerate(exercise.sous_questions):
        if i < len(history):
            h = history[i]
            status = h.get("status") or "correct_first_try"
            clickable = True
        elif i == current_index:
            status = "current"
            clickable = False
        else:
            status = "pending"
            clickable = False
        progress.append(
            {
                "index": i,
                "numero": sq.get("numero", str(i + 1)),
                "status": status,
                "clickable": clickable,
                "is_viewing": (i == viewing_index),
            }
        )
    return progress


# ============================================================================
# Accueil de la sous-épreuve
# ============================================================================


@router.get("/", response_class=HTMLResponse)
def problemes_home(
    request: Request,
    theme: str = "",
    s: DBSession = Depends(db_session),
):
    """Accueil : liste des exercices, éventuellement filtrée par thème."""
    exercises = prob_loader.list_for_home(s, theme=theme or None)
    available_themes = [
        (t, THEME_LABELS.get(t, t)) for t in prob_models.list_themes(s)
    ]
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "exercises": exercises,
            "available_themes": available_themes,
            "selected_theme": theme or "",
            "theme_labels": THEME_LABELS,
        },
    )


# ============================================================================
# Démarrage d'un exercice
# ============================================================================


@router.get("/start/{exercise_id}")
def start_exercise(
    request: Request,
    exercise_id: str,
    s: DBSession = Depends(db_session),
):
    """Démarre un exercice : crée une session DB et place l'état cookie."""
    exercise = prob_models.get_exercise(s, exercise_id)
    if exercise is None:
        return RedirectResponse(
            url=f"{PREFIX}/?erreur=introuvable", status_code=303
        )

    new_sess = core_db.create_session(
        s,
        subject_kind=SUBJECT_KIND,
        subject_id=None,
        mode="semi_assiste",
        user_key=request.headers.get("x-user-key") or None,
    )
    state = {
        "db_session_id": new_sess.id,
        "exercise_id": exercise.id,
        "current_index": 0,
        "current_hints": 0,
        "previous_answers": [],
        "revealed": False,
        "missed_ids": [],
        "score": 0,
        "history": [],
    }
    _set_state(request, state)
    return RedirectResponse(url=f"{PREFIX}/travail", status_code=303)


# ============================================================================
# Affichage de la sous-question courante
# ============================================================================


@router.get("/travail", response_class=HTMLResponse)
def travail_page(
    request: Request,
    s: DBSession = Depends(db_session),
):
    """Affiche la sous-question courante de l'exercice en cours."""
    state = _get_state(request)
    if state is None:
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    exercise = _current_exercise(s, state)
    if exercise is None:
        _clear_state(request)
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    subquestion = _current_subquestion(exercise, state)
    if subquestion is None:
        return RedirectResponse(
            url=f"{PREFIX}/travail/synthese", status_code=303
        )

    current_index = state["current_index"]
    total = len(exercise.sous_questions)
    return templates.TemplateResponse(
        request,
        "travail.html",
        {
            "exercise": exercise,
            "subquestion": subquestion,
            "theme_label": THEME_LABELS.get(exercise.theme, exercise.theme),
            "position": current_index + 1,
            "total": total,
            "score": state.get("score", 0),
            "hints_used": state.get("current_hints", 0),
            "max_hints": MAX_HINTS_PER_SUBQUESTION,
            "session_id": state["db_session_id"],
            "progress": _build_progress(exercise, state, current_index),
            "read_only": False,
            "read_only_entry": None,
        },
    )


@router.get("/travail/revoir/{index}", response_class=HTMLResponse)
def travail_revoir(
    request: Request,
    index: int,
    s: DBSession = Depends(db_session),
):
    """Affiche en lecture seule une sous-question déjà traitée.

    N'altère pas ``state["current_index"]`` : l'élève peut revenir au
    point courant de son parcours en cliquant sur la dernière pastille
    de la barre de progression ou sur le lien « Reprendre ».
    """
    state = _get_state(request)
    if state is None:
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    exercise = _current_exercise(s, state)
    if exercise is None:
        _clear_state(request)
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    history = state.get("history") or []
    sous_questions = exercise.sous_questions
    if not (0 <= index < len(history)) or index >= len(sous_questions):
        # Index hors historique : on renvoie sur le parcours en cours.
        return RedirectResponse(url=f"{PREFIX}/travail", status_code=303)

    subquestion = sous_questions[index]
    entry = history[index]
    total = len(sous_questions)
    return templates.TemplateResponse(
        request,
        "travail.html",
        {
            "exercise": exercise,
            "subquestion": subquestion,
            "theme_label": THEME_LABELS.get(exercise.theme, exercise.theme),
            "position": index + 1,
            "total": total,
            "score": state.get("score", 0),
            "hints_used": entry.get("hints_used", 0),
            "max_hints": MAX_HINTS_PER_SUBQUESTION,
            "session_id": state["db_session_id"],
            "progress": _build_progress(exercise, state, index),
            "read_only": True,
            "read_only_entry": entry,
        },
    )


# ============================================================================
# Soumission d'une réponse
# ============================================================================


@router.post("/travail/answer", response_class=HTMLResponse)
def travail_answer(
    request: Request,
    answer: str = Form(...),
    s: DBSession = Depends(db_session),
):
    """Évalue une réponse et renvoie un fragment HTMX."""
    state = _get_state(request)
    if state is None:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    exercise = _current_exercise(s, state)
    if exercise is None:
        _clear_state(request)
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    subquestion = _current_subquestion(exercise, state)
    if subquestion is None:
        return RedirectResponse(
            url=f"{PREFIX}/travail/synthese", status_code=303
        )

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

    is_correct = evaluate_answer(exercise, subquestion, answer)
    hints_used = state.get("current_hints", 0)

    scoring = subquestion.get("scoring") or {}
    prob_models.add_attempt(
        s,
        session_id=state["db_session_id"],
        exercise_id=exercise.id,
        subquestion_id=subquestion.get("id", "?"),
        student_answer=answer,
        is_correct=is_correct,
        hints_used=hints_used,
        scoring_mode=scoring.get("mode") or "?",
    )
    user_key = request.headers.get("x-user-key")
    if user_key:
        core_db.record_progress(s, user_key, SUBJECT_KIND, subquestion.get("id", "?"), is_correct)

    if is_correct:
        if hints_used == 0:
            state["score"] = state.get("score", 0) + 1
        _push_history(
            state,
            sq_id=subquestion.get("id", "?"),
            status=(
                "correct_first_try" if hints_used == 0 else "correct_with_hints"
            ),
            answer=answer,
            hints_used=hints_used,
        )
        _advance(state)
        _set_state(request, state)
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
    _set_state(request, state)
    return templates.TemplateResponse(
        request,
        "_partials/feedback.html",
        {
            "kind": "incorrect",
            "message": "Ce n'est pas ça. Tu peux retenter, demander un indice, ou demander la réponse.",
            "hints_used": hints_used,
            "max_hints": MAX_HINTS_PER_SUBQUESTION,
            "show_next": False,
        },
    )


# ============================================================================
# Indice
# ============================================================================


@router.post("/travail/hint", response_class=HTMLResponse)
def travail_hint(
    request: Request,
    s: DBSession = Depends(db_session),
):
    """Renvoie un indice gradué (niveau 1, 2 ou 3)."""
    state = _get_state(request)
    if state is None:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    exercise = _current_exercise(s, state)
    if exercise is None:
        _clear_state(request)
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    subquestion = _current_subquestion(exercise, state)
    if subquestion is None:
        return RedirectResponse(
            url=f"{PREFIX}/travail/synthese", status_code=303
        )

    hints_used = state.get("current_hints", 0)
    if hints_used >= MAX_HINTS_PER_SUBQUESTION:
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
        exercise, subquestion, next_level, state.get("previous_answers") or []
    )
    state["current_hints"] = next_level
    _set_state(request, state)
    return templates.TemplateResponse(
        request,
        "_partials/feedback.html",
        {
            "kind": "hint",
            "message": hint_text,
            "hint_level": next_level,
            "max_hints": MAX_HINTS_PER_SUBQUESTION,
            "show_next": False,
        },
    )


# ============================================================================
# Révélation explicite (« je sèche »)
# ============================================================================


@router.post("/travail/reveal", response_class=HTMLResponse)
def travail_reveal(
    request: Request,
    s: DBSession = Depends(db_session),
):
    """Révèle la bonne réponse et marque la sous-question comme manquée."""
    state = _get_state(request)
    if state is None:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    exercise = _current_exercise(s, state)
    if exercise is None:
        _clear_state(request)
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    subquestion = _current_subquestion(exercise, state)
    if subquestion is None:
        return RedirectResponse(
            url=f"{PREFIX}/travail/synthese", status_code=303
        )

    reveal_text = reveal_answer(exercise, subquestion)
    scoring = subquestion.get("scoring") or {}
    prob_models.add_attempt(
        s,
        session_id=state["db_session_id"],
        exercise_id=exercise.id,
        subquestion_id=subquestion.get("id", "?"),
        student_answer="",
        is_correct=False,
        hints_used=state.get("current_hints", 0),
        scoring_mode=scoring.get("mode") or "?",
    )
    user_key = request.headers.get("x-user-key")
    if user_key:
        core_db.record_progress(s, user_key, SUBJECT_KIND, subquestion.get("id", "?"), False)
    missed = state.get("missed_ids") or []
    sq_id = subquestion.get("id")
    if sq_id and sq_id not in missed:
        missed.append(sq_id)
    state["missed_ids"] = missed
    state["revealed"] = True
    _push_history(
        state,
        sq_id=sq_id or "?",
        status="revealed",
        answer="",
        hints_used=state.get("current_hints", 0),
    )
    _advance(state)
    _set_state(request, state)
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
# Synthèse de fin d'exercice
# ============================================================================


@router.get("/travail/synthese", response_class=HTMLResponse)
def travail_synthese(
    request: Request,
    s: DBSession = Depends(db_session),
):
    state = _get_state(request)
    if state is None:
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    exercise = _current_exercise(s, state)
    if exercise is None:
        _clear_state(request)
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    sous_questions = exercise.sous_questions
    total = len(sous_questions)
    score = state.get("score", 0)
    missed_ids = set(state.get("missed_ids") or [])
    history = state.get("history") or []

    # Récap complet : une entrée par sous-question avec son statut,
    # la réponse donnée par l'élève et la réponse attendue. Permet
    # d'afficher un tableau de bord en fin d'exercice plutôt qu'une
    # simple liste des ratés.
    recap: list[dict] = []
    for i, sq in enumerate(sous_questions):
        entry = history[i] if i < len(history) else None
        scoring = sq.get("scoring") or {}
        reponse_attendue = (
            scoring.get("reponse_canonique")
            or scoring.get("reponse_modele")
            or "?"
        )
        unite = scoring.get("unite") or ""
        recap.append(
            {
                "index": i,
                "numero": sq.get("numero", str(i + 1)),
                "texte": sq.get("texte", ""),
                "status": (entry.get("status") if entry else "skipped"),
                "student_answer": entry.get("answer") if entry else "",
                "hints_used": entry.get("hints_used", 0) if entry else 0,
                "reponse_attendue": reponse_attendue,
                "unite": unite,
                "reveal_explication": sq.get("reveal_explication") or "",
            }
        )
    missed_subquestions = [
        sq for sq in sous_questions if sq.get("id") in missed_ids
    ]

    return templates.TemplateResponse(
        request,
        "synthese.html",
        {
            "exercise": exercise,
            "theme_label": THEME_LABELS.get(exercise.theme, exercise.theme),
            "total": total,
            "score": score,
            "missed_subquestions": missed_subquestions,
            "has_missed": bool(missed_subquestions),
            "recap": recap,
        },
    )


@router.get("/restart")
def problemes_restart(request: Request):
    _clear_state(request)
    return RedirectResponse(url=f"{PREFIX}/", status_code=303)


__all__ = ["router", "PREFIX"]

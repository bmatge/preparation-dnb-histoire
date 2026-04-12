"""
Routes FastAPI de l'épreuve « Repères chronologiques et spatiaux ».

Ce router est inclus par `app.histoire_geo_emc.routes` (router racine
matière) sous le préfixe `/reperes`. URLs finales :

  GET  /histoire-geo-emc/reperes/                accueil (choix thème + start)
  POST /histoire-geo-emc/reperes/quiz/new        création d'un quiz 15 questions
  GET  /histoire-geo-emc/reperes/quiz            affiche la question courante
  POST /histoire-geo-emc/reperes/quiz/answer     évalue, renvoie partial HTMX
  GET  /histoire-geo-emc/reperes/quiz/synthese   écran de fin de partie
  GET  /histoire-geo-emc/reperes/restart         efface le quiz courant

L'état du quiz (liste des repères tirés, index courant, indices
consommés, file de réexposition) vit dans le cookie Starlette
(`request.session["reperes_quiz"]`). Les tentatives sont aussi tracées
en DB (`RepereAttempt`) pour analytics.
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
from app.histoire_geo_emc.reperes import models as reperes_models
from app.histoire_geo_emc.reperes.pedagogy import (
    evaluate_answer,
    generate_hint,
    generate_question,
    reveal_answer,
)
from app.histoire_geo_emc.reperes.prompts import random_positive_feedback

logger = logging.getLogger(__name__)

PREFIX = "/histoire-geo-emc/reperes"

router = APIRouter(tags=["histoire-geo-emc / repères"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_HGEMC_TEMPLATES = _HERE.parent / "templates"  # pour _hgemc_base.html + _tools_fab.html
_REPERES_TEMPLATES = _HERE / "templates"

templates = Jinja2Templates(
    directory=[str(_REPERES_TEMPLATES), str(_HGEMC_TEMPLATES), str(_CORE_TEMPLATES)]
)
templates.env.filters["eval_md"] = lambda txt: Markup(render_eval_markdown(txt or ""))


# ============================================================================
# Constantes
# ============================================================================


QUIZ_LENGTH = 15  # nombre de repères tirés par quiz


# ============================================================================
# Helpers quiz-state (cookie Starlette)
# ============================================================================


def _get_quiz_state(request: Request) -> dict | None:
    state = request.session.get("reperes_quiz")
    if not isinstance(state, dict):
        return None
    return state


def _set_quiz_state(request: Request, state: dict) -> None:
    request.session["reperes_quiz"] = state


def _clear_quiz_state(request: Request) -> None:
    request.session.pop("reperes_quiz", None)


def _current_repere(
    s: DBSession, state: dict
) -> reperes_models.Repere | None:
    ids = state.get("repere_ids") or []
    idx = state.get("current_index", 0)
    if idx >= len(ids):
        return None
    return reperes_models.get_repere(s, ids[idx])


def _advance(state: dict) -> None:
    """Avance au repère suivant et réinitialise les compteurs."""
    state["current_index"] = state.get("current_index", 0) + 1
    state["current_hints"] = 0
    state["previous_answers"] = []
    state["revealed"] = False
    state["current_question"] = None


# ============================================================================
# Accueil de l'épreuve
# ============================================================================


@router.get("/", response_class=HTMLResponse)
def reperes_home(
    request: Request,
    s: DBSession = Depends(db_session),
):
    """Page d'accueil : sélection du thème + bouton « Commencer »."""
    themes = reperes_models.list_themes(s)
    themes_by_discipline: dict[str, list[str]] = {
        "histoire": [],
        "geographie": [],
        "emc": [],
    }
    from sqlmodel import select

    rows = s.exec(
        select(reperes_models.Repere.discipline, reperes_models.Repere.theme).distinct()
    ).all()
    for disc, theme in rows:
        if disc in themes_by_discipline and theme:
            themes_by_discipline[disc].append(theme)
    for k in themes_by_discipline:
        themes_by_discipline[k] = sorted(set(themes_by_discipline[k]))

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "themes_by_discipline": themes_by_discipline,
            "quiz_length": QUIZ_LENGTH,
        },
    )


# ============================================================================
# Création d'un quiz
# ============================================================================


@router.post("/quiz/new")
def quiz_new(
    request: Request,
    discipline: str = Form(default=""),
    theme: str = Form(default=""),
    mode: str = Form(default="tout"),
    user_key: str = Form(default=""),
    s: DBSession = Depends(db_session),
):
    """Crée un quiz de N repères pseudo-aléatoires et démarre la session."""
    exclude_ids: list[str] | None = None
    only_ids: list[str] | None = None
    user_key = user_key or request.headers.get("x-user-key") or ""
    if user_key and mode == "skip-reussies":
        exclude_ids = core_db.get_item_ids_by_status(s, user_key, "hgemc_reperes", "reussi")
    elif user_key and mode == "refaire-echecs":
        only_ids = core_db.get_item_ids_by_status(s, user_key, "hgemc_reperes", "rate")
        if not only_ids:
            only_ids = None  # fallback tirage normal

    reperes = reperes_models.random_reperes(
        s,
        n=QUIZ_LENGTH,
        discipline=discipline or None,
        theme=theme or None,
        exclude_ids=exclude_ids,
        only_ids=only_ids,
    )
    if not reperes:
        return RedirectResponse(
            url=f"{PREFIX}/?erreur=aucun_repere", status_code=303
        )

    new_sess = core_db.create_session(
        s,
        subject_kind="hgemc_reperes",
        subject_id=None,
        mode="semi_assiste",
        user_key=user_key or None,
    )
    state = {
        "db_session_id": new_sess.id,
        "repere_ids": [r.id for r in reperes],
        "current_index": 0,
        "current_hints": 0,
        "previous_answers": [],
        "revealed": False,
        "current_question": None,
        "missed_ids": [],
        "score": 0,
        "filter_discipline": discipline or None,
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
    """Affiche la question courante du quiz en cours.

    Si on est au-delà de la dernière question → redirige vers la synthèse.
    """
    state = _get_quiz_state(request)
    if state is None:
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    repere = _current_repere(s, state)
    if repere is None:
        return RedirectResponse(url=f"{PREFIX}/quiz/synthese", status_code=303)

    # On ne régénère la question qu'une fois par repère (sinon à chaque
    # rafraîchissement de page on refait un aller-retour Albert).
    question = state.get("current_question")
    if not question:
        question = generate_question(repere)
        state["current_question"] = question
        _set_quiz_state(request, state)

    return templates.TemplateResponse(
        request,
        "quiz.html",
        {
            "question": question,
            "position": state["current_index"] + 1,
            "total": len(state["repere_ids"]),
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
    """Évalue une réponse et renvoie un fragment HTMX selon le cas.

    Cas possibles :
    - Correct → feedback positif, le repère est marqué comme traité,
      bouton « Question suivante ».
    - Incorrect et hints < 3 → indice gradué, l'élève peut retenter.
    - Incorrect et hints == 3 → réponse révélée, marquage `missed`,
      bouton « Question suivante ».
    """
    state = _get_quiz_state(request)
    if state is None:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    repere = _current_repere(s, state)
    if repere is None:
        return RedirectResponse(url=f"{PREFIX}/quiz/synthese", status_code=303)

    answer = (answer or "").strip()
    if not answer:
        return templates.TemplateResponse(
            request,
            "_partials/feedback.html",
            {
                "kind": "error",
                "message": "Écris une réponse avant d'envoyer.",
                "show_next": False,
            },
        )

    is_correct = evaluate_answer(repere, answer, question=state.get("current_question"))
    hints_used = state.get("current_hints", 0)

    if is_correct:
        # Trace analytique
        reperes_models.add_attempt(
            s,
            session_id=state["db_session_id"],
            repere_id=repere.id,
            question_asked=state.get("current_question", "") or "",
            student_answer=answer,
            is_correct=True,
            hints_used=hints_used,
        )
        user_key = request.headers.get("x-user-key")
        if user_key:
            core_db.record_progress(s, user_key, "hgemc_reperes", str(repere.id), True)
        state["score"] = state.get("score", 0) + (1 if hints_used == 0 else 0)
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

    # Incorrect
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
    """Génère un indice gradué pour le repère courant."""
    state = _get_quiz_state(request)
    if state is None:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    repere = _current_repere(s, state)
    if repere is None:
        return RedirectResponse(url=f"{PREFIX}/quiz/synthese", status_code=303)

    hints_used = state.get("current_hints", 0)
    if hints_used >= 3:
        return templates.TemplateResponse(
            request,
            "_partials/feedback.html",
            {
                "kind": "error",
                "message": "Tu as déjà utilisé tes 3 indices. Retente ou demande la réponse.",
                "show_next": False,
            },
        )

    hint_level = hints_used + 1
    previous = state.get("previous_answers") or []
    hint_text = generate_hint(repere, hint_level, previous)
    state["current_hints"] = hint_level
    _set_quiz_state(request, state)
    return templates.TemplateResponse(
        request,
        "_partials/feedback.html",
        {
            "kind": "hint",
            "message": hint_text,
            "hint_level": hint_level,
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
    """Révèle la bonne réponse et marque le repère comme manqué."""
    state = _get_quiz_state(request)
    if state is None:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    repere = _current_repere(s, state)
    if repere is None:
        return RedirectResponse(url=f"{PREFIX}/quiz/synthese", status_code=303)

    reveal_text = reveal_answer(repere)
    reperes_models.add_attempt(
        s,
        session_id=state["db_session_id"],
        repere_id=repere.id,
        question_asked=state.get("current_question", "") or "",
        student_answer="",
        is_correct=False,
        hints_used=state.get("current_hints", 0),
    )
    user_key = request.headers.get("x-user-key")
    if user_key:
        core_db.record_progress(s, user_key, "hgemc_reperes", str(repere.id), False)
    missed = state.get("missed_ids") or []
    if repere.id not in missed:
        missed.append(repere.id)
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
    """Écran de fin de quiz : score, repères manqués, bouton « revoir »."""
    state = _get_quiz_state(request)
    if state is None:
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    missed_ids: list[str] = state.get("missed_ids") or []
    missed_reperes = [
        r for r in (reperes_models.get_repere(s, rid) for rid in missed_ids) if r
    ]
    total = len(state.get("repere_ids") or [])
    score = state.get("score", 0)

    return templates.TemplateResponse(
        request,
        "synthese.html",
        {
            "score": score,
            "total": total,
            "missed_reperes": missed_reperes,
            "has_missed": bool(missed_reperes),
        },
    )


@router.post("/quiz/revoir")
def quiz_revoir(
    request: Request,
    s: DBSession = Depends(db_session),
):
    """Relance un quiz uniquement sur les repères manqués."""
    state = _get_quiz_state(request)
    if state is None:
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    missed_ids = state.get("missed_ids") or []
    if not missed_ids:
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    # Nouvelle session DB, même règles.
    new_sess = core_db.create_session(
        s,
        subject_kind="hgemc_reperes",
        subject_id=None,
        mode="semi_assiste",
        user_key=request.headers.get("x-user-key") or None,
    )
    new_state = {
        "db_session_id": new_sess.id,
        "repere_ids": list(missed_ids),
        "current_index": 0,
        "current_hints": 0,
        "previous_answers": [],
        "revealed": False,
        "current_question": None,
        "missed_ids": [],
        "score": 0,
        "filter_discipline": state.get("filter_discipline"),
        "filter_theme": state.get("filter_theme"),
    }
    _set_quiz_state(request, new_state)
    return RedirectResponse(url=f"{PREFIX}/quiz", status_code=303)


# ============================================================================
# Restart
# ============================================================================


@router.get("/restart")
def reperes_restart(request: Request):
    _clear_quiz_state(request)
    return RedirectResponse(url=f"{PREFIX}/", status_code=303)


__all__ = ["router", "PREFIX"]

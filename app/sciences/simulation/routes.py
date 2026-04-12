"""Routes FastAPI de l'epreuve « Simulation » sciences.

Monte par ``app.sciences.routes`` (router racine matiere) sous le prefixe
``/simulation``. URLs finales :

  GET  /sciences/simulation/                      grille des sujets
  GET  /sciences/simulation/start/{sujet_id}      demarre une session
  GET  /sciences/simulation/travail               question courante
  POST /sciences/simulation/travail/answer        evalue, renvoie partial HTMX
  POST /sciences/simulation/travail/hint          indice gradue
  POST /sciences/simulation/travail/reveal        revele la reponse
  GET  /sciences/simulation/travail/synthese      bilan par discipline
  GET  /sciences/simulation/restart               efface l'etat
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
from app.sciences.simulation import models as sim_models
from app.sciences.simulation.loader import DISCIPLINE_LABELS
from app.sciences.simulation.pedagogy import (
    evaluate_answer,
    generate_hint,
    reveal_answer,
)
from app.sciences.simulation.prompts import random_positive_feedback

logger = logging.getLogger(__name__)

PREFIX = "/sciences/simulation"

router = APIRouter(tags=["sciences / simulation"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_SCIENCES_TEMPLATES = _HERE.parent / "templates"
_SIM_TEMPLATES = _HERE / "templates"

templates = Jinja2Templates(
    directory=[
        str(_SIM_TEMPLATES),
        str(_SCIENCES_TEMPLATES),
        str(_CORE_TEMPLATES),
    ]
)
templates.env.filters["eval_md"] = lambda txt: Markup(render_eval_markdown(txt or ""))


# ============================================================================
# Constantes
# ============================================================================

SUBJECT_KIND = "sciences_simulation"


# ============================================================================
# Helpers d'etat (cookie Starlette)
# ============================================================================


def _get_state(request: Request) -> dict | None:
    state = request.session.get("sciences_sim")
    if not isinstance(state, dict):
        return None
    return state


def _set_state(request: Request, state: dict) -> None:
    request.session["sciences_sim"] = state


def _clear_state(request: Request) -> None:
    request.session.pop("sciences_sim", None)


def _current_sujet(
    s: DBSession, state: dict
) -> sim_models.SimulationSujet | None:
    sujet_id = state.get("sujet_id")
    if not sujet_id:
        return None
    return sim_models.get_sujet(s, sujet_id)


def _current_position(state: dict) -> dict | None:
    """Renvoie la position courante {disc_idx, q_idx} ou None si fini."""
    path = state.get("question_path") or []
    step = state.get("current_step", 0)
    if step >= len(path):
        return None
    return path[step]


def _advance(state: dict) -> None:
    state["current_step"] = state.get("current_step", 0) + 1
    state["current_hints"] = 0
    state["previous_answers"] = []
    state["revealed"] = False


def _build_question_path(sujet: sim_models.SimulationSujet) -> list[dict]:
    """Construit la liste aplatie des positions (disc_idx, q_idx)."""
    path = []
    for disc_idx, disc in enumerate(sujet.disciplines):
        questions = disc.get("questions", [])
        for q_idx in range(len(questions)):
            path.append({"disc_idx": disc_idx, "q_idx": q_idx})
    return path


# ============================================================================
# Grille des sujets
# ============================================================================


@router.get("/", response_class=HTMLResponse)
def simulation_home(
    request: Request,
    s: DBSession = Depends(db_session),
):
    sujets = sim_models.list_sujets(s)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "sujets": sujets,
            "discipline_labels": DISCIPLINE_LABELS,
        },
    )


# ============================================================================
# Demarrage d'une simulation
# ============================================================================


@router.get("/start/{sujet_id}")
def start_simulation(
    request: Request,
    sujet_id: str,
    s: DBSession = Depends(db_session),
):
    sujet = sim_models.get_sujet(s, sujet_id)
    if sujet is None:
        raise HTTPException(status_code=404, detail="Sujet introuvable")

    question_path = _build_question_path(sujet)
    if not question_path:
        raise HTTPException(status_code=400, detail="Sujet sans questions")

    new_sess = core_db.create_session(
        s,
        subject_kind=SUBJECT_KIND,
        subject_id=None,
        mode="semi_assiste",
    )

    disc_scores = {}
    for disc in sujet.disciplines:
        disc_scores[disc.get("id", "")] = {
            "points": 0.0,
            "max_points": disc.get("points", 25.0),
            "discipline": disc.get("discipline", ""),
        }

    state = {
        "db_session_id": new_sess.id,
        "sujet_id": sujet_id,
        "question_path": question_path,
        "current_step": 0,
        "current_hints": 0,
        "previous_answers": [],
        "revealed": False,
        "missed_ids": [],
        "disc_scores": disc_scores,
        "total_points": 0.0,
    }
    _set_state(request, state)
    return RedirectResponse(url=f"{PREFIX}/travail", status_code=303)


# ============================================================================
# Page de travail (question courante)
# ============================================================================


@router.get("/travail", response_class=HTMLResponse)
def travail_page(
    request: Request,
    s: DBSession = Depends(db_session),
):
    state = _get_state(request)
    if state is None:
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    sujet = _current_sujet(s, state)
    if sujet is None:
        _clear_state(request)
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    pos = _current_position(state)
    if pos is None:
        return RedirectResponse(url=f"{PREFIX}/travail/synthese", status_code=303)

    disc_idx = pos["disc_idx"]
    q_idx = pos["q_idx"]
    disc = sujet.get_discipline(disc_idx)
    question = sujet.get_question(disc_idx, q_idx)

    if not question:
        return RedirectResponse(url=f"{PREFIX}/travail/synthese", status_code=303)

    # Documents a afficher : ceux references par la question, ou tous si vide
    docs_ref = question.get("documents_ref") or []
    all_docs = disc.get("documents", [])
    if docs_ref:
        current_documents = [d for d in all_docs if d.get("id") in docs_ref]
    else:
        current_documents = all_docs

    discipline_name = disc.get("discipline", "")
    total_questions = sujet.total_questions()
    step = state.get("current_step", 0)

    return templates.TemplateResponse(
        request,
        "travail.html",
        {
            "sujet": sujet,
            "question": question,
            "discipline": disc,
            "discipline_label": DISCIPLINE_LABELS.get(discipline_name, discipline_name),
            "theme_titre": disc.get("theme_titre", ""),
            "current_documents": current_documents,
            "position": step + 1,
            "total": total_questions,
            "disc_number": disc_idx + 1,
            "disc_total": len(sujet.disciplines),
            "hints_used": state.get("current_hints", 0),
            "revealed": state.get("revealed", False),
            "total_points": state.get("total_points", 0.0),
            "session_id": state["db_session_id"],
        },
    )


# ============================================================================
# Soumission d'une reponse
# ============================================================================


@router.post("/travail/answer", response_class=HTMLResponse)
def travail_answer(
    request: Request,
    answer: str = Form(...),
    s: DBSession = Depends(db_session),
):
    state = _get_state(request)
    if state is None:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    sujet = _current_sujet(s, state)
    if sujet is None:
        _clear_state(request)
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    pos = _current_position(state)
    if pos is None:
        return RedirectResponse(url=f"{PREFIX}/travail/synthese", status_code=303)

    disc_idx = pos["disc_idx"]
    q_idx = pos["q_idx"]
    disc = sujet.get_discipline(disc_idx)
    question = sujet.get_question(disc_idx, q_idx)

    if not question:
        return RedirectResponse(url=f"{PREFIX}/travail/synthese", status_code=303)

    answer = (answer or "").strip()
    if not answer:
        return templates.TemplateResponse(
            request,
            "_partials/feedback.html",
            {
                "kind": "error",
                "message": "Ecris une reponse avant de valider.",
                "show_next": False,
            },
        )

    discipline_name = disc.get("discipline", "")
    theme_titre = disc.get("theme_titre", "")

    is_correct = evaluate_answer(
        question, answer,
        discipline=discipline_name,
        theme_titre=theme_titre,
    )
    hints_used = state.get("current_hints", 0)
    scoring = question.get("scoring") or {}

    sim_models.add_attempt(
        s,
        session_id=state["db_session_id"],
        sujet_id=state["sujet_id"],
        discipline_id=disc.get("id", ""),
        question_id=question.get("id", ""),
        student_answer=answer,
        is_correct=is_correct,
        hints_used=hints_used,
        scoring_mode=scoring.get("mode", "python"),
    )

    if is_correct:
        if hints_used == 0:
            q_points = question.get("points", 0.0)
            disc_id = disc.get("id", "")
            disc_scores = state.get("disc_scores") or {}
            if disc_id in disc_scores:
                disc_scores[disc_id]["points"] = disc_scores[disc_id].get("points", 0.0) + q_points
            state["total_points"] = state.get("total_points", 0.0) + q_points
            state["disc_scores"] = disc_scores
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
            "message": "Ce n'est pas ca. Tu peux retenter, demander un indice, ou demander la reponse.",
            "hints_used": hints_used,
            "max_hints": 3,
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
    state = _get_state(request)
    if state is None:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    sujet = _current_sujet(s, state)
    if sujet is None:
        _clear_state(request)
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    pos = _current_position(state)
    if pos is None:
        return RedirectResponse(url=f"{PREFIX}/travail/synthese", status_code=303)

    disc = sujet.get_discipline(pos["disc_idx"])
    question = sujet.get_question(pos["disc_idx"], pos["q_idx"])

    if not question:
        return RedirectResponse(url=f"{PREFIX}/travail/synthese", status_code=303)

    hints_used = state.get("current_hints", 0)
    if hints_used >= 3:
        return templates.TemplateResponse(
            request,
            "_partials/feedback.html",
            {
                "kind": "error",
                "message": "Tu as deja eu les 3 indices. Tu peux demander la reponse.",
                "show_next": False,
            },
        )

    next_level = hints_used + 1
    hint_text = generate_hint(
        question,
        discipline=disc.get("discipline", ""),
        theme_titre=disc.get("theme_titre", ""),
        hint_level=next_level,
        previous_answers=state.get("previous_answers") or [],
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
            "max_hints": 3,
            "show_next": False,
        },
    )


# ============================================================================
# Revelation explicite
# ============================================================================


@router.post("/travail/reveal", response_class=HTMLResponse)
def travail_reveal(
    request: Request,
    s: DBSession = Depends(db_session),
):
    state = _get_state(request)
    if state is None:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    sujet = _current_sujet(s, state)
    if sujet is None:
        _clear_state(request)
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})

    pos = _current_position(state)
    if pos is None:
        return RedirectResponse(url=f"{PREFIX}/travail/synthese", status_code=303)

    disc = sujet.get_discipline(pos["disc_idx"])
    question = sujet.get_question(pos["disc_idx"], pos["q_idx"])

    if not question:
        return RedirectResponse(url=f"{PREFIX}/travail/synthese", status_code=303)

    reveal_text = reveal_answer(
        question,
        discipline=disc.get("discipline", ""),
        theme_titre=disc.get("theme_titre", ""),
    )

    scoring = question.get("scoring") or {}
    sim_models.add_attempt(
        s,
        session_id=state["db_session_id"],
        sujet_id=state["sujet_id"],
        discipline_id=disc.get("id", ""),
        question_id=question.get("id", ""),
        student_answer="",
        is_correct=False,
        hints_used=state.get("current_hints", 0),
        scoring_mode=scoring.get("mode", "python"),
    )

    missed = state.get("missed_ids") or []
    q_id = question.get("id", "")
    if q_id and q_id not in missed:
        missed.append(q_id)
    state["missed_ids"] = missed
    state["revealed"] = True
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
# Synthese de fin de simulation
# ============================================================================


@router.get("/travail/synthese", response_class=HTMLResponse)
def travail_synthese(
    request: Request,
    s: DBSession = Depends(db_session),
):
    state = _get_state(request)
    if state is None:
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    sujet = _current_sujet(s, state)
    if sujet is None:
        _clear_state(request)
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)

    disc_scores = state.get("disc_scores") or {}
    total_points = state.get("total_points", 0.0)
    missed_ids = set(state.get("missed_ids") or [])

    # Construire le bilan par discipline avec les questions ratees
    disciplines_bilan = []
    for disc_idx, disc in enumerate(sujet.disciplines):
        disc_id = disc.get("id", "")
        score_info = disc_scores.get(disc_id, {})
        missed_questions = []
        for q in disc.get("questions", []):
            if q.get("id") in missed_ids:
                missed_questions.append(q)
        disciplines_bilan.append({
            "discipline": disc.get("discipline", ""),
            "discipline_label": DISCIPLINE_LABELS.get(
                disc.get("discipline", ""), disc.get("discipline", "")
            ),
            "theme_titre": disc.get("theme_titre", ""),
            "points_obtenus": score_info.get("points", 0.0),
            "points_max": score_info.get("max_points", 25.0),
            "missed_questions": missed_questions,
        })

    return templates.TemplateResponse(
        request,
        "synthese.html",
        {
            "sujet": sujet,
            "disciplines_bilan": disciplines_bilan,
            "total_points": total_points,
            "points_total_max": sujet.points_total,
        },
    )


# ============================================================================
# Abandon
# ============================================================================


@router.get("/restart")
def simulation_restart(request: Request):
    _clear_state(request)
    return RedirectResponse(url=f"{PREFIX}/", status_code=303)


__all__ = ["router", "PREFIX"]

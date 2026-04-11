"""Routes FastAPI de la sous-épreuve compréhension/interprétation.

Parcours élève (MVP) :

    GET  /                              → accueil, liste des exercices, bouton "commencer"
    POST /session/new                   → crée la session, tire un exo, redirige vers item 1
    GET  /session/{sid}/item/{order}    → écran de travail (texte + question)
    POST /session/{sid}/item/{order}/answer  → HTMX : évalue la réponse
    POST /session/{sid}/item/{order}/hint    → HTMX : génère l'indice suivant
    POST /session/{sid}/item/{order}/reveal  → HTMX : révèle la bonne réponse
    GET  /session/{sid}/item/{order}/next    → passe à l'item suivant
    GET  /session/{sid}/synthese        → bilan de fin de session
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session as DBSession

from app.core.db import (
    add_turn,
    create_session,
    db_session,
    get_session,
    get_turns,
    get_turns_by_step,
    update_session_step,
)
from app.francais.comprehension import pedagogy as ped
from app.francais.comprehension.loader import (
    get_exercise,
    list_exercises,
    pick_exercise,
)
from app.francais.comprehension.models import SUBJECT_KIND, ExerciseItem

logger = logging.getLogger(__name__)

router = APIRouter(tags=["francais-comprehension"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent.parent
_REPO_ROOT = _APP_DIR.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_FR_TEMPLATES = _HERE.parent / "templates"  # pour _francais_base.html + _tools_fab.html
_COMP_TEMPLATES = _HERE / "templates"
_IMAGES_DIR = _REPO_ROOT / "content" / "francais" / "comprehension" / "images"

templates = Jinja2Templates(
    directory=[str(_COMP_TEMPLATES), str(_FR_TEMPLATES), str(_CORE_TEMPLATES)]
)


def _image_url_for(slug: str) -> str | None:
    """URL publique de l'illustration du sujet, ou None si le PNG n'existe pas."""
    if (_IMAGES_DIR / f"{slug}.png").exists():
        return f"/francais-images/{slug}.png"
    return None


# ============================================================================
# Helpers
# ============================================================================


def _load_session_exo(db: DBSession, session_id: int):
    """Charge la session + l'exercice associé, ou 404."""
    sess = get_session(db, session_id)
    if sess is None or sess.subject_kind != SUBJECT_KIND:
        raise HTTPException(status_code=404, detail="Session introuvable.")
    if sess.subject_id is None:
        raise HTTPException(status_code=500, detail="Session sans exercice associé.")
    row = get_exercise(db, sess.subject_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Exercice introuvable.")
    exo = row.load()
    items = exo.flatten_items(include_image_questions=True)
    return sess, row, exo, items


def _find_item(items: list[ExerciseItem], order: int) -> ExerciseItem:
    for it in items:
        if it.order == order:
            return it
    raise HTTPException(status_code=404, detail=f"Item {order} introuvable.")


def _last_user_answer(db: DBSession, session_id: int, step: int) -> str:
    """Récupère la dernière réponse élève pour un step, ou chaîne vide."""
    turns = get_turns_by_step(db, session_id, step)
    for t in reversed(turns):
        if t.role == "user":
            return t.content
    return ""


# ============================================================================
# Accueil / création de session
# ============================================================================


@router.get("/", response_class=HTMLResponse)
def comprehension_home(
    request: Request,
    db: DBSession = Depends(db_session),
):
    exercises = list_exercises(db)
    return templates.TemplateResponse(
        request,
        "home.html",
        {"exercises": exercises, "total": len(exercises)},
    )


@router.post("/session/new")
def new_session(
    request: Request,
    exercise_id: int | None = Form(None),
    db: DBSession = Depends(db_session),
):
    """Crée une session, tire un exercice (aléatoire si non précisé), redirige."""
    if exercise_id is not None:
        row = get_exercise(db, exercise_id)
    else:
        row = pick_exercise(db)

    if row is None:
        raise HTTPException(
            status_code=500,
            detail="Aucun exercice disponible. Vérifie le chargement du catalogue.",
        )

    sess = create_session(db, subject_kind=SUBJECT_KIND, subject_id=row.id)
    update_session_step(db, sess.id, 1)
    return RedirectResponse(
        url=f"/francais/comprehension/session/{sess.id}/item/1",
        status_code=303,
    )


# ============================================================================
# Écran de travail
# ============================================================================


@router.get("/session/{session_id}/item/{order}", response_class=HTMLResponse)
def show_item(
    session_id: int,
    order: int,
    request: Request,
    db: DBSession = Depends(db_session),
):
    sess, row, exo, items = _load_session_exo(db, session_id)
    item = _find_item(items, order)

    # Avance le curseur si l'élève navigue vers un item plus avancé
    if sess.current_step != order:
        update_session_step(db, session_id, order)

    # Historique de l'item pour ré-afficher les indices déjà obtenus et la
    # dernière réponse de l'élève sur retour à la page.
    turns = get_turns_by_step(db, session_id, order)
    hints_used = ped.count_hints_at_step(db, session_id, order)
    last_answer = _last_user_answer(db, session_id, order)
    image_url = _image_url_for(row.slug)

    return templates.TemplateResponse(
        request,
        "exercise.html",
        {
            "session": sess,
            "exercise": exo,
            "items": items,
            "item": item,
            "turns": turns,
            "hints_used": hints_used,
            "last_answer": last_answer,
            "total_items": len(items),
            "image_url": image_url,
        },
    )


# ============================================================================
# Actions HTMX
# ============================================================================


@router.post(
    "/session/{session_id}/item/{order}/answer", response_class=HTMLResponse
)
def submit_answer(
    session_id: int,
    order: int,
    request: Request,
    reponse: str = Form(...),
    db: DBSession = Depends(db_session),
):
    sess, row, exo, items = _load_session_exo(db, session_id)
    item = _find_item(items, order)

    result = ped.evaluate_answer(db, session_id, exo, item, reponse)

    next_order = order + 1 if order < len(items) else None
    is_last = next_order is None

    return templates.TemplateResponse(
        request,
        "_partials/feedback.html",
        {
            "item": item,
            "result": result,
            "session_id": session_id,
            "order": order,
            "next_order": next_order,
            "is_last": is_last,
            "hints_used": ped.count_hints_at_step(db, session_id, order),
        },
    )


@router.post(
    "/session/{session_id}/item/{order}/hint", response_class=HTMLResponse
)
def request_hint(
    session_id: int,
    order: int,
    request: Request,
    db: DBSession = Depends(db_session),
):
    sess, row, exo, items = _load_session_exo(db, session_id)
    item = _find_item(items, order)

    already = ped.count_hints_at_step(db, session_id, order)
    if already >= 3:
        raise HTTPException(
            status_code=400,
            detail="Tu as déjà reçu 3 indices. Tu peux demander la réponse.",
        )

    level = already + 1
    last_answer = _last_user_answer(db, session_id, order)
    hint = ped.generate_hint(db, session_id, exo, item, last_answer, level=level)

    return templates.TemplateResponse(
        request,
        "_partials/hint.html",
        {
            "item": item,
            "hint": hint,
            "level": level,
            "hints_used": level,
            "session_id": session_id,
            "order": order,
            "can_request_another": level < 3,
        },
    )


@router.post(
    "/session/{session_id}/item/{order}/reveal", response_class=HTMLResponse
)
def do_reveal(
    session_id: int,
    order: int,
    request: Request,
    db: DBSession = Depends(db_session),
):
    sess, row, exo, items = _load_session_exo(db, session_id)
    item = _find_item(items, order)

    last_answer = _last_user_answer(db, session_id, order)
    revealed = ped.reveal_answer(db, session_id, exo, item, last_answer)

    next_order = order + 1 if order < len(items) else None
    is_last = next_order is None

    return templates.TemplateResponse(
        request,
        "_partials/reveal.html",
        {
            "item": item,
            "reveal": revealed,
            "session_id": session_id,
            "order": order,
            "next_order": next_order,
            "is_last": is_last,
        },
    )


# ============================================================================
# Navigation
# ============================================================================


@router.get("/session/{session_id}/item/{order}/next")
def go_next(
    session_id: int,
    order: int,
    db: DBSession = Depends(db_session),
):
    sess, row, exo, items = _load_session_exo(db, session_id)
    next_order = order + 1
    if next_order > len(items):
        return RedirectResponse(
            url=f"/francais/comprehension/session/{session_id}/synthese",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/francais/comprehension/session/{session_id}/item/{next_order}",
        status_code=303,
    )


@router.get("/session/{session_id}/synthese", response_class=HTMLResponse)
def show_synthese(
    session_id: int,
    request: Request,
    db: DBSession = Depends(db_session),
):
    sess, row, exo, items = _load_session_exo(db, session_id)

    # Pour chaque item, détermine si l'élève a trouvé seul (verdict CORRECTE
    # sans révélation) ou après indices/révélation.
    items_resolved: list[tuple[ExerciseItem, str, bool]] = []
    for item in items:
        turns = get_turns_by_step(db, session_id, item.order)
        if not turns:
            continue
        last_user = _last_user_answer(db, session_id, item.order)
        revealed = any(
            t.role == "assistant" and t.content.startswith("[reveal]") for t in turns
        )
        # Trouvé seul·e = pas de révélation, et au moins une évaluation
        # sans marqueur d'indice
        items_resolved.append((item, last_user, not revealed))

    synthese = ped.build_synthese(db, session_id, items_resolved)

    return templates.TemplateResponse(
        request,
        "synthese.html",
        {
            "exercise": exo,
            "items_resolved": items_resolved,
            "synthese": synthese,
            "session_id": session_id,
        },
    )


__all__ = ["router"]

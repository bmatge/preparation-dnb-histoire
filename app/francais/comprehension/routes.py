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
    record_progress,
    update_session_step,
)
from app.francais.comprehension import pedagogy as ped
from app.francais.comprehension.loader import (
    get_exercise,
    list_annees,
    list_centres,
    list_for_home,
    list_sessions,
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


def _item_status(
    db: DBSession, session_id: int, step: int
) -> tuple[str, int]:
    """Calcule le statut final d'un item + le nombre d'indices utilisés.

    Les turns d'un item sont classés ainsi :
    - ``[reveal] …`` = révélation explicite demandée par l'élève
    - ``[indice-N] …`` = indice gradué (N ∈ {1,2,3})
    - tout autre turn assistant = verdict d'évaluation brut Albert

    Statuts retournés :
    - ``revealed`` : un reveal a été déclenché
    - ``correct_first_try`` : dernier verdict CORRECTE avec 0 indice
    - ``correct_with_hints`` : dernier verdict CORRECTE avec 1+ indice
    - ``in_progress`` : au moins une tentative, pas encore résolu
    - ``pending`` : aucune trace
    """
    turns = get_turns_by_step(db, session_id, step)
    if not turns:
        return "pending", 0

    has_reveal = any(
        t.role == "assistant" and t.content.startswith("[reveal]") for t in turns
    )
    if has_reveal:
        hints_used = sum(
            1 for t in turns if t.role == "assistant" and t.content.startswith("[indice-")
        )
        return "revealed", hints_used

    last_verdict: str | None = None
    for t in reversed(turns):
        if t.role != "assistant":
            continue
        if t.content.startswith("[indice-") or t.content.startswith("[reveal]"):
            continue
        verdict = ped.extract_verdict(t.content)
        if verdict:
            last_verdict = verdict
            break

    hints_used = sum(
        1 for t in turns if t.role == "assistant" and t.content.startswith("[indice-")
    )
    if last_verdict == "CORRECTE":
        return (
            "correct_first_try" if hints_used == 0 else "correct_with_hints"
        ), hints_used
    return "in_progress", hints_used


def _build_progress(
    db: DBSession,
    session_id: int,
    items: list[ExerciseItem],
    viewing_order: int,
    current_step: int,
) -> list[dict]:
    """Construit les pastilles de progression cliquables pour un parcours.

    ``viewing_order`` = l'item actuellement affiché (peut être un item
    historique en mode revoir). ``current_step`` = le point d'avancement
    réel de l'élève, qui sert à distinguer « en cours » (cliquable à
    venir) de « à venir » (pas encore atteint). Un item terminé (resolved)
    est toujours cliquable via la route ``/revoir/``.
    """
    progress: list[dict] = []
    for it in items:
        status, _ = _item_status(db, session_id, it.order)
        clickable = status in {"correct_first_try", "correct_with_hints", "revealed"}
        display_status = status
        if status in {"pending", "in_progress"}:
            display_status = "current" if it.order == current_step else "pending"
        progress.append(
            {
                "order": it.order,
                "label": it.label,
                "status": display_status,
                "clickable": clickable,
                "is_viewing": (it.order == viewing_order),
            }
        )
    return progress


# ============================================================================
# Accueil / création de session
# ============================================================================


@router.get("/", response_class=HTMLResponse)
def comprehension_home(
    request: Request,
    annee: str = "",
    centre: str = "",
    session_label: str = "",
    db: DBSession = Depends(db_session),
):
    """Accueil : liste d'annales avec filtres année / centre / session.

    Les filtres sont masqués côté template quand une seule valeur distincte
    existe en banque (pas la peine d'afficher un filtre « Centre : Métropole »
    solitaire). Tant qu'on n'a que des annales de session « juin », le filtre
    Session reste invisible — il réapparaîtra si on ajoute des sessions
    septembre ou des sujets zéro.
    """
    annee_int: int | None = None
    if annee:
        try:
            annee_int = int(annee)
        except ValueError:
            annee_int = None

    exercises = list_for_home(
        db,
        annee=annee_int,
        centre=centre or None,
        session_label=session_label or None,
    )

    available_annees = list_annees(db)
    available_centres = list_centres(db)
    available_sessions = list_sessions(db)

    current_filters = {
        "annee": annee or "",
        "centre": centre or "",
        "session_label": session_label or "",
    }

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "exercises": exercises,
            "total": len(exercises),
            "total_catalog": len(list_exercises(db)),
            "available_annees": available_annees,
            "available_centres": available_centres,
            "available_sessions": available_sessions,
            "current_filters": current_filters,
        },
    )


@router.post("/session/new")
def new_session(
    request: Request,
    exercise_id: int | None = Form(None),
    user_key: str = Form(default=""),
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

    sess = create_session(
        db,
        subject_kind=SUBJECT_KIND,
        subject_id=row.id,
        user_key=user_key or request.headers.get("x-user-key") or None,
    )
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
    progress = _build_progress(db, session_id, items, viewing_order=order, current_step=order)

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
            "progress": progress,
            "read_only": False,
            "read_only_entry": None,
        },
    )


@router.get("/session/{session_id}/item/{order}/revoir", response_class=HTMLResponse)
def show_item_revoir(
    session_id: int,
    order: int,
    request: Request,
    db: DBSession = Depends(db_session),
):
    """Affiche en lecture seule un item déjà traité.

    Ne modifie pas ``sess.current_step`` : l'élève peut revenir à son point
    d'avancement en cliquant sur la dernière pastille cliquable ou sur
    « Reprendre le parcours ».
    """
    sess, row, exo, items = _load_session_exo(db, session_id)
    item = _find_item(items, order)

    status, hints_used = _item_status(db, session_id, order)
    # Si l'item n'a pas encore été traité, rediriger vers la vue normale :
    # la vue read-only n'a pas de sens sans historique.
    if status in {"pending", "in_progress"}:
        return RedirectResponse(
            url=f"/francais/comprehension/session/{session_id}/item/{order}",
            status_code=303,
        )

    last_answer = _last_user_answer(db, session_id, order)
    image_url = _image_url_for(row.slug)
    progress = _build_progress(
        db, session_id, items, viewing_order=order, current_step=sess.current_step or 1
    )

    read_only_entry = {
        "status": status,
        "answer": last_answer,
        "hints_used": hints_used,
    }

    return templates.TemplateResponse(
        request,
        "exercise.html",
        {
            "session": sess,
            "exercise": exo,
            "items": items,
            "item": item,
            "turns": [],
            "hints_used": hints_used,
            "last_answer": last_answer,
            "total_items": len(items),
            "image_url": image_url,
            "progress": progress,
            "read_only": True,
            "read_only_entry": read_only_entry,
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
    user_key = request.headers.get("x-user-key")
    if user_key:
        record_progress(db, user_key, SUBJECT_KIND, f"{exo.id}:{item.label}", result.is_correct)

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

    # Pour chaque item, on reconstitue le statut final (3 états utiles :
    # correct du premier coup, correct avec indices, révélé) + la dernière
    # réponse de l'élève. Le tuple ``(item, reponse, trouve_seul)`` reste
    # accepté par ``ped.build_synthese`` pour le prompt Albert ; on passe
    # en plus une structure enrichie au template pour la récap.
    items_resolved: list[tuple[ExerciseItem, str, bool]] = []
    recap: list[dict] = []
    for item in items:
        status, hints_used = _item_status(db, session_id, item.order)
        last_user = _last_user_answer(db, session_id, item.order)
        recap.append(
            {
                "item": item,
                "status": status,
                "answer": last_user,
                "hints_used": hints_used,
            }
        )
        if status == "pending":
            # Item pas encore touché : on ne l'inclut pas dans items_resolved
            # pour ne pas fausser l'appel Albert, mais il apparaîtra dans
            # recap comme « non traité ».
            continue
        trouve_seul = status == "correct_first_try"
        items_resolved.append((item, last_user, trouve_seul))

    synthese = ped.build_synthese(db, session_id, items_resolved)

    return templates.TemplateResponse(
        request,
        "synthese.html",
        {
            "exercise": exo,
            "items_resolved": items_resolved,
            "recap": recap,
            "synthese": synthese,
            "session_id": session_id,
        },
    )


__all__ = ["router"]

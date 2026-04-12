"""
Routes FastAPI de l'épreuve « développement construit » (DNB histoire-géo-EMC).

Ce router est inclus par `app.histoire_geo_emc.routes` (router racine de la
matière) sous le préfixe `/developpement-construit`. Les URLs finales
commencent donc toutes par `/histoire-geo-emc/developpement-construit`.

  POST .../session/new      crée une session + redirect vers /step/1
  GET  .../restart          efface la session courante
  GET  .../                 accueil de l'épreuve (tirage de sujet)
  GET  .../step/1           affichage du sujet
  POST .../step/1/help      coup de pouce socratique
  GET  .../step/2           formulaire 1ʳᵉ proposition
  POST .../step/2/submit    → 1ʳᵉ évaluation (partial HTMX)
  GET  .../step/4           formulaire 2ᵉ proposition
  POST .../step/4/submit    → 2ᵉ évaluation (partial HTMX)
  GET  .../step/6           formulaire rédaction complète
  POST .../step/6/submit    → correction finale (partial HTMX)

Le MVP est en mode `SEMI_ASSISTE` uniquement.
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
from app.histoire_geo_emc.developpement_construit import models as hgemc_models
from app.histoire_geo_emc.developpement_construit.pedagogy import (
    run_step_1_help,
    run_step_3,
    run_step_5,
    run_step_7,
)
from app.histoire_geo_emc.developpement_construit.prompts import Mode

logger = logging.getLogger(__name__)

# ============================================================================
# Router + templates
# ============================================================================

# Préfixe complet de l'épreuve une fois montée par le router racine matière :
# `/histoire-geo-emc` (root matière) + `/developpement-construit` (cette épreuve).
PREFIX = "/histoire-geo-emc/developpement-construit"

# Ce router n'a pas de prefix propre : le prefix `/developpement-construit`
# est appliqué par `include_router` côté `app.histoire_geo_emc.routes`.
router = APIRouter(tags=["histoire-geo-emc / développement construit"])

# Templates : templates de l'épreuve DC en priorité, core/templates en
# fallback (notamment pour base.html dont tous les templates héritent).
# L'ordre compte : core/templates contient aussi un home.html qui est le
# sélecteur de matière — si on le mettait en premier, il shadowerait le
# home.html de l'épreuve et on tournerait en boucle sur le sélecteur.
_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_HGEMC_TEMPLATES = _HERE.parent / "templates"  # pour _hgemc_base.html + _tools_fab.html
_DC_TEMPLATES = _HERE / "templates"

templates = Jinja2Templates(
    directory=[str(_DC_TEMPLATES), str(_HGEMC_TEMPLATES), str(_CORE_TEMPLATES)]
)
templates.env.filters["eval_md"] = lambda txt: Markup(render_eval_markdown(txt or ""))


# ============================================================================
# Helpers
# ============================================================================


def _current_session_id(request: Request) -> int | None:
    sid = request.session.get("session_id")
    return int(sid) if sid is not None else None


def _require_session(request: Request, s: DBSession) -> core_db.Session:
    sid = _current_session_id(request)
    if sid is None:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    sess = core_db.get_session(s, sid)
    if sess is None:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return sess


def _subject_dict(subj: hgemc_models.Subject) -> dict:
    return {
        "id": subj.id,
        "consigne": subj.consigne,
        "discipline": subj.discipline,
        "theme": subj.theme,
        "year": subj.year,
        "session_label": subj.session_label,
        "verbe_cle": subj.verbe_cle,
        "bornes_chrono": subj.bornes_chrono,
        "bornes_spatiales": subj.bornes_spatiales,
        "notions_attendues": subj.notions_attendues,
        "bareme_points": subj.bareme_points,
    }


# ============================================================================
# Routes transverses à la matière
# ============================================================================


@router.get("/", response_class=HTMLResponse)
def hgemc_home(request: Request):
    """Accueil de la matière : formulaire de tirage de sujet."""
    return templates.TemplateResponse(request, "home.html")


@router.post("/session/new")
def session_new(
    request: Request,
    discipline: str = Form(default=""),
    source: str = Form(default="annales"),
    user_key: str = Form(default=""),
    s: DBSession = Depends(db_session),
):
    """Crée une session avec un sujet aléatoire.

    `source` vaut :
    - "annales"   → tirage dans les sujets réels extraits des PDF d'annales.
    - "variation" → tirage dans les variations générées offline par
                    scripts/generate_variations.py (Opus).
    """
    is_variation = source == "variation"
    subj = hgemc_models.random_subject(
        s,
        discipline=discipline or None,
        is_variation=is_variation,
    )
    if subj is None:
        # Pas de variation disponible → on redirige vers l'accueil matière avec
        # un message plutôt que de retomber silencieusement sur une annale,
        # sinon l'élève ne comprend pas ce qui s'est passé.
        err = "aucune_variation" if is_variation else "aucun_sujet"
        return RedirectResponse(url=f"{PREFIX}/?erreur={err}", status_code=303)
    new_sess = core_db.create_session(
        s,
        subject_kind="hgemc_dc",
        subject_id=subj.id,
        mode=Mode.SEMI_ASSISTE.value,
        user_key=user_key or request.headers.get("x-user-key") or None,
    )
    request.session["session_id"] = new_sess.id
    return RedirectResponse(url=f"{PREFIX}/step/1", status_code=303)


@router.get("/restart")
def restart(request: Request):
    request.session.pop("session_id", None)
    return RedirectResponse(url="/", status_code=303)


@router.get("/resume/{subject_id}/step/{step}")
def resume(
    request: Request,
    subject_id: int,
    step: int,
    s: DBSession = Depends(db_session),
):
    """Reprend une session pour un sujet et une étape précis.

    Cette route est appelée par le bandeau global de reprise du helper
    ``draft_autosave.js`` quand l'élève clique sur "Reprendre →" depuis
    l'accueil ou un sélecteur. Elle recrée une session DB pointant sur le
    bon sujet (indépendamment de ce qui était éventuellement déjà stocké
    dans le cookie Starlette) avant de rediriger vers l'étape demandée,
    ce qui garantit que ``step_{N}.html`` rend bien le sujet d'origine
    du brouillon localStorage.
    """
    if step not in (2, 4, 6):
        raise HTTPException(status_code=404, detail="Étape inconnue.")
    subj = hgemc_models.get_subject(s, subject_id)
    if subj is None:
        # Sujet supprimé depuis la sauvegarde du brouillon : on repart
        # proprement sur l'accueil matière plutôt que d'afficher une 404.
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)
    new_sess = core_db.create_session(
        s,
        subject_kind="hgemc_dc",
        subject_id=subj.id,
        mode=Mode.SEMI_ASSISTE.value,
        user_key=request.headers.get("x-user-key") or None,
    )
    request.session["session_id"] = new_sess.id
    return RedirectResponse(url=f"{PREFIX}/step/{step}", status_code=303)


# ---------------------------------------------------------------------------
# Étape 1 — affichage du sujet
# ---------------------------------------------------------------------------


@router.get("/step/1", response_class=HTMLResponse)
def step_1(request: Request, s: DBSession = Depends(db_session)):
    sess = _require_session(request, s)
    subj = hgemc_models.get_subject(s, sess.subject_id)
    core_db.update_session_step(s, sess.id, step=1)
    return templates.TemplateResponse(
        request,
        "step_1_subject.html",
        {"subject": _subject_dict(subj), "session_id": sess.id},
    )


@router.post("/step/1/help", response_class=HTMLResponse)
def step_1_help(request: Request, s: DBSession = Depends(db_session)):
    """Coup de pouce : questions ciblées pour décrypter le sujet.

    Répond en partial HTMX, à injecter dans #help-area de step_1_subject.html.
    """
    sess = _require_session(request, s)
    reply = run_step_1_help(s, sess.id)
    return templates.TemplateResponse(
        request,
        "_partials/help_response.html",
        {"content": reply},
    )


# ---------------------------------------------------------------------------
# Étape 2 → 3 : proposition v1, puis première évaluation
# ---------------------------------------------------------------------------


@router.get("/step/2", response_class=HTMLResponse)
def step_2(request: Request, s: DBSession = Depends(db_session)):
    sess = _require_session(request, s)
    subj = hgemc_models.get_subject(s, sess.subject_id)
    core_db.update_session_step(s, sess.id, step=2)
    return templates.TemplateResponse(
        request,
        "step_2_proposal.html",
        {"subject": _subject_dict(subj), "session_id": sess.id},
    )


@router.post("/step/2/submit", response_class=HTMLResponse)
def step_2_submit(
    request: Request,
    proposition: str = Form(...),
    s: DBSession = Depends(db_session),
):
    sess = _require_session(request, s)
    proposition = (proposition or "").strip()
    if len(proposition) < 20:
        return templates.TemplateResponse(
            request,
            "_partials/error.html",
            {
                "message": "Écris au moins quelques phrases pour ta proposition (essaie d'expliquer ton plan et tes idées principales).",
            },
        )

    reply = run_step_3(s, sess.id, first_proposal=proposition, mode=Mode.SEMI_ASSISTE)
    return templates.TemplateResponse(
        request,
        "_partials/eval_response.html",
        {
            "title": "Première évaluation",
            "content": reply,
            "next_url": f"{PREFIX}/step/4",
            "next_label": "Je retravaille ma proposition",
        },
    )


# ---------------------------------------------------------------------------
# Étape 4 → 5 : proposition v2, puis seconde évaluation
# ---------------------------------------------------------------------------


@router.get("/step/4", response_class=HTMLResponse)
def step_4(request: Request, s: DBSession = Depends(db_session)):
    sess = _require_session(request, s)
    subj = hgemc_models.get_subject(s, sess.subject_id)
    first = core_db.get_last_user_turn(s, sess.id, step=2)
    core_db.update_session_step(s, sess.id, step=4)
    return templates.TemplateResponse(
        request,
        "step_4_reproposal.html",
        {
            "subject": _subject_dict(subj),
            "previous_proposal": first.content if first else "",
            "session_id": sess.id,
        },
    )


@router.post("/step/4/submit", response_class=HTMLResponse)
def step_4_submit(
    request: Request,
    proposition: str = Form(...),
    s: DBSession = Depends(db_session),
):
    sess = _require_session(request, s)
    proposition = (proposition or "").strip()
    if len(proposition) < 20:
        return templates.TemplateResponse(
            request,
            "_partials/error.html",
            {
                "message": "Ta nouvelle proposition est un peu courte — étoffe-la avant que je puisse t'aider à voir tes progrès.",
            },
        )

    reply = run_step_5(s, sess.id, second_proposal=proposition, mode=Mode.SEMI_ASSISTE)
    return templates.TemplateResponse(
        request,
        "_partials/eval_response.html",
        {
            "title": "Seconde évaluation",
            "content": reply,
            "next_url": f"{PREFIX}/step/6",
            "next_label": "Je passe à la rédaction complète",
        },
    )


# ---------------------------------------------------------------------------
# Étape 6 → 7 : rédaction complète, puis correction finale
# ---------------------------------------------------------------------------


@router.get("/step/6", response_class=HTMLResponse)
def step_6(request: Request, s: DBSession = Depends(db_session)):
    sess = _require_session(request, s)
    subj = hgemc_models.get_subject(s, sess.subject_id)
    second = core_db.get_last_user_turn(s, sess.id, step=4)
    core_db.update_session_step(s, sess.id, step=6)
    return templates.TemplateResponse(
        request,
        "step_6_writing.html",
        {
            "subject": _subject_dict(subj),
            "previous_proposal": second.content if second else "",
            "session_id": sess.id,
        },
    )


@router.post("/step/6/submit", response_class=HTMLResponse)
def step_6_submit(
    request: Request,
    redaction: str = Form(...),
    s: DBSession = Depends(db_session),
):
    sess = _require_session(request, s)
    redaction = (redaction or "").strip()
    if len(redaction) < 200:
        return templates.TemplateResponse(
            request,
            "_partials/error.html",
            {
                "message": "Un développement construit fait au moins une quinzaine de lignes — continue à rédiger avant de me l'envoyer.",
            },
        )

    reply = run_step_7(s, sess.id, student_text=redaction, mode=Mode.SEMI_ASSISTE)
    return templates.TemplateResponse(
        request,
        "_partials/eval_response.html",
        {
            "title": "Correction finale",
            "content": reply,
            "next_url": f"{PREFIX}/restart",
            "next_label": "Recommencer avec un autre sujet",
        },
    )


__all__ = ["router", "PREFIX"]

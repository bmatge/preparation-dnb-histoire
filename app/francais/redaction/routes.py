"""Routes FastAPI de la sous-épreuve « Rédaction » (français).

Parcours élève (mode SEMI_ASSISTÉ, MVP) — 7 étapes :

  GET  /                              accueil de l'épreuve (tirage de sujet)
  POST /session/new                   crée une session + redirect vers /step/1
  GET  /restart                       efface la session courante
  GET  /step/1                        affichage des deux options + choix
  POST /step/1/help                   coup de pouce socratique (HTMX partial)
  POST /step/1/choose                 enregistre l'option choisie + redirect /step/2
  GET  /step/2                        formulaire brouillon / plan (1ʳᵉ proposition)
  POST /step/2/submit                 → 1ʳᵉ évaluation (HTMX partial)
  GET  /step/4                        formulaire 2ᵉ proposition
  POST /step/4/submit                 → 2ᵉ évaluation (HTMX partial)
  GET  /step/6                        formulaire rédaction complète
  POST /step/6/submit                 → correction finale (HTMX partial)

L'option choisie (`imagination` | `reflexion`) est stockée dans
``request.session["redaction_option"]`` après l'étape 1, pour être
réutilisée dans les étapes suivantes sans alourdir la table `Session`.
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
from app.francais.redaction.loader import (
    get_subject,
    list_subjects,
    pick_subject,
)
from app.francais.redaction.models import SUBJECT_KIND, FrenchRedactionSubject
from app.francais.redaction.pedagogy import (
    run_step_1_help,
    run_step_3,
    run_step_5,
    run_step_7,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Router + templates
# ============================================================================

# Préfixe complet une fois monté par le router racine matière :
# `/francais` (root matière) + `/redaction` (cette épreuve).
PREFIX = "/francais/redaction"

router = APIRouter(tags=["francais-redaction"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_FR_TEMPLATES = _HERE.parent / "templates"  # pour _francais_base.html + _tools_fab.html
_REDAC_TEMPLATES = _HERE / "templates"

templates = Jinja2Templates(
    directory=[str(_REDAC_TEMPLATES), str(_FR_TEMPLATES), str(_CORE_TEMPLATES)]
)
templates.env.filters["eval_md"] = lambda txt: Markup(render_eval_markdown(txt or ""))


# ============================================================================
# Helpers
# ============================================================================


def _current_session_id(request: Request) -> int | None:
    sid = request.session.get("redaction_session_id")
    return int(sid) if sid is not None else None


def _require_session(request: Request, s: DBSession) -> core_db.Session:
    sid = _current_session_id(request)
    if sid is None:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})
    sess = core_db.get_session(s, sid)
    if sess is None or sess.subject_kind != SUBJECT_KIND:
        raise HTTPException(status_code=303, headers={"Location": f"{PREFIX}/"})
    return sess


def _require_subject(
    s: DBSession, sess: core_db.Session
) -> FrenchRedactionSubject:
    if sess.subject_id is None:
        raise HTTPException(status_code=500, detail="Session sans sujet associé.")
    row = get_subject(s, sess.subject_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Sujet introuvable.")
    return row


def _subject_view(row: FrenchRedactionSubject) -> dict:
    """Vue dict prête à l'emploi pour les templates Jinja."""
    payload = row.load()
    return {
        "id": row.id,
        "annee": row.annee,
        "centre": row.centre,
        "texte_support_ref": payload.texte_support_ref,
        "imagination": payload.sujet_imagination.model_dump(),
        "reflexion": payload.sujet_reflexion.model_dump(),
    }


def _require_option(request: Request) -> str:
    opt = request.session.get("redaction_option")
    if opt not in ("imagination", "reflexion"):
        raise HTTPException(
            status_code=303, headers={"Location": f"{PREFIX}/step/1"}
        )
    return opt


# ============================================================================
# Routes transverses à l'épreuve
# ============================================================================


@router.get("/", response_class=HTMLResponse)
def redaction_home(request: Request, db: DBSession = Depends(db_session)):
    subjects = list_subjects(db)
    return templates.TemplateResponse(
        request,
        "home.html",
        {"subjects": subjects},
    )


@router.post("/session/new")
def session_new(
    request: Request,
    annee: str = Form(default=""),
    s: DBSession = Depends(db_session),
):
    """Crée une session avec un sujet de rédaction tiré au hasard.

    `annee` est optionnel : si fourni, on filtre sur cette année avant tirage.
    """
    annee_int: int | None = None
    if annee:
        try:
            annee_int = int(annee)
        except ValueError:
            annee_int = None

    row = pick_subject(s, annee=annee_int)
    if row is None:
        return RedirectResponse(
            url=f"{PREFIX}/?erreur=aucun_sujet", status_code=303
        )
    new_sess = core_db.create_session(
        s,
        subject_kind=SUBJECT_KIND,
        subject_id=row.id,
        mode="semi_assiste",
    )
    request.session["redaction_session_id"] = new_sess.id
    request.session.pop("redaction_option", None)
    return RedirectResponse(url=f"{PREFIX}/step/1", status_code=303)


@router.get("/restart")
def restart(request: Request):
    request.session.pop("redaction_session_id", None)
    request.session.pop("redaction_option", None)
    return RedirectResponse(url=f"{PREFIX}/", status_code=303)


@router.get("/resume/{subject_id}/step/{step}")
def resume(
    request: Request,
    subject_id: int,
    step: int,
    option: str,
    s: DBSession = Depends(db_session),
):
    """Reprend une session pour un sujet, une option et une étape précis.

    Appelée par le bandeau global de reprise du helper
    ``draft_autosave.js``. Recrée une session DB pointant sur le bon
    sujet et restaure l'option (imagination/réflexion) dans le cookie
    Starlette — sans quoi ``_require_option`` redirigerait l'élève vers
    l'étape 1 et lui ferait choisir à nouveau son option, ce qui
    changerait la clé localStorage et perdrait le brouillon.
    """
    if step not in (2, 4, 6):
        raise HTTPException(status_code=404, detail="Étape inconnue.")
    if option not in ("imagination", "reflexion"):
        raise HTTPException(status_code=400, detail="Option invalide.")
    row = get_subject(s, subject_id)
    if row is None:
        return RedirectResponse(url=f"{PREFIX}/", status_code=303)
    new_sess = core_db.create_session(
        s,
        subject_kind=SUBJECT_KIND,
        subject_id=row.id,
        mode="semi_assiste",
    )
    request.session["redaction_session_id"] = new_sess.id
    request.session["redaction_option"] = option
    return RedirectResponse(url=f"{PREFIX}/step/{step}", status_code=303)


# ---------------------------------------------------------------------------
# Étape 1 — affichage des deux options + choix
# ---------------------------------------------------------------------------


@router.get("/step/1", response_class=HTMLResponse)
def step_1(request: Request, s: DBSession = Depends(db_session)):
    sess = _require_session(request, s)
    row = _require_subject(s, sess)
    core_db.update_session_step(s, sess.id, step=1)
    return templates.TemplateResponse(
        request,
        "step_1_subject.html",
        {"subject": _subject_view(row), "session_id": sess.id},
    )


@router.post("/step/1/help", response_class=HTMLResponse)
def step_1_help(request: Request, s: DBSession = Depends(db_session)):
    """Coup de pouce socratique pour aider l'élève à choisir."""
    sess = _require_session(request, s)
    reply = run_step_1_help(s, sess.id)
    return templates.TemplateResponse(
        request,
        "_partials/help_response.html",
        {"content": reply},
    )


@router.post("/step/1/choose")
def step_1_choose(
    request: Request,
    option: str = Form(...),
    s: DBSession = Depends(db_session),
):
    """Enregistre l'option choisie en session cookie + redirige vers étape 2."""
    sess = _require_session(request, s)
    if option not in ("imagination", "reflexion"):
        raise HTTPException(status_code=400, detail="Option invalide.")
    request.session["redaction_option"] = option
    core_db.update_session_step(s, sess.id, step=2)
    return RedirectResponse(url=f"{PREFIX}/step/2", status_code=303)


# ---------------------------------------------------------------------------
# Étape 2 → 3 : brouillon, puis première évaluation
# ---------------------------------------------------------------------------


@router.get("/step/2", response_class=HTMLResponse)
def step_2(request: Request, s: DBSession = Depends(db_session)):
    sess = _require_session(request, s)
    row = _require_subject(s, sess)
    option = _require_option(request)
    core_db.update_session_step(s, sess.id, step=2)
    return templates.TemplateResponse(
        request,
        "step_2_proposal.html",
        {
            "subject": _subject_view(row),
            "option": option,
            "session_id": sess.id,
        },
    )


@router.post("/step/2/submit", response_class=HTMLResponse)
def step_2_submit(
    request: Request,
    proposition: str = Form(...),
    s: DBSession = Depends(db_session),
):
    sess = _require_session(request, s)
    option = _require_option(request)
    proposition = (proposition or "").strip()
    if len(proposition) < 30:
        return templates.TemplateResponse(
            request,
            "_partials/error.html",
            {
                "message": (
                    "Étoffe ton brouillon avant que je puisse t'aider — "
                    "écris au moins ton plan et les grandes idées que tu "
                    "comptes développer."
                ),
            },
        )

    reply = run_step_3(
        s, sess.id, option_choisie=option, first_proposal=proposition
    )
    return templates.TemplateResponse(
        request,
        "_partials/eval_response.html",
        {
            "title": "Première évaluation",
            "content": reply,
            "next_url": f"{PREFIX}/step/4",
            "next_label": "Je retravaille mon brouillon",
        },
    )


# ---------------------------------------------------------------------------
# Étape 4 → 5 : seconde proposition, puis seconde évaluation
# ---------------------------------------------------------------------------


@router.get("/step/4", response_class=HTMLResponse)
def step_4(request: Request, s: DBSession = Depends(db_session)):
    sess = _require_session(request, s)
    row = _require_subject(s, sess)
    option = _require_option(request)
    first = core_db.get_last_user_turn(s, sess.id, step=2)
    core_db.update_session_step(s, sess.id, step=4)
    return templates.TemplateResponse(
        request,
        "step_4_reproposal.html",
        {
            "subject": _subject_view(row),
            "option": option,
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
    option = _require_option(request)
    proposition = (proposition or "").strip()
    if len(proposition) < 30:
        return templates.TemplateResponse(
            request,
            "_partials/error.html",
            {
                "message": (
                    "Ta nouvelle version est encore un peu courte — étoffe-la "
                    "avant que je puisse voir tes progrès."
                ),
            },
        )

    reply = run_step_5(
        s, sess.id, option_choisie=option, second_proposal=proposition
    )
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
    row = _require_subject(s, sess)
    option = _require_option(request)
    second = core_db.get_last_user_turn(s, sess.id, step=4)
    core_db.update_session_step(s, sess.id, step=6)
    return templates.TemplateResponse(
        request,
        "step_6_writing.html",
        {
            "subject": _subject_view(row),
            "option": option,
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
    option = _require_option(request)
    redaction = (redaction or "").strip()
    # Une rédaction DNB fait ~30-50 lignes, soit au moins ~600 caractères
    # une fois mis bout à bout. On reste tolérant en bas de fourchette
    # (~400) pour ne pas bloquer un brouillon final un peu court.
    if len(redaction) < 400:
        return templates.TemplateResponse(
            request,
            "_partials/error.html",
            {
                "message": (
                    "Une rédaction DNB fait au moins une trentaine de "
                    "lignes — continue à écrire avant de me l'envoyer pour "
                    "correction."
                ),
            },
        )

    reply = run_step_7(
        s, sess.id, option_choisie=option, student_text=redaction
    )
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

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
from app.core.db import db_session, get_last_user_turn, get_turns_by_step
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
# Helpers de progression (barre cliquable + synthèse)
# ============================================================================

# Labels courts affichés sous chaque segment de la barre de progression.
_STEP_LABELS: dict[int, str] = {
    1: "Sujet",
    2: "Proposition v1",
    3: "Éval 1",
    4: "Proposition v2",
    5: "Éval 2",
    6: "Rédaction",
    7: "Correction",
}

# Routes éditables par l'élève. Les étapes 3/5 sont des sorties Albert,
# visibles dans l'étape paire qui les a produites. L'étape 7 pointe vers
# la synthèse si elle existe.
_STEP_HREFS: dict[int, str] = {
    1: f"{PREFIX}/step/1",
    2: f"{PREFIX}/step/2",
    4: f"{PREFIX}/step/4",
    6: f"{PREFIX}/step/6",
    7: f"{PREFIX}/step/synthese",
}


def _step_is_done(s: DBSession, session_id: int, step: int) -> bool:
    """Vrai si l'étape a produit une trace DB (réponse élève ou sortie Albert)."""
    if step in (2, 4, 6):
        return get_last_user_turn(s, session_id, step=step) is not None
    if step in (3, 5, 7):
        turns = get_turns_by_step(s, session_id, step)
        return any(t.role == "assistant" for t in turns)
    return False


def _progress_state(
    s: DBSession, session_id: int, current_step: int
) -> list[dict]:
    """Construit l'état de chaque segment de la barre de progression.

    Une étape est cliquable quand elle est éditable (1/2/4/6) et déjà
    atteinte, ou quand c'est l'étape 7 et qu'une correction finale existe
    — auquel cas elle mène à la synthèse du parcours.
    """
    state: list[dict] = []
    for n in range(1, 8):
        is_done = _step_is_done(s, session_id, n) or n < current_step
        is_current = n == current_step
        status = "done" if is_done else ("current" if is_current else "todo")
        clickable = n in _STEP_HREFS and (is_done or is_current) and not (
            n == 7 and not _step_is_done(s, session_id, 7)
        )
        state.append(
            {
                "num": n,
                "label": _STEP_LABELS.get(n, ""),
                "status": status,
                "clickable": clickable,
                "href": _STEP_HREFS.get(n, ""),
                "is_current": is_current,
            }
        )
    return state


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
    if (sess.current_step or 0) < 1:
        core_db.update_session_step(s, sess.id, step=1)
    progress = _progress_state(s, sess.id, current_step=sess.current_step or 1)
    return templates.TemplateResponse(
        request,
        "step_1_subject.html",
        {
            "subject": _subject_dict(subj),
            "session_id": sess.id,
            "progress": progress,
        },
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
    # Pre-fill : si l'élève revient à l'étape 2 via la barre après être
    # passé à l'étape 4, on lui ré-affiche sa proposition v1 au lieu d'un
    # textarea vide.
    previous = get_last_user_turn(s, sess.id, step=2)
    if (sess.current_step or 0) < 2:
        core_db.update_session_step(s, sess.id, step=2)
    progress = _progress_state(s, sess.id, current_step=sess.current_step or 2)
    return templates.TemplateResponse(
        request,
        "step_2_proposal.html",
        {
            "subject": _subject_dict(subj),
            "session_id": sess.id,
            "previous_proposal": previous.content if previous else "",
            "progress": progress,
        },
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
    # Pre-fill : privilégier la proposition v2 déjà soumise à la v1 quand
    # l'élève revient sur l'étape 4 après être passé à la rédaction.
    existing_v2 = core_db.get_last_user_turn(s, sess.id, step=4)
    if existing_v2:
        previous = existing_v2.content
    else:
        first = core_db.get_last_user_turn(s, sess.id, step=2)
        previous = first.content if first else ""
    if (sess.current_step or 0) < 4:
        core_db.update_session_step(s, sess.id, step=4)
    progress = _progress_state(s, sess.id, current_step=sess.current_step or 4)
    return templates.TemplateResponse(
        request,
        "step_4_reproposal.html",
        {
            "subject": _subject_dict(subj),
            "previous_proposal": previous,
            "session_id": sess.id,
            "progress": progress,
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
    # Pre-fill : si la rédaction finale a déjà été soumise, on l'affiche ;
    # sinon on propose la proposition v2 comme canvas.
    existing_final = core_db.get_last_user_turn(s, sess.id, step=6)
    if existing_final:
        previous = existing_final.content
    else:
        second = core_db.get_last_user_turn(s, sess.id, step=4)
        previous = second.content if second else ""
    if (sess.current_step or 0) < 6:
        core_db.update_session_step(s, sess.id, step=6)
    progress = _progress_state(s, sess.id, current_step=sess.current_step or 6)
    return templates.TemplateResponse(
        request,
        "step_6_writing.html",
        {
            "subject": _subject_dict(subj),
            "previous_proposal": previous,
            "session_id": sess.id,
            "progress": progress,
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
            "next_url": f"{PREFIX}/step/synthese",
            "next_label": "Voir le bilan de mon parcours",
        },
    )


# ---------------------------------------------------------------------------
# Synthèse — récap du parcours complet (proposition v1 → correction finale)
# ---------------------------------------------------------------------------


@router.get("/step/synthese", response_class=HTMLResponse)
def step_synthese(request: Request, s: DBSession = Depends(db_session)):
    """Affiche le parcours complet : sujet, les 3 propositions élève et
    les 3 retours Albert, dans l'ordre chronologique.

    Accessible par la barre de progression quand l'étape 7 est faite, ou
    automatiquement après la correction finale. Les moments encore vides
    sont affichés en grisé pour que l'élève voie ce qui reste à faire
    quand il arrive ici prématurément via la barre.
    """
    sess = _require_session(request, s)
    subj = hgemc_models.get_subject(s, sess.subject_id)

    def _last_user(step: int) -> str:
        turn = get_last_user_turn(s, sess.id, step=step)
        return turn.content if turn else ""

    def _last_assistant(step: int) -> str:
        turns = get_turns_by_step(s, sess.id, step)
        for t in reversed(turns):
            if t.role == "assistant":
                return t.content
        return ""

    moments = [
        {
            "title": "Première proposition",
            "kind": "user",
            "step": 2,
            "content": _last_user(2),
            "edit_href": f"{PREFIX}/step/2",
        },
        {
            "title": "Première évaluation",
            "kind": "assistant",
            "step": 3,
            "content": _last_assistant(3),
            "edit_href": None,
        },
        {
            "title": "Seconde proposition",
            "kind": "user",
            "step": 4,
            "content": _last_user(4),
            "edit_href": f"{PREFIX}/step/4",
        },
        {
            "title": "Seconde évaluation",
            "kind": "assistant",
            "step": 5,
            "content": _last_assistant(5),
            "edit_href": None,
        },
        {
            "title": "Développement construit complet",
            "kind": "user",
            "step": 6,
            "content": _last_user(6),
            "edit_href": f"{PREFIX}/step/6",
        },
        {
            "title": "Correction finale",
            "kind": "assistant",
            "step": 7,
            "content": _last_assistant(7),
            "edit_href": None,
        },
    ]

    progress = _progress_state(s, sess.id, current_step=sess.current_step or 7)
    return templates.TemplateResponse(
        request,
        "synthese.html",
        {
            "subject": _subject_dict(subj),
            "session_id": sess.id,
            "moments": moments,
            "progress": progress,
        },
    )


__all__ = ["router", "PREFIX"]

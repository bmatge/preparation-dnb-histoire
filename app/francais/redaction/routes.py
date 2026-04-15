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
from app.core.db import db_session, get_last_user_turn, get_turns_by_step
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
# Helpers de progression (barre cliquable + synthèse)
# ============================================================================

# Labels courts affichés sous chaque segment de la barre de progression.
_STEP_LABELS: dict[int, str] = {
    1: "Choix",
    2: "Brouillon v1",
    3: "Éval 1",
    4: "Brouillon v2",
    5: "Éval 2",
    6: "Rédaction",
    7: "Correction",
}

# Routes éditables par l'élève : les étapes paires (formulaires) + l'étape 1
# (choix d'option) et l'étape 7 (synthèse). Les étapes 3/5 sont des sorties
# Albert intercalées, visibles dans l'étape paire qui les a déclenchées.
_STEP_HREFS: dict[int, str] = {
    1: f"{PREFIX}/step/1",
    2: f"{PREFIX}/step/2",
    4: f"{PREFIX}/step/4",
    6: f"{PREFIX}/step/6",
    7: f"{PREFIX}/step/synthese",
}


def _step_is_done(s: DBSession, session_id: int, step: int) -> bool:
    """Une étape est « done » quand elle a produit une trace DB.

    Pour une étape paire (2/4/6), on regarde la dernière réponse élève à ce
    step. Pour une étape impaire (3/5/7), on regarde la trace assistant.
    L'étape 1 (choix) est done dès qu'on a choisi une option ; on considère
    qu'elle est done quand ``current_step >= 2``.
    """
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

    L'élève peut cliquer sur toute étape éditable déjà atteinte (steps 1,
    2, 4, 6) ou sur l'étape 7 si une correction finale a été produite (la
    route synthese consolide alors tout le parcours).
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
    user_key: str = Form(default=""),
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
        user_key=user_key or request.headers.get("x-user-key") or None,
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
        user_key=request.headers.get("x-user-key") or None,
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
    # L'étape 1 ne touche pas current_step si l'élève a déjà avancé ;
    # elle le repositionne sur 1 seulement si la session n'a pas encore
    # démarré, pour ne pas écraser la progression quand on revient en
    # arrière via la barre.
    if (sess.current_step or 0) < 1:
        core_db.update_session_step(s, sess.id, step=1)
    progress = _progress_state(s, sess.id, current_step=sess.current_step or 1)
    return templates.TemplateResponse(
        request,
        "step_1_subject.html",
        {
            "subject": _subject_view(row),
            "session_id": sess.id,
            "progress": progress,
        },
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
    # Ne pas écraser current_step si l'élève est déjà passé à 4 ou 6 et
    # revient éditer son brouillon v1 via la barre de progression.
    if (sess.current_step or 0) < 2:
        core_db.update_session_step(s, sess.id, step=2)
    # Pre-fill : si l'élève revient en arrière pour retravailler sa
    # proposition 1, on lui ré-affiche son texte au lieu d'un textarea vide.
    previous = get_last_user_turn(s, sess.id, step=2)
    progress = _progress_state(s, sess.id, current_step=sess.current_step or 2)
    return templates.TemplateResponse(
        request,
        "step_2_proposal.html",
        {
            "subject": _subject_view(row),
            "option": option,
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
    # Pre-fill : si l'élève revient à l'étape 4 après être passé à l'étape 6,
    # on lui ré-affiche son brouillon v2 plutôt que son v1, pour ne pas
    # écraser le travail en cours.
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
            "subject": _subject_view(row),
            "option": option,
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
    # Pre-fill : si l'élève a déjà soumis sa rédaction finale et revient
    # via la barre, on ré-affiche sa rédaction ; sinon on part du brouillon
    # v2 comme canvas initial.
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
            "subject": _subject_view(row),
            "option": option,
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
            "next_url": f"{PREFIX}/step/synthese",
            "next_label": "Voir le bilan de mon parcours",
        },
    )


# ---------------------------------------------------------------------------
# Synthèse — récap du parcours complet (1ʳᵉ proposition → correction finale)
# ---------------------------------------------------------------------------


@router.get("/step/synthese", response_class=HTMLResponse)
def step_synthese(request: Request, s: DBSession = Depends(db_session)):
    """Affiche le parcours complet : les 3 propositions élève et les 3
    retours Albert, dans l'ordre chronologique.

    Accessible par la barre de progression ou automatiquement après la
    correction finale. Si un des moments manque (p. ex. l'élève arrive ici
    avant d'avoir fini), on l'affiche comme « à venir » plutôt que de
    rediriger — l'élève voit clairement ce qui lui reste à faire.
    """
    sess = _require_session(request, s)
    row = _require_subject(s, sess)
    option = _require_option(request)

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
            "title": "Premier brouillon",
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
            "title": "Deuxième brouillon",
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
            "title": "Rédaction complète",
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
            "subject": _subject_view(row),
            "option": option,
            "session_id": sess.id,
            "moments": moments,
            "progress": progress,
        },
    )


__all__ = ["router", "PREFIX"]

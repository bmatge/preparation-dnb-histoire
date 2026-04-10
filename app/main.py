"""
Application FastAPI — point d'entrée du serveur.

Architecture (volontairement basique) :
- Templates Jinja2 + HTMX pour les formulaires (pas de build JS).
- Tailwind CDN pour le style (pas de bundler).
- Session côté client via cookie signé (Starlette SessionMiddleware) qui ne
  contient qu'un `session_id` pointant vers la table `Session` en base.
- Persistance SQLite locale (data/app.db).

Parcours élève (7 étapes du HANDOFF) :

  /                         page d'accueil
  /session/new              POST → crée la session + redirige vers /step/1
  /step/1                   tirage du sujet
  /step/2                   GET formulaire 1ʳᵉ proposition
  /step/2/submit            POST → étape 3 (1ʳᵉ éval) renvoyée en partial
  /step/4                   GET formulaire 2ᵉ proposition
  /step/4/submit            POST → étape 5 (2ᵉ éval) en partial
  /step/6                   GET formulaire rédaction complète
  /step/6/submit            POST → étape 7 (correction finale) en partial
  /restart                  efface la session courante et revient à l'accueil

Le MVP est en mode `SEMI_ASSISTE` uniquement (cf HANDOFF §2).
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

# Charge .env automatiquement avant tout import qui lit les vars d'env
# (notamment app.albert_client / app.rag qui exigent ALBERT_API_KEY).
# En Docker, les vars sont déjà injectées via env_file → load_dotenv est no-op.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlmodel import Session as DBSession
from starlette.middleware.sessions import SessionMiddleware

from app.formatting import render_eval_markdown

from app import db
from app.db import db_session
from app.pedagogy import run_step_3, run_step_5, run_step_7
from app.prompts import Mode

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


# ============================================================================
# Création de l'app
# ============================================================================

app = FastAPI(title="revise-ton-dnb", docs_url=None, redoc_url=None)

# Clé de signature du cookie de session. En prod, on peut la passer via env.
_session_secret = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    same_site="lax",
    https_only=False,  # le TLS est terminé en amont par Traefik
    max_age=60 * 60 * 24 * 7,  # 1 semaine
)

# Static (vide pour l'instant mais on monte quand même)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["eval_md"] = lambda txt: Markup(render_eval_markdown(txt or ""))


@app.on_event("startup")
def on_startup() -> None:
    db.init_db()
    logger.info("DB prête (%s)", db.DB_PATH)


# ============================================================================
# Helpers
# ============================================================================


def _current_session_id(request: Request) -> int | None:
    sid = request.session.get("session_id")
    return int(sid) if sid is not None else None


def _require_session(request: Request, s: DBSession) -> db.Session:
    sid = _current_session_id(request)
    if sid is None:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    sess = db.get_session(s, sid)
    if sess is None:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return sess


def _subject_dict(subj: db.Subject) -> dict:
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
# Routes
# ============================================================================


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "home.html")


@app.post("/session/new")
def session_new(
    request: Request,
    discipline: str = Form(default=""),
    s: DBSession = Depends(db_session),
):
    """Crée une session avec un sujet aléatoire (filtrable par discipline)."""
    subj = db.random_subject(s, discipline=discipline or None)
    if subj is None:
        return RedirectResponse(url="/?erreur=aucun_sujet", status_code=303)
    new_sess = db.create_session(s, subject_id=subj.id, mode=Mode.SEMI_ASSISTE.value)
    request.session["session_id"] = new_sess.id
    return RedirectResponse(url="/step/1", status_code=303)


@app.get("/restart")
def restart(request: Request):
    request.session.pop("session_id", None)
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# Étape 1 — affichage du sujet
# ---------------------------------------------------------------------------


@app.get("/step/1", response_class=HTMLResponse)
def step_1(request: Request, s: DBSession = Depends(db_session)):
    sess = _require_session(request, s)
    subj = db.get_subject(s, sess.subject_id)
    db.update_session_step(s, sess.id, step=1)
    return templates.TemplateResponse(
        request,
        "step_1_subject.html",
        {"subject": _subject_dict(subj)},
    )


# ---------------------------------------------------------------------------
# Étape 2 → 3 : proposition v1, puis première évaluation
# ---------------------------------------------------------------------------


@app.get("/step/2", response_class=HTMLResponse)
def step_2(request: Request, s: DBSession = Depends(db_session)):
    sess = _require_session(request, s)
    subj = db.get_subject(s, sess.subject_id)
    db.update_session_step(s, sess.id, step=2)
    return templates.TemplateResponse(
        request,
        "step_2_proposal.html",
        {"subject": _subject_dict(subj)},
    )


@app.post("/step/2/submit", response_class=HTMLResponse)
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
            "next_url": "/step/4",
            "next_label": "Je retravaille ma proposition",
        },
    )


# ---------------------------------------------------------------------------
# Étape 4 → 5 : proposition v2, puis seconde évaluation
# ---------------------------------------------------------------------------


@app.get("/step/4", response_class=HTMLResponse)
def step_4(request: Request, s: DBSession = Depends(db_session)):
    sess = _require_session(request, s)
    subj = db.get_subject(s, sess.subject_id)
    first = db.get_last_user_turn(s, sess.id, step=2)
    db.update_session_step(s, sess.id, step=4)
    return templates.TemplateResponse(
        request,
        "step_4_reproposal.html",
        {
            "subject": _subject_dict(subj),
            "previous_proposal": first.content if first else "",
        },
    )


@app.post("/step/4/submit", response_class=HTMLResponse)
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
            "next_url": "/step/6",
            "next_label": "Je passe à la rédaction complète",
        },
    )


# ---------------------------------------------------------------------------
# Étape 6 → 7 : rédaction complète, puis correction finale
# ---------------------------------------------------------------------------


@app.get("/step/6", response_class=HTMLResponse)
def step_6(request: Request, s: DBSession = Depends(db_session)):
    sess = _require_session(request, s)
    subj = db.get_subject(s, sess.subject_id)
    second = db.get_last_user_turn(s, sess.id, step=4)
    db.update_session_step(s, sess.id, step=6)
    return templates.TemplateResponse(
        request,
        "step_6_writing.html",
        {
            "subject": _subject_dict(subj),
            "previous_proposal": second.content if second else "",
        },
    )


@app.post("/step/6/submit", response_class=HTMLResponse)
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
            "next_url": "/restart",
            "next_label": "Recommencer avec un autre sujet",
        },
    )


# ---------------------------------------------------------------------------
# Health check (Traefik / smoke tests)
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz():
    return {"ok": True}

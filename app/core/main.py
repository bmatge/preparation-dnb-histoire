"""
Application FastAPI — point d'entrée racine.

Plateforme multi-matières : ce module ne contient que ce qui est transverse
(accueil avec sélecteur de matière, health check, middleware de session,
statiques). L'intégralité d'un parcours élève vit dans les sous-modules de
matière (ex. `app/histoire_geo_emc/routes.py`) qui sont montés ici comme
routers FastAPI.

Routes exposées directement par ce module :

  GET  /                         sélecteur de matière
  GET  /healthz                  smoke check pour Traefik
  GET  /step/{rest}              redirect 307 → /histoire-geo-emc/step/{rest}
  POST /step/{rest}              redirect 307 (préserve corps POST + cookies)
  POST /session/new              redirect 307 → /histoire-geo-emc/session/new
  GET  /restart                  redirect 303 → /histoire-geo-emc/restart

Tout le reste est sous `/<matière>/...`.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

# Charge .env automatiquement avant tout import qui lit les vars d'env
# (notamment app.core.albert_client / app.core.rag qui exigent ALBERT_API_KEY).
# `override=True` : le .env est la source de vérité — si le shell a déjà une
# vieille valeur (ex: un ancien `source .env` avec une clé tronquée), on la
# remplace plutôt que de la garder. En Docker, les vars sont injectées par
# env_file avant le démarrage du process → le .env local n'existe pas, et
# load_dotenv est un no-op silencieux.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=True)

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.core import db as core_db
from app.francais.comprehension.loader import init_french_comprehension
from app.francais.redaction.loader import init_french_redaction
from app.francais.routes import router as francais_router
from app.histoire_geo_emc.developpement_construit.models import init_hgemc_subjects
from app.histoire_geo_emc.reperes.models import init_reperes
from app.histoire_geo_emc.routes import router as hgemc_router, PREFIX as HGEMC_PREFIX

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


APP_DIR = Path(__file__).resolve().parent.parent  # = app/
REPO_ROOT = APP_DIR.parent
CORE_TEMPLATES = APP_DIR / "core" / "templates"
STATIC_DIR = APP_DIR / "static"
FRANCAIS_IMAGES_DIR = REPO_ROOT / "content" / "francais" / "comprehension" / "images"


# ============================================================================
# Création de l'app
# ============================================================================

app = FastAPI(title="revise-ton-dnb", docs_url=None, redoc_url=None)

# Clé de signature du cookie de session. En prod, on peut la passer via env.
# IMPORTANT : conserver la même valeur entre redémarrages pour ne pas
# invalider les sessions élèves en cours.
_session_secret = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    same_site="lax",
    https_only=False,  # le TLS est terminé en amont par Traefik
    max_age=60 * 60 * 24 * 7,  # 1 semaine
)

# Static (contient les vendors tailwind/htmx).
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Illustrations français compréhension servies directement depuis content/.
if FRANCAIS_IMAGES_DIR.exists():
    app.mount(
        "/francais-images",
        StaticFiles(directory=str(FRANCAIS_IMAGES_DIR)),
        name="francais-images",
    )

# Templates core : uniquement pour l'accueil global et base.html.
templates = Jinja2Templates(directory=str(CORE_TEMPLATES))


# ============================================================================
# Sous-routers (une entrée par matière)
# ============================================================================

app.include_router(hgemc_router)
app.include_router(francais_router)


# ============================================================================
# Startup : init DB + chargement des contenus métier
# ============================================================================


@app.on_event("startup")
def on_startup() -> None:
    core_db.init_db()
    n_hgemc = init_hgemc_subjects()
    n_reperes = init_reperes()
    n_francais = init_french_comprehension()
    n_redaction = init_french_redaction()
    logger.info(
        "DB prête (%s) — %d sujets DC, %d repères, %d exos français compréhension, %d sujets rédaction chargés",
        core_db.DB_PATH,
        n_hgemc,
        n_reperes,
        n_francais,
        n_redaction,
    )


# ============================================================================
# Routes transverses
# ============================================================================


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Accueil global : sélecteur de matière."""
    return templates.TemplateResponse(request, "home.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Redirects de compat : les anciennes URLs (/step/N, /session/new, /restart)
# sont redirigées vers /histoire-geo-emc/*. Utilise 307 pour préserver le
# verbe HTTP et le corps des POST (les formulaires en vol restent valides).
# ---------------------------------------------------------------------------


@app.api_route("/session/new", methods=["GET", "POST"])
def legacy_session_new():
    return RedirectResponse(url=f"{HGEMC_PREFIX}/session/new", status_code=307)


@app.get("/restart")
def legacy_restart():
    return RedirectResponse(url=f"{HGEMC_PREFIX}/restart", status_code=303)


@app.api_route("/step/{rest:path}", methods=["GET", "POST"])
def legacy_step(rest: str):
    return RedirectResponse(url=f"{HGEMC_PREFIX}/step/{rest}", status_code=307)

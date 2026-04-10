"""
Router racine de la matière histoire-géographie-EMC.

Ce router est monté par `app.core.main` sous le préfixe `/histoire-geo-emc`.
Son rôle est double :

1. Exposer une **page d'index matière** qui liste les épreuves disponibles
   (aujourd'hui : développement construit + repères).

2. **Inclure les sous-routers** d'épreuve :
   - `developpement-construit/*` → `app.histoire_geo_emc.developpement_construit.routes`
   - `reperes/*`                 → `app.histoire_geo_emc.reperes.routes`

3. Maintenir la **compatibilité des anciennes URLs** mono-épreuve (avant le
   refacto par épreuve) en redirigeant :
   - `GET|POST /histoire-geo-emc/step/{rest}` → `/histoire-geo-emc/developpement-construit/step/{rest}` (301)
   - `POST /histoire-geo-emc/session/new`     → `/histoire-geo-emc/developpement-construit/session/new` (307, préserve le corps)
   - `GET  /histoire-geo-emc/restart`         → `/histoire-geo-emc/developpement-construit/restart` (303)

Ces redirects évitent de casser les sessions élèves en cours : le cookie
Starlette reste valide, seule l'URL change.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.histoire_geo_emc.developpement_construit.routes import router as dc_router
from app.histoire_geo_emc.reperes.routes import router as reperes_router

logger = logging.getLogger(__name__)

# ============================================================================
# Router + templates matière
# ============================================================================

PREFIX = "/histoire-geo-emc"

router = APIRouter(prefix=PREFIX, tags=["histoire-geo-emc"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_HGEMC_TEMPLATES = _HERE / "templates"

# Templates matière : index.html en priorité, core en fallback pour base.html.
templates = Jinja2Templates(directory=[str(_HGEMC_TEMPLATES), str(_CORE_TEMPLATES)])


# ============================================================================
# Index matière
# ============================================================================


@router.get("/", response_class=HTMLResponse)
def hgemc_index(request: Request):
    """Page d'index de la matière : liste des épreuves disponibles."""
    return templates.TemplateResponse(request, "index.html")


# ============================================================================
# Redirects de compat (anciennes URLs DC mono-épreuve → nouvelles)
# ============================================================================


@router.api_route("/step/{rest:path}", methods=["GET", "POST"])
def _legacy_step(rest: str):
    """Redirige les anciennes URLs `/histoire-geo-emc/step/{n}` vers le DC.

    307 pour préserver le verbe HTTP et le corps de requête (utile si un
    élève a un formulaire en vol au moment du déploiement).
    """
    return RedirectResponse(
        url=f"{PREFIX}/developpement-construit/step/{rest}",
        status_code=307,
    )


@router.post("/session/new")
def _legacy_session_new():
    return RedirectResponse(
        url=f"{PREFIX}/developpement-construit/session/new",
        status_code=307,
    )


@router.get("/restart")
def _legacy_restart():
    return RedirectResponse(
        url=f"{PREFIX}/developpement-construit/restart",
        status_code=303,
    )


# ============================================================================
# Inclusion des sous-routers d'épreuve
# ============================================================================

router.include_router(dc_router, prefix="/developpement-construit")
router.include_router(reperes_router, prefix="/reperes")


__all__ = ["router", "PREFIX"]

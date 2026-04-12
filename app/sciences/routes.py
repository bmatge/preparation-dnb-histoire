"""Router racine de la matière Sciences.

Monté par `app.core.main` sous le préfixe `/sciences`. Expose la page
d'index matière (3 cartes discipline : physique-chimie, SVT, technologie)
et inclut le sous-router de l'épreuve `revision`, qui gère l'intégralité
du parcours « quiz par thème » pour les trois disciplines via un champ
`discipline` dans l'URL.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.sciences.revision.routes import router as revision_router
from app.sciences.simulation.routes import router as simulation_router

logger = logging.getLogger(__name__)

PREFIX = "/sciences"

router = APIRouter(prefix=PREFIX, tags=["sciences"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_SCIENCES_TEMPLATES = _HERE / "templates"

templates = Jinja2Templates(
    directory=[str(_SCIENCES_TEMPLATES), str(_CORE_TEMPLATES)]
)


@router.get("/", response_class=HTMLResponse)
def sciences_index(request: Request):
    """Page d'index matière : liste des disciplines disponibles."""
    return templates.TemplateResponse(request, "index.html")


router.include_router(revision_router, prefix="/revision")
router.include_router(simulation_router, prefix="/simulation")


__all__ = ["router", "PREFIX"]

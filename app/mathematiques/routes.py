"""Router racine de la matière mathématiques.

Monté par `app.core.main` sous le préfixe `/mathematiques`. Expose la
page d'index matière et inclut les sous-routers d'épreuve : vague 1 les
automatismes (Partie 1 du DNB), vague 2 la sous-épreuve « raisonnement
et résolution de problèmes » (Partie 2).
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.mathematiques.automatismes.routes import router as automatismes_router
from app.mathematiques.problemes.routes import router as problemes_router

logger = logging.getLogger(__name__)

PREFIX = "/mathematiques"

router = APIRouter(prefix=PREFIX, tags=["mathematiques"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_MATH_TEMPLATES = _HERE / "templates"

templates = Jinja2Templates(directory=[str(_MATH_TEMPLATES), str(_CORE_TEMPLATES)])


@router.get("/", response_class=HTMLResponse)
def math_index(request: Request):
    """Page d'index matière : liste des épreuves disponibles."""
    return templates.TemplateResponse(request, "index.html")


router.include_router(automatismes_router, prefix="/automatismes")
router.include_router(problemes_router, prefix="/problemes")


__all__ = ["router", "PREFIX"]

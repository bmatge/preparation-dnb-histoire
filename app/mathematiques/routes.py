"""Router racine de la matière mathématiques.

Monté par `app.core.main` sous le préfixe `/mathematiques`. Expose la
page d'index matière et inclut les sous-routers d'épreuve. Vague 1 :
seule l'épreuve « Automatismes » est active.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.mathematiques.automatismes.routes import router as automatismes_router

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


__all__ = ["router", "PREFIX"]

"""Router racine de la matière français.

Monté par `app.core.main` sous le préfixe `/francais`. Rôle :

1. Exposer une **page d'index matière** qui liste les sous-épreuves
   disponibles (aujourd'hui : compréhension/interprétation uniquement).

2. **Inclure le sous-router** de chaque sous-épreuve :
   - `comprehension/*` → `app.francais.comprehension.routes`

Pas de redirections de compat : le français est une nouvelle matière, il
n'a pas d'historique d'URL à préserver.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.francais.comprehension.routes import router as comprehension_router

logger = logging.getLogger(__name__)

PREFIX = "/francais"

router = APIRouter(prefix=PREFIX, tags=["francais"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_FR_TEMPLATES = _HERE / "templates"

templates = Jinja2Templates(directory=[str(_FR_TEMPLATES), str(_CORE_TEMPLATES)])


@router.get("/", response_class=HTMLResponse)
def francais_index(request: Request):
    """Page d'index de la matière : liste des sous-épreuves."""
    return templates.TemplateResponse(request, "index.html")


router.include_router(comprehension_router, prefix="/comprehension")

__all__ = ["router", "PREFIX"]

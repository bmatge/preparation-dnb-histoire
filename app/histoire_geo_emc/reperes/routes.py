"""
Routes de l'épreuve « Repères chronologiques et spatiaux ».

Stub minimal : le vrai parcours sera implémenté dans un commit dédié pour
isoler le refacto (commit a) du contenu fonctionnel nouveau (commit d).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

router = APIRouter(tags=["histoire-geo-emc / repères"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_REPERES_TEMPLATES = _HERE / "templates"

templates = Jinja2Templates(directory=[str(_REPERES_TEMPLATES), str(_CORE_TEMPLATES)])


@router.get("/", response_class=HTMLResponse)
def reperes_index(request: Request):
    """Page d'accueil de l'épreuve repères (placeholder refacto)."""
    return templates.TemplateResponse(request, "index.html")


__all__ = ["router"]

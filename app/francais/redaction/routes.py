"""Routes FastAPI de la sous-épreuve « Rédaction » (français).

MVP : ce router n'expose pour l'instant qu'une page d'accueil qui liste les
sujets disponibles. Le parcours pédagogique complet (7 étapes : choix du
sujet, brouillon, eval1, repropo, eval2, rédaction finale, correction)
sera ajouté dans une itération suivante (cf. issue #6).
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session as DBSession

from app.core.db import db_session
from app.francais.redaction.loader import list_subjects

logger = logging.getLogger(__name__)

router = APIRouter(tags=["francais-redaction"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_REDAC_TEMPLATES = _HERE / "templates"

templates = Jinja2Templates(directory=[str(_REDAC_TEMPLATES), str(_CORE_TEMPLATES)])


@router.get("/", response_class=HTMLResponse)
def redaction_home(request: Request, db: DBSession = Depends(db_session)):
    subjects = list_subjects(db)
    return templates.TemplateResponse(
        request,
        "home.html",
        {"subjects": subjects},
    )


__all__ = ["router"]

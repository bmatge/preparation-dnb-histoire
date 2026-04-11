"""Routes FastAPI de la sous-épreuve « Dictée » (français).

Parcours élève (MVP) :

    GET  /                              accueil, liste des dictées + sélection voix
    POST /session/new                   crée une session, redirige vers /session/{id}
    GET  /session/{sid}                 écran de travail (lecteur audio + textarea)
    POST /session/{sid}/answer          évalue la copie, renvoie le partial feedback
    GET  /session/{sid}/synthese        bilan final
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session as DBSession

from app.core.db import (
    add_turn,
    create_session,
    db_session,
    get_session,
    get_turns,
    update_session_step,
)
from app.francais.dictee import pedagogy
from app.francais.dictee.loader import (
    AUDIO_DIR,
    get_dictee,
    list_dictees,
    pick_dictee,
)
from app.francais.dictee.models import SUBJECT_KIND, FrenchDictee

logger = logging.getLogger(__name__)

router = APIRouter(tags=["francais-dictee"])

_HERE = Path(__file__).resolve().parent
_APP_DIR = _HERE.parent.parent
_CORE_TEMPLATES = _APP_DIR / "core" / "templates"
_FR_TEMPLATES = _HERE.parent / "templates"  # pour _francais_base.html + _tools_fab.html
_DICTEE_TEMPLATES = _HERE / "templates"

templates = Jinja2Templates(
    directory=[str(_DICTEE_TEMPLATES), str(_FR_TEMPLATES), str(_CORE_TEMPLATES)]
)

# Voix disponibles côté UI. Le slug doit matcher le nom du dossier sous
# `content/francais/dictee/audio/<slug>/`. Synchronisé avec
# `scripts/generate_dictee_audio.py:VOICES`.
AVAILABLE_VOICES: list[tuple[str, str]] = [
    ("damien", "Damien (voix masculine)"),
    ("tammie", "Tammie (voix féminine)"),
]
DEFAULT_VOICE = "damien"


# ============================================================================
# Helpers
# ============================================================================


def _load_session_dictee(db: DBSession, session_id: int):
    sess = get_session(db, session_id)
    if sess is None or sess.subject_kind != SUBJECT_KIND:
        raise HTTPException(status_code=404, detail="Session introuvable.")
    if sess.subject_id is None:
        raise HTTPException(status_code=500, detail="Session sans dictée associée.")
    row = get_dictee(db, sess.subject_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Dictée introuvable.")
    return sess, row, row.load()


def _audio_phrases_for(slug: str, voice: str, n_phrases: int) -> list[str]:
    """URLs publiques des MP3 phrase par phrase. Vide si voix inconnue."""
    if voice not in {v[0] for v in AVAILABLE_VOICES}:
        return []
    voice_dir = AUDIO_DIR / voice / slug
    if not voice_dir.exists():
        return []
    return [
        f"/francais-dictees-audio/{voice}/{slug}/phrase_{i:02d}.mp3"
        for i in range(1, n_phrases + 1)
    ]


def _last_eleve_answer(db: DBSession, session_id: int) -> str:
    turns = get_turns(db, session_id)
    for t in reversed(turns):
        if t.role == "user":
            return t.content
    return ""


# ============================================================================
# Accueil / création de session
# ============================================================================


@router.get("/", response_class=HTMLResponse)
def dictee_home(request: Request, db: DBSession = Depends(db_session)):
    dictees = list_dictees(db)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "dictees": dictees,
            "total": len(dictees),
            "voices": AVAILABLE_VOICES,
            "default_voice": DEFAULT_VOICE,
        },
    )


@router.post("/session/new")
def new_session(
    request: Request,
    dictee_id: int | None = Form(None),
    voice: str = Form(DEFAULT_VOICE),
    db: DBSession = Depends(db_session),
):
    if dictee_id is not None:
        row = get_dictee(db, dictee_id)
    else:
        row = pick_dictee(db)

    if row is None:
        raise HTTPException(
            status_code=500,
            detail="Aucune dictée disponible. Vérifie le chargement du catalogue.",
        )

    if voice not in {v[0] for v in AVAILABLE_VOICES}:
        voice = DEFAULT_VOICE

    sess = create_session(db, subject_kind=SUBJECT_KIND, subject_id=row.id)
    update_session_step(db, sess.id, 1)
    request.session["dictee_voice"] = voice
    return RedirectResponse(
        url=f"/francais/dictee/session/{sess.id}",
        status_code=303,
    )


# ============================================================================
# Écran de travail
# ============================================================================


@router.get("/session/{session_id}", response_class=HTMLResponse)
def show_session(
    session_id: int,
    request: Request,
    db: DBSession = Depends(db_session),
):
    sess, row, dictee = _load_session_dictee(db, session_id)
    voice = request.session.get("dictee_voice", DEFAULT_VOICE)
    if voice not in {v[0] for v in AVAILABLE_VOICES}:
        voice = DEFAULT_VOICE

    audio_urls = _audio_phrases_for(row.slug, voice, len(dictee.phrases))
    last_answer = _last_eleve_answer(db, session_id)

    return templates.TemplateResponse(
        request,
        "exercise.html",
        {
            "session": sess,
            "row": row,
            "dictee": dictee,
            "audio_urls": audio_urls,
            "voices": AVAILABLE_VOICES,
            "current_voice": voice,
            "last_answer": last_answer,
        },
    )


@router.post(
    "/session/{session_id}/voice", response_class=HTMLResponse
)
def switch_voice(
    session_id: int,
    request: Request,
    voice: str = Form(...),
    db: DBSession = Depends(db_session),
):
    """Change la voix en cours de session sans repartir de zéro."""
    _load_session_dictee(db, session_id)  # juste valider l'existence
    if voice not in {v[0] for v in AVAILABLE_VOICES}:
        voice = DEFAULT_VOICE
    request.session["dictee_voice"] = voice
    return RedirectResponse(
        url=f"/francais/dictee/session/{session_id}",
        status_code=303,
    )


@router.post(
    "/session/{session_id}/answer", response_class=HTMLResponse
)
def submit_answer(
    session_id: int,
    request: Request,
    copie: str = Form(...),
    db: DBSession = Depends(db_session),
):
    sess, row, dictee = _load_session_dictee(db, session_id)
    add_turn(db, session_id, 1, "user", copie)
    result = pedagogy.evaluate(dictee.texte_complet, copie)
    add_turn(
        db,
        session_id,
        1,
        "assistant",
        f"NOTE={result.note_sur_10}/10 FAUTES={result.nb_fautes}",
    )
    return templates.TemplateResponse(
        request,
        "_partials/feedback.html",
        {
            "session_id": session_id,
            "result": result,
            "dictee": dictee,
        },
    )


@router.get("/session/{session_id}/synthese", response_class=HTMLResponse)
def synthese(
    session_id: int,
    request: Request,
    db: DBSession = Depends(db_session),
):
    sess, row, dictee = _load_session_dictee(db, session_id)
    eleve = _last_eleve_answer(db, session_id)
    result = (
        pedagogy.evaluate(dictee.texte_complet, eleve) if eleve else None
    )
    return templates.TemplateResponse(
        request,
        "synthese.html",
        {
            "session": sess,
            "dictee": dictee,
            "result": result,
        },
    )


__all__ = ["router"]

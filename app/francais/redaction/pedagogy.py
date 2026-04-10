"""Orchestration des étapes du parcours « Rédaction » français.

Glue entre :

- les prompts (``app/francais/redaction/prompts.py``)
- le client Albert avec routage modèle + post-filtres
  (``app/core/albert_client.py``)
- le client RAG (``app/core/rag.py``)
- la persistance (``app/core/db.py`` + ``app/francais/redaction/loader.py``)

Chaque ``run_step_*`` :

1. Charge la session + le sujet + l'option choisie.
2. Construit la requête RAG adaptée à l'étape.
3. Construit le prompt via ``prompts.py``.
4. Appelle Albert via ``AlbertClient``.
5. Sauve l'input élève et la réponse Albert dans la DB.
6. Renvoie le texte généré (ou un message d'erreur gracieux).

Toutes les erreurs Albert (ghostwriting détecté, citations manquantes,
réseau…) sont attrapées ici et converties en messages français lisibles
pour l'élève. L'app n'expose JAMAIS de stack trace.
"""

from __future__ import annotations

import logging

from sqlmodel import Session as DBSession

from app.core.albert_client import (
    AlbertClient,
    AlbertError,
    GhostwritingDetected,
    MissingCitations,
    Task,
)
from app.core.db import (
    add_turn,
    get_last_user_turn,
    get_session,
    update_session_step,
)
from app.core.rag import get_default_rag_client
from app.francais.redaction.loader import get_subject
from app.francais.redaction.models import (
    SUBJECT_KIND,
    FrenchRedactionSubject,
    RedactionSubject,
    SujetOption,
)
from app.francais.redaction.prompts import (
    RedactionContext,
    build_final_correction_redaction,
    build_first_eval_redaction,
    build_help_choose,
    build_second_eval_redaction,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Singleton client Albert
# ============================================================================

_albert_client: AlbertClient | None = None


def get_albert_client() -> AlbertClient:
    global _albert_client
    if _albert_client is None:
        _albert_client = AlbertClient()
    return _albert_client


# ============================================================================
# Helpers
# ============================================================================


GENERIC_ERROR_MSG = (
    "Désolé, j'ai eu un petit souci pour te répondre. Réessaie dans quelques "
    "secondes — si ça recommence, préviens ton·ta prof."
)


def _load_session_subject(
    s: DBSession, session_id: int
) -> tuple[FrenchRedactionSubject, RedactionSubject] | None:
    sess = get_session(s, session_id)
    if sess is None or sess.subject_id is None:
        return None
    row = get_subject(s, sess.subject_id)
    if row is None:
        return None
    return row, row.load()


def _option_from_choice(
    payload: RedactionSubject, choice: str
) -> SujetOption | None:
    if choice == "imagination":
        return payload.sujet_imagination
    if choice == "reflexion":
        return payload.sujet_reflexion
    return None


def _build_context(
    row: FrenchRedactionSubject, payload: RedactionSubject, choice: str
) -> RedactionContext | None:
    opt = _option_from_choice(payload, choice)
    if opt is None:
        return None
    return RedactionContext(
        annee=row.annee,
        centre=row.centre,
        option=opt,
        texte_support_ref=payload.texte_support_ref,
    )


def _build_rag_query(
    payload: RedactionSubject,
    option_type: str | None = None,
    student_text: str | None = None,
) -> str:
    """Construit une requête RAG compacte pour la collection français.

    On combine : type de sujet (imagination/réflexion), centre + année,
    consigne tronquée, et le début du texte élève si fourni. Albert se
    charge de la tokenisation côté serveur.
    """
    parts: list[str] = []
    if option_type == "imagination":
        opt = payload.sujet_imagination
        parts.append("rédaction sujet d'imagination")
    elif option_type == "reflexion":
        opt = payload.sujet_reflexion
        parts.append("rédaction sujet de réflexion")
    else:
        opt = None
        parts.append("rédaction DNB français")

    parts.append(f"DNB {payload.source.annee} {payload.source.centre}")
    if opt is not None:
        parts.append(opt.consigne[:300])
    if student_text:
        parts.append(student_text[:400])
    return " — ".join(p for p in parts if p)


def _safe_chat(task: Task, messages: list[dict]) -> str:
    try:
        result = get_albert_client().chat(task, messages)
        return result.content.strip()
    except GhostwritingDetected:
        logger.warning("Ghostwriting détecté pour task=%s", task)
        return (
            "Je préfère ne pas te donner ça tel quel — j'ai bien envie de "
            "rédiger à ta place et ce n'est pas mon rôle. Reformule ta "
            "demande, ou retravaille ton texte et redemande-moi."
        )
    except MissingCitations:
        logger.warning("Citations absentes après retry pour task=%s", task)
        return (
            "Je n'ai pas réussi à m'appuyer assez clairement sur la "
            "méthodologie pour te répondre. Réessaie — quitte à étoffer un "
            "peu ce que tu m'as envoyé."
        )
    except AlbertError as e:
        logger.error("Erreur Albert task=%s : %s", task, e)
        return GENERIC_ERROR_MSG
    except Exception as e:  # noqa: BLE001
        logger.exception("Erreur inattendue task=%s : %s", task, e)
        return GENERIC_ERROR_MSG


# ============================================================================
# Étape 1 — Aide au choix entre les deux options
# ============================================================================


def run_step_1_help(s: DBSession, session_id: int) -> str:
    loaded = _load_session_subject(s, session_id)
    if loaded is None:
        return GENERIC_ERROR_MSG
    _row, payload = loaded

    rag = get_default_rag_client().search_for_task(
        SUBJECT_KIND,
        Task.FR_REDACTION_HELP,
        query=_build_rag_query(payload),
        limit=4,
    )
    messages = build_help_choose(subject=payload, rag=rag)
    reply = _safe_chat(Task.FR_REDACTION_HELP, messages)

    add_turn(s, session_id, step=1, role="assistant", content=reply)
    return reply


# ============================================================================
# Étape 3 — Première évaluation du brouillon
# ============================================================================


def run_step_3(
    s: DBSession,
    session_id: int,
    option_choisie: str,
    first_proposal: str,
) -> str:
    loaded = _load_session_subject(s, session_id)
    if loaded is None:
        return GENERIC_ERROR_MSG
    row, payload = loaded
    ctx = _build_context(row, payload, option_choisie)
    if ctx is None:
        return GENERIC_ERROR_MSG

    add_turn(s, session_id, step=2, role="user", content=first_proposal)

    rag = get_default_rag_client().search_for_task(
        SUBJECT_KIND,
        Task.FR_REDACTION_FIRST_EVAL,
        query=_build_rag_query(payload, option_choisie, first_proposal),
        limit=5,
    )
    messages = build_first_eval_redaction(
        ctx=ctx,
        student_proposal=first_proposal,
        rag=rag,
    )
    reply = _safe_chat(Task.FR_REDACTION_FIRST_EVAL, messages)

    add_turn(s, session_id, step=3, role="assistant", content=reply)
    update_session_step(s, session_id, step=4)
    return reply


# ============================================================================
# Étape 5 — Seconde évaluation
# ============================================================================


def run_step_5(
    s: DBSession,
    session_id: int,
    option_choisie: str,
    second_proposal: str,
) -> str:
    loaded = _load_session_subject(s, session_id)
    if loaded is None:
        return GENERIC_ERROR_MSG
    row, payload = loaded
    ctx = _build_context(row, payload, option_choisie)
    if ctx is None:
        return GENERIC_ERROR_MSG

    first = get_last_user_turn(s, session_id, step=2)
    first_text = first.content if first else ""

    add_turn(s, session_id, step=4, role="user", content=second_proposal)

    rag = get_default_rag_client().search_for_task(
        SUBJECT_KIND,
        Task.FR_REDACTION_SECOND_EVAL,
        query=_build_rag_query(payload, option_choisie, second_proposal),
        limit=5,
    )
    messages = build_second_eval_redaction(
        ctx=ctx,
        first_proposal=first_text,
        second_proposal=second_proposal,
        rag=rag,
    )
    reply = _safe_chat(Task.FR_REDACTION_SECOND_EVAL, messages)

    add_turn(s, session_id, step=5, role="assistant", content=reply)
    update_session_step(s, session_id, step=6)
    return reply


# ============================================================================
# Étape 7 — Correction finale
# ============================================================================


def run_step_7(
    s: DBSession,
    session_id: int,
    option_choisie: str,
    student_text: str,
) -> str:
    loaded = _load_session_subject(s, session_id)
    if loaded is None:
        return GENERIC_ERROR_MSG
    row, payload = loaded
    ctx = _build_context(row, payload, option_choisie)
    if ctx is None:
        return GENERIC_ERROR_MSG

    add_turn(s, session_id, step=6, role="user", content=student_text)

    rag = get_default_rag_client().search_for_task(
        SUBJECT_KIND,
        Task.FR_REDACTION_FINAL_CORRECTION,
        query=_build_rag_query(payload, option_choisie, student_text),
        limit=6,
    )
    messages = build_final_correction_redaction(
        ctx=ctx,
        student_text=student_text,
        rag=rag,
    )
    reply = _safe_chat(Task.FR_REDACTION_FINAL_CORRECTION, messages)

    add_turn(s, session_id, step=7, role="assistant", content=reply)
    update_session_step(s, session_id, step=7)
    return reply


__all__ = [
    "run_step_1_help",
    "run_step_3",
    "run_step_5",
    "run_step_7",
    "get_albert_client",
]

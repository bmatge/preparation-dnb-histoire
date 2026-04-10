"""
Orchestration des étapes du parcours élève.

Ce module est la "glue" entre :
- les prompts pédagogiques (`app/prompts.py`)
- le client Albert avec routage modèle + post-filtres (`app/albert_client.py`)
- le client RAG (`app/rag.py`)
- la persistance SQLite (`app/db.py`)

Chaque fonction `run_step_*` :
1. Charge le sujet et l'historique de la session.
2. Construit une requête RAG adaptée à l'étape.
3. Construit le prompt via prompts.py.
4. Appelle Albert via albert_client.
5. Sauve l'input élève et la réponse Albert dans la DB.
6. Renvoie le texte généré (ou un message d'erreur gracieux).

Toutes les erreurs Albert (ghostwriting détecté, citations manquantes,
réseau…) sont attrapées ici et converties en messages français lisibles
pour l'élève. L'app ne doit JAMAIS exposer une stack trace.
"""

from __future__ import annotations

import logging

from sqlmodel import Session as DBSession

from app.albert_client import (
    AlbertClient,
    AlbertError,
    GhostwritingDetected,
    MissingCitations,
    Task,
)
from app.db import (
    Subject,
    add_turn,
    get_last_user_turn,
    get_session,
    get_subject,
    update_session_step,
)
from app.prompts import (
    Mode,
    SubjectContext,
    build_final_correction,
    build_first_eval,
    build_second_eval,
)
from app.rag import AlbertRagClient, get_default_rag_client

logger = logging.getLogger(__name__)


# ============================================================================
# Singleton client Albert (chat) — partagé par tous les appels
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


def _subject_to_context(subj: Subject) -> SubjectContext:
    """Convertit un Subject (DB) en SubjectContext (prompts)."""
    return SubjectContext(
        consigne=subj.consigne,
        discipline=subj.discipline,
        theme=subj.theme,
        annee=subj.year,
        verbe_cle=subj.verbe_cle,
        bornes_chrono=subj.bornes_chrono,
        bornes_spatiales=subj.bornes_spatiales,
        notions_attendues=subj.notions_attendues,
    )


def _build_rag_query(subj: Subject, student_text: str | None = None) -> str:
    """Construit une requête RAG pertinente à partir du sujet + (optionnel) texte élève.

    On reste compact : Albert fait sa propre tokenisation, pas besoin d'envoyer
    des tartines. Le thème + les notions attendues + un extrait du texte élève
    suffisent à ramener des passages pertinents.
    """
    parts: list[str] = [subj.theme, subj.consigne]
    if subj.notions_attendues:
        parts.append(" ".join(subj.notions_attendues))
    if student_text:
        # On garde le début pour ne pas saturer la query.
        parts.append(student_text[:400])
    return " — ".join(p for p in parts if p)


def _safe_chat(task: Task, messages: list[dict]) -> str:
    """Wrappe l'appel Albert et convertit les erreurs en message gracieux."""
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
            "Je n'ai pas réussi à m'appuyer assez clairement sur le programme "
            "officiel pour répondre. Réessaie : pose ta question autrement ou "
            "ajoute un peu plus de contenu."
        )
    except AlbertError as e:
        logger.error("Erreur Albert task=%s : %s", task, e)
        return GENERIC_ERROR_MSG
    except Exception as e:  # noqa: BLE001
        logger.exception("Erreur inattendue task=%s : %s", task, e)
        return GENERIC_ERROR_MSG


# ============================================================================
# Étape 3 — première évaluation
# ============================================================================


def run_step_3(
    s: DBSession,
    session_id: int,
    first_proposal: str,
    mode: Mode = Mode.SEMI_ASSISTE,
) -> str:
    """Évalue la première proposition de l'élève. Renvoie le texte d'Albert."""
    sess = get_session(s, session_id)
    if sess is None:
        return GENERIC_ERROR_MSG
    subj = get_subject(s, sess.subject_id)
    if subj is None:
        return GENERIC_ERROR_MSG

    add_turn(s, session_id, step=2, role="user", content=first_proposal)

    rag = get_default_rag_client().search_for_task(
        Task.FIRST_EVAL,
        query=_build_rag_query(subj, first_proposal),
        limit=5,
    )
    messages = build_first_eval(
        subject=_subject_to_context(subj),
        student_proposal=first_proposal,
        rag=rag,
        mode=mode,
    )
    reply = _safe_chat(Task.FIRST_EVAL, messages)

    add_turn(s, session_id, step=3, role="assistant", content=reply)
    update_session_step(s, session_id, step=4)
    return reply


# ============================================================================
# Étape 5 — seconde évaluation
# ============================================================================


def run_step_5(
    s: DBSession,
    session_id: int,
    second_proposal: str,
    mode: Mode = Mode.SEMI_ASSISTE,
) -> str:
    sess = get_session(s, session_id)
    if sess is None:
        return GENERIC_ERROR_MSG
    subj = get_subject(s, sess.subject_id)
    if subj is None:
        return GENERIC_ERROR_MSG

    first = get_last_user_turn(s, session_id, step=2)
    first_text = first.content if first else ""

    add_turn(s, session_id, step=4, role="user", content=second_proposal)

    rag = get_default_rag_client().search_for_task(
        Task.SECOND_EVAL,
        query=_build_rag_query(subj, second_proposal),
        limit=5,
    )
    messages = build_second_eval(
        subject=_subject_to_context(subj),
        first_proposal=first_text,
        second_proposal=second_proposal,
        rag=rag,
        mode=mode,
    )
    reply = _safe_chat(Task.SECOND_EVAL, messages)

    add_turn(s, session_id, step=5, role="assistant", content=reply)
    update_session_step(s, session_id, step=6)
    return reply


# ============================================================================
# Étape 7 — correction finale
# ============================================================================


def run_step_7(
    s: DBSession,
    session_id: int,
    student_text: str,
    mode: Mode = Mode.SEMI_ASSISTE,
) -> str:
    sess = get_session(s, session_id)
    if sess is None:
        return GENERIC_ERROR_MSG
    subj = get_subject(s, sess.subject_id)
    if subj is None:
        return GENERIC_ERROR_MSG

    add_turn(s, session_id, step=6, role="user", content=student_text)

    rag = get_default_rag_client().search_for_task(
        Task.FINAL_CORRECTION,
        query=_build_rag_query(subj, student_text),
        limit=6,
    )
    messages = build_final_correction(
        subject=_subject_to_context(subj),
        student_text=student_text,
        rag=rag,
        mode=mode,
    )
    reply = _safe_chat(Task.FINAL_CORRECTION, messages)

    add_turn(s, session_id, step=7, role="assistant", content=reply)
    update_session_step(s, session_id, step=7)
    return reply


__all__ = [
    "run_step_3",
    "run_step_5",
    "run_step_7",
    "get_albert_client",
]

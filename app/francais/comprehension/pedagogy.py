"""Orchestration du parcours compréhension.

Fait la glue entre les prompts, le client Albert, et la persistance. Chaque
fonction publique correspond à une action élève possible sur un item :
- `evaluate_answer`  : 1re (ou N-ième) réponse à une question sans indice.
- `generate_hint`    : demande explicite d'indice (niveau 1, 2 ou 3).
- `reveal_answer`    : révélation après épuisement des indices.
- `build_synthese`   : bilan de fin de session.

Modèle d'interaction :

1. L'élève lit la question, tape une réponse, clique « Vérifier ».
   → `evaluate_answer` renvoie un verdict + commentaire.
2. Si verdict = CORRECTE : passage à la question suivante.
3. Si verdict = PARTIELLE/INSUFFISANTE : l'élève peut retenter ou demander
   un indice. `generate_hint(level=N)` fournit l'indice.
4. Après le 3e indice, si l'élève bloque encore, `reveal_answer` donne la
   bonne réponse + raisonnement + une phrase pour la prochaine fois.
5. À la fin de la session, `build_synthese` produit un bilan.

Toutes les erreurs Albert (réseau, garde-fou) sont converties en messages
français lisibles pour l'élève. Jamais de stack trace exposée.
"""

from __future__ import annotations

import logging
import re

from sqlmodel import Session as DBSession

from app.core.albert_client import AlbertClient, AlbertError, Task
from app.core.db import add_turn, get_turns_by_step, update_session_step
from app.francais.comprehension.models import (
    ComprehensionExercise,
    ExerciseItem,
)
from app.francais.comprehension.prompts import (
    SYSTEM_PERSONA,
    ExerciseContext,
    build_first_eval,
    build_hint,
    build_reecriture_eval,
    build_reecriture_hint,
    build_reecriture_reveal,
    build_reveal_answer,
    build_session_synthese,
)

logger = logging.getLogger(__name__)


GENERIC_ERROR_MSG = (
    "Désolé, j'ai eu un petit souci pour te répondre. Réessaie dans quelques "
    "secondes — si ça recommence, préviens ton·ta prof."
)


# ============================================================================
# Singleton client Albert
# ============================================================================

_client: AlbertClient | None = None


def get_client() -> AlbertClient:
    global _client
    if _client is None:
        _client = AlbertClient()
    return _client


# ============================================================================
# Types de sortie
# ============================================================================


class EvalVerdict:
    CORRECTE = "CORRECTE"
    PARTIELLE = "PARTIELLE"
    INSUFFISANTE = "INSUFFISANTE"


class EvalAction:
    VALIDER = "VALIDER"
    INDICE = "INDICE"
    RETENTER = "RETENTER"


class EvalResult:
    """Résultat structuré d'une évaluation."""

    def __init__(self, verdict: str, commentaire: str, action: str, raw: str):
        self.verdict = verdict
        self.commentaire = commentaire
        self.action = action
        self.raw = raw

    @property
    def is_correct(self) -> bool:
        return self.verdict == EvalVerdict.CORRECTE


_VERDICT_RE = re.compile(r"VERDICT\s*:\s*(\w+)", re.IGNORECASE)
_ACTION_RE = re.compile(r"PROCHAINE[_\s]ACTION\s*:\s*(\w+)", re.IGNORECASE)
_COMMENT_RE = re.compile(
    r"COMMENTAIRE\s*:\s*(.+?)(?=PROCHAINE[_\s]ACTION|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _parse_eval(raw: str) -> EvalResult:
    """Parse la sortie structurée d'`build_first_eval`.

    Tolérant : si le modèle dérive un peu, on reconstitue du mieux possible,
    avec fallback raisonnable (verdict = INSUFFISANTE, action = RETENTER).
    """
    verdict_match = _VERDICT_RE.search(raw)
    action_match = _ACTION_RE.search(raw)
    comment_match = _COMMENT_RE.search(raw)

    verdict = (verdict_match.group(1) if verdict_match else "INSUFFISANTE").upper()
    if verdict not in (
        EvalVerdict.CORRECTE,
        EvalVerdict.PARTIELLE,
        EvalVerdict.INSUFFISANTE,
    ):
        verdict = EvalVerdict.INSUFFISANTE

    action = (action_match.group(1) if action_match else "RETENTER").upper()
    if action not in (EvalAction.VALIDER, EvalAction.INDICE, EvalAction.RETENTER):
        action = EvalAction.RETENTER

    commentaire = (comment_match.group(1) if comment_match else raw).strip()
    # Nettoyage : enlever un éventuel « COMMENTAIRE : » initial qui aurait
    # été capturé, et les lignes vides terminales.
    commentaire = re.sub(r"^\s*COMMENTAIRE\s*:\s*", "", commentaire, flags=re.IGNORECASE)
    commentaire = commentaire.strip()

    return EvalResult(verdict=verdict, commentaire=commentaire, action=action, raw=raw)


# ============================================================================
# Helpers
# ============================================================================


def _build_context(
    exo: ComprehensionExercise, item: ExerciseItem
) -> ExerciseContext:
    return ExerciseContext(
        texte_lignes=exo.texte_support.lignes,
        notes=exo.notes_texte,
        paratexte=exo.paratexte,
        item=item,
    )


def _chat(task: Task, prompt: str) -> str:
    """Appel Albert minimal : system persona + user prompt, non-streaming."""
    client = get_client()
    result = client.chat(
        task=task,
        messages=[
            {"role": "system", "content": SYSTEM_PERSONA},
            {"role": "user", "content": prompt},
        ],
    )
    return result.content


def count_attempts_at_step(
    db: DBSession, session_id: int, step: int
) -> int:
    """Nombre de tentatives élève déjà enregistrées pour ce step."""
    turns = get_turns_by_step(db, session_id, step)
    return sum(1 for t in turns if t.role == "user")


def count_hints_at_step(db: DBSession, session_id: int, step: int) -> int:
    """Nombre d'indices déjà fournis par l'assistant pour ce step.

    On compte les turns `assistant` dont le contenu est taggé avec le
    marqueur interne `[indice-N]` qu'on préfixe dans `generate_hint`.
    """
    turns = get_turns_by_step(db, session_id, step)
    return sum(
        1 for t in turns if t.role == "assistant" and t.content.startswith("[indice-")
    )


# ============================================================================
# Actions publiques
# ============================================================================


def evaluate_answer(
    db: DBSession,
    session_id: int,
    exo: ComprehensionExercise,
    item: ExerciseItem,
    reponse_eleve: str,
) -> EvalResult:
    """Évalue une réponse élève. Ne donne jamais la bonne réponse.

    Dispatche vers le prompt dédié si l'item est une question de
    réécriture (la grille d'évaluation est totalement différente :
    vérification contrainte par contrainte plutôt qu'interprétation).

    Sauve la réponse élève et le retour Albert dans la DB en tant que turns.
    """
    add_turn(db, session_id, step=item.order, role="user", content=reponse_eleve)

    ctx = _build_context(exo, item)
    if item.type == "reecriture":
        prompt = build_reecriture_eval(ctx, reponse_eleve)
    else:
        prompt = build_first_eval(ctx, reponse_eleve)

    try:
        raw = _chat(Task.FR_COMP_EVAL, prompt)
    except AlbertError as e:
        logger.warning("AlbertError en FR_COMP_EVAL : %s", e)
        add_turn(db, session_id, step=item.order, role="assistant", content=GENERIC_ERROR_MSG)
        return EvalResult(
            verdict=EvalVerdict.INSUFFISANTE,
            commentaire=GENERIC_ERROR_MSG,
            action=EvalAction.RETENTER,
            raw="",
        )

    result = _parse_eval(raw)
    add_turn(db, session_id, step=item.order, role="assistant", content=raw)
    return result


def generate_hint(
    db: DBSession,
    session_id: int,
    exo: ComprehensionExercise,
    item: ExerciseItem,
    reponse_eleve: str,
    level: int,
) -> str:
    """Génère un indice gradué pour l'item. `level` ∈ {1, 2, 3}.

    Le turn est sauvé avec le préfixe `[indice-N]` pour que les comptages
    ultérieurs (via `count_hints_at_step`) soient exacts même si le contenu
    brut de l'indice contient le mot « indice ».
    """
    if level not in (1, 2, 3):
        raise ValueError(f"level must be 1, 2 or 3, got {level}")

    ctx = _build_context(exo, item)
    if item.type == "reecriture":
        prompt = build_reecriture_hint(ctx, reponse_eleve, level)
    else:
        prompt = build_hint(ctx, reponse_eleve, level)

    try:
        raw = _chat(Task.FR_COMP_HINT, prompt)
    except AlbertError as e:
        logger.warning("AlbertError en FR_COMP_HINT (niveau %d) : %s", level, e)
        raw = GENERIC_ERROR_MSG

    tagged = f"[indice-{level}] {raw}"
    add_turn(db, session_id, step=item.order, role="assistant", content=tagged)
    return raw


def reveal_answer(
    db: DBSession,
    session_id: int,
    exo: ComprehensionExercise,
    item: ExerciseItem,
    reponse_eleve: str,
) -> str:
    """Révèle la bonne réponse avec raisonnement. À appeler APRÈS les 3 indices."""
    ctx = _build_context(exo, item)
    if item.type == "reecriture":
        prompt = build_reecriture_reveal(ctx, reponse_eleve)
    else:
        prompt = build_reveal_answer(ctx, reponse_eleve)

    try:
        raw = _chat(Task.FR_COMP_REVEAL, prompt)
    except AlbertError as e:
        logger.warning("AlbertError en FR_COMP_REVEAL : %s", e)
        raw = GENERIC_ERROR_MSG

    add_turn(
        db, session_id, step=item.order, role="assistant", content=f"[reveal] {raw}"
    )
    return raw


def build_synthese(
    db: DBSession,
    session_id: int,
    items_resolved: list[tuple[ExerciseItem, str, bool]],
) -> str:
    """Bilan pédagogique de fin de session."""
    prompt = build_session_synthese(items_resolved)
    try:
        raw = _chat(Task.FR_COMP_SYNTHESE, prompt)
    except AlbertError as e:
        logger.warning("AlbertError en FR_COMP_SYNTHESE : %s", e)
        raw = (
            "Bravo pour ton travail sur cette séance ! Pour la prochaine fois, "
            "relis les questions sur lesquelles tu as bloqué et essaie de "
            "repérer ce que le texte suggère sans le dire directement."
        )
    add_turn(db, session_id, step=0, role="assistant", content=f"[synthese] {raw}")
    return raw


def advance_step(db: DBSession, session_id: int, new_step: int) -> None:
    """Petit helper pour avancer le curseur de session."""
    update_session_step(db, session_id, new_step)


__all__ = [
    "EvalVerdict",
    "EvalAction",
    "EvalResult",
    "evaluate_answer",
    "generate_hint",
    "reveal_answer",
    "build_synthese",
    "advance_step",
    "count_attempts_at_step",
    "count_hints_at_step",
]

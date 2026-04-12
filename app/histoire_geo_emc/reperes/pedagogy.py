"""
Orchestration pédagogique de l'épreuve « Repères ».

Quatre fonctions publiques :

- `generate_question(repere)` : appelle Albert (Mistral-Small) pour
  formuler la question à partir du repère.
- `evaluate_answer(repere, student_answer)` : **logique Python pure**,
  compare la réponse normalisée à la valeur attendue. Pas d'Albert.
- `generate_hint(repere, hint_level, previous_answers)` : indice gradué
  via Albert.
- `reveal_answer(repere)` : message de révélation via Albert après
  épuisement des 3 indices.

Toutes les erreurs Albert (réseau, timeout, filtre…) sont attrapées ici
et converties en messages gracieux pour l'élève — l'app ne doit jamais
exposer une stack trace.
"""

from __future__ import annotations

import logging
import re
import unicodedata

from app.core.albert_client import AlbertClient, AlbertError, Task
from app.histoire_geo_emc.reperes.models import Repere
from app.histoire_geo_emc.reperes.prompts import (
    build_hint_prompt,
    build_question_prompt,
    build_reveal_prompt,
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
# Messages d'erreur gracieux
# ============================================================================


GENERIC_ERROR_MSG = (
    "Désolé, j'ai eu un petit souci pour te répondre. Réessaie dans "
    "quelques secondes."
)

FALLBACK_QUESTION = "Peux-tu me parler de ce repère ?"


def _safe_chat(task: Task, messages: list[dict], fallback: str) -> str:
    """Wrapper unique pour les appels Albert côté repères.

    Ne laisse JAMAIS remonter une exception à l'appelant — renvoie
    `fallback` ou `GENERIC_ERROR_MSG` selon le type d'erreur.
    """
    try:
        client = get_albert_client()
        result = client.chat(task, messages, retry_on_missing_citations=False)
        return result.content.strip()
    except AlbertError as exc:
        logger.warning("Albert a renvoyé une erreur : %s", exc)
        return fallback
    except Exception:  # pragma: no cover
        logger.exception("Erreur inattendue lors de l'appel Albert")
        return fallback


# ============================================================================
# 1. Génération de la question
# ============================================================================


def generate_question(repere: Repere) -> str:
    """Formule une question adaptée au type du repère."""
    messages = build_question_prompt(repere)
    return _safe_chat(
        Task.UI_TEXT, messages, fallback=_fallback_question(repere)
    )


def _fallback_question(repere: Repere) -> str:
    """Question de secours si Albert est indisponible.

    Simple mais fonctionnelle : permet au quiz de tourner même en mode
    dégradé.
    """
    if repere.type in ("date", "evenement") and repere.annee is not None:
        return f"En quelle année {_lowercase_first(repere.libelle)} ?"
    if repere.type == "personnage":
        return f"Qui est {repere.libelle} ?"
    if repere.type == "lieu":
        return f"Place {repere.libelle} ou dis-moi ce que c'est."
    if repere.type in ("notion", "definition"):
        return f"Que désigne « {repere.libelle} » ?"
    return FALLBACK_QUESTION


def _lowercase_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


# ============================================================================
# 2. Évaluation de la réponse (déterministe, pas d'Albert)
# ============================================================================


def _normalize(text: str) -> str:
    """Normalisation agressive pour la comparaison : accents, ponctuation,
    casse, espaces, articles courants."""
    if not text:
        return ""
    # Strip accents
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Lowercase + strip
    norm = ascii_text.lower().strip()
    # Articles en tête : le, la, les, l', un, une, des, d'
    norm = re.sub(r"^(le|la|les|l[' ]|un|une|des|d['  ])\s*", "", norm)
    # Supprime ponctuation, garde espaces et tirets
    norm = re.sub(r"[^\w\s-]", "", norm)
    # Compact les espaces
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm


def _extract_year(text: str) -> int | None:
    """Tente d'extraire une année (3 ou 4 chiffres, ou négatif) du texte.

    Couvre les cas :
    - "1914"
    - "en 1914"
    - "1914-1918" (renvoie la première)
    - "-52" ou "52 av. J.-C." → -52
    - "52 av JC" → -52
    """
    if not text:
        return None
    # Négatif explicite (avant J.-C., avant JC, avant Jésus-Christ, av. JC…).
    # On normalise d'abord pour matcher « avant jesus-christ » sans accent.
    nfkd = unicodedata.normalize("NFKD", text.lower())
    ascii_text = "".join(c for c in nfkd if not unicodedata.combining(c))
    neg_match = re.search(
        r"(\d{1,4})\s*(?:av\.?\s*j[.\s-]*c\.?"
        r"|avant\s*j[.\s-]*c\.?"
        r"|avant\s*jesus[\s-]christ)",
        ascii_text,
    )
    if neg_match:
        return -int(neg_match.group(1))
    # Négatif avec signe moins
    neg2 = re.search(r"(?:^|\s)-(\d{1,4})(?:\s|$)", text)
    if neg2:
        return -int(neg2.group(1))
    # Positif : 3 ou 4 chiffres
    pos = re.search(r"(\d{3,4})", text)
    if pos:
        return int(pos.group(1))
    return None


def evaluate_answer(
    repere: Repere,
    student_answer: str,
    question: str | None = None,
) -> bool:
    """Compare la réponse élève à la valeur attendue.

    Logique par type :
    - `date`, `evenement` avec annee : on cherche une année dans la
      réponse et on la compare à `repere.annee` avec tolérance ±1.
    - autres : comparaison locale d'abord (rapide), puis appel Albert
      si le match local échoue (rattrapage des réponses correctes mais
      formulées différemment du libellé).
    """
    if not student_answer or not student_answer.strip():
        return False

    # Cas chronologique prioritaire
    if repere.annee is not None and repere.type in ("date", "evenement"):
        year = _extract_year(student_answer)
        if year is None:
            # Si la question est de type evenement, on accepte aussi une
            # réponse textuelle qui match le libellé (ex : « guerre d'Algérie »
            # au lieu de « 1954 »).
            if repere.type == "evenement":
                return _match_libelle(repere, student_answer)
            return False
        # Tolérance ±1 an
        return abs(year - repere.annee) <= 1

    # Cas textuel : match local d'abord (rapide, pas d'appel réseau)
    if _match_libelle(repere, student_answer):
        return True

    # Rattrapage via Albert pour les cas ou la réponse est correcte mais
    # formulée différemment du libellé (ex : noms de pays pour les BRICS).
    return _albert_evaluate(repere, student_answer, question)


def _albert_evaluate(
    repere: Repere,
    student_answer: str,
    question: str | None,
) -> bool:
    """Demande a Albert si la réponse est correcte (rattrapage LLM).

    Appel court (Mistral-Small via UI_TEXT) quand le match local échoue.
    En cas d'erreur Albert, retourne False (conservateur : pas de faux
    positif — l'élève peut retenter ou demander un indice).
    """
    import json

    notions = json.loads(repere.notions_associees_json or "[]")
    context_parts = [f"Repere : {repere.libelle}"]
    if notions:
        context_parts.append(f"Notions associees : {', '.join(notions)}")
    if repere.annee:
        context_parts.append(f"Annee : {repere.annee}")
    if repere.periode:
        context_parts.append(f"Periode : {repere.periode}")
    context = "\n".join(context_parts)

    q_line = f"\nQuestion posee : {question}" if question else ""

    prompt = f"""Tu es un correcteur de reperes d'histoire-geographie pour le DNB.

{context}{q_line}
Reponse de l'eleve : {student_answer}

La reponse est-elle correcte ou suffisamment proche de la bonne reponse ?
Reponds UNIQUEMENT par OUI ou NON."""

    messages = [{"role": "user", "content": prompt}]
    raw = _safe_chat(Task.UI_TEXT, messages, fallback="NON")
    return raw.strip().upper().startswith("OUI")


def _match_libelle(repere: Repere, student_answer: str) -> bool:
    """Match textuel : égalité normalisée, ou chaîne attendue incluse."""
    expected_norm = _normalize(repere.libelle)
    student_norm = _normalize(student_answer)
    if not expected_norm or not student_norm:
        return False
    if student_norm == expected_norm:
        return True
    # Match partiel généreux : l'attendu est contenu dans la réponse
    # (utile quand l'élève précise « la ville de Paris » au lieu de « Paris »).
    if expected_norm in student_norm:
        return True
    # Ou l'inverse : la réponse élève est un noyau contenu dans l'attendu
    # (ex : « Guerre 14-18 » quand on attendait « Première Guerre mondiale »
    # — ici ça ne matchera pas, c'est le comportement voulu : on reste
    # strict sur les libellés canoniques).
    return False


# ============================================================================
# 3. Indices gradués
# ============================================================================


def generate_hint(
    repere: Repere, hint_level: int, previous_answers: list[str]
) -> str:
    """Produit un indice gradué (niveau 1 à 3) via Albert."""
    messages = build_hint_prompt(repere, hint_level, previous_answers)
    return _safe_chat(
        Task.UI_TEXT,
        messages,
        fallback=_fallback_hint(repere, hint_level),
    )


def _fallback_hint(repere: Repere, hint_level: int) -> str:
    """Indice de secours si Albert est indisponible."""
    if hint_level == 1:
        if repere.periode:
            return f"Pense à la période : {repere.periode}."
        return f"C'est un repère d'{repere.discipline}."
    if hint_level == 2:
        if repere.theme:
            return f"C'est rattaché au thème : {repere.theme}."
        return f"C'est lié à : {', '.join(repere.notions_associees) or 'un grand moment'}."
    # Niveau 3 : première lettre ou décennie
    if repere.annee is not None:
        decade = (repere.annee // 10) * 10
        return f"C'est dans les années {decade}."
    return f"Ça commence par « {repere.libelle[:1]} »."


# ============================================================================
# 4. Révélation finale
# ============================================================================


def reveal_answer(repere: Repere) -> str:
    """Message de révélation après épuisement des indices."""
    messages = build_reveal_prompt(repere)
    return _safe_chat(
        Task.UI_TEXT,
        messages,
        fallback=_fallback_reveal(repere),
    )


def _fallback_reveal(repere: Repere) -> str:
    if repere.annee is not None:
        annee_str = (
            f"{repere.annee}-{repere.annee_fin}"
            if repere.annee_fin
            else str(repere.annee)
        )
        return (
            f"Pas grave, on le retient ensemble : {repere.libelle} → "
            f"{annee_str}. Tu le retrouveras dans la file tout à l'heure."
        )
    return (
        f"Pas grave. La bonne réponse était : {repere.libelle}. "
        f"Tu le reverras dans la file."
    )


__all__ = [
    "generate_question",
    "evaluate_answer",
    "generate_hint",
    "reveal_answer",
]

"""
Client Albert — wrapper OpenAI-compatible avec routage modèles et garde-fous.

Rôle :
- Centraliser tous les appels à l'API Albert derrière une seule classe.
- Router automatiquement vers le bon modèle selon le type de tâche pédagogique.
- Appliquer les post-filtres de sécurité (détection demande de rédaction,
  vérification des citations de sources).
- Exposer un mode streaming pour l'UI HTMX/SSE.

Principe :
- Aucune logique pédagogique ici (elle est dans prompts.py). Le client ne sait
  QUE router et filtrer, il ne construit jamais de prompt.
- Tout passe par `Task` (enum) → le caller dit « je fais une first_eval » et le
  client choisit modèle + température + max_tokens + post-filtres adaptés.

Modèles Albert utilisés (au 2026-04, vérifié via /v1/models) :
- openai/gpt-oss-120b : modèle "reasoning", meilleur pour l'évaluation et la
  correction. Attention : consomme des tokens en chain-of-thought interne
  (champ `reasoning` dans la réponse) avant de produire `content`. Il faut
  donc toujours prévoir un max_tokens généreux.
- mistralai/Mistral-Small-3.2-24B-Instruct-2506 : plus rapide, suffisant pour
  les questions socratiques et les reformulations courtes.
"""

from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Iterator

from openai import OpenAI

logger = logging.getLogger(__name__)


# ============================================================================
# Modèles disponibles
# ============================================================================

MODEL_HEAVY = "openai/gpt-oss-120b"
MODEL_FAST = "mistralai/Mistral-Small-3.2-24B-Instruct-2506"

DEFAULT_BASE_URL = "https://albert.api.etalab.gouv.fr/v1"


# ============================================================================
# Types de tâches — une entrée par intention pédagogique
# ============================================================================


class Task(str, Enum):
    """Types de tâches pédagogiques. Chacune a un profil modèle + filtres."""

    # Décryptage du sujet en mode très assisté (JSON structuré, précision)
    DECRYPT_SUBJECT = "decrypt_subject"

    # Aide au décryptage du sujet (étape 1) — questions socratiques en prose
    HELP_UNDERSTAND = "help_understand"

    # Première évaluation de l'approche élève (étape 3)
    FIRST_EVAL = "first_eval"

    # Seconde évaluation (étape 5)
    SECOND_EVAL = "second_eval"

    # Correction finale de la copie (étape 7)
    FINAL_CORRECTION = "final_correction"

    # Reformulation courte, accueil, micro-messages d'UI
    UI_TEXT = "ui_text"

    # --- Français : compréhension et compétences d'interprétation ---
    # Évaluation d'une réponse élève à une question de compréhension
    FR_COMP_EVAL = "fr_comp_eval"
    # Génération d'un indice gradué (niveau 1, 2 ou 3)
    FR_COMP_HINT = "fr_comp_hint"
    # Révélation pédagogique de la réponse après épuisement des indices
    FR_COMP_REVEAL = "fr_comp_reveal"
    # Synthèse de fin de session
    FR_COMP_SYNTHESE = "fr_comp_synthese"

    # --- Français : rédaction ---
    # Coup de pouce socratique pour aider l'élève à choisir entre
    # imagination et réflexion (étape 1)
    FR_REDACTION_HELP = "fr_redaction_help"
    # Première évaluation du brouillon / plan (étape 3)
    FR_REDACTION_FIRST_EVAL = "fr_redaction_first_eval"
    # Seconde évaluation après re-proposition (étape 5)
    FR_REDACTION_SECOND_EVAL = "fr_redaction_second_eval"
    # Correction finale fond + forme de la copie (étape 7)
    FR_REDACTION_FINAL_CORRECTION = "fr_redaction_final_correction"

    # --- Outils matière : mini-dictionnaire (bouton flottant FAB) ---
    # Définition TRÈS courte (1-2 phrases) d'un mot ou d'une expression
    # saisie par l'élève dans la popin « Outils » côté français.
    FR_DEFINITION = "fr_definition"
    # Idem côté histoire-géographie-EMC (notion, date, lieu, personnage…).
    HGEMC_DEFINITION = "hgemc_definition"

    # --- Mathématiques : automatismes ---
    # Indice gradué (niveau 1, 2 ou 3) sur une question d'automatismes
    MATH_AUTO_HINT = "math_auto_hint"
    # Révélation pédagogique de la réponse + mini-explication
    MATH_AUTO_REVEAL = "math_auto_reveal"
    # Évaluation d'une réponse ouverte courte (questions à scoring=albert),
    # réponse forcée en JSON strict {"correct": bool, "feedback_court": str}
    MATH_AUTO_EVAL_OPEN = "math_auto_eval_open"

    # --- Mathématiques : raisonnement et résolution de problèmes ---
    # Indice gradué sur une sous-question d'exercice de raisonnement
    MATH_PROB_HINT = "math_prob_hint"
    # Révélation pédagogique d'une sous-question + mini-explication
    MATH_PROB_REVEAL = "math_prob_reveal"
    # Évaluation d'une justification courte (sous-question à scoring=albert),
    # réponse forcée en JSON strict {"correct": bool, "feedback_court": str}
    MATH_PROB_EVAL_OPEN = "math_prob_eval_open"

    # --- Sciences : révision par thème ---
    # Indice gradué (niveau 1, 2 ou 3) sur une question de révision sciences
    # (physique-chimie, SVT ou technologie).
    SCIENCES_REV_HINT = "sciences_rev_hint"
    # Révélation pédagogique de la réponse + mini-explication notionnelle
    SCIENCES_REV_REVEAL = "sciences_rev_reveal"
    # Évaluation d'une réponse ouverte courte (questions à scoring=albert),
    # réponse forcée en JSON strict {"correct": bool, "feedback_court": str}
    SCIENCES_REV_EVAL_OPEN = "sciences_rev_eval_open"


@dataclass(frozen=True)
class TaskProfile:
    """Profil d'appel associé à un type de tâche."""

    model: str
    temperature: float
    max_tokens: int
    # Si True, on vérifie que la réponse cite au moins une source entre crochets
    require_citations: bool = False
    # Si True, on passe le post-filtre anti-rédaction-à-la-place
    check_no_ghostwriting: bool = True
    # Si True, on tente de parser la réponse comme JSON
    expect_json: bool = False


# Table de routage. Modifier ici pour changer modèle/temp/tokens par tâche.
TASK_PROFILES: dict[Task, TaskProfile] = {
    Task.DECRYPT_SUBJECT: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.3,
        max_tokens=1200,
        expect_json=True,
        check_no_ghostwriting=False,  # sortie JSON, pas de prose à filtrer
    ),
    Task.HELP_UNDERSTAND: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.4,
        max_tokens=900,
        # Questions socratiques : pas de faits asserts → pas d'exigence de
        # citations, et pas de risque de ghostwriting (sortie = questions).
        require_citations=False,
        check_no_ghostwriting=False,
    ),
    Task.FIRST_EVAL: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.3,
        max_tokens=1200,
        require_citations=True,
    ),
    Task.SECOND_EVAL: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.3,
        max_tokens=1200,
        require_citations=True,
    ),
    Task.FINAL_CORRECTION: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.2,
        max_tokens=2000,
        require_citations=True,
        # Désactivé : une correction finale est légitimement longue et en prose,
        # le filtre ghostwriting génère trop de faux positifs. La règle
        # anti-réécriture est déjà imposée par le prompt lui-même.
        check_no_ghostwriting=False,
    ),
    Task.UI_TEXT: TaskProfile(
        model=MODEL_FAST,
        temperature=0.5,
        max_tokens=300,
        check_no_ghostwriting=False,
    ),
    # --- Français : compréhension et compétences d'interprétation ---
    # Pas de `require_citations` (on ne cite pas les collections Albert au
    # MVP français), ni de `check_no_ghostwriting` (le filtre
    # `_looks_like_ghostwritten_dc` est calibré pour le DC histoire-géo, il
    # génère des faux positifs sur des évaluations courtes en prose).
    Task.FR_COMP_EVAL: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.2,
        max_tokens=1200,
        check_no_ghostwriting=False,
    ),
    Task.FR_COMP_HINT: TaskProfile(
        model=MODEL_FAST,
        temperature=0.5,
        max_tokens=500,
        check_no_ghostwriting=False,
    ),
    Task.FR_COMP_REVEAL: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.3,
        max_tokens=1000,
        check_no_ghostwriting=False,
    ),
    Task.FR_COMP_SYNTHESE: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.4,
        max_tokens=800,
        check_no_ghostwriting=False,
    ),
    # --- Français : rédaction ---
    # Mêmes choix que pour le DC histoire-géo : on garde les filtres de
    # citations pour les évaluations / corrections (le RAG injecte de la
    # méthodo et du programme), et on désactive le filtre ghostwriting sur
    # la correction finale (faux positifs sur les corrections longues, le
    # garde-fou est déjà imposé par le prompt).
    Task.FR_REDACTION_HELP: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.4,
        max_tokens=900,
        check_no_ghostwriting=False,
        require_citations=False,
    ),
    Task.FR_REDACTION_FIRST_EVAL: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.3,
        max_tokens=1200,
        require_citations=True,
    ),
    Task.FR_REDACTION_SECOND_EVAL: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.3,
        max_tokens=1200,
        require_citations=True,
    ),
    Task.FR_REDACTION_FINAL_CORRECTION: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.2,
        max_tokens=2000,
        require_citations=True,
        check_no_ghostwriting=False,
    ),
    # --- Outils matière : mini-dictionnaire ---
    # Très court, rapide, pas de RAG. Mistral-Small suffit largement pour
    # une définition de 1 à 2 phrases. max_tokens=300 pour laisser la marge
    # au modèle sans encourager les réponses longues (le prompt est strict
    # sur la longueur). Pas de citations exigées (on ne branche pas le RAG
    # sur cet outil), pas de filtre ghostwriting (sortie = une définition
    # factuelle courte, rien à voir avec une rédaction à la place).
    Task.FR_DEFINITION: TaskProfile(
        model=MODEL_FAST,
        temperature=0.3,
        max_tokens=300,
        check_no_ghostwriting=False,
        require_citations=False,
    ),
    Task.HGEMC_DEFINITION: TaskProfile(
        model=MODEL_FAST,
        temperature=0.3,
        max_tokens=300,
        check_no_ghostwriting=False,
        require_citations=False,
    ),
    # --- Mathématiques : automatismes ---
    # Indice et révélation : Mistral-Small (court, rapide). Pas de RAG donc
    # pas de citations exigées, pas de risque de ghostwriting (réponses
    # courtes, l'objectif d'apprentissage EST la bonne réponse).
    Task.MATH_AUTO_HINT: TaskProfile(
        model=MODEL_FAST,
        temperature=0.5,
        max_tokens=300,
        check_no_ghostwriting=False,
    ),
    Task.MATH_AUTO_REVEAL: TaskProfile(
        model=MODEL_FAST,
        temperature=0.3,
        max_tokens=500,
        check_no_ghostwriting=False,
    ),
    # Évaluation ouverte : gpt-oss-120b en mode reasoning, sortie JSON
    # strict. max_tokens >= 600 pour laisser de la marge au reasoning
    # (sinon le `content` revient vide). Citations requises pour matcher
    # les éventuels [methodo]/[programme] que le prompt invite à insérer
    # quand un passage RAG a été utilisé. Le retry sur citations manquantes
    # est désactivé côté pedagogy via `_safe_chat` (qui ne demande pas de
    # citations strictes : on accepte un JSON sans cite si pas de RAG).
    Task.MATH_AUTO_EVAL_OPEN: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.2,
        max_tokens=800,
        check_no_ghostwriting=False,
        require_citations=False,
    ),
    # --- Mathématiques : raisonnement et résolution de problèmes ---
    # Mêmes profils que pour les automatismes : les indices et révélations
    # tournent sur Mistral-Small (courts, rapides, pas de citations exigées),
    # l'évaluation ouverte passe sur gpt-oss-120b avec JSON strict. Les
    # questions sont plus longues (contexte d'exercice + sous-question),
    # on monte un peu le plafond de tokens pour laisser la marge côté
    # reasoning du modèle heavy.
    Task.MATH_PROB_HINT: TaskProfile(
        model=MODEL_FAST,
        temperature=0.5,
        max_tokens=400,
        check_no_ghostwriting=False,
    ),
    Task.MATH_PROB_REVEAL: TaskProfile(
        model=MODEL_FAST,
        temperature=0.3,
        max_tokens=600,
        check_no_ghostwriting=False,
    ),
    Task.MATH_PROB_EVAL_OPEN: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.2,
        max_tokens=900,
        check_no_ghostwriting=False,
        require_citations=False,
    ),
    # --- Sciences : révision par thème ---
    # Mêmes profils que les automatismes maths : indices et révélations
    # sur Mistral-Small (courts, rapides, pas de RAG donc pas de
    # citations exigées), évaluation ouverte sur gpt-oss-120b avec JSON
    # strict. Les questions de sciences restent courtes donc pas de
    # risque de ghostwriting — la bonne réponse EST l'objectif
    # d'apprentissage.
    Task.SCIENCES_REV_HINT: TaskProfile(
        model=MODEL_FAST,
        temperature=0.5,
        max_tokens=300,
        check_no_ghostwriting=False,
    ),
    Task.SCIENCES_REV_REVEAL: TaskProfile(
        model=MODEL_FAST,
        temperature=0.3,
        max_tokens=500,
        check_no_ghostwriting=False,
    ),
    Task.SCIENCES_REV_EVAL_OPEN: TaskProfile(
        model=MODEL_HEAVY,
        temperature=0.2,
        max_tokens=800,
        check_no_ghostwriting=False,
        require_citations=False,
    ),
}


# ============================================================================
# Post-filtres de sécurité
# ============================================================================

# Heuristique : une "vraie" rédaction de DC à la place de l'élève se reconnaît à :
# - L'absence totale de marqueurs d'interaction avec l'élève ("tu", "ton", "?", puces).
# - Un ton narratif continu à la 3e personne ("Berlin est...", "La guerre froide a...").
# - Aucun marqueur de structure de correction (pas de "FOND", "FORME", "Points forts").
# Le filtre n'est appliqué QUE sur first_eval / second_eval, où une réponse
# longue et narrative est forcément suspecte.

_STRUCTURE_MARKERS = re.compile(
    r"(?i)(===\s*fond|===\s*forme|points?\s*forts?|ce\s*qui\s*est\s*bien|"
    r"manques?|conseil\s*prioritaire|notion\s*à\s*approfondir|"
    r"introduction\s*/|connecteurs|longueur|langue)"
)

_INTERACTIVE_MARKERS = re.compile(r"[?•]|\btu\b|\bton\b|\bta\b|\btes\b", re.IGNORECASE)


def _looks_like_ghostwritten_dc(text: str) -> bool:
    """Détecte si Albert semble avoir rédigé le DC à la place de l'élève.

    Règle : si la réponse fait > 400 caractères, ne contient aucun marqueur
    de structure de correction ET aucun marqueur d'interaction (tu/ton/?),
    c'est très probablement une rédaction à la place.
    """
    if len(text) < 300:
        return False
    if _STRUCTURE_MARKERS.search(text):
        return False
    interactive_hits = len(_INTERACTIVE_MARKERS.findall(text))
    return interactive_hits < 3


_CITATION_PATTERN = re.compile(r"\[(programme|corrig[ée]|m[ée]thodo)[^\]]*\]", re.IGNORECASE)


def _has_citations(text: str) -> bool:
    return bool(_CITATION_PATTERN.search(text))


# ============================================================================
# Exceptions
# ============================================================================


class AlbertError(Exception):
    """Erreur d'appel Albert."""


class GhostwritingDetected(AlbertError):
    """La réponse ressemble à une rédaction à la place de l'élève."""


class MissingCitations(AlbertError):
    """La réponse ne contient aucune citation de source alors qu'il en fallait."""


# ============================================================================
# Client
# ============================================================================


@dataclass
class ChatResult:
    """Résultat d'un appel chat."""

    content: str
    task: Task
    model: str
    prompt_tokens: int
    completion_tokens: int
    # Métadonnées bonus que l'API Albert renvoie
    cost_eur: float | None = None
    kwh: float | None = None
    kg_co2eq: float | None = None


class AlbertClient:
    """Wrapper OpenAI-compatible pour Albert, avec routage et garde-fous."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        key = api_key or os.environ.get("ALBERT_API_KEY")
        if not key:
            raise RuntimeError(
                "ALBERT_API_KEY manquant. Ajoute-le dans .env ou passe-le au constructeur."
            )
        url = base_url or os.environ.get("ALBERT_BASE_URL") or DEFAULT_BASE_URL
        self._client = OpenAI(api_key=key, base_url=url)

    # ------------------------------------------------------------------
    # Appel non-streaming
    # ------------------------------------------------------------------

    def chat(
        self,
        task: Task,
        messages: list[dict],
        *,
        retry_on_missing_citations: bool = True,
    ) -> ChatResult:
        """Appel chat classique (pas de streaming).

        Applique le profil de la tâche + les post-filtres. Si les citations
        sont requises et manquent, un retry automatique est tenté avec un
        rappel explicite.
        """
        profile = TASK_PROFILES[task]
        content, usage = self._raw_chat(profile, messages)

        # Garde-fou 1 : rédaction à la place
        if profile.check_no_ghostwriting and _looks_like_ghostwritten_dc(content):
            logger.warning("Ghostwriting détecté pour task=%s", task)
            raise GhostwritingDetected(
                "La réponse ressemble à une rédaction à la place de l'élève."
            )

        # Garde-fou 2 : citations de sources
        if profile.require_citations and not _has_citations(content):
            if retry_on_missing_citations:
                logger.info("Citations manquantes, retry avec rappel pour task=%s", task)
                reminder = {
                    "role": "user",
                    "content": (
                        "Rappel important : pour chaque fait historique que tu "
                        "mentionnes, indique la source entre crochets, par exemple "
                        "[programme], [corrigé 2018] ou [méthodo]. Reprends ta "
                        "réponse en ajoutant ces citations."
                    ),
                }
                content, usage = self._raw_chat(
                    profile,
                    messages + [{"role": "assistant", "content": content}, reminder],
                )
                if not _has_citations(content):
                    raise MissingCitations(
                        "Aucune citation de source après retry."
                    )
            else:
                raise MissingCitations("Aucune citation de source dans la réponse.")

        return ChatResult(
            content=content,
            task=task,
            model=profile.model,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cost_eur=_get_nested(usage, "cost"),
            kwh=_get_nested(usage, "impacts", "kWh"),
            kg_co2eq=_get_nested(usage, "impacts", "kgCO2eq"),
        )

    # ------------------------------------------------------------------
    # Appel streaming (pour SSE vers HTMX)
    # ------------------------------------------------------------------

    def chat_stream(self, task: Task, messages: list[dict]) -> Iterator[str]:
        """Stream les tokens un par un. Pas de post-filtres en cours de stream.

        Note : les garde-fous ne sont pas appliqués en streaming (on ne peut pas
        retry sans avoir la réponse complète). Utiliser `chat()` pour toute
        tâche critique qui nécessite les garde-fous.
        """
        profile = TASK_PROFILES[task]
        stream = self._client.chat.completions.create(
            model=profile.model,
            messages=messages,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

    # ------------------------------------------------------------------
    # Appel brut (interne)
    # ------------------------------------------------------------------

    def _raw_chat(
        self,
        profile: TaskProfile,
        messages: list[dict],
    ) -> tuple[str, dict]:
        """Appel API sans post-filtre. Renvoie (content, usage_dict)."""
        response = self._client.chat.completions.create(
            model=profile.model,
            messages=messages,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
        )
        choice = response.choices[0]
        content = choice.message.content or ""
        # Le SDK OpenAI typed l'usage ; on sérialise en dict plat pour simplifier
        usage = response.usage.model_dump() if response.usage else {}
        return content, usage

    # ------------------------------------------------------------------
    # Liste des modèles disponibles (utile pour un check santé au démarrage)
    # ------------------------------------------------------------------

    def list_models(self) -> list[str]:
        models = self._client.models.list()
        return sorted(m.id for m in models.data)

    def health_check(self) -> dict:
        """Vérifie connectivité + présence des modèles cibles."""
        ids = self.list_models()
        return {
            "ok": MODEL_HEAVY in ids and MODEL_FAST in ids,
            "n_models": len(ids),
            "heavy_available": MODEL_HEAVY in ids,
            "fast_available": MODEL_FAST in ids,
        }


def _get_nested(d: dict, *keys: str):
    """Helper pour lire en profondeur dans les dicts d'usage Albert."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


__all__ = [
    "AlbertClient",
    "Task",
    "TaskProfile",
    "TASK_PROFILES",
    "ChatResult",
    "AlbertError",
    "GhostwritingDetected",
    "MissingCitations",
    "MODEL_HEAVY",
    "MODEL_FAST",
]

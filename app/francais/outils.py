"""Outils matière français — route du mini-dictionnaire (bouton FAB).

Expose un endpoint POST `/francais/outils/definition` appelé via HTMX
depuis le bouton flottant « Outils » présent sur toutes les pages
français. L'élève tape un mot ou une expression qu'il ne comprend pas
(vocabulaire d'un texte littéraire, grammaire, figure de style,
consigne d'examen…), et le serveur renvoie un fragment HTML contenant
une définition courte (1 à 2 phrases) générée par Albert.

Garde-fous :
- L'input est tronqué et nettoyé avant envoi (pas d'injection HTML,
  longueur max 80 caractères).
- Le prompt est strict sur la longueur et refuse poliment toute
  demande hors-scope (rédaction, plan, analyse longue).
- Toute erreur Albert est rattrapée et renvoyée sous forme de message
  français lisible dans le fragment résultat — jamais de stack trace
  côté élève (cf. convention `_safe_chat`).
- Albert tourne sur Mistral-Small (profil `Task.FR_DEFINITION`), ce
  qui garde la latence courte et le coût minimal sur ce volume
  potentiellement élevé.
"""

from __future__ import annotations

import logging
import re

from fastapi import Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.albert_client import (
    AlbertClient,
    AlbertError,
    Task,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Prompt système
# ============================================================================

SYSTEM_PROMPT_FR = (
    "Tu es un mini-dictionnaire intégré à une application de révision du "
    "Diplôme national du brevet (DNB) pour un élève français de 3e. Tu "
    "aides uniquement sur la matière français : vocabulaire d'un texte "
    "littéraire, terme de grammaire, figure de style, notion d'analyse, "
    "mot d'une consigne d'examen.\n\n"
    "Ta tâche : donner une définition TRÈS COURTE — 1 à 2 phrases au "
    "maximum. Français simple, mais terminologie correcte. Tutoiement "
    "bienveillant. Donne directement la définition, sans formule "
    "d'introduction du type « Voici la définition ».\n\n"
    "Règles strictes (en une phrase chacune si tu dois les appliquer) :\n"
    "- Si on te demande de rédiger un paragraphe, faire un plan, analyser "
    "un texte long ou résumer une œuvre : rappelle-lui que tu ne fais que "
    "des définitions courtes.\n"
    "- Si le mot est vraiment ambigu : demande de préciser le contexte.\n"
    "- Si tu n'es pas sûr du sens exact : dis-le plutôt que d'inventer.\n"
    "- Jamais de liste à puces, jamais de plan, jamais plus de 2 phrases."
)


# ============================================================================
# Client Albert (singleton paresseux, monkeypatchable en tests)
# ============================================================================

_albert_client: AlbertClient | None = None


def _get_client() -> AlbertClient:
    global _albert_client
    if _albert_client is None:
        _albert_client = AlbertClient()
    return _albert_client


# ============================================================================
# Nettoyage de l'input et de la sortie
# ============================================================================

_MAX_TERM_LEN = 80
_MAX_DEF_LEN = 600  # borne de sûreté, même si le prompt dit 2 phrases
_HTML_TAG = re.compile(r"<[^>]+>")


def _clean_term(raw: str) -> str:
    """Nettoie un terme saisi par l'élève avant envoi à Albert.

    Supprime les balises HTML, trim, et tronque à `_MAX_TERM_LEN`.
    """
    t = _HTML_TAG.sub("", raw or "").strip()
    if len(t) > _MAX_TERM_LEN:
        t = t[:_MAX_TERM_LEN].rstrip()
    return t


def _clean_definition(raw: str) -> str:
    """Nettoie la définition renvoyée par Albert.

    Trim, borne de longueur de sûreté, suppression des formules
    d'introduction parasites que le modèle pourrait tout de même
    ajouter malgré le prompt.
    """
    t = (raw or "").strip()
    # Retire les préfixes parasites fréquents.
    for prefix in (
        "voici la définition",
        "voici une définition",
        "définition",
    ):
        if t.lower().startswith(prefix):
            rest = t[len(prefix):].lstrip(" :.-—–").strip()
            if rest:
                t = rest
                break
    if len(t) > _MAX_DEF_LEN:
        t = t[:_MAX_DEF_LEN].rstrip() + "…"
    return t


# ============================================================================
# Handler de route
# ============================================================================


def register_route(router, templates: Jinja2Templates) -> None:
    """Enregistre la route `/outils/definition` sur le router matière.

    Passée en paramètre depuis `app.francais.routes` pour réutiliser le
    même `Jinja2Templates` (qui connaît déjà `_definition_result.html`
    via `_CORE_TEMPLATES`).
    """

    @router.post("/outils/definition", response_class=HTMLResponse)
    def define_term(
        request: Request,
        term: str = Form(default=""),
    ):
        cleaned = _clean_term(term)
        if not cleaned:
            return templates.TemplateResponse(
                request,
                "_definition_result.html",
                {
                    "error": "Tape un mot ou une expression avant de valider.",
                },
            )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_FR},
            {"role": "user", "content": cleaned},
        ]
        try:
            result = _get_client().chat(
                Task.FR_DEFINITION,
                messages,
                retry_on_missing_citations=False,
            )
            definition = _clean_definition(result.content)
        except AlbertError as exc:
            logger.warning("Définition FR — erreur Albert: %s", exc)
            return templates.TemplateResponse(
                request,
                "_definition_result.html",
                {
                    "error": (
                        "Je n'ai pas pu récupérer la définition, réessaie "
                        "dans quelques instants."
                    ),
                },
            )
        except Exception as exc:  # pragma: no cover — sécurité UI
            logger.exception("Définition FR — erreur inattendue: %s", exc)
            return templates.TemplateResponse(
                request,
                "_definition_result.html",
                {
                    "error": (
                        "Petit souci côté serveur. Réessaie dans un instant."
                    ),
                },
            )

        if not definition:
            return templates.TemplateResponse(
                request,
                "_definition_result.html",
                {
                    "error": (
                        "Désolé, je n'ai rien trouvé pour ce mot. "
                        "Essaie de le reformuler ou ajoute un peu de contexte."
                    ),
                },
            )

        return templates.TemplateResponse(
            request,
            "_definition_result.html",
            {
                "term": cleaned,
                "definition": definition,
            },
        )


__all__ = ["register_route", "SYSTEM_PROMPT_FR"]

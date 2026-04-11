"""Outils matière histoire-géo-EMC — route du mini-dictionnaire (bouton FAB).

Expose un endpoint POST `/histoire-geo-emc/outils/definition` appelé
via HTMX depuis le bouton flottant « Outils » présent sur toutes les
pages HG-EMC. L'élève tape un mot ou une expression qu'il ne comprend
pas (notion historique, concept géographique, institution, date,
personnage, lieu, vocabulaire civique…), et le serveur renvoie un
fragment HTML contenant une définition courte (1 à 2 phrases) générée
par Albert.

Garde-fous : identiques au module français (cf. `app/francais/outils.py`) :
input tronqué/nettoyé, prompt strict sur la longueur et sur le refus
poli des demandes hors-scope, erreurs Albert rattrapées en message
français lisible, Mistral-Small pour la latence.

Cf. `app/francais/outils.py` pour le jumeau côté français — les deux
modules partagent le même partial de résultat (`_definition_result.html`
dans `app/core/templates/`) et la même structure. Ils sont dupliqués
plutôt que factorisés parce que la matière est isolée (garde-fou #6 du
CLAUDE.md : ne pas importer de code d'une matière vers une autre).
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

SYSTEM_PROMPT_HGEMC = (
    "Tu es un mini-dictionnaire intégré à une application de révision du "
    "Diplôme national du brevet (DNB) pour un élève français de 3e. Tu "
    "aides uniquement sur la matière histoire-géographie-enseignement "
    "moral et civique (HG-EMC) : notion historique, concept géographique, "
    "institution, date, personnage, lieu, vocabulaire civique, terme d'un "
    "programme de 3e.\n\n"
    "Ta tâche : donner une définition TRÈS COURTE — 1 à 2 phrases au "
    "maximum. Français simple, mais factuelle et précise (dates, acteurs, "
    "lieux). Tutoiement bienveillant. Donne directement la définition, "
    "sans formule d'introduction du type « Voici la définition ».\n\n"
    "Règles strictes (en une phrase chacune si tu dois les appliquer) :\n"
    "- Si on te demande de rédiger un développement construit, faire un "
    "plan, analyser un document ou raconter un événement en détail : "
    "rappelle-lui que tu ne fais que des définitions courtes.\n"
    "- Si le mot est ambigu (ex. « résistance » — militaire, civile, "
    "électrique…) : demande de préciser le contexte en une phrase.\n"
    "- Si tu n'es pas sûr du sens exact ou de la date : dis-le plutôt que "
    "d'inventer.\n"
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
_MAX_DEF_LEN = 600
_HTML_TAG = re.compile(r"<[^>]+>")


def _clean_term(raw: str) -> str:
    """Nettoie un terme saisi par l'élève avant envoi à Albert."""
    t = _HTML_TAG.sub("", raw or "").strip()
    if len(t) > _MAX_TERM_LEN:
        t = t[:_MAX_TERM_LEN].rstrip()
    return t


def _clean_definition(raw: str) -> str:
    """Nettoie la définition renvoyée par Albert (trim, borne, préfixes)."""
    t = (raw or "").strip()
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
    """Enregistre la route `/outils/definition` sur le router matière."""

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
            {"role": "system", "content": SYSTEM_PROMPT_HGEMC},
            {"role": "user", "content": cleaned},
        ]
        try:
            result = _get_client().chat(
                Task.HGEMC_DEFINITION,
                messages,
                retry_on_missing_citations=False,
            )
            definition = _clean_definition(result.content)
        except AlbertError as exc:
            logger.warning("Définition HG-EMC — erreur Albert: %s", exc)
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
            logger.exception("Définition HG-EMC — erreur inattendue: %s", exc)
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


__all__ = ["register_route", "SYSTEM_PROMPT_HGEMC"]

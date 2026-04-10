"""
Rendu Markdown des réponses Albert.

Albert renvoie systématiquement du markdown (gras, listes, titres) plus des
séparateurs maison du type `===== FOND =====` qu'on demande explicitement
dans nos prompts. On normalise ces séparateurs en titres standard puis on
laisse `markdown` produire le HTML.

Sécurité : la sortie d'Albert est un input semi-fiable (LLM contrôlé par
nous, prompts qui interdisent toute injection HTML). On limite les
extensions markdown à `extra` (pas d'iframes, pas de raw HTML actif côté
ado). C'est suffisant pour le contexte (outil scolaire interne).
"""

from __future__ import annotations

import re

import markdown as md_lib

# Convertit les bannières maison `===== TITRE =====` en titres markdown.
# On accepte les variantes avec ou sans `**` autour, qui apparaissent souvent
# dans les sorties gpt-oss-120b.
_BANNER_RE = re.compile(
    r"^\s*\*{0,2}\s*={3,}\s*([^=]+?)\s*={3,}\s*\*{0,2}\s*$",
    re.MULTILINE,
)

# Lignes du genre `**Points forts**` (titre gras seul sur sa ligne).
# On les promeut en sous-titre `### …` pour un rendu plus aéré.
_BOLD_HEADING_RE = re.compile(r"^\s*\*\*([^*\n]{2,80})\*\*\s*$", re.MULTILINE)

# Pattern récurrent d'Albert : un item de liste niveau 1 dont le contenu est
# uniquement un titre en gras, suivi d'items numérotés/indentés. On veut
# l'afficher comme un sous-titre H3 puis une liste classique.
_BULLET_BOLD_HEADING_RE = re.compile(
    r"^[-*]\s+\*\*([^*\n]{2,80})\*\*\s*$",
    re.MULTILINE,
)

# Items numérotés ou à puce indentés de 2 ou 3 espaces : on les ramène à 0
# pour qu'ils deviennent une liste de niveau 1 standard. Markdown attend 4
# espaces pour de l'imbrication réelle ; comme on déstructure les sous-titres
# en H3 juste au-dessus, l'imbrication n'a plus lieu d'être.
_INDENTED_LIST_RE = re.compile(
    r"^ {2,3}((?:\d+\.|[-*])\s)",
    re.MULTILINE,
)


def _normalize_albert_markdown(text: str) -> str:
    """Pré-traite la sortie d'Albert avant le passage au moteur markdown."""
    out = _BANNER_RE.sub(lambda m: f"## {m.group(1).strip().title()}", text)
    out = _BULLET_BOLD_HEADING_RE.sub(lambda m: f"### {m.group(1).strip()}", out)
    out = _BOLD_HEADING_RE.sub(lambda m: f"### {m.group(1).strip()}", out)
    out = _INDENTED_LIST_RE.sub(r"\1", out)
    # Tirets décoratifs `---` seuls : laissés tels quels (markdown les transforme en <hr>).
    return out


_MD = md_lib.Markdown(
    extensions=["extra", "sane_lists", "nl2br"],
    output_format="html5",
)


def render_eval_markdown(text: str) -> str:
    """Rend une réponse Albert en HTML prêt à injecter dans un template.

    Le résultat doit être marqué `|safe` côté Jinja, ou wrappé en
    `markupsafe.Markup` côté caller.
    """
    if not text:
        return ""
    normalized = _normalize_albert_markdown(text.strip())
    _MD.reset()
    return _MD.convert(normalized)


__all__ = ["render_eval_markdown"]

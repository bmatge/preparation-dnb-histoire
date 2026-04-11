"""Scoring déterministe des sous-questions d'exercices « problèmes ».

La logique est identique à celle des automatismes (mêmes
``type_reponse``, mêmes normalisations, mêmes tolérances par défaut).
On expose simplement un alias ``check`` qui délègue à
``app.mathematiques.automatismes.scoring.check`` pour éviter la
duplication de code.

Si un jour les règles divergent (par exemple une tolérance par défaut
différente pour les sous-questions de problèmes), ce fichier sera
l'endroit naturel pour surcharger — mais pour l'instant on garde le
contrat commun.
"""

from __future__ import annotations

from typing import Any

from app.mathematiques.automatismes.scoring import (
    check as _auto_check,
    normalize_fraction,
    normalize_number,
    normalize_percentage,
)


def check(scoring: dict[str, Any], student_answer: str) -> bool:
    """Évalue si la réponse d'une sous-question est correcte.

    Délègue directement à la logique déterministe d'automatismes :
    mêmes types de réponse, mêmes conventions de tolérance, mêmes
    ``formes_acceptees``.
    """
    return _auto_check(scoring, student_answer)


__all__ = [
    "check",
    "normalize_number",
    "normalize_percentage",
    "normalize_fraction",
]

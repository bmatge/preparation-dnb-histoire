"""Loader / labels pour l'epreuve Simulation sciences.

Libelles humains des disciplines (partages avec le module revision mais
dupliques ici : pas d'import cross-epreuve).
"""

from __future__ import annotations


DISCIPLINE_LABELS: dict[str, str] = {
    "physique_chimie": "Physique-Chimie",
    "svt": "SVT",
    "technologie": "Technologie",
}


__all__ = [
    "DISCIPLINE_LABELS",
]

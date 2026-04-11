"""Loader / sélecteur d'exercices pour la sous-épreuve « problèmes ».

Sert d'aiguillage entre les routes (qui veulent lister les exercices
disponibles, éventuellement filtrés par thème) et
``models.list_exercises`` (qui fait la requête SQL bas niveau).

On garde ici les libellés humains des thèmes pour qu'ils soient
cohérents entre la page d'accueil de la sous-épreuve, les badges de la
page d'exercice et la synthèse.
"""

from __future__ import annotations

from sqlmodel import Session as DBSession

from app.mathematiques.problemes import models as prob_models


# Libellés affichés à l'élève pour chaque thème d'exercice.
# Ordre et clés alignés sur ``prob_models.ALLOWED_THEMES``.
THEME_LABELS: dict[str, str] = {
    "statistiques": "Statistiques",
    "probabilites": "Probabilités",
    "fonctions": "Fonctions",
    "geometrie": "Géométrie",
    "arithmetique": "Arithmétique",
    "grandeurs_mesures": "Grandeurs et mesures",
    "programmes_calcul": "Programmes de calcul",
}


def list_for_home(
    s: DBSession, theme: str | None = None
) -> list[prob_models.ProblemExercise]:
    """Renvoie les exercices à afficher sur la page d'accueil, filtrés.

    Pas de tirage aléatoire côté problèmes (contrairement aux
    automatismes) : l'élève choisit explicitement l'exercice sur lequel
    il veut travailler, parce que chaque exercice est long (5 à 15 min)
    et mérite d'être sélectionné plutôt que subi.
    """
    return prob_models.list_exercises(s, theme=theme)


__all__ = ["THEME_LABELS", "list_for_home"]

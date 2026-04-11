"""Loader / sélecteur de questions pour les quiz d'automatismes.

Sert d'aiguillage entre les routes (qui veulent N questions tirées au
sort filtrées par thème) et `models.random_questions_by_theme` (qui
fait la requête SQL bas niveau). On garde les libellés humains des
thèmes ici plutôt que dans le template, pour qu'ils soient cohérents
entre la page d'accueil, l'écran de quiz et la synthèse.
"""

from __future__ import annotations

from sqlmodel import Session as DBSession

from app.mathematiques.automatismes import models as auto_models


# Libellés affichés à l'élève. La clé est l'identifiant interne (cf.
# `ALLOWED_THEMES`), la valeur est le libellé court qui apparaît dans
# les listes déroulantes et les badges du quiz.
THEME_LABELS: dict[str, str] = {
    "calcul_numerique": "Calcul numérique",
    "calcul_litteral": "Calcul littéral",
    "fractions": "Fractions",
    "pourcentages_proportionnalite": "Pourcentages et proportionnalité",
    "stats_probas": "Statistiques et probabilités",
    "grandeurs_mesures": "Grandeurs et mesures",
    "geometrie_numerique": "Géométrie (numérique)",
    "programmes_calcul": "Programmes de calcul",
}


def pick_for_quiz(
    s: DBSession,
    n: int,
    theme: str | None = None,
) -> list[auto_models.AutoQuestion]:
    """Renvoie N questions tirées au sort, optionnellement filtrées par thème.

    Si `theme` est fourni mais que la banque n'a aucune question pour ce
    thème, la fonction retourne `[]` — la route appelante affichera un
    message d'erreur. On ne fait jamais de fallback silencieux vers
    « toutes » : ce serait surprenant pour l'élève.

    Si la banque a moins de N questions, on rend toutes celles disponibles
    (pas d'erreur). C'est utile en début de vie du corpus.
    """
    if n <= 0:
        return []
    return auto_models.random_questions_by_theme(s, n=n, theme=theme)


__all__ = ["THEME_LABELS", "pick_for_quiz"]

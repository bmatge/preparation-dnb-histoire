"""Loader / sélecteur de questions pour l'épreuve Révision sciences.

Deux responsabilités :

1. Tenir les libellés humains affichés à l'élève (disciplines et thèmes).
2. Aiguiller les routes vers `models.random_questions` pour tirer N
   questions filtrées par discipline et éventuellement par thème.

Les libellés vivent ici plutôt que dans les templates pour rester
cohérents entre la page d'accueil matière, la page d'accueil discipline,
l'écran de quiz et la synthèse.
"""

from __future__ import annotations

from sqlmodel import Session as DBSession

from app.sciences.revision import models as science_models


DISCIPLINE_LABELS: dict[str, str] = {
    "physique_chimie": "Physique-Chimie",
    "svt": "SVT",
    "technologie": "Technologie",
}


DISCIPLINE_SLUGS: dict[str, str] = {
    "physique_chimie": "physique-chimie",
    "svt": "svt",
    "technologie": "technologie",
}


DISCIPLINE_FROM_SLUG: dict[str, str] = {
    slug: discipline for discipline, slug in DISCIPLINE_SLUGS.items()
}


THEME_LABELS: dict[str, str] = {
    # Physique-Chimie
    "organisation_matiere": "Organisation de la matière",
    "mouvements_energie": "Mouvements et énergie",
    "electricite_signaux": "Électricité et signaux",
    "univers_melanges": "Univers, atomes et mélanges",
    # SVT
    "corps_sante": "Corps humain et santé",
    "terre_evolution": "Terre, environnement et évolution",
    "genetique": "Génétique et hérédité",
    # Technologie
    "objets_techniques": "Objets techniques et conception",
    "materiaux_innovation": "Matériaux et innovation",
    "programmation_robotique": "Programmation et robotique",
    "chaine_energie": "Chaîne d'énergie",
}


def pick_for_quiz(
    s: DBSession,
    n: int,
    discipline: str,
    theme: str | None = None,
    exclude_ids: list[str] | None = None,
    only_ids: list[str] | None = None,
) -> list[science_models.SciencesQuestionRow]:
    """Renvoie N questions tirées au sort pour une discipline, filtrées
    optionnellement par thème.

    Si la banque a moins de N questions, on rend toutes celles disponibles
    (pas d'erreur) — utile en début de vie du corpus. Si `theme` est
    fourni mais qu'aucune question ne matche, on retourne une liste vide
    (la route affichera un message d'erreur — pas de fallback silencieux
    vers « tous les thèmes »).
    """
    if n <= 0:
        return []
    return science_models.random_questions(
        s, n=n, discipline=discipline, theme=theme,
        exclude_ids=exclude_ids, only_ids=only_ids,
    )


__all__ = [
    "DISCIPLINE_LABELS",
    "DISCIPLINE_SLUGS",
    "DISCIPLINE_FROM_SLUG",
    "THEME_LABELS",
    "pick_for_quiz",
]

"""Épreuve « Révision par thème » (sciences DNB).

Parcours d'entraînement identique pour les trois disciplines
Physique-Chimie, SVT et Technologie : l'élève choisit une discipline
et éventuellement un thème, puis enchaîne 5 ou 10 questions courtes
(QCM, vrai/faux, calcul, définition) évaluées en hybride Python/Albert,
avec indices gradués 3× et révélation bienveillante.

Le dispatch par discipline est intégralement fait via un champ `discipline`
en DB et un path parameter dans l'URL — pas de duplication de code entre
les disciplines. On pourra extraire un module par discipline plus tard
si une divergence forte apparaît.
"""

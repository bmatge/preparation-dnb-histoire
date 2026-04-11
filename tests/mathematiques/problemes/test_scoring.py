"""Tests du scoring des sous-questions de problèmes.

Le scoring délègue à ``app.mathematiques.automatismes.scoring`` (déjà
testé en profondeur dans ``tests/mathematiques/automatismes/test_scoring.py``).
Ce fichier ne refait pas le tour complet des parsers numériques : il
vérifie simplement quelques cas-clés représentatifs des sous-questions
du corpus committé, pour garantir qu'un correctif côté automatismes ne
casse pas l'évaluation des problèmes.
"""

from __future__ import annotations

import pytest

from app.mathematiques.problemes import scoring as prob_scoring


# ============================================================================
# Cas directement tirés du corpus committé
# ============================================================================


CASES_CORRECTS = [
    # A1 Q1 : moyenne 64.29 kg (decimal, tol 0.05) — virgule française OK.
    (
        {
            "mode": "python",
            "type_reponse": "decimal",
            "reponse_canonique": "64.29",
            "tolerances": {"abs": 0.05},
        },
        "64,29",
    ),
    # Même question, sans virgule et avec l'unité → la forme acceptée
    # est prioritaire quand elle matche littéralement.
    (
        {
            "mode": "python",
            "type_reponse": "decimal",
            "reponse_canonique": "64.29",
            "tolerances": {"abs": 0.05},
            "formes_acceptees": ["64,29", "64.29", "64.28"],
        },
        "64.28",
    ),
    # A3 Q3 : antécédent -3/4 — le décimal -0.75 doit être accepté.
    (
        {
            "mode": "python",
            "type_reponse": "fraction",
            "reponse_canonique": "-3/4",
        },
        "-0,75",
    ),
    # A3 Q3 : 6/8 est aussi acceptable car la fraction se simplifie.
    (
        {
            "mode": "python",
            "type_reponse": "fraction",
            "reponse_canonique": "-3/4",
        },
        "-6/8",
    ),
    # B2 Q1 : probabilité 1/7 — l'élève écrit 3/21 (avant simplification).
    (
        {
            "mode": "python",
            "type_reponse": "fraction",
            "reponse_canonique": "1/7",
        },
        "3/21",
    ),
    # B4 Q1a : 7×13 — on accepte 13*7 via formes_acceptees + normalisation.
    (
        {
            "mode": "python",
            "type_reponse": "texte_court",
            "reponse_canonique": "7×13",
            "formes_acceptees": [
                "7×13",
                "7*13",
                "13×7",
                "13*7",
            ],
        },
        "13*7",
    ),
    # A2 Q2b : lettre C (texte_court) — ponctuation et casse tolérées.
    (
        {
            "mode": "python",
            "type_reponse": "texte_court",
            "reponse_canonique": "C",
            "formes_acceptees": ["C", "c", "C.", "reponse C"],
        },
        "c",
    ),
    # B3 Q1 : 2600 g (entier) — l'élève tape "2 600" (espace fin).
    (
        {
            "mode": "python",
            "type_reponse": "entier",
            "reponse_canonique": "2600",
        },
        "2 600",
    ),
    # A1 Q2b (pourcentage) : 32.7 avec tolérance 0.2 — 32.68 doit passer.
    (
        {
            "mode": "python",
            "type_reponse": "pourcentage",
            "reponse_canonique": "32.7",
            "tolerances": {"abs": 0.2},
        },
        "32,68",
    ),
]


CASES_INCORRECTS = [
    # Mauvais décimal loin de la réponse.
    (
        {
            "mode": "python",
            "type_reponse": "decimal",
            "reponse_canonique": "64.29",
            "tolerances": {"abs": 0.05},
        },
        "65",
    ),
    # Fraction qui ne simplifie pas vers -3/4.
    (
        {
            "mode": "python",
            "type_reponse": "fraction",
            "reponse_canonique": "-3/4",
        },
        "3/4",
    ),
    # Entier incorrect.
    (
        {
            "mode": "python",
            "type_reponse": "entier",
            "reponse_canonique": "2600",
        },
        "2400",
    ),
    # Réponse vide → False.
    (
        {
            "mode": "python",
            "type_reponse": "entier",
            "reponse_canonique": "7",
        },
        "",
    ),
    # Réponse non parsable numériquement.
    (
        {
            "mode": "python",
            "type_reponse": "decimal",
            "reponse_canonique": "64.29",
        },
        "beaucoup",
    ),
    # Mode albert : le scoring Python renvoie toujours False
    # (l'aiguillage vers Albert se fait dans pedagogy.evaluate_answer).
    (
        {
            "mode": "albert",
            "reponse_modele": "la masse totale en grammes",
            "criteres_validation": ["mentionne grammes"],
        },
        "la masse totale",
    ),
]


@pytest.mark.parametrize("scoring,answer", CASES_CORRECTS)
def test_check_accepte_reponses_correctes(scoring, answer):
    assert prob_scoring.check(scoring, answer) is True


@pytest.mark.parametrize("scoring,answer", CASES_INCORRECTS)
def test_check_rejette_reponses_incorrectes(scoring, answer):
    assert prob_scoring.check(scoring, answer) is False

"""Tests de ``ComprehensionExercise.flatten_items``.

Cette méthode déplie les questions et leurs sous-questions en une liste
linéaire numérotée 1..N, sur laquelle est branché le parcours élève
question-par-question. Toute régression ici décale ou casse la navigation.

Cas couverts :
- Question simple sans sous-questions → 1 item.
- Question avec sous-questions → N items.
- Numérotation ``order`` continue.
- Filtres ``include_grammar`` / ``include_reecriture`` /
  ``include_image_questions``.
- Composition de l'énoncé via ``_build_enonce_complet`` (préfixe parent
  conservé sauf si la sous-question le répète déjà).
- Héritage des champs ``passage_a_reecrire`` / ``contraintes`` /
  ``mots_soulignes`` depuis la question parente.
- ``label`` : ``"4a"`` pour une sous-question, ``"7"`` pour une question
  simple.
"""

from __future__ import annotations

import pytest

from app.francais.comprehension.models import (
    ComprehensionExercise,
    Epreuve,
    LignesCiblees,
    Ligne,
    Question,
    Source,
    SousQuestion,
    TexteSupport,
)


# ============================================================================
# Helpers
# ============================================================================


def _exercise(*questions: Question) -> ComprehensionExercise:
    """Construit un exercice minimal avec une liste de questions."""
    return ComprehensionExercise(
        id="test",
        source=Source(
            annee=2023, session="inconnu", centre="Métropole", code_sujet=None
        ),
        epreuve=Epreuve(
            intitule="Compréhension",
            duree_minutes=70,
            points_total=50,
            points_comprehension=32,
            points_grammaire=18,
        ),
        paratexte=None,
        texte_support=TexteSupport(
            auteur="Test",
            oeuvre="Test",
            genre="roman",
            lignes=[Ligne(n=1, texte="Phrase de test.")],
        ),
        notes_texte=[],
        image=None,
        questions=list(questions),
    )


def _q_simple(
    numero: str,
    *,
    partie: str = "comprehension",
    type_: str = "standard",
    necessite_image: bool = False,
    enonce: str = "Question simple ?",
    points: float = 4,
) -> Question:
    return Question(
        numero=numero,
        partie=partie,
        type=type_,
        enonce=enonce,
        points=points,
        competence="reperage_explicite",
        necessite_image=necessite_image,
    )


def _q_with_subs(
    numero: str,
    n_subs: int,
    *,
    enonce: str = "Énoncé principal de la question.",
) -> Question:
    return Question(
        numero=numero,
        partie="comprehension",
        type="standard",
        enonce=enonce,
        points=float(n_subs * 2),
        sous_questions=[
            SousQuestion(
                lettre=chr(ord("a") + i),
                enonce=f"Sous-question {chr(ord('a') + i)} ?",
                points=2.0,
                competence="comprehension_implicite",
            )
            for i in range(n_subs)
        ],
    )


# ============================================================================
# Tests
# ============================================================================


class TestFlattenItems:
    def test_single_simple_question_yields_one_item(self):
        exo = _exercise(_q_simple("1"))
        items = exo.flatten_items()

        assert len(items) == 1
        assert items[0].order == 1
        assert items[0].question_numero == "1"
        assert items[0].sous_question_lettre is None
        assert items[0].label == "1"

    def test_question_with_subs_yields_n_items(self):
        exo = _exercise(_q_with_subs("4", 3))
        items = exo.flatten_items()

        assert len(items) == 3
        assert [it.label for it in items] == ["4a", "4b", "4c"]
        assert [it.order for it in items] == [1, 2, 3]

    def test_order_is_continuous_across_questions(self):
        exo = _exercise(
            _q_simple("1"),
            _q_with_subs("2", 2),
            _q_simple("3"),
            _q_with_subs("4", 3),
        )
        items = exo.flatten_items()

        # 1 + 2 + 1 + 3 = 7 items
        assert len(items) == 7
        assert [it.order for it in items] == [1, 2, 3, 4, 5, 6, 7]
        assert [it.label for it in items] == [
            "1", "2a", "2b", "3", "4a", "4b", "4c"
        ]

    def test_include_grammar_false_filters_grammar(self):
        exo = _exercise(
            _q_simple("1", partie="comprehension"),
            _q_simple("2", partie="grammaire"),
            _q_simple("3", partie="comprehension"),
        )
        items = exo.flatten_items(include_grammar=False)

        assert [it.question_numero for it in items] == ["1", "3"]

    def test_include_reecriture_false_filters_reecriture(self):
        exo = _exercise(
            _q_simple("1"),
            _q_simple("8", partie="grammaire", type_="reecriture"),
        )
        items = exo.flatten_items(include_reecriture=False)

        assert [it.question_numero for it in items] == ["1"]

    def test_image_questions_excluded_by_default(self):
        exo = _exercise(
            _q_simple("1"),
            _q_simple("5", necessite_image=True),
            _q_simple("6"),
        )
        items = exo.flatten_items()

        # Question 5 doit être filtrée (image pas encore branchée par défaut),
        # mais l'order ne saute pas pour autant — il reste continu.
        assert [it.question_numero for it in items] == ["1", "6"]
        assert [it.order for it in items] == [1, 2]

    def test_include_image_questions_true_keeps_them(self):
        exo = _exercise(
            _q_simple("1"),
            _q_simple("5", necessite_image=True),
        )
        items = exo.flatten_items(include_image_questions=True)
        assert [it.question_numero for it in items] == ["1", "5"]

    def test_enonce_complet_prefixes_parent_when_distinct(self):
        # L'énoncé parent contient un contexte qui n'est pas dans la
        # sous-question → on doit le préfixer.
        q = Question(
            numero="3",
            partie="comprehension",
            type="standard",
            enonce='« En de certains endroits, elle était fort profonde. »',
            points=4.0,
            sous_questions=[
                SousQuestion(
                    lettre="a",
                    enonce="Quel sentiment cette phrase évoque-t-elle ?",
                    points=2.0,
                    competence="interpretation",
                ),
            ],
        )
        exo = _exercise(q)
        items = exo.flatten_items()

        assert len(items) == 1
        # L'énoncé complet contient à la fois le contexte parent et la
        # formulation de la sous-question.
        assert "fort profonde" in items[0].enonce_complet
        assert "Quel sentiment" in items[0].enonce_complet

    def test_enonce_complet_does_not_duplicate_when_sub_repeats_parent(self):
        # Si la sous-question répète déjà tout le contexte parent, on ne
        # préfixe pas (sinon on aurait du contenu en double).
        parent = "Cite trois mots du champ lexical de la peur."
        q = Question(
            numero="2",
            partie="comprehension",
            type="standard",
            enonce=parent,
            points=2.0,
            sous_questions=[
                SousQuestion(
                    lettre="a",
                    enonce=parent + " Justifie ton choix.",
                    points=2.0,
                    competence="champ_lexical",
                ),
            ],
        )
        items = _exercise(q).flatten_items()
        assert items[0].enonce_complet.count("champ lexical") == 1

    def test_reecriture_fields_inherited_from_parent(self):
        # Sur une question de réécriture avec sous-questions, le passage à
        # réécrire et les contraintes vivent au niveau parent — chaque item
        # déplié doit en hériter.
        q = Question(
            numero="9",
            partie="grammaire",
            type="reecriture",
            enonce="Réécris ce passage en respectant les contraintes.",
            passage_a_reecrire="Il marchait lentement dans la rue.",
            contraintes=["mettre au passé simple", "féminin pluriel"],
            mots_soulignes=["marchait"],
            points=10.0,
            sous_questions=[
                SousQuestion(
                    lettre="a", enonce="Réécris.", points=8.0, competence="reecriture"
                ),
                SousQuestion(
                    lettre="b",
                    enonce="Quel sentiment cela exprime-t-il ?",
                    points=2.0,
                    competence="interpretation",
                ),
            ],
        )
        items = _exercise(q).flatten_items()
        assert len(items) == 2
        for it in items:
            assert it.passage_a_reecrire == "Il marchait lentement dans la rue."
            assert it.contraintes == ["mettre au passé simple", "féminin pluriel"]
            assert it.mots_soulignes == ["marchait"]

    def test_competence_falls_back_to_parent_when_sub_has_none(self):
        # Si une sous-question n'a pas de compétence taguée, on retombe sur
        # celle du parent.
        q = Question(
            numero="6",
            partie="comprehension",
            type="standard",
            enonce="…",
            competence="interpretation",
            points=4.0,
            sous_questions=[
                SousQuestion(
                    lettre="a", enonce="…", points=2.0, competence=None
                ),
                SousQuestion(
                    lettre="b",
                    enonce="…",
                    points=2.0,
                    competence="champ_lexical",
                ),
            ],
        )
        items = _exercise(q).flatten_items()
        assert items[0].competence == "interpretation"  # héritée du parent
        assert items[1].competence == "champ_lexical"   # propre

    def test_lignes_ciblees_propagated(self):
        q = _q_simple("3")
        q.lignes_ciblees = [LignesCiblees(start=15, end=18)]
        items = _exercise(q).flatten_items()
        assert items[0].lignes_ciblees == [LignesCiblees(start=15, end=18)]

    def test_empty_questions_list(self):
        exo = _exercise()
        assert exo.flatten_items() == []

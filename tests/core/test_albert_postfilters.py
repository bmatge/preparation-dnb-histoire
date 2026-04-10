"""Tests des post-filtres de sécurité du client Albert.

Cibles :
- ``_looks_like_ghostwritten_dc`` : doit rattraper une rédaction fournie à
  la place de l'élève (gotcha §5.3 HANDOFF) sans bloquer les corrections
  légitimes.
- ``_has_citations`` : doit reconnaître les marqueurs ``[programme]``,
  ``[corrigé]``, ``[méthodo]`` et leurs variantes accentuées.

Ces deux fonctions sont les garde-fous les plus critiques du runtime —
toute régression ici a déjà coûté de la qualité de tutorat en prod.
"""

from __future__ import annotations

from app.core.albert_client import _has_citations, _looks_like_ghostwritten_dc


# ============================================================================
# _looks_like_ghostwritten_dc
# ============================================================================


class TestLooksLikeGhostwrittenDc:
    def test_short_response_is_never_ghostwriting(self):
        # Sous le seuil de 300 caractères → toujours OK même sans markers.
        text = "Berlin a été divisé en quatre secteurs à la fin de la guerre."
        assert _looks_like_ghostwritten_dc(text) is False

    def test_long_narrative_without_interaction_is_flagged(self):
        # Cas typique de la rédaction à la place de l'élève : prose narrative
        # à la 3e personne, aucun "tu", aucune question, aucun marker de
        # structure de correction.
        text = (
            "La guerre froide oppose les États-Unis à l'URSS de 1947 à 1991. "
            "Berlin devient le symbole de cette opposition avec la "
            "construction du mur en 1961. La crise de Cuba en 1962 fait "
            "trembler le monde. La détente s'installe ensuite avec les "
            "accords SALT. Finalement, le mur tombe en 1989 et l'URSS "
            "disparaît en 1991. Cette guerre froide a profondément "
            "transformé les relations internationales du XXe siècle. "
            "Elle a créé un monde bipolaire et une course aux armements."
        )
        assert _looks_like_ghostwritten_dc(text) is True

    def test_correction_with_structure_markers_is_not_flagged(self):
        # Une vraie correction d'Albert contient toujours `===== FOND =====`
        # ou des labels de la grille → ne doit pas être flaggée même si
        # longue.
        text = (
            "===== FOND =====\n\n"
            "Adéquation au sujet : tu réponds bien à la question posée. "
            "Connaissances : la chronologie est correcte mais il manque la "
            "crise de Cuba. Bornes : OK. ===== FORME ===== Introduction : "
            "présente bien le sujet. Plan apparent : trois parties "
            "distinctes, c'est bien. Connecteurs logiques : à étoffer."
        )
        assert _looks_like_ghostwritten_dc(text) is False

    def test_socratic_questions_are_not_flagged(self):
        # Une réponse pleine de "tu" et de "?" est interactive donc OK
        # même sans markers de structure.
        text = (
            "Tu as bien identifié la division de Berlin en 1945. As-tu "
            "pensé à expliquer pourquoi les Alliés ont choisi cette "
            "solution ? Et qu'est-ce que cela révèle sur les tensions "
            "naissantes entre l'URSS et les États-Unis ? Ta proposition "
            "gagnerait à creuser ce point. Est-ce que tu vois ce que ça "
            "change pour la suite ?"
        )
        assert _looks_like_ghostwritten_dc(text) is False

    def test_borderline_300_chars_no_markers_is_flagged(self):
        # Juste au-dessus du seuil, narratif pur, doit être flaggé.
        text = (
            "Le développement de Berlin entre 1945 et 1989 illustre toute "
            "la guerre froide. La ville est partagée en quatre secteurs "
            "d'occupation par les Alliés. Le blocus de 1948 marque la "
            "première grande crise européenne. La construction du mur en "
            "1961 fige la situation pour près de trente ans. La chute du "
            "mur en 1989 ouvre la fin du conflit bipolaire."
        )
        assert len(text) > 300
        assert _looks_like_ghostwritten_dc(text) is True


# ============================================================================
# _has_citations
# ============================================================================


class TestHasCitations:
    def test_programme_citation(self):
        assert _has_citations("Tu as bien mobilisé la notion [programme].")

    def test_corrige_citation_with_year(self):
        assert _has_citations("Cf. la structure du [corrigé 2018] sur ce sujet.")

    def test_methodo_citation(self):
        assert _has_citations(
            "Pense à équilibrer tes parties [méthodo MrDarras]."
        )

    def test_methodo_without_accent(self):
        # Le pattern doit aussi matcher "methodo" sans accent (variantes
        # courantes des modèles).
        assert _has_citations("Cf. [methodo].")

    def test_corrige_without_accent(self):
        assert _has_citations("Le [corrige] mentionne ce point.")

    def test_no_citation(self):
        assert not _has_citations(
            "Tu as bien identifié les principaux acteurs de cette période."
        )

    def test_empty(self):
        assert not _has_citations("")

    def test_brackets_without_keyword(self):
        # Des crochets quelconques ne suffisent pas — il faut le mot-clé.
        assert not _has_citations("À voir entre [parenthèses] ou non.")

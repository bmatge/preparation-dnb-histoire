"""Tests de présence de la FAB « Outils » (calculette) côté mathématiques.

La FAB est un ajout transverse à toutes les pages mathématiques (index
matière + les deux sous-épreuves). On vérifie qu'elle est bien injectée
sur les pages maths, et qu'elle n'apparaît PAS sur les autres matières
(pour ne pas imposer son contexte JS).
"""

from __future__ import annotations


FAB_MARKER = 'id="math-fab-root"'


# ============================================================================
# Présence de la FAB sur les pages maths
# ============================================================================


class TestFabPresentSurPagesMaths:
    def test_index_matiere(self, test_client):
        r = test_client.get("/mathematiques/")
        assert r.status_code == 200
        assert FAB_MARKER in r.text

    def test_automatismes_home(self, test_client):
        r = test_client.get("/mathematiques/automatismes/")
        assert r.status_code == 200
        assert FAB_MARKER in r.text

    def test_problemes_home(self, test_client):
        r = test_client.get("/mathematiques/problemes/")
        assert r.status_code == 200
        assert FAB_MARKER in r.text

    def test_problemes_travail(self, test_client):
        """La FAB doit aussi être présente pendant le travail sur un exercice,
        pour que l'élève puisse ouvrir la calculette en pleine résolution."""
        test_client.get(
            "/mathematiques/problemes/start/prob_2026A_ex2",
            follow_redirects=False,
        )
        r = test_client.get("/mathematiques/problemes/travail")
        assert r.status_code == 200
        assert FAB_MARKER in r.text


# ============================================================================
# Absence sur les autres matières
# ============================================================================


class TestFabAbsenteSurAutresMatieres:
    def test_hgemc_index(self, test_client):
        r = test_client.get("/histoire-geo-emc/")
        assert r.status_code == 200
        assert FAB_MARKER not in r.text

    def test_francais_index(self, test_client):
        r = test_client.get("/francais/")
        assert r.status_code == 200
        assert FAB_MARKER not in r.text

    def test_accueil_global(self, test_client):
        """L'accueil global (sélecteur de matière) n'affiche pas la FAB
        (on ne sait pas encore quelle matière l'élève va choisir)."""
        r = test_client.get("/")
        assert r.status_code == 200
        assert FAB_MARKER not in r.text

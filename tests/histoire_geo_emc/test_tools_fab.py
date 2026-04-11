"""Tests du bouton flottant « Outils » côté histoire-géographie-EMC.

Couvre :
- la présence du marqueur FAB (`id="hgemc-fab-root"`) sur toutes les
  pages HG-EMC (index matière + développement construit + repères) ;
- son absence sur les autres matières et sur l'accueil global ;
- le endpoint HTMX `/histoire-geo-emc/outils/definition` : input
  vide, appel Albert mocké, erreur Albert rattrapée, préfixe
  parasite nettoyé.

Le singleton `app.histoire_geo_emc.outils._albert_client` est
monkeypatché dans `conftest.py` par le fake partagé.
"""

from __future__ import annotations

from app.core.albert_client import AlbertError, Task


FAB_MARKER = 'id="hgemc-fab-root"'


# ============================================================================
# Présence de la FAB sur les pages HG-EMC
# ============================================================================


class TestFabPresentSurPagesHgemc:
    def test_index_matiere(self, test_client):
        r = test_client.get("/histoire-geo-emc/")
        assert r.status_code == 200
        assert FAB_MARKER in r.text

    def test_dc_home(self, test_client):
        r = test_client.get("/histoire-geo-emc/developpement-construit/")
        assert r.status_code == 200
        assert FAB_MARKER in r.text

    def test_reperes_index(self, test_client):
        r = test_client.get("/histoire-geo-emc/reperes/")
        assert r.status_code == 200
        assert FAB_MARKER in r.text


# ============================================================================
# Absence de la FAB sur les autres matières
# ============================================================================


class TestFabAbsenteSurAutresMatieres:
    def test_francais_index(self, test_client):
        r = test_client.get("/francais/")
        assert r.status_code == 200
        assert FAB_MARKER not in r.text

    def test_mathematiques_index(self, test_client):
        r = test_client.get("/mathematiques/")
        assert r.status_code == 200
        assert FAB_MARKER not in r.text

    def test_accueil_global(self, test_client):
        r = test_client.get("/")
        assert r.status_code == 200
        assert FAB_MARKER not in r.text


# ============================================================================
# Endpoint /histoire-geo-emc/outils/definition
# ============================================================================


class TestEndpointDefinition:
    def test_input_vide_renvoie_erreur(self, test_client, fake_albert):
        r = test_client.post(
            "/histoire-geo-emc/outils/definition",
            data={"term": ""},
        )
        assert r.status_code == 200
        assert "Tape un mot" in r.text
        assert len(fake_albert.calls) == 0

    def test_definition_renvoie_fragment(self, test_client, fake_albert):
        fake_albert.queue_response(
            "Période de tensions entre les États-Unis et l'URSS de 1947 à 1991."
        )
        r = test_client.post(
            "/histoire-geo-emc/outils/definition",
            data={"term": "guerre froide"},
        )
        assert r.status_code == 200
        assert "guerre froide" in r.text
        assert "1947" in r.text
        assert len(fake_albert.calls) == 1
        assert fake_albert.calls[0].task == Task.HGEMC_DEFINITION

    def test_nettoyage_balises_html(self, test_client, fake_albert):
        fake_albert.queue_response("Séparation du politique et du religieux.")
        r = test_client.post(
            "/histoire-geo-emc/outils/definition",
            data={"term": "<b>laïcité</b>"},
        )
        assert r.status_code == 200
        assert len(fake_albert.calls) == 1
        sent_user_msg = fake_albert.calls[0].messages[-1]["content"]
        assert "<b>" not in sent_user_msg
        assert "laïcité" in sent_user_msg

    def test_erreur_albert_message_francais(self, test_client, fake_albert):
        fake_albert.queue_exception(AlbertError("panne"))
        r = test_client.post(
            "/histoire-geo-emc/outils/definition",
            data={"term": "décolonisation"},
        )
        assert r.status_code == 200
        assert "réessaie" in r.text
        assert "Traceback" not in r.text
        assert "AlbertError" not in r.text

    def test_troncature_terme_long(self, test_client, fake_albert):
        """Un terme > 80 caractères est tronqué avant envoi à Albert."""
        fake_albert.queue_response("Définition courte.")
        long_term = "a" * 200
        r = test_client.post(
            "/histoire-geo-emc/outils/definition",
            data={"term": long_term},
        )
        assert r.status_code == 200
        sent = fake_albert.calls[0].messages[-1]["content"]
        assert len(sent) <= 80

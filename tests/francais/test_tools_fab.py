"""Tests du bouton flottant « Outils » côté français.

Couvre :
- la présence du marqueur FAB (`id="fr-fab-root"`) sur toutes les pages
  français (index matière + sous-épreuves compréhension, dictée,
  rédaction) ;
- son absence sur les autres matières et sur l'accueil global ;
- le endpoint HTMX `/francais/outils/definition` : input vide, appel
  Albert mocké qui renvoie une définition, erreur Albert rattrapée en
  message français, input nettoyé des balises HTML.

Le singleton `app.francais.outils._albert_client` est monkeypatché dans
`conftest.py` par le fake partagé — les tests du endpoint consomment
la même file `queued_responses` que les autres suites.
"""

from __future__ import annotations

from app.core.albert_client import AlbertError, Task


FAB_MARKER = 'id="fr-fab-root"'


# ============================================================================
# Présence de la FAB sur les pages français
# ============================================================================


class TestFabPresentSurPagesFrancais:
    def test_index_matiere(self, test_client):
        r = test_client.get("/francais/")
        assert r.status_code == 200
        assert FAB_MARKER in r.text

    def test_comprehension_home(self, test_client):
        r = test_client.get("/francais/comprehension/")
        assert r.status_code == 200
        assert FAB_MARKER in r.text

    def test_dictee_home(self, test_client):
        r = test_client.get("/francais/dictee/")
        assert r.status_code == 200
        assert FAB_MARKER in r.text

    def test_redaction_home(self, test_client):
        r = test_client.get("/francais/redaction/")
        assert r.status_code == 200
        assert FAB_MARKER in r.text


# ============================================================================
# Absence de la FAB sur les autres matières
# ============================================================================


class TestFabAbsenteSurAutresMatieres:
    def test_hgemc_index(self, test_client):
        r = test_client.get("/histoire-geo-emc/")
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
# Endpoint /francais/outils/definition
# ============================================================================


class TestEndpointDefinition:
    def test_input_vide_renvoie_erreur(self, test_client, fake_albert):
        r = test_client.post("/francais/outils/definition", data={"term": "   "})
        assert r.status_code == 200
        assert "Tape un mot" in r.text
        # Pas d'appel Albert sur input vide.
        assert len(fake_albert.calls) == 0

    def test_definition_renvoie_fragment(self, test_client, fake_albert):
        fake_albert.queue_response(
            "Vers de douze syllabes, classique en poésie française."
        )
        r = test_client.post(
            "/francais/outils/definition",
            data={"term": "alexandrin"},
        )
        assert r.status_code == 200
        assert "alexandrin" in r.text
        assert "douze syllabes" in r.text
        # La bonne task a bien été utilisée.
        assert len(fake_albert.calls) == 1
        assert fake_albert.calls[0].task == Task.FR_DEFINITION

    def test_nettoyage_balises_html(self, test_client, fake_albert):
        """Le terme est nettoyé des balises HTML avant envoi à Albert."""
        fake_albert.queue_response("Figure de style : accumulation rapide de mots.")
        r = test_client.post(
            "/francais/outils/definition",
            data={"term": "<script>alert(1)</script>asyndète"},
        )
        assert r.status_code == 200
        assert len(fake_albert.calls) == 1
        sent_user_msg = fake_albert.calls[0].messages[-1]["content"]
        assert "<script>" not in sent_user_msg
        assert "asyndète" in sent_user_msg

    def test_erreur_albert_message_francais(self, test_client, fake_albert):
        fake_albert.queue_exception(AlbertError("panne"))
        r = test_client.post(
            "/francais/outils/definition",
            data={"term": "métonymie"},
        )
        assert r.status_code == 200
        # Message utilisateur en français, pas de stack trace.
        assert "réessaie" in r.text
        assert "Traceback" not in r.text
        assert "AlbertError" not in r.text

    def test_prefixe_parasite_retire(self, test_client, fake_albert):
        """Le filtrage de post-traitement retire les amorces parasites."""
        fake_albert.queue_response(
            "Voici la définition : image qui relie deux idées éloignées."
        )
        r = test_client.post(
            "/francais/outils/definition",
            data={"term": "métaphore filée"},
        )
        assert r.status_code == 200
        assert "Voici la définition" not in r.text
        assert "image qui relie" in r.text

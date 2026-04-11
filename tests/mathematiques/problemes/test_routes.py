"""Smoke tests d'intégration des routes ``/mathematiques/problemes/*``.

Couvre le plumbing du parcours élève : index matière → accueil épreuve →
choix d'un exercice → première sous-question → réponse correcte (scoring
déterministe Python) → indice (fallback Albert) → reveal (fallback
Albert) → synthèse. Pas d'appel Albert réel : le ``FakeAlbertClient``
du conftest renvoie une chaîne canned.
"""

from __future__ import annotations


PREFIX = "/mathematiques/problemes"


# ============================================================================
# Index matière + accueil épreuve
# ============================================================================


class TestPagesAccueil:
    def test_accueil_epreuve_repond_200(self, test_client):
        r = test_client.get(f"{PREFIX}/")
        assert r.status_code == 200
        assert "Raisonnement" in r.text

    def test_accueil_liste_les_exercices(self, test_client):
        """L'accueil doit lister au moins un des exercices committés."""
        r = test_client.get(f"{PREFIX}/")
        assert r.status_code == 200
        # Au moins un titre d'exercice visible.
        assert "Programme de calcul" in r.text or "PGCD" in r.text

    def test_filtre_par_theme(self, test_client):
        r = test_client.get(f"{PREFIX}/?theme=arithmetique")
        assert r.status_code == 200
        # Le filtre est appliqué : on voit l'exercice B4 (club sciences).
        assert "Club sciences" in r.text

    def test_index_matiere_liste_problemes(self, test_client):
        """L'index matière doit maintenant proposer les deux épreuves."""
        r = test_client.get("/mathematiques/")
        assert r.status_code == 200
        assert "problemes" in r.text.lower() or "Problèmes" in r.text


# ============================================================================
# Démarrage d'un exercice
# ============================================================================


def _start_exercise(test_client, exercise_id: str) -> None:
    """Démarre l'exercice et navigue jusqu'à /travail."""
    r = test_client.get(
        f"{PREFIX}/start/{exercise_id}", follow_redirects=False
    )
    assert r.status_code == 303
    assert "/travail" in r.headers["location"]
    r = test_client.get(f"{PREFIX}/travail")
    assert r.status_code == 200


class TestDemarrageExercice:
    def test_start_exercice_existant_redirige_vers_travail(self, test_client):
        r = test_client.get(
            f"{PREFIX}/start/prob_2026A_ex2", follow_redirects=False
        )
        assert r.status_code == 303
        assert "/travail" in r.headers["location"]

    def test_start_exercice_inconnu_renvoie_erreur(self, test_client):
        r = test_client.get(
            f"{PREFIX}/start/exercice_fantome", follow_redirects=False
        )
        assert r.status_code == 303
        assert "erreur" in r.headers["location"]

    def test_travail_affiche_premiere_sous_question(self, test_client):
        _start_exercise(test_client, "prob_2026A_ex2")
        r = test_client.get(f"{PREFIX}/travail")
        assert r.status_code == 200
        # La page contient le titre de l'exercice et un input/textarea pour
        # la réponse.
        assert "Programme de calcul" in r.text
        assert 'name="answer"' in r.text


# ============================================================================
# Réponse, indice, reveal
# ============================================================================


class TestTravailAnswer:
    def test_reponse_vide_renvoie_message_erreur(self, test_client):
        _start_exercise(test_client, "prob_2026A_ex2")
        r = test_client.post(
            f"{PREFIX}/travail/answer", data={"answer": "   "}
        )
        assert r.status_code == 200
        assert (
            "Écris une réponse" in r.text
            or "réponse" in r.text.lower()
        )

    def test_reponse_correcte_marque_succes(self, test_client):
        """Sur l'exercice 2026A_ex2 Q1, la bonne réponse est 55
        (scoring Python déterministe, pas d'appel Albert nécessaire)."""
        _start_exercise(test_client, "prob_2026A_ex2")
        r = test_client.post(
            f"{PREFIX}/travail/answer", data={"answer": "55"}
        )
        assert r.status_code == 200
        # Le feedback doit être "correct" : fond vert (classe emerald) +
        # bouton "sous-question suivante". Le texte du message est
        # aléatoire (random_positive_feedback), on ne l'assertie pas
        # littéralement.
        assert "bg-emerald-50" in r.text
        assert "suivante" in r.text.lower()

    def test_reponse_incorrecte_ne_marque_pas_succes(self, test_client):
        _start_exercise(test_client, "prob_2026A_ex2")
        r = test_client.post(
            f"{PREFIX}/travail/answer", data={"answer": "999"}
        )
        assert r.status_code == 200
        # Pas de bouton "suivante" (on reste sur la même sous-question)
        assert "suivante" not in r.text.lower()


class TestTravailHint:
    def test_indice_repond_200_avec_fallback(self, test_client):
        """Le ``FakeAlbertClient`` retourne sa réponse par défaut, donc
        l'appel passe et on doit recevoir un fragment d'indice."""
        _start_exercise(test_client, "prob_2026A_ex2")
        r = test_client.post(f"{PREFIX}/travail/hint", data={})
        assert r.status_code == 200
        assert "Indice" in r.text


class TestTravailReveal:
    def test_reveal_repond_200(self, test_client):
        _start_exercise(test_client, "prob_2026A_ex2")
        r = test_client.post(f"{PREFIX}/travail/reveal", data={})
        assert r.status_code == 200


# ============================================================================
# Synthèse + restart
# ============================================================================


class TestSynthese:
    def test_synthese_redirige_si_pas_de_travail(self, test_client):
        r = test_client.get(
            f"{PREFIX}/travail/synthese", follow_redirects=False
        )
        assert r.status_code == 303

    def test_restart_efface_letat(self, test_client):
        _start_exercise(test_client, "prob_2026A_ex2")
        r = test_client.get(f"{PREFIX}/restart", follow_redirects=False)
        assert r.status_code == 303
        # Après le restart, /travail redirige vers /
        r2 = test_client.get(f"{PREFIX}/travail", follow_redirects=False)
        assert r2.status_code == 303

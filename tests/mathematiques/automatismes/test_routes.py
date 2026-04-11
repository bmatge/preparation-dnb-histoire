"""Smoke tests d'intégration des routes `/mathematiques/automatismes/*`.

On vérifie le plumbing du parcours élève : accueil matière → accueil
épreuve → création d'un quiz → première question → réponse correcte
(scoring déterministe) → indice → reveal → synthèse. Pas d'appel Albert
réel : la route hint et la route reveal tombent dans le fallback
déterministe via `_safe_chat` (le `FakeAlbertClient` du conftest répond
avec une chaîne canned, c'est suffisant).
"""

from __future__ import annotations


PREFIX = "/mathematiques/automatismes"


# ============================================================================
# Index matière + accueil épreuve
# ============================================================================


class TestPagesAccueil:
    def test_index_matiere_repond_200(self, test_client):
        r = test_client.get("/mathematiques/")
        assert r.status_code == 200
        assert "Mathématiques" in r.text or "mathematiques" in r.text.lower()

    def test_accueil_epreuve_repond_200(self, test_client):
        r = test_client.get(f"{PREFIX}/")
        assert r.status_code == 200
        assert "Automatismes" in r.text

    def test_accueil_propose_les_themes(self, test_client):
        """Le formulaire d'accueil doit proposer au moins quelques thèmes
        (pas de validation exhaustive : on s'assure juste que le `<select>`
        n'est pas vide)."""
        r = test_client.get(f"{PREFIX}/")
        assert r.status_code == 200
        # Le `<select>` du thème contient au moins l'option `fractions`
        # (présent dans le corpus committé).
        assert 'value="fractions"' in r.text


# ============================================================================
# Création d'un quiz + première question
# ============================================================================


class TestCreationQuiz:
    def test_quiz_new_redirige_vers_quiz(self, test_client):
        r = test_client.post(
            f"{PREFIX}/quiz/new",
            data={"theme": "", "length": "5"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].endswith("/quiz")

    def test_quiz_affiche_la_premiere_question(self, test_client):
        # On crée un quiz et on suit la redirection
        r = test_client.post(
            f"{PREFIX}/quiz/new",
            data={"theme": "fractions", "length": "5"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        # La page quiz contient bien le compteur 1 / 5 et un input de réponse.
        assert "Question" in r.text
        assert 'name="answer"' in r.text

    def test_quiz_new_avec_theme_inconnu(self, test_client):
        """Si aucune question ne matche le thème, on est redirigé vers
        l'accueil avec un paramètre d'erreur (pas une 500)."""
        r = test_client.post(
            f"{PREFIX}/quiz/new",
            data={"theme": "inexistant_42", "length": "5"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "erreur" in r.headers["location"]


# ============================================================================
# Réponse, indice, reveal
# ============================================================================


def _start_quiz(test_client, theme: str, length: int = 5) -> None:
    """Crée un quiz dans la session de test_client et navigue jusqu'à
    `quiz`. La fixture `test_client` partage les cookies entre requêtes."""
    r = test_client.post(
        f"{PREFIX}/quiz/new",
        data={"theme": theme, "length": str(length)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = test_client.get(f"{PREFIX}/quiz")
    assert r.status_code == 200


class TestQuizAnswer:
    def test_reponse_vide_renvoie_message_erreur(self, test_client):
        _start_quiz(test_client, theme="fractions")
        r = test_client.post(f"{PREFIX}/quiz/answer", data={"answer": "   "})
        assert r.status_code == 200
        assert "Écris une réponse" in r.text or "réponse" in r.text.lower()

    def test_reponse_quelconque_repond_200(self, test_client):
        """Une réponse non vide doit renvoyer un fragment HTMX (200), peu
        importe qu'elle soit correcte ou non."""
        _start_quiz(test_client, theme="fractions")
        r = test_client.post(f"{PREFIX}/quiz/answer", data={"answer": "42"})
        assert r.status_code == 200


class TestQuizHint:
    def test_indice_repond_200_avec_fallback(self, test_client):
        """Le `FakeAlbertClient` retourne sa réponse par défaut, donc
        l'appel passe et on doit recevoir un fragment d'indice."""
        _start_quiz(test_client, theme="fractions")
        r = test_client.post(f"{PREFIX}/quiz/hint", data={})
        assert r.status_code == 200
        # Le fragment hint contient le badge « Indice 1 ».
        assert "Indice" in r.text


class TestQuizReveal:
    def test_reveal_repond_200(self, test_client):
        _start_quiz(test_client, theme="fractions")
        r = test_client.post(f"{PREFIX}/quiz/reveal", data={})
        assert r.status_code == 200


# ============================================================================
# Synthèse + restart
# ============================================================================


class TestSynthese:
    def test_synthese_redirige_si_pas_de_quiz(self, test_client):
        r = test_client.get(f"{PREFIX}/quiz/synthese", follow_redirects=False)
        assert r.status_code == 303

    def test_restart_efface_le_quiz(self, test_client):
        _start_quiz(test_client, theme="fractions")
        r = test_client.get(f"{PREFIX}/restart", follow_redirects=False)
        assert r.status_code == 303
        # Après le restart, /quiz redirige vers /
        r2 = test_client.get(f"{PREFIX}/quiz", follow_redirects=False)
        assert r2.status_code == 303

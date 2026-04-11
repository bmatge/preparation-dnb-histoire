"""Smoke tests d'intégration des routes `/sciences/revision/*`.

Plumbing du parcours élève : accueil matière → index de l'épreuve
(3 cartes disciplines) → accueil discipline → création d'un quiz →
première question → réponse → indice → reveal → synthèse → restart.
Pas d'appel Albert réel : le `FakeAlbertClient` du conftest retourne
une chaîne canned, suffisante pour vérifier les status codes et la
structure des fragments HTMX.
"""

from __future__ import annotations


PREFIX = "/sciences/revision"
PC_SLUG = "physique-chimie"
SVT_SLUG = "svt"


# ============================================================================
# Pages d'accueil
# ============================================================================


class TestPagesAccueil:
    def test_index_matiere_repond_200(self, test_client):
        r = test_client.get("/sciences/")
        assert r.status_code == 200
        assert "Sciences" in r.text

    def test_index_epreuve_repond_200(self, test_client):
        r = test_client.get(f"{PREFIX}/")
        assert r.status_code == 200
        # Les 3 disciplines sont listées.
        assert "Physique-Chimie" in r.text
        assert "SVT" in r.text
        assert "Technologie" in r.text

    def test_accueil_discipline_repond_200(self, test_client):
        r = test_client.get(f"{PREFIX}/{PC_SLUG}/")
        assert r.status_code == 200
        assert "Physique-Chimie" in r.text

    def test_accueil_discipline_slug_inconnu_404(self, test_client):
        r = test_client.get(f"{PREFIX}/discipline-qui-n-existe-pas/")
        assert r.status_code == 404

    def test_accueil_propose_les_themes(self, test_client):
        r = test_client.get(f"{PREFIX}/{PC_SLUG}/")
        assert r.status_code == 200
        # Au moins un thème PC attendu dans le sélecteur.
        assert 'value="mouvements_energie"' in r.text


# ============================================================================
# Création d'un quiz + première question
# ============================================================================


class TestCreationQuiz:
    def test_quiz_new_redirige_vers_quiz(self, test_client):
        r = test_client.post(
            f"{PREFIX}/{PC_SLUG}/quiz/new",
            data={"theme": "", "length": "5"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].endswith("/quiz")

    def test_quiz_affiche_la_premiere_question(self, test_client):
        r = test_client.post(
            f"{PREFIX}/{PC_SLUG}/quiz/new",
            data={"theme": "mouvements_energie", "length": "5"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "Question" in r.text
        assert 'name="answer"' in r.text
        assert "Physique-Chimie" in r.text

    def test_quiz_new_avec_theme_inconnu_redirige_avec_erreur(self, test_client):
        r = test_client.post(
            f"{PREFIX}/{PC_SLUG}/quiz/new",
            data={"theme": "theme_qui_n_existe_pas", "length": "5"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "erreur" in r.headers["location"]

    def test_quiz_new_sur_svt_cree_bien_un_quiz_svt(self, test_client):
        r = test_client.post(
            f"{PREFIX}/{SVT_SLUG}/quiz/new",
            data={"theme": "genetique", "length": "5"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "SVT" in r.text


# ============================================================================
# Réponse, indice, reveal
# ============================================================================


def _start_quiz(test_client, discipline_slug: str, theme: str, length: int = 5) -> None:
    r = test_client.post(
        f"{PREFIX}/{discipline_slug}/quiz/new",
        data={"theme": theme, "length": str(length)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = test_client.get(f"{PREFIX}/quiz")
    assert r.status_code == 200


class TestQuizAnswer:
    def test_reponse_vide_renvoie_erreur_lisible(self, test_client):
        _start_quiz(test_client, PC_SLUG, "mouvements_energie")
        r = test_client.post(f"{PREFIX}/quiz/answer", data={"answer": "   "})
        assert r.status_code == 200
        assert "réponse" in r.text.lower()

    def test_reponse_quelconque_repond_200(self, test_client):
        _start_quiz(test_client, PC_SLUG, "mouvements_energie")
        r = test_client.post(f"{PREFIX}/quiz/answer", data={"answer": "42"})
        assert r.status_code == 200


class TestQuizHint:
    def test_indice_repond_200(self, test_client):
        _start_quiz(test_client, SVT_SLUG, "genetique")
        r = test_client.post(f"{PREFIX}/quiz/hint", data={})
        assert r.status_code == 200
        assert "Indice" in r.text


class TestQuizReveal:
    def test_reveal_repond_200(self, test_client):
        _start_quiz(test_client, SVT_SLUG, "genetique")
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
        _start_quiz(test_client, PC_SLUG, "mouvements_energie")
        r = test_client.get(f"{PREFIX}/restart", follow_redirects=False)
        assert r.status_code == 303
        r2 = test_client.get(f"{PREFIX}/quiz", follow_redirects=False)
        assert r2.status_code == 303

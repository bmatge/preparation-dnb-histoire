"""Tests d'intégration des routes ``/francais/redaction/*``.

On monte la vraie app FastAPI via la fixture ``test_client`` (engine
SQLite isolé, RAG et AlbertClient mockés). Chaque test parcourt un ou
plusieurs steps réels, valide les redirects HTTP, le contenu HTML rendu,
les Turn écrits en DB, et les call-traces du fake Albert.

Aucun test ne touche le réseau.
"""

from __future__ import annotations

import pytest
from sqlmodel import select

from app.core.albert_client import GhostwritingDetected, MissingCitations, Task
from app.core.db import Turn, get_engine
from sqlmodel import Session as DBSession
from app.francais.redaction.models import (
    SUBJECT_KIND,
    FrenchRedactionSubject,
)


PREFIX = "/francais/redaction"


# ============================================================================
# Helpers
# ============================================================================


def _create_session_and_choose(
    test_client, option: str = "imagination"
) -> int:
    """Crée une session rédaction et choisit une option. Retourne le sid DB.

    Utilisé par tous les tests qui veulent partir d'un état « élève en
    train de rédiger l'option X ».
    """
    r = test_client.post(f"{PREFIX}/session/new", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/step/1")

    # Récupère le sid via la DB plutôt que via le cookie (le cookie est
    # signé et opaque, c'est plus simple de lire directement la table).
    with DBSession(get_engine()) as s:
        rows = list(s.exec(select(FrenchRedactionSubject)).all())
        assert len(rows) > 0, "Le corpus rédaction n'est pas chargé."

    # Le cookie de session est posé par le serveur sur le client httpx, qui
    # le renvoie automatiquement aux requêtes suivantes.
    r = test_client.post(
        f"{PREFIX}/step/1/choose",
        data={"option": option},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("/step/2")

    # Récupère le session_id depuis la DB (le dernier créé).
    from app.core.db import Session as DbSession  # noqa: N812

    with DBSession(get_engine()) as s:
        sess = s.exec(
            select(DbSession)
            .where(DbSession.subject_kind == SUBJECT_KIND)
            .order_by(DbSession.id.desc())  # type: ignore[attr-defined]
        ).first()
        assert sess is not None
        return sess.id


# ============================================================================
# Accueil + tirage de session
# ============================================================================


class TestHomeAndSessionNew:
    def test_home_lists_subjects(self, test_client):
        r = test_client.get(f"{PREFIX}/")
        assert r.status_code == 200
        assert "Rédaction" in r.text
        # Au moins un sujet du corpus 2018-2025 doit apparaître.
        assert "2018" in r.text or "2025" in r.text

    def test_session_new_redirects_to_step_1(self, test_client):
        r = test_client.post(
            f"{PREFIX}/session/new", follow_redirects=False
        )
        assert r.status_code == 303
        assert r.headers["location"] == f"{PREFIX}/step/1"

    def test_session_new_with_year_filter(self, test_client):
        r = test_client.post(
            f"{PREFIX}/session/new",
            data={"annee": "2023"},
            follow_redirects=False,
        )
        assert r.status_code == 303

        with DBSession(get_engine()) as s:
            from app.core.db import Session as DbSession  # noqa: N812

            sess = s.exec(
                select(DbSession)
                .where(DbSession.subject_kind == SUBJECT_KIND)
                .order_by(DbSession.id.desc())  # type: ignore[attr-defined]
            ).first()
            assert sess is not None
            row = s.get(FrenchRedactionSubject, sess.subject_id)
            assert row is not None
            assert row.annee == 2023


# ============================================================================
# Étape 1 — affichage des deux options + aide + choix
# ============================================================================


class TestStep1:
    def test_step_1_shows_both_options(self, test_client):
        test_client.post(f"{PREFIX}/session/new", follow_redirects=False)
        r = test_client.get(f"{PREFIX}/step/1")
        assert r.status_code == 200
        # Les deux options doivent être visibles côte à côte.
        assert "Imagination" in r.text
        assert "Réflexion" in r.text

    def test_step_1_help_returns_partial(self, test_client, fake_albert):
        test_client.post(f"{PREFIX}/session/new", follow_redirects=False)
        fake_albert.queue_response(
            "Pour le sujet d'imagination, demande-toi : que veux-tu raconter ?"
        )
        r = test_client.post(f"{PREFIX}/step/1/help")
        assert r.status_code == 200
        assert "Décryptons" in r.text  # titre du partial help_response.html
        assert "que veux-tu raconter" in r.text
        # L'appel a bien atteint le fake Albert.
        assert len(fake_albert.calls) == 1
        assert fake_albert.calls[0].task == Task.FR_REDACTION_HELP

    def test_step_1_choose_imagination_redirects_to_step_2(self, test_client):
        test_client.post(f"{PREFIX}/session/new", follow_redirects=False)
        r = test_client.post(
            f"{PREFIX}/step/1/choose",
            data={"option": "imagination"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].endswith("/step/2")

    def test_step_1_choose_reflexion_redirects_to_step_2(self, test_client):
        test_client.post(f"{PREFIX}/session/new", follow_redirects=False)
        r = test_client.post(
            f"{PREFIX}/step/1/choose",
            data={"option": "reflexion"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].endswith("/step/2")

    def test_step_1_choose_invalid_option_returns_400(self, test_client):
        test_client.post(f"{PREFIX}/session/new", follow_redirects=False)
        r = test_client.post(
            f"{PREFIX}/step/1/choose",
            data={"option": "n'importe-quoi"},
            follow_redirects=False,
        )
        assert r.status_code == 400


# ============================================================================
# Garde-fous : navigation sans session ou sans option
# ============================================================================


class TestNavigationGuards:
    def test_step_2_without_session_redirects_home(self, test_client):
        r = test_client.get(f"{PREFIX}/step/2", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].endswith("/francais/redaction/")

    def test_step_2_without_choosing_option_redirects_to_step_1(self, test_client):
        # Session créée mais pas encore d'option choisie → step/2 renvoie
        # vers step/1 pour forcer le choix.
        test_client.post(f"{PREFIX}/session/new", follow_redirects=False)
        r = test_client.get(f"{PREFIX}/step/2", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].endswith("/step/1")


# ============================================================================
# Étape 2 → 3 : brouillon + première évaluation
# ============================================================================


class TestStep2Submit:
    def test_short_proposition_rejected_with_friendly_message(self, test_client, fake_albert):
        _create_session_and_choose(test_client)
        r = test_client.post(
            f"{PREFIX}/step/2/submit",
            data={"proposition": "trop court"},
        )
        assert r.status_code == 200
        assert "Étoffe" in r.text or "étoffe" in r.text
        # Le fake Albert ne doit JAMAIS être appelé sur une proposition rejetée.
        assert len(fake_albert.calls) == 0

    def test_happy_path_calls_albert_and_renders_eval(self, test_client, fake_albert):
        _create_session_and_choose(test_client)
        fake_albert.queue_response(
            "Bravo, ton plan est cohérent. As-tu pensé à creuser le climat de la scène ? [méthodo]"
        )
        proposition = (
            "Plan : ouverture sur la chambre, élément perturbateur quand "
            "j'entends un bruit, péripéties dans le grenier, dénouement au "
            "petit matin. Idées : descriptions sensorielles, peur, soulagement."
        )
        r = test_client.post(
            f"{PREFIX}/step/2/submit", data={"proposition": proposition}
        )
        assert r.status_code == 200
        assert "Première évaluation" in r.text
        assert "creuser le climat" in r.text
        # Le bouton « next » pointe vers step/4 (re-proposition).
        assert "/step/4" in r.text

        # Albert appelé une fois, avec le bon Task.
        assert len(fake_albert.calls) == 1
        assert fake_albert.calls[0].task == Task.FR_REDACTION_FIRST_EVAL

        # Le brouillon élève est persisté en DB (Turn step=2 role=user).
        with DBSession(get_engine()) as s:
            turns = list(
                s.exec(select(Turn).where(Turn.step == 2, Turn.role == "user")).all()
            )
            assert len(turns) == 1
            assert "péripéties dans le grenier" in turns[0].content

    def test_ghostwriting_returns_friendly_message(self, test_client, fake_albert):
        _create_session_and_choose(test_client)
        fake_albert.queue_exception(GhostwritingDetected("test"))
        r = test_client.post(
            f"{PREFIX}/step/2/submit",
            data={"proposition": "Plan détaillé avec idées principales et exemples."},
        )
        assert r.status_code == 200
        assert "ce n'est pas mon rôle" in r.text or "rédiger à ta place" in r.text

    def test_missing_citations_returns_friendly_message(self, test_client, fake_albert):
        _create_session_and_choose(test_client)
        fake_albert.queue_exception(MissingCitations("test"))
        r = test_client.post(
            f"{PREFIX}/step/2/submit",
            data={"proposition": "Plan détaillé avec idées principales et exemples."},
        )
        assert r.status_code == 200
        assert "méthodologie" in r.text


# ============================================================================
# Étape 4 → 5 : seconde proposition + seconde évaluation
# ============================================================================


class TestStep4Submit:
    def test_step_4_pre_fills_previous_proposal(self, test_client, fake_albert):
        _create_session_and_choose(test_client)
        # Étape 2 d'abord pour créer la v1.
        v1 = (
            "Plan v1 : ouverture, milieu, fin. Idées : suspense, peur, "
            "résolution paisible au matin."
        )
        test_client.post(f"{PREFIX}/step/2/submit", data={"proposition": v1})

        r = test_client.get(f"{PREFIX}/step/4")
        assert r.status_code == 200
        # La v1 doit être pré-remplie dans le textarea.
        assert "suspense, peur" in r.text

    def test_step_4_submit_happy_path(self, test_client, fake_albert):
        _create_session_and_choose(test_client)
        test_client.post(
            f"{PREFIX}/step/2/submit",
            data={"proposition": "Plan v1 avec idées initiales et structure de base."},
        )
        fake_albert.queue_response(
            "Tu as ajouté les sensations, c'est nettement mieux. Tu peux passer à la rédaction."
        )
        v2 = (
            "Plan v2 enrichi : ouverture sensorielle (vue, odeur), élément "
            "perturbateur précis, péripéties détaillées, dénouement avec "
            "réflexion personnelle."
        )
        r = test_client.post(
            f"{PREFIX}/step/4/submit", data={"proposition": v2}
        )
        assert r.status_code == 200
        assert "Seconde évaluation" in r.text
        assert "tu peux passer à la rédaction" in r.text.lower()
        assert "/step/6" in r.text

        # Deux appels Albert au total (étape 3 + étape 5).
        assert len(fake_albert.calls) == 2
        assert fake_albert.calls[1].task == Task.FR_REDACTION_SECOND_EVAL

    def test_step_4_short_v2_rejected(self, test_client, fake_albert):
        _create_session_and_choose(test_client)
        test_client.post(
            f"{PREFIX}/step/2/submit",
            data={"proposition": "Plan v1 ok pour avancer dans le parcours."},
        )
        n_calls_before = len(fake_albert.calls)
        r = test_client.post(
            f"{PREFIX}/step/4/submit", data={"proposition": "trop court"}
        )
        assert r.status_code == 200
        assert "courte" in r.text
        # Albert ne doit pas être appelé pour la v2 rejetée.
        assert len(fake_albert.calls) == n_calls_before


# ============================================================================
# Étape 6 → 7 : rédaction finale + correction
# ============================================================================


class TestStep6Submit:
    def test_step_6_short_redaction_rejected(self, test_client, fake_albert):
        _create_session_and_choose(test_client)
        n_calls_before = len(fake_albert.calls)
        r = test_client.post(
            f"{PREFIX}/step/6/submit",
            data={"redaction": "Trop court pour être une vraie rédaction DNB."},
        )
        assert r.status_code == 200
        assert "trentaine de" in r.text or "lignes" in r.text
        assert len(fake_albert.calls) == n_calls_before

    def test_step_6_happy_path(self, test_client, fake_albert):
        _create_session_and_choose(test_client)
        fake_albert.queue_response(
            "===== FOND =====\n\n"
            "Tu réponds bien à la consigne. Tes descriptions gagneraient "
            "à être plus précises [méthodo].\n\n"
            "===== FORME =====\n\n"
            "Plan apparent en trois parties, c'est bien. Quelques fautes "
            "d'orthographe à corriger.\n\n"
            "Bravo pour ton travail. La prochaine fois, étoffe tes "
            "descriptions sensorielles."
        )
        # Une rédaction d'au moins 400 caractères.
        redaction = (
            "Ce matin-là, je me suis réveillé en sursaut. La chambre était "
            "plongée dans une obscurité étrange, et un bruit sourd venait "
            "du grenier. Je me suis levé, le coeur battant, et j'ai pris "
            "une lampe torche. Les marches grinçaient sous mes pieds. "
            "En haut, j'ai découvert un vieux coffre qui n'était pas là "
            "la veille. J'ai hésité longtemps avant de l'ouvrir. À "
            "l'intérieur, des lettres jaunies racontaient une histoire "
            "que j'avais toujours voulu connaître. Au petit matin, je "
            "savais enfin d'où venait notre famille."
        )
        r = test_client.post(
            f"{PREFIX}/step/6/submit", data={"redaction": redaction}
        )
        assert r.status_code == 200
        assert "Correction finale" in r.text
        assert "FOND" in r.text or "Fond" in r.text
        # Le bouton next pointe vers /restart.
        assert "/restart" in r.text

        # Albert appelé exactement une fois (l'éval finale uniquement).
        assert len(fake_albert.calls) == 1
        assert fake_albert.calls[0].task == Task.FR_REDACTION_FINAL_CORRECTION

        # La copie est persistée (Turn step=6 role=user).
        with DBSession(get_engine()) as s:
            turns = list(
                s.exec(
                    select(Turn).where(Turn.step == 6, Turn.role == "user")
                ).all()
            )
            assert len(turns) == 1
            assert "vieux coffre" in turns[0].content


# ============================================================================
# Restart
# ============================================================================


class TestRestart:
    def test_restart_clears_session_and_redirects_home(self, test_client):
        test_client.post(f"{PREFIX}/session/new", follow_redirects=False)
        r = test_client.get(f"{PREFIX}/restart", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].endswith("/francais/redaction/")

        # Après restart, /step/1 doit re-rediriger vers home (plus de session).
        r2 = test_client.get(f"{PREFIX}/step/1", follow_redirects=False)
        assert r2.status_code == 303


# ============================================================================
# Resume — reprise ciblée depuis un brouillon localStorage
# ============================================================================


class TestResume:
    """Route /resume/{subject_id}/step/{step}?option=... utilisée par le
    bandeau global de reprise du helper ``draft_autosave.js``.

    On vérifie que :
    - le subject_id passé dans l'URL est celui effectivement rendu
      ensuite sur /step/N (pas celui éventuellement stocké dans le
      cookie courant) ;
    - l'option est bien rétablie dans ``request.session`` pour que
      ``_require_option`` laisse passer.
    """

    def test_resume_forces_target_subject_and_option(self, test_client):
        # Session en cours sur imagination, pour poser un cookie différent
        _create_session_and_choose(test_client, option="imagination")

        with DBSession(get_engine()) as s:
            target = s.exec(select(FrenchRedactionSubject).limit(1)).first()
            assert target is not None
            target_id = target.id

        r = test_client.get(
            f"{PREFIX}/resume/{target_id}/step/6?option=reflexion",
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].endswith("/step/6")

        r2 = test_client.get(f"{PREFIX}/step/6")
        assert r2.status_code == 200
        assert (
            f'data-draft-key="fr:redaction:step6:{target_id}:reflexion"'
            in r2.text
        )

    def test_resume_unknown_subject_redirects_home(self, test_client):
        r = test_client.get(
            f"{PREFIX}/resume/999999/step/2?option=imagination",
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].endswith("/francais/redaction/")

    def test_resume_invalid_step_returns_404(self, test_client):
        r = test_client.get(
            f"{PREFIX}/resume/1/step/99?option=imagination",
            follow_redirects=False,
        )
        assert r.status_code == 404

    def test_resume_invalid_option_returns_400(self, test_client):
        r = test_client.get(
            f"{PREFIX}/resume/1/step/2?option=bogus",
            follow_redirects=False,
        )
        assert r.status_code == 400

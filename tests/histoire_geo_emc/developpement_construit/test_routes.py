"""Tests d'intégration des routes du DC histoire-géo.

Couvre le parcours bout en bout en mode SEMI_ASSISTÉ : tirage du sujet,
proposition v1, éval, re-proposition, éval comparative, rédaction, et
correction finale. Albert et RAG sont mockés.
"""

from __future__ import annotations

from sqlmodel import select, Session as DBSession

from app.core.albert_client import GhostwritingDetected, Task
from app.core.db import Session as DbSession, Turn, get_engine


PREFIX = "/histoire-geo-emc/developpement-construit"


# ============================================================================
# Helpers
# ============================================================================


def _create_session(test_client, discipline: str = "histoire") -> int:
    r = test_client.post(
        f"{PREFIX}/session/new",
        data={"discipline": discipline, "source": "annales"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("/step/1")

    with DBSession(get_engine()) as s:
        sess = s.exec(
            select(DbSession)
            .where(DbSession.subject_kind == "hgemc_dc")
            .order_by(DbSession.id.desc())  # type: ignore[attr-defined]
        ).first()
        assert sess is not None
        return sess.id


# ============================================================================
# Accueil + tirage
# ============================================================================


class TestHomeAndSessionNew:
    def test_home_returns_form(self, test_client):
        r = test_client.get(f"{PREFIX}/")
        assert r.status_code == 200
        assert "histoire" in r.text.lower() or "géo" in r.text.lower()

    def test_session_new_histoire(self, test_client):
        sid = _create_session(test_client, discipline="histoire")
        with DBSession(get_engine()) as s:
            sess = s.get(DbSession, sid)
            assert sess is not None
            assert sess.subject_kind == "hgemc_dc"
            assert sess.subject_id is not None

    def test_session_new_geographie(self, test_client):
        sid = _create_session(test_client, discipline="geographie")
        with DBSession(get_engine()) as s:
            sess = s.get(DbSession, sid)
            assert sess is not None


# ============================================================================
# Étape 1 — affichage du sujet + aide
# ============================================================================


class TestStep1:
    def test_step_1_renders_subject(self, test_client):
        _create_session(test_client)
        r = test_client.get(f"{PREFIX}/step/1")
        assert r.status_code == 200
        # La carte sujet doit être rendue.
        assert "Thème" in r.text or "Consigne" in r.text

    def test_step_1_help_calls_albert(self, test_client, fake_albert):
        _create_session(test_client)
        fake_albert.queue_response(
            "Pour bien répondre, demande-toi : quelle est la période exactement ?"
        )
        r = test_client.post(f"{PREFIX}/step/1/help")
        assert r.status_code == 200
        assert "quelle est la période" in r.text
        assert len(fake_albert.calls) == 1
        assert fake_albert.calls[0].task == Task.HELP_UNDERSTAND


# ============================================================================
# Étape 2 → 3 : v1 + première évaluation
# ============================================================================


class TestStep2Submit:
    def test_short_proposition_rejected(self, test_client, fake_albert):
        _create_session(test_client)
        r = test_client.post(
            f"{PREFIX}/step/2/submit", data={"proposition": "court"}
        )
        assert r.status_code == 200
        # Le wording d'erreur DC parle de « plan » et « idées principales ».
        assert "plan" in r.text or "idées" in r.text
        assert len(fake_albert.calls) == 0

    def test_happy_path(self, test_client, fake_albert):
        _create_session(test_client)
        fake_albert.queue_response(
            "Bravo, ton plan est cohérent. As-tu pensé à la dimension "
            "économique ? [programme]"
        )
        proposition = (
            "I. Première partie : présentation des acteurs et des dates "
            "clés. II. Deuxième partie : analyse des conséquences et des "
            "transformations. III. Bilan et ouverture."
        )
        r = test_client.post(
            f"{PREFIX}/step/2/submit", data={"proposition": proposition}
        )
        assert r.status_code == 200
        assert "Première évaluation" in r.text
        assert "dimension économique" in r.text
        assert "/step/4" in r.text

        assert len(fake_albert.calls) == 1
        assert fake_albert.calls[0].task == Task.FIRST_EVAL

        with DBSession(get_engine()) as s:
            turns = list(
                s.exec(
                    select(Turn).where(Turn.step == 2, Turn.role == "user")
                ).all()
            )
            assert len(turns) == 1

    def test_ghostwriting_friendly_error(self, test_client, fake_albert):
        _create_session(test_client)
        fake_albert.queue_exception(GhostwritingDetected("test"))
        r = test_client.post(
            f"{PREFIX}/step/2/submit",
            data={"proposition": "Plan détaillé en trois parties avec idées."},
        )
        assert r.status_code == 200
        assert "ce n'est pas mon rôle" in r.text or "rédiger à ta place" in r.text


# ============================================================================
# Étape 4 → 5 : v2 + seconde évaluation
# ============================================================================


class TestStep4Submit:
    def test_step_4_pre_fills_v1(self, test_client, fake_albert):
        _create_session(test_client)
        v1 = "Plan v1 avec acteurs principaux et bornes chrono."
        test_client.post(
            f"{PREFIX}/step/2/submit", data={"proposition": v1}
        )
        r = test_client.get(f"{PREFIX}/step/4")
        assert r.status_code == 200
        assert "acteurs principaux" in r.text

    def test_happy_path(self, test_client, fake_albert):
        _create_session(test_client)
        test_client.post(
            f"{PREFIX}/step/2/submit",
            data={"proposition": "Plan v1 minimal pour avancer."},
        )
        fake_albert.queue_response(
            "Tu as bien complété la partie sur les acteurs. Tu peux passer "
            "à la rédaction. [méthodo]"
        )
        v2 = "Plan v2 enrichi avec acteurs, dates, exemples concrets et bilan."
        r = test_client.post(
            f"{PREFIX}/step/4/submit", data={"proposition": v2}
        )
        assert r.status_code == 200
        assert "Seconde évaluation" in r.text
        assert "/step/6" in r.text
        assert len(fake_albert.calls) == 2
        assert fake_albert.calls[1].task == Task.SECOND_EVAL


# ============================================================================
# Étape 6 → 7 : rédaction + correction finale
# ============================================================================


class TestStep6Submit:
    def test_short_redaction_rejected(self, test_client, fake_albert):
        _create_session(test_client)
        r = test_client.post(
            f"{PREFIX}/step/6/submit",
            data={"redaction": "Trop court pour un DC complet."},
        )
        assert r.status_code == 200
        assert "quinzaine de" in r.text or "lignes" in r.text
        assert len(fake_albert.calls) == 0

    def test_happy_path(self, test_client, fake_albert):
        _create_session(test_client)
        fake_albert.queue_response(
            "===== FOND =====\n\nTu réponds bien à la consigne [programme].\n\n"
            "===== FORME =====\n\nPlan apparent en trois parties.\n\n"
            "Bonne continuation, étoffe ta partie sur les acteurs."
        )
        # Un DC fait au moins 200 caractères dans la validation côté route.
        redaction = (
            "Pendant la guerre froide, Berlin est devenue le symbole de "
            "l'opposition entre les deux blocs. La ville est divisée en "
            "quatre secteurs d'occupation à partir de 1945. En 1961, le "
            "mur est construit par les Soviétiques pour empêcher les "
            "Berlinois de l'Est de fuir vers l'Ouest. Cette situation dure "
            "jusqu'en 1989, date à laquelle le mur tombe. Berlin devient "
            "alors le symbole de la fin de la guerre froide et de la "
            "réunification allemande l'année suivante."
        )
        r = test_client.post(
            f"{PREFIX}/step/6/submit", data={"redaction": redaction}
        )
        assert r.status_code == 200
        assert "Correction finale" in r.text
        assert "/restart" in r.text
        assert len(fake_albert.calls) == 1
        assert fake_albert.calls[0].task == Task.FINAL_CORRECTION


# ============================================================================
# Restart
# ============================================================================


class TestRestart:
    def test_restart_clears_session(self, test_client):
        _create_session(test_client)
        r = test_client.get(f"{PREFIX}/restart", follow_redirects=False)
        assert r.status_code == 303
        # Le restart DC redirige vers la racine globale "/", pas vers la
        # matière elle-même (cf. routes.py::restart).
        assert r.headers["location"] == "/"

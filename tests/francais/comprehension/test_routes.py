"""Smoke tests d'intégration des routes ``/francais/comprehension/*``.

Couverture pragmatique : on vérifie le plumbing du parcours élève
question-par-question (home → session/new → item → answer → hint → reveal
→ next → synthese), pas la sémantique de l'évaluation (qui est testée
indirectement via le parsing du verdict).

Le format des réponses canned imite la structure attendue par
``_parse_eval`` (``VERDICT:`` + ``COMMENTAIRE:`` + ``PROCHAINE_ACTION:``)
pour traverser le pipeline sans tomber dans le fallback générique.
"""

from __future__ import annotations

from sqlmodel import select, Session as DBSession

from app.core.albert_client import Task
from app.core.db import Session as DbSession, Turn, get_engine
from app.francais.comprehension.models import SUBJECT_KIND


PREFIX = "/francais/comprehension"


# ============================================================================
# Helpers
# ============================================================================


def _create_session(test_client) -> int:
    r = test_client.post(f"{PREFIX}/session/new", follow_redirects=False)
    assert r.status_code == 303
    # La cible contient /item/1 — on en extrait le session_id pour les
    # appels suivants au lieu de naviguer aveuglément.
    location = r.headers["location"]
    # Format : /francais/comprehension/session/{sid}/item/1
    parts = location.rstrip("/").split("/")
    return int(parts[-3])


def _eval_response(verdict: str = "PARTIELLE", action: str = "INDICE") -> str:
    """Compose une réponse structurée que ``_parse_eval`` peut désérialiser."""
    return (
        f"VERDICT: {verdict}\n"
        "COMMENTAIRE: Tu as bien commencé. Pense à relire la ligne 12.\n"
        f"PROCHAINE_ACTION: {action}"
    )


# ============================================================================
# Accueil
# ============================================================================


class TestHome:
    def test_home_lists_exercises(self, test_client):
        r = test_client.get(f"{PREFIX}/")
        assert r.status_code == 200
        # Au moins le titre de l'épreuve doit apparaître.
        assert "compréhension" in r.text.lower() or "comprehension" in r.text.lower()


# ============================================================================
# Création de session + premier item
# ============================================================================


class TestSessionFlow:
    def test_session_new_creates_session_and_redirects(self, test_client):
        sid = _create_session(test_client)
        with DBSession(get_engine()) as s:
            sess = s.get(DbSession, sid)
            assert sess is not None
            assert sess.subject_kind == SUBJECT_KIND
            assert sess.subject_id is not None

    def test_first_item_renders(self, test_client):
        sid = _create_session(test_client)
        r = test_client.get(f"{PREFIX}/session/{sid}/item/1")
        assert r.status_code == 200
        # Le template `exercise.html` doit afficher le numéro d'item et un
        # textarea pour la réponse.
        assert "textarea" in r.text or "réponse" in r.text.lower()


# ============================================================================
# Évaluation d'une réponse
# ============================================================================


class TestSubmitAnswer:
    def test_answer_calls_albert_and_renders_partial(
        self, test_client, fake_albert
    ):
        sid = _create_session(test_client)
        fake_albert.queue_response(_eval_response(verdict="PARTIELLE"))
        r = test_client.post(
            f"{PREFIX}/session/{sid}/item/1/answer",
            data={"reponse": "Le narrateur est triste parce que sa mère est partie."},
        )
        assert r.status_code == 200
        # Le partial feedback contient le commentaire ou un wording dérivé.
        assert "ligne 12" in r.text or "bien commencé" in r.text

        assert len(fake_albert.calls) == 1
        assert fake_albert.calls[0].task == Task.FR_COMP_EVAL

        # La réponse élève est persistée comme un Turn (step=order=1, role=user).
        with DBSession(get_engine()) as s:
            turns = list(
                s.exec(
                    select(Turn)
                    .where(Turn.session_id == sid, Turn.step == 1, Turn.role == "user")
                ).all()
            )
            assert len(turns) == 1
            assert "narrateur est triste" in turns[0].content


# ============================================================================
# Indice gradué
# ============================================================================


class TestHints:
    def test_hint_first_level(self, test_client, fake_albert):
        sid = _create_session(test_client)
        fake_albert.queue_response(
            "Regarde plutôt du côté du champ lexical de la solitude."
        )
        r = test_client.post(f"{PREFIX}/session/{sid}/item/1/hint")
        assert r.status_code == 200
        assert "champ lexical" in r.text or "solitude" in r.text
        assert len(fake_albert.calls) == 1
        assert fake_albert.calls[0].task == Task.FR_COMP_HINT

    def test_three_hints_then_fourth_blocked(self, test_client, fake_albert):
        sid = _create_session(test_client)
        fake_albert.queue_response("Indice 1.")
        fake_albert.queue_response("Indice 2.")
        fake_albert.queue_response("Indice 3.")
        for _ in range(3):
            r = test_client.post(f"{PREFIX}/session/{sid}/item/1/hint")
            assert r.status_code == 200

        # Le 4e indice doit être bloqué côté serveur.
        r4 = test_client.post(f"{PREFIX}/session/{sid}/item/1/hint")
        assert r4.status_code == 400


# ============================================================================
# Révélation finale
# ============================================================================


class TestReveal:
    def test_reveal_returns_partial_with_answer(self, test_client, fake_albert):
        sid = _create_session(test_client)
        fake_albert.queue_response(
            "La réponse attendue est la solitude du narrateur, suggérée par "
            "le champ lexical du vide aux lignes 12 à 15."
        )
        r = test_client.post(f"{PREFIX}/session/{sid}/item/1/reveal")
        assert r.status_code == 200
        assert "solitude" in r.text or "champ lexical" in r.text
        assert len(fake_albert.calls) == 1
        assert fake_albert.calls[0].task == Task.FR_COMP_REVEAL


# ============================================================================
# Navigation : next item
# ============================================================================


class TestGoNext:
    def test_next_redirects_to_item_2(self, test_client):
        sid = _create_session(test_client)
        r = test_client.get(
            f"{PREFIX}/session/{sid}/item/1/next", follow_redirects=False
        )
        assert r.status_code == 303
        assert "/item/2" in r.headers["location"]

"""Tests du loader / sélecteur d'exercices problèmes maths.

Couverture :
- ``init_problemes`` charge le corpus committé sans erreur, idempotent.
- Les sous-questions sont bien sérialisées / désérialisées en JSON.
- ``list_exercises`` respecte le filtre par thème.
- Chaque thème déclaré dans les JSON a au moins un exercice.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import Session as DBSession, select

from app.mathematiques.problemes import loader as prob_loader
from app.mathematiques.problemes import models as prob_models


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
EXERCISES_DIR = (
    REPO_ROOT / "content" / "mathematiques" / "problemes" / "exercices"
)


def _expected_total_from_disk() -> int:
    total = 0
    for json_path in sorted(EXERCISES_DIR.glob("*.json")):
        if json_path.name.startswith("_"):
            continue
        data = json.loads(json_path.read_text(encoding="utf-8"))
        total += len(data.get("exercices") or [])
    return total


# ============================================================================
# init_problemes
# ============================================================================


class TestInitProblemes:
    def test_charge_le_corpus_committe(self, tmp_engine, monkeypatch):
        """Le loader charge tous les fichiers du dossier ``exercices/``."""
        monkeypatch.setattr(prob_models, "get_engine", lambda: tmp_engine)
        n = prob_models.init_problemes()
        expected = _expected_total_from_disk()
        assert n == expected, (
            f"Nombre d'exercices incorrect : {n} chargés, {expected} attendus."
        )
        assert n >= 6, (
            f"Corpus trop petit : {n} exercices (≥ 6 attendus selon la spec V1)."
        )

    def test_idempotent(self, tmp_engine, monkeypatch):
        """Deux appels successifs donnent le même nombre de lignes."""
        monkeypatch.setattr(prob_models, "get_engine", lambda: tmp_engine)
        n1 = prob_models.init_problemes()
        n2 = prob_models.init_problemes()
        assert n1 == n2

        with DBSession(tmp_engine) as s:
            rows = s.exec(select(prob_models.ProblemExercise)).all()
            assert len(rows) == n1

    def test_sous_questions_sont_bien_deserialisees(
        self, tmp_engine, monkeypatch
    ):
        """Chaque exercice doit avoir ≥ 1 sous-question après chargement."""
        monkeypatch.setattr(prob_models, "get_engine", lambda: tmp_engine)
        prob_models.init_problemes()

        with DBSession(tmp_engine) as s:
            rows = s.exec(select(prob_models.ProblemExercise)).all()
            for ex in rows:
                sqs = ex.sous_questions
                assert isinstance(sqs, list)
                assert len(sqs) >= 1, (
                    f"Exercice {ex.id} n'a aucune sous-question."
                )
                # Chaque sous-question a au minimum id, numero, texte, scoring.
                for sq in sqs:
                    assert "id" in sq
                    assert "numero" in sq
                    assert "texte" in sq
                    assert "scoring" in sq
                    assert sq["scoring"].get("mode") in ("python", "albert")

    def test_themes_declares_existent_tous(self, tmp_engine, monkeypatch):
        """Tous les thèmes utilisés par les exercices du corpus sont
        présents dans ``ALLOWED_THEMES``."""
        monkeypatch.setattr(prob_models, "get_engine", lambda: tmp_engine)
        prob_models.init_problemes()

        with DBSession(tmp_engine) as s:
            rows = s.exec(select(prob_models.ProblemExercise)).all()
            corpus_themes = {ex.theme for ex in rows}
        inconnus = corpus_themes - set(prob_models.ALLOWED_THEMES)
        assert not inconnus, f"Thèmes inconnus dans le corpus : {inconnus}"

    def test_theme_labels_couvre_les_themes_alloues(self):
        """Tous les thèmes de ``ALLOWED_THEMES`` ont un libellé humain."""
        for theme in prob_models.ALLOWED_THEMES:
            assert theme in prob_loader.THEME_LABELS
            label = prob_loader.THEME_LABELS[theme]
            assert isinstance(label, str) and label


# ============================================================================
# list_exercises / list_for_home
# ============================================================================


class TestListExercises:
    def test_sans_filtre_renvoie_tout(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(prob_models, "get_engine", lambda: tmp_engine)
        prob_models.init_problemes()

        with DBSession(tmp_engine) as s:
            exs = prob_loader.list_for_home(s)
        assert len(exs) == _expected_total_from_disk()

    def test_filtre_par_theme(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(prob_models, "get_engine", lambda: tmp_engine)
        prob_models.init_problemes()

        with DBSession(tmp_engine) as s:
            exs = prob_loader.list_for_home(s, theme="fonctions")
        assert len(exs) >= 1
        assert all(e.theme == "fonctions" for e in exs)

    def test_filtre_par_theme_inconnu_renvoie_vide(
        self, tmp_engine, monkeypatch
    ):
        monkeypatch.setattr(prob_models, "get_engine", lambda: tmp_engine)
        prob_models.init_problemes()

        with DBSession(tmp_engine) as s:
            exs = prob_loader.list_for_home(s, theme="inexistant_42")
        assert exs == []

    def test_list_themes_ordre_canonique(self, tmp_engine, monkeypatch):
        """Les thèmes sont retournés dans l'ordre canonique d'ALLOWED_THEMES."""
        monkeypatch.setattr(prob_models, "get_engine", lambda: tmp_engine)
        prob_models.init_problemes()

        with DBSession(tmp_engine) as s:
            themes = prob_models.list_themes(s)
        # Ordre stable : chaque thème doit apparaître au plus dans l'ordre
        # d'ALLOWED_THEMES.
        canonical = list(prob_models.ALLOWED_THEMES)
        indices = [canonical.index(t) for t in themes]
        assert indices == sorted(indices)


# ============================================================================
# Helpers get_exercise + get_subquestion
# ============================================================================


class TestExerciseHelpers:
    def test_get_exercise_existant(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(prob_models, "get_engine", lambda: tmp_engine)
        prob_models.init_problemes()

        with DBSession(tmp_engine) as s:
            ex = prob_models.get_exercise(s, "prob_2026A_ex2")
            assert ex is not None
            assert ex.theme == "programmes_calcul"
            assert "Programme" in ex.titre

    def test_get_exercise_inconnu(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(prob_models, "get_engine", lambda: tmp_engine)
        prob_models.init_problemes()

        with DBSession(tmp_engine) as s:
            assert prob_models.get_exercise(s, "ne_pas_exister") is None

    def test_get_subquestion_helper(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(prob_models, "get_engine", lambda: tmp_engine)
        prob_models.init_problemes()

        with DBSession(tmp_engine) as s:
            ex = prob_models.get_exercise(s, "prob_2026A_ex2")
        assert ex is not None
        sq = ex.get_subquestion("prob_2026A_ex2_q1")
        assert sq is not None
        assert sq["numero"] == "1"
        assert ex.get_subquestion("ne_pas_exister") is None

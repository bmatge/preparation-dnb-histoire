"""Tests du loader / sélecteur de questions automatismes maths.

Couverture :
- `init_automatismes` charge le corpus committé sans erreur et est idempotent.
- `pick_for_quiz` respecte le filtre par thème et la taille demandée.
- `pick_for_quiz` ne crashe pas quand on demande plus de questions
  que ce qu'il y a en banque (rendu partiel attendu).
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import select

from app.mathematiques.automatismes import loader as auto_loader
from app.mathematiques.automatismes import models as auto_models


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
QUESTIONS_DIR = (
    REPO_ROOT / "content" / "mathematiques" / "automatismes" / "questions"
)


def _expected_total_from_disk() -> int:
    total = 0
    for json_path in sorted(QUESTIONS_DIR.glob("*.json")):
        if json_path.name.startswith("_"):
            continue
        data = json.loads(json_path.read_text(encoding="utf-8"))
        total += len(data.get("questions") or [])
    return total


# ============================================================================
# init_automatismes
# ============================================================================


class TestInitAutomatismes:
    def test_charge_le_corpus_committe(self, tmp_engine, monkeypatch):
        """Le loader charge tous les fichiers du dossier `questions/`."""
        monkeypatch.setattr(auto_models, "get_engine", lambda: tmp_engine)
        n = auto_models.init_automatismes()
        assert n >= 150, (
            f"Corpus trop petit : {n} questions chargées (≥ 150 attendues "
            "selon la spec V1 de l'issue #21)."
        )
        assert n == _expected_total_from_disk(), (
            "Le nombre de questions chargées doit correspondre au nombre "
            "total de questions des fichiers JSON committés."
        )

    def test_idempotent(self, tmp_engine, monkeypatch):
        """Deux appels successifs donnent le même nombre de lignes."""
        monkeypatch.setattr(auto_models, "get_engine", lambda: tmp_engine)
        n1 = auto_models.init_automatismes()
        n2 = auto_models.init_automatismes()
        assert n1 == n2

        # Vérification directe en DB : même nombre de lignes.
        from sqlmodel import Session as DBSession

        with DBSession(tmp_engine) as s:
            rows = s.exec(select(auto_models.AutoQuestion)).all()
            assert len(rows) == n1

    def test_themes_couvrent_les_8_categories(self, tmp_engine, monkeypatch):
        """Tous les thèmes officiels sont représentés dans le corpus."""
        monkeypatch.setattr(auto_models, "get_engine", lambda: tmp_engine)
        auto_models.init_automatismes()
        from sqlmodel import Session as DBSession

        with DBSession(tmp_engine) as s:
            themes = set(auto_models.list_themes(s))
        assert themes == set(auto_models.ALLOWED_THEMES), (
            f"Thèmes manquants : {set(auto_models.ALLOWED_THEMES) - themes}"
        )

    def test_repartition_minimum_par_theme(self, tmp_engine, monkeypatch):
        """Chaque thème doit contenir au moins 10 questions pour qu'un
        quiz de 10 sur un thème unique puisse tourner."""
        monkeypatch.setattr(auto_models, "get_engine", lambda: tmp_engine)
        auto_models.init_automatismes()
        from sqlmodel import Session as DBSession

        with DBSession(tmp_engine) as s:
            for theme in auto_models.ALLOWED_THEMES:
                rows = s.exec(
                    select(auto_models.AutoQuestion).where(
                        auto_models.AutoQuestion.theme == theme
                    )
                ).all()
                assert len(rows) >= 10, (
                    f"Thème '{theme}' n'a que {len(rows)} questions "
                    "(≥ 10 attendues pour un quiz de 10)."
                )


# ============================================================================
# pick_for_quiz
# ============================================================================


class TestPickForQuiz:
    def test_renvoie_n_questions_sans_filtre(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(auto_models, "get_engine", lambda: tmp_engine)
        auto_models.init_automatismes()
        from sqlmodel import Session as DBSession

        with DBSession(tmp_engine) as s:
            picked = auto_loader.pick_for_quiz(s, n=10)
        assert len(picked) == 10

    def test_filtre_par_theme(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(auto_models, "get_engine", lambda: tmp_engine)
        auto_models.init_automatismes()
        from sqlmodel import Session as DBSession

        with DBSession(tmp_engine) as s:
            picked = auto_loader.pick_for_quiz(s, n=5, theme="fractions")
        assert len(picked) == 5
        assert all(q.theme == "fractions" for q in picked)

    def test_ne_crash_pas_si_n_trop_grand(self, tmp_engine, monkeypatch):
        """Si on demande 100 questions et qu'on n'en a que 22 sur un thème,
        on renvoie tout ce qu'on a sans erreur."""
        monkeypatch.setattr(auto_models, "get_engine", lambda: tmp_engine)
        auto_models.init_automatismes()
        from sqlmodel import Session as DBSession

        with DBSession(tmp_engine) as s:
            picked = auto_loader.pick_for_quiz(s, n=10_000, theme="programmes_calcul")
        # On a 14 questions programmes_calcul ; doit retourner toutes.
        assert len(picked) >= 10
        assert all(q.theme == "programmes_calcul" for q in picked)

    def test_n_zero_renvoie_vide(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(auto_models, "get_engine", lambda: tmp_engine)
        auto_models.init_automatismes()
        from sqlmodel import Session as DBSession

        with DBSession(tmp_engine) as s:
            picked = auto_loader.pick_for_quiz(s, n=0)
        assert picked == []

    def test_theme_inexistant_renvoie_vide(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(auto_models, "get_engine", lambda: tmp_engine)
        auto_models.init_automatismes()
        from sqlmodel import Session as DBSession

        with DBSession(tmp_engine) as s:
            picked = auto_loader.pick_for_quiz(s, n=5, theme="inexistant")
        assert picked == []


# ============================================================================
# THEME_LABELS — couvre les 8 thèmes officiels
# ============================================================================


def test_theme_labels_couvre_les_8_themes():
    for theme in auto_models.ALLOWED_THEMES:
        assert theme in auto_loader.THEME_LABELS
        label = auto_loader.THEME_LABELS[theme]
        assert isinstance(label, str) and label

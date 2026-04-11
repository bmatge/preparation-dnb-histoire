"""Tests du loader / sélecteur de questions révision sciences.

Couverture :
- `init_sciences_revision` charge le corpus committé sans erreur, est idempotent.
- Toutes les disciplines et thèmes attendus sont représentés.
- Chaque thème contient un minimum de questions (pour qu'un quiz de 10 tourne).
- `pick_for_quiz` respecte le filtre par discipline + thème.
- Les filtres vides ou inexistants sont gérés proprement.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import Session as DBSession, select

from app.sciences.revision import loader as science_loader
from app.sciences.revision import models as science_models


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
QUESTIONS_DIR = REPO_ROOT / "content" / "sciences" / "revision" / "questions"


def _expected_total_from_disk() -> int:
    total = 0
    for json_path in sorted(QUESTIONS_DIR.glob("*.json")):
        if json_path.name.startswith("_"):
            continue
        data = json.loads(json_path.read_text(encoding="utf-8"))
        total += len(data.get("questions") or [])
    return total


# ============================================================================
# init_sciences_revision
# ============================================================================


class TestInitSciencesRevision:
    def test_charge_le_corpus_committe(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(science_models, "get_engine", lambda: tmp_engine)
        n = science_models.init_sciences_revision()
        assert n >= 150, (
            f"Corpus sciences trop petit : {n} questions chargées "
            "(≥ 150 attendues selon la spec vague 1)."
        )
        assert n == _expected_total_from_disk()

    def test_idempotent(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(science_models, "get_engine", lambda: tmp_engine)
        n1 = science_models.init_sciences_revision()
        n2 = science_models.init_sciences_revision()
        assert n1 == n2
        with DBSession(tmp_engine) as s:
            rows = s.exec(select(science_models.SciencesQuestionRow)).all()
            assert len(rows) == n1

    def test_toutes_les_disciplines_presentes(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(science_models, "get_engine", lambda: tmp_engine)
        science_models.init_sciences_revision()
        with DBSession(tmp_engine) as s:
            rows = s.exec(
                select(science_models.SciencesQuestionRow.discipline).distinct()
            ).all()
        assert set(rows) == set(science_models.ALLOWED_DISCIPLINES)

    def test_tous_les_themes_couverts(self, tmp_engine, monkeypatch):
        """Chaque thème défini dans ALLOWED_THEMES doit avoir au moins une
        question dans le corpus committé."""
        monkeypatch.setattr(science_models, "get_engine", lambda: tmp_engine)
        science_models.init_sciences_revision()
        with DBSession(tmp_engine) as s:
            rows = s.exec(
                select(science_models.SciencesQuestionRow.theme).distinct()
            ).all()
        themes_presents = set(rows)
        assert themes_presents == set(science_models.ALLOWED_THEMES), (
            f"Thèmes manquants : {set(science_models.ALLOWED_THEMES) - themes_presents}"
        )

    def test_minimum_questions_par_discipline(self, tmp_engine, monkeypatch):
        """Un quiz de 5 doit pouvoir tourner sur chaque discipline (≥ 5 questions)."""
        monkeypatch.setattr(science_models, "get_engine", lambda: tmp_engine)
        science_models.init_sciences_revision()
        with DBSession(tmp_engine) as s:
            for discipline in science_models.ALLOWED_DISCIPLINES:
                rows = s.exec(
                    select(science_models.SciencesQuestionRow).where(
                        science_models.SciencesQuestionRow.discipline == discipline
                    )
                ).all()
                assert len(rows) >= 10, (
                    f"Discipline {discipline!r} n'a que {len(rows)} questions "
                    "(≥ 10 attendues pour un quiz de 10)."
                )

    def test_minimum_questions_par_theme(self, tmp_engine, monkeypatch):
        """Un quiz de 5 questions sur un thème unique doit tourner."""
        monkeypatch.setattr(science_models, "get_engine", lambda: tmp_engine)
        science_models.init_sciences_revision()
        with DBSession(tmp_engine) as s:
            for theme in science_models.ALLOWED_THEMES:
                rows = s.exec(
                    select(science_models.SciencesQuestionRow).where(
                        science_models.SciencesQuestionRow.theme == theme
                    )
                ).all()
                assert len(rows) >= 5, (
                    f"Thème {theme!r} n'a que {len(rows)} questions "
                    "(≥ 5 attendues pour un quiz de 5 minimum)."
                )

    def test_coherence_discipline_theme(self, tmp_engine, monkeypatch):
        """Chaque question doit avoir un thème qui appartient bien à sa discipline."""
        monkeypatch.setattr(science_models, "get_engine", lambda: tmp_engine)
        science_models.init_sciences_revision()
        with DBSession(tmp_engine) as s:
            rows = s.exec(select(science_models.SciencesQuestionRow)).all()
        for q in rows:
            expected_discipline = science_models.THEME_TO_DISCIPLINE.get(q.theme)
            assert expected_discipline == q.discipline, (
                f"Question {q.id} : thème {q.theme!r} rattaché à "
                f"{q.discipline!r}, attendu {expected_discipline!r}."
            )


# ============================================================================
# pick_for_quiz
# ============================================================================


class TestPickForQuiz:
    def test_renvoie_n_questions_discipline_entiere(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(science_models, "get_engine", lambda: tmp_engine)
        science_models.init_sciences_revision()
        with DBSession(tmp_engine) as s:
            picked = science_loader.pick_for_quiz(s, n=10, discipline="physique_chimie")
        assert len(picked) == 10
        assert all(q.discipline == "physique_chimie" for q in picked)

    def test_filtre_par_theme(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(science_models, "get_engine", lambda: tmp_engine)
        science_models.init_sciences_revision()
        with DBSession(tmp_engine) as s:
            picked = science_loader.pick_for_quiz(
                s, n=5, discipline="svt", theme="genetique"
            )
        assert len(picked) == 5
        assert all(q.theme == "genetique" for q in picked)
        assert all(q.discipline == "svt" for q in picked)

    def test_ne_crash_pas_si_n_trop_grand(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(science_models, "get_engine", lambda: tmp_engine)
        science_models.init_sciences_revision()
        with DBSession(tmp_engine) as s:
            picked = science_loader.pick_for_quiz(
                s, n=1000, discipline="technologie", theme="chaine_energie"
            )
        # ~7 questions dans ce thème, on en demande 1000.
        assert 0 < len(picked) <= 1000

    def test_theme_inexistant_renvoie_vide(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(science_models, "get_engine", lambda: tmp_engine)
        science_models.init_sciences_revision()
        with DBSession(tmp_engine) as s:
            picked = science_loader.pick_for_quiz(
                s, n=5, discipline="svt", theme="qqchose_inexistant"
            )
        assert picked == []

    def test_n_zero_renvoie_vide(self, tmp_engine, monkeypatch):
        monkeypatch.setattr(science_models, "get_engine", lambda: tmp_engine)
        science_models.init_sciences_revision()
        with DBSession(tmp_engine) as s:
            picked = science_loader.pick_for_quiz(s, n=0, discipline="physique_chimie")
        assert picked == []


# ============================================================================
# Labels et slugs
# ============================================================================


def test_discipline_labels_couvre_toutes_les_disciplines():
    for d in science_models.ALLOWED_DISCIPLINES:
        assert d in science_loader.DISCIPLINE_LABELS
        assert d in science_loader.DISCIPLINE_SLUGS


def test_discipline_slug_roundtrip():
    for d in science_models.ALLOWED_DISCIPLINES:
        slug = science_loader.DISCIPLINE_SLUGS[d]
        assert science_loader.DISCIPLINE_FROM_SLUG[slug] == d


def test_theme_labels_couvre_tous_les_themes():
    for theme in science_models.ALLOWED_THEMES:
        assert theme in science_loader.THEME_LABELS
        label = science_loader.THEME_LABELS[theme]
        assert isinstance(label, str) and label


def test_list_themes_for_discipline_respecte_ordre_canonique(tmp_engine, monkeypatch):
    monkeypatch.setattr(science_models, "get_engine", lambda: tmp_engine)
    science_models.init_sciences_revision()
    with DBSession(tmp_engine) as s:
        themes_pc = science_models.list_themes_for_discipline(s, "physique_chimie")
    expected_order = list(science_models.ALLOWED_THEMES_PAR_DISCIPLINE["physique_chimie"])
    # Les thèmes rendus doivent être dans l'ordre canonique (sous-ensemble).
    assert themes_pc == [t for t in expected_order if t in themes_pc]

"""Tests du loader des sujets de rédaction.

Couvre :
- Le chargement idempotent (deuxième passage = no-op).
- L'upsert sur un slug existant.
- Le matching best-effort vers ``FrenchExercise`` (texte support de
  compréhension).
- La tolérance aux fichiers JSON mal formés (loggué + skip, pas de crash).
- La tolérance au dossier vide ou absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import select

from app.francais.comprehension.models import FrenchExercise
from app.francais.redaction.loader import (
    _best_effort_comprehension_slug,
    load_redaction_subjects,
)
from app.francais.redaction.models import FrenchRedactionSubject


# ============================================================================
# Helpers
# ============================================================================


def _make_subject_json(
    *,
    slug: str,
    annee: int,
    centre: str,
    texte_support_ref: str | None = None,
    source_file: str | None = None,
) -> dict:
    """Construit un dict JSON conforme au schéma RedactionSubject."""
    return {
        "id": slug,
        "source": {
            "annee": annee,
            "session": "inconnu",
            "centre": centre,
            "code_sujet": None,
        },
        "epreuve": {
            "intitule": "Rédaction",
            "duree_minutes": 90,
            "points_total": 40,
        },
        "texte_support_ref": texte_support_ref,
        "sujet_imagination": {
            "type": "imagination",
            "numero": "Sujet d'imagination",
            "amorce": None,
            "consigne": "Raconte un événement marquant de ton enfance.",
            "contraintes": ["récit à la première personne"],
            "longueur_min_lignes": None,
            "reference_texte_support": None,
        },
        "sujet_reflexion": {
            "type": "reflexion",
            "numero": "Sujet de réflexion",
            "amorce": None,
            "consigne": "Selon toi, raconter sa vie aide-t-il à se connaître ?",
            "contraintes": ["développement argumenté"],
            "longueur_min_lignes": None,
            "reference_texte_support": None,
        },
        "source_file": source_file or f"{slug}.pdf",
    }


def _write_subject(dir_: Path, slug: str, **kwargs) -> Path:
    data = _make_subject_json(slug=slug, **kwargs)
    path = dir_ / f"{slug}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return path


# ============================================================================
# load_redaction_subjects
# ============================================================================


class TestLoadRedactionSubjects:
    def test_loads_one_subject(self, tmp_session, tmp_path):
        d = tmp_path / "subjects"
        d.mkdir()
        _write_subject(d, slug="2018_metropole", annee=2018, centre="Métropole")

        n = load_redaction_subjects(tmp_session, subjects_dir=d)
        assert n == 1

        rows = list(tmp_session.exec(select(FrenchRedactionSubject)).all())
        assert len(rows) == 1
        assert rows[0].slug == "2018_metropole"
        assert rows[0].annee == 2018
        assert rows[0].centre == "Métropole"
        assert rows[0].source_file == "2018_metropole.pdf"

    def test_loads_multiple_subjects(self, tmp_session, tmp_path):
        d = tmp_path / "subjects"
        d.mkdir()
        _write_subject(d, slug="2018_metropole", annee=2018, centre="Métropole")
        _write_subject(
            d, slug="2018_amerique-nord", annee=2018, centre="Amérique du Nord"
        )
        _write_subject(d, slug="2019_metropole", annee=2019, centre="Métropole")

        n = load_redaction_subjects(tmp_session, subjects_dir=d)
        assert n == 3

    def test_idempotent_upsert(self, tmp_session, tmp_path):
        d = tmp_path / "subjects"
        d.mkdir()
        path = _write_subject(
            d, slug="2018_metropole", annee=2018, centre="Métropole"
        )

        # Premier chargement.
        load_redaction_subjects(tmp_session, subjects_dir=d)
        n_first = len(
            list(tmp_session.exec(select(FrenchRedactionSubject)).all())
        )

        # Deuxième chargement avec une donnée modifiée → upsert sur slug,
        # pas de doublon.
        modified = _make_subject_json(
            slug="2018_metropole",
            annee=2018,
            centre="Métropole-Modifiée",  # changement pour vérifier l'upsert
        )
        path.write_text(json.dumps(modified, ensure_ascii=False))

        load_redaction_subjects(tmp_session, subjects_dir=d)
        rows = list(tmp_session.exec(select(FrenchRedactionSubject)).all())
        assert len(rows) == n_first  # toujours un seul row
        assert rows[0].centre == "Métropole-Modifiée"

    def test_skips_all_json_consolidated(self, tmp_session, tmp_path):
        d = tmp_path / "subjects"
        d.mkdir()
        _write_subject(d, slug="2018_metropole", annee=2018, centre="Métropole")
        # Le fichier consolidé doit être ignoré pour ne pas être chargé en
        # double comme un sujet supplémentaire.
        (d / "_all.json").write_text("[]")

        n = load_redaction_subjects(tmp_session, subjects_dir=d)
        assert n == 1

    def test_invalid_json_is_skipped_not_crash(self, tmp_session, tmp_path):
        d = tmp_path / "subjects"
        d.mkdir()
        _write_subject(d, slug="2018_metropole", annee=2018, centre="Métropole")
        # JSON invalide → doit être loggué et ignoré sans casser le batch.
        (d / "broken.json").write_text("{ pas du tout du json")

        n = load_redaction_subjects(tmp_session, subjects_dir=d)
        assert n == 1  # le bon est passé, le cassé est skip

    def test_pydantic_invalid_is_skipped(self, tmp_session, tmp_path):
        d = tmp_path / "subjects"
        d.mkdir()
        _write_subject(d, slug="2018_metropole", annee=2018, centre="Métropole")
        # JSON valide mais qui ne respecte pas le schéma Pydantic → skip.
        (d / "wrong_schema.json").write_text(
            json.dumps({"id": "x", "incomplet": True})
        )

        n = load_redaction_subjects(tmp_session, subjects_dir=d)
        assert n == 1

    def test_missing_directory_returns_zero(self, tmp_session, tmp_path):
        # Tant que l'extraction Opus n'a pas tourné, le dossier n'existe
        # pas. Le loader doit logger un warning et renvoyer 0, pas crasher.
        n = load_redaction_subjects(
            tmp_session, subjects_dir=tmp_path / "inexistant"
        )
        assert n == 0

    def test_empty_directory_returns_zero(self, tmp_session, tmp_path):
        d = tmp_path / "subjects"
        d.mkdir()
        n = load_redaction_subjects(tmp_session, subjects_dir=d)
        assert n == 0


# ============================================================================
# _best_effort_comprehension_slug
# ============================================================================


class TestBestEffortComprehensionSlug:
    def test_matches_when_french_exercise_exists(self, tmp_session):
        # On crée un FrenchExercise au slug canonique 2018_metropole.
        ex = FrenchExercise(
            slug="2018_metropole",
            source_file="2018_Metropole_francais_questions-grammaire-comp.pdf",
            annee=2018,
            centre="Métropole",
            data_json="{}",
        )
        tmp_session.add(ex)
        tmp_session.commit()

        result = _best_effort_comprehension_slug(
            tmp_session, annee=2018, centre="Métropole"
        )
        assert result == "2018_metropole"

    def test_normalizes_centre_to_slug(self, tmp_session):
        # Centre avec espaces → doit être slugifié en tirets-minuscules.
        ex = FrenchExercise(
            slug="2018_amerique du nord",
            source_file="x.pdf",
            annee=2018,
            centre="Amérique du Nord",
            data_json="{}",
        )
        # Note : le slug attendu est "amerique-du-nord" (minuscules + tirets),
        # pas "amerique du nord". On teste la convention exacte.
        ex2 = FrenchExercise(
            slug="2018_amerique-du-nord",
            source_file="y.pdf",
            annee=2018,
            centre="Amérique du Nord",
            data_json="{}",
        )
        tmp_session.add(ex)
        tmp_session.add(ex2)
        tmp_session.commit()

        result = _best_effort_comprehension_slug(
            tmp_session, annee=2018, centre="Amérique du Nord"
        )
        assert result == "2018_amerique-du-nord"

    def test_returns_none_when_no_match(self, tmp_session):
        # Pas de FrenchExercise inséré → None, sans crash.
        result = _best_effort_comprehension_slug(
            tmp_session, annee=2018, centre="Métropole"
        )
        assert result is None

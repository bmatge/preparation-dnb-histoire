"""Tests de validation du corpus pédagogique committé.

Ces tests parcourent les JSON de ``content/**`` et vérifient que chacun
valide contre son schéma Pydantic. C'est le filet de sécurité contre une
régression d'extraction Opus, contre un fichier modifié à la main qui
casserait son schéma, et contre l'oubli d'un champ requis lors d'une
évolution future du modèle.

On ne teste pas le contenu sémantique (liberté éditoriale conservée),
seulement la validité du schéma. Les tests sont **paramétrés** par fichier
pour qu'une régression sur un sujet précis échoue avec un message clair.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.francais.comprehension.models import ComprehensionExercise
from app.francais.redaction.models import RedactionSubject

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

REDACTION_DIR = REPO_ROOT / "content" / "francais" / "redaction" / "subjects"
COMPREHENSION_DIR = (
    REPO_ROOT / "content" / "francais" / "comprehension" / "exercises"
)


def _list_jsons(d: Path) -> list[Path]:
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.json") if p.name != "_all.json")


# ============================================================================
# Rédaction française
# ============================================================================


REDACTION_JSONS = _list_jsons(REDACTION_DIR)


@pytest.mark.skipif(
    not REDACTION_JSONS, reason="Aucun sujet de rédaction dans le corpus."
)
@pytest.mark.parametrize(
    "json_path", REDACTION_JSONS, ids=lambda p: p.stem
)
def test_redaction_subject_valides_schema(json_path: Path):
    """Chaque JSON de rédaction doit valider contre ``RedactionSubject``."""
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    subj = RedactionSubject.model_validate(raw)

    # Garde-fous métier supplémentaires.
    assert subj.sujet_imagination.type == "imagination"
    assert subj.sujet_reflexion.type == "reflexion"
    assert subj.source.annee >= 2000
    assert len(subj.sujet_imagination.consigne.strip()) > 10
    assert len(subj.sujet_reflexion.consigne.strip()) > 10


def test_redaction_corpus_size():
    """Le corpus rédaction doit contenir au moins 30 sujets (38 attendus)."""
    if not REDACTION_DIR.exists():
        pytest.skip("Corpus rédaction absent.")
    assert len(REDACTION_JSONS) >= 30, (
        f"Corpus rédaction trop petit : {len(REDACTION_JSONS)} sujets "
        "(38 attendus). Re-lancer scripts/extract_french_redactions.py ?"
    )


def test_redaction_slugs_are_unique():
    """Pas de doublon de slug ``id`` dans le corpus rédaction."""
    if not REDACTION_JSONS:
        pytest.skip("Corpus rédaction absent.")
    seen: set[str] = set()
    for path in REDACTION_JSONS:
        raw = json.loads(path.read_text(encoding="utf-8"))
        slug = raw.get("id")
        assert slug, f"Slug vide dans {path.name}"
        assert slug not in seen, (
            f"Slug dupliqué : {slug} déjà vu avant {path.name}"
        )
        seen.add(slug)


# ============================================================================
# Compréhension française
# ============================================================================


COMPREHENSION_JSONS = _list_jsons(COMPREHENSION_DIR)


@pytest.mark.skipif(
    not COMPREHENSION_JSONS,
    reason="Aucun exercice de compréhension dans le corpus.",
)
@pytest.mark.parametrize(
    "json_path", COMPREHENSION_JSONS, ids=lambda p: p.stem
)
def test_comprehension_exercise_valide_schema(json_path: Path):
    """Chaque JSON de compréhension doit valider contre ``ComprehensionExercise``."""
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    exo = ComprehensionExercise.model_validate(raw)

    assert exo.source.annee >= 2000
    assert len(exo.questions) > 0
    assert len(exo.texte_support.lignes) > 0


def test_comprehension_slugs_are_unique():
    if not COMPREHENSION_JSONS:
        pytest.skip("Corpus compréhension absent.")
    seen: set[str] = set()
    for path in COMPREHENSION_JSONS:
        raw = json.loads(path.read_text(encoding="utf-8"))
        slug = raw.get("id")
        assert slug, f"Slug vide dans {path.name}"
        assert slug not in seen, (
            f"Slug dupliqué : {slug} déjà vu avant {path.name}"
        )
        seen.add(slug)

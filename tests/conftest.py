"""Configuration partagée des tests.

Vague 1 : on ne teste que des fonctions pures et le round-trip JSON →
modèle → DB. Pas d'Albert, pas de RAG, pas de réseau. Toutes les fixtures
qui touchent à la DB utilisent un fichier SQLite temporaire isolé pour ne
pas polluer ``data/app.db``.

Note : on neutralise ``ALBERT_API_KEY`` à l'import pour empêcher tout test
de toucher inadvertamment au client live (le constructeur d'``AlbertClient``
exige la variable, donc l'absence garantit qu'aucun test ne l'instancie
silencieusement).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlmodel import SQLModel, Session as DBSession, create_engine

# Imports nécessaires pour enregistrer toutes les tables dans
# ``SQLModel.metadata`` AVANT le ``create_all`` des fixtures. Sans ces
# imports, les FK croisées (ex. ``Session.subject_id`` qui pointe sur
# ``subject``) provoquent un ``NoReferencedTableError`` au moment de la
# création des tables. On suit le même ordre que ``app/core/main.py``.
from app.core import db as _core_db  # noqa: F401  (enregistre Session/Turn)
from app.francais.comprehension import models as _fr_comp_models  # noqa: F401
from app.francais.redaction import models as _fr_redac_models  # noqa: F401
from app.histoire_geo_emc.developpement_construit import (  # noqa: F401
    models as _hgemc_models,
)
from app.histoire_geo_emc.reperes import models as _reperes_models  # noqa: F401

# La racine du repo (un parent du dossier tests/).
REPO_ROOT = Path(__file__).resolve().parent.parent


# Empêche tout test de tomber sur une vraie clé Albert. Si un test essaie
# d'instancier AlbertClient sans mock, ça lèvera RuntimeError immédiatement
# au lieu de partir en appel réseau.
os.environ.pop("ALBERT_API_KEY", None)


from tests.fakes import FakeAlbertClient, FakeRagClient


@pytest.fixture()
def tmp_engine(tmp_path):
    """Engine SQLite isolé sur fichier temporaire.

    On ne peut pas utiliser ``:memory:`` parce que les loaders ouvrent
    leur propre ``DBSession(get_engine())`` et que la mémoire serait
    perdue entre la fixture et le code testé. Un fichier dans
    ``tmp_path`` règle le problème et est nettoyé automatiquement par
    pytest en fin de test.
    """
    db_path = tmp_path / "test_app.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture()
def tmp_session(tmp_engine):
    """Session SQLModel ouverte sur l'engine temporaire."""
    with DBSession(tmp_engine) as s:
        yield s


# ============================================================================
# Vague 2 : fixtures TestClient FastAPI avec Albert/RAG mockés
# ============================================================================
#
# Le harness ci-dessous monte la vraie app FastAPI mais swappe trois choses
# au niveau module avant qu'aucune session de test ne s'ouvre :
#
# 1. ``app.core.db._engine``  → un engine SQLite isolé (fichier ``tmp_path``)
#    pour ne pas écrire dans ``data/app.db`` du dev.
# 2. ``app.core.rag._default_client`` → un ``FakeRagClient`` qui ne touche
#    pas Albert (sinon le constructeur réel exige ``ALBERT_API_KEY``).
# 3. Les singletons ``_albert_client`` (DC HG, FR rédaction) et ``_client``
#    (FR compréhension) → un ``FakeAlbertClient`` partagé par les trois
#    pédagogies, pour qu'un test puisse vérifier les call-traces du même
#    fake quel que soit le router invoqué.
#
# Les startup events de FastAPI (``on_startup``) tournent à l'entrée du
# context manager ``with TestClient(app):``. Comme l'engine est déjà patché
# à ce moment-là, ``init_db()`` crée les tables dans la DB de test et les
# loaders métier (``init_hgemc_subjects``, ``init_french_redaction``…)
# rechargent depuis ``content/**`` dans le même engine. Le corpus réel est
# donc disponible dans tous les tests, sans pollution croisée.


@pytest.fixture()
def fake_albert():
    return FakeAlbertClient()


@pytest.fixture()
def fake_rag():
    return FakeRagClient()


@pytest.fixture()
def test_client(tmp_path, monkeypatch, fake_albert, fake_rag):
    """Lance une instance FastAPI complète sur DB temporaire + Albert mocké.

    Les startup events sont déclenchés (chargement réel du corpus depuis
    ``content/**`` dans la DB temporaire). À utiliser avec une assertion
    sur ``fake_albert.calls`` quand on veut inspecter ce qui aurait été
    envoyé à Albert en prod.
    """
    db_path = tmp_path / "test_app.db"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    # ── 1. Engine de test ────────────────────────────────────────────────
    from app.core import db as core_db

    monkeypatch.setattr(core_db, "_engine", test_engine)

    # ── 2. RAG mocké ──────────────────────────────────────────────────────
    from app.core import rag as core_rag

    monkeypatch.setattr(core_rag, "_default_client", fake_rag)

    # ── 3. Singletons AlbertClient mockés dans chaque pédagogie ──────────
    from app.histoire_geo_emc.developpement_construit import (
        pedagogy as hgemc_ped,
    )
    from app.francais.redaction import pedagogy as fr_redac_ped
    from app.francais.comprehension import pedagogy as fr_comp_ped

    monkeypatch.setattr(hgemc_ped, "_albert_client", fake_albert)
    monkeypatch.setattr(fr_redac_ped, "_albert_client", fake_albert)
    monkeypatch.setattr(fr_comp_ped, "_client", fake_albert)

    # ── 4. App + TestClient ──────────────────────────────────────────────
    from fastapi.testclient import TestClient
    from app.core import main as core_main

    with TestClient(core_main.app) as client:
        yield client

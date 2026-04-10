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

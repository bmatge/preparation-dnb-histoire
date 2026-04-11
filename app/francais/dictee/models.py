"""Modèles pour la sous-épreuve « Dictée » du DNB français.

- Schémas Pydantic alignés sur le JSON produit par `scripts/extract_dictees.py`
- Table SQLModel `FrenchDictee` qui stocke le contenu riche dans `data_json`,
  sur le même modèle que `FrenchExercise` et `FrenchRedactionSubject`.

`Session.subject_id` du core pointe ici vers `FrenchDictee.id` quand
`subject_kind == "francais_dictee"`.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field as PField
from sqlmodel import Field, SQLModel

SUBJECT_KIND = "francais_dictee"


# ============================================================================
# Schémas Pydantic
# ============================================================================


class Source(BaseModel):
    annee: int
    session: str
    centre: str
    code_sujet: str | None = None


class Reference(BaseModel):
    auteur: str
    oeuvre: str
    annee_publication: int | None = None


class Difficulte(BaseModel):
    type: Literal[
        "lexicale",
        "accord",
        "conjugaison",
        "homophone",
        "trait_union",
        "majuscule",
        "apostrophe",
        "autre",
    ]
    mot: str
    explication: str


class Phrase(BaseModel):
    ordre: int
    texte: str
    difficultes: list[Difficulte] = PField(default_factory=list)


class Dictee(BaseModel):
    id: str
    source: Source
    titre: str | None = None
    reference: Reference
    texte_complet: str
    phrases: list[Phrase]
    notes_examinateur: list[str] = PField(default_factory=list)
    source_file: str | None = None


# ============================================================================
# Table SQLModel
# ============================================================================


class FrenchDictee(SQLModel, table=True):
    """Une dictée DNB chargée depuis les JSON extraits."""

    __tablename__ = "french_dictee"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    source_file: str
    annee: int = Field(index=True)
    centre: str
    data_json: str

    def load(self) -> Dictee:
        return Dictee.model_validate_json(self.data_json)


__all__ = [
    "SUBJECT_KIND",
    "Source",
    "Reference",
    "Difficulte",
    "Phrase",
    "Dictee",
    "FrenchDictee",
]

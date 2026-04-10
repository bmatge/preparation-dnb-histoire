"""Modèles pour la sous-épreuve « Rédaction » du DNB français.

Contient :

1. Des schémas Pydantic alignés sur le JSON produit par
   ``scripts/extract_french_redactions.py`` : un sujet de rédaction DNB
   contient toujours **deux options au choix** pour l'élève
   (``sujet_imagination`` et ``sujet_reflexion``), optionnellement liées à
   un texte support via ``texte_support_ref``.

2. Une table SQLModel ``FrenchRedactionSubject`` qui stocke le contenu
   riche dans une colonne JSON (``data_json``), sur le même modèle que
   ``FrenchExercise`` — pas de normalisation multi-tables, évolution du
   schéma sans migration.

Rappel contexte : ``Session.subject_id`` de ``app.core.db`` pointe ici
vers ``FrenchRedactionSubject.id`` quand
``subject_kind == "francais_redaction"``.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field as PField
from sqlmodel import Field, SQLModel

SUBJECT_KIND = "francais_redaction"


# ============================================================================
# Schémas Pydantic alignés sur le JSON produit par l'extraction Opus
# ============================================================================


class Source(BaseModel):
    annee: int
    session: str
    centre: str
    code_sujet: str | None = None


class Epreuve(BaseModel):
    intitule: str = "Rédaction"
    duree_minutes: int = 90
    points_total: int = 40


class SujetOption(BaseModel):
    """Une des deux options proposées à l'élève (imagination ou réflexion)."""

    type: Literal["imagination", "reflexion"]
    numero: str  # ex: "Sujet A", "1", "Sujet d'imagination"
    amorce: str | None = None
    # Texte de l'énoncé tel qu'il apparaît sur le sujet (consigne principale).
    consigne: str
    # Contraintes explicitement listées dans le sujet : longueur, point de
    # vue, registre, personnages imposés, etc. Liste vide si aucune.
    contraintes: list[str] = PField(default_factory=list)
    # Longueur minimale suggérée en nombre de lignes si le sujet la précise.
    longueur_min_lignes: int | None = None
    # Référence éventuelle au texte support de l'épreuve de compréhension
    # (ex. "cf. texte de Colette lignes 15 à 20"). Null si le sujet ne
    # renvoie pas au texte support.
    reference_texte_support: str | None = None


class RedactionSubject(BaseModel):
    """Un sujet de rédaction DNB avec ses deux options au choix."""

    id: str
    source: Source
    epreuve: Epreuve = PField(default_factory=Epreuve)
    # Lien best-effort vers l'exercice de compréhension correspondant
    # (même année + même centre). Null si introuvable côté loader.
    texte_support_ref: str | None = None
    sujet_imagination: SujetOption
    sujet_reflexion: SujetOption
    source_file: str | None = None


# ============================================================================
# Table SQLModel (catalogue)
# ============================================================================


class FrenchRedactionSubject(SQLModel, table=True):
    """Un sujet de rédaction DNB, chargé depuis les JSON extraits."""

    __tablename__ = "french_redaction_subject"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    source_file: str
    annee: int = Field(index=True)
    centre: str
    # Slug de l'exercice de compréhension lié (best-effort, peut être None).
    texte_support_ref: str | None = Field(default=None, index=True)
    data_json: str

    def load(self) -> RedactionSubject:
        raw = json.loads(self.data_json)
        return RedactionSubject.model_validate(raw)


__all__ = [
    "SUBJECT_KIND",
    "Source",
    "Epreuve",
    "SujetOption",
    "RedactionSubject",
    "FrenchRedactionSubject",
]

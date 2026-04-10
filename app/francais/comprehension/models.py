"""Modèles pour la sous-épreuve « Compréhension et compétences d'interprétation ».

Contient :

1. Une table SQLModel `FrenchExercise` — catalogue des annales de compréhension,
   chargé au démarrage depuis `content/francais/comprehension/exercises/*.json`.
   Le contenu riche (texte, questions, notes, image) est stocké dans une
   colonne JSON (`data_json`) pour rester simple : pas de normalisation
   multi-tables, schéma facile à faire évoluer.

2. Des schémas Pydantic (`ComprehensionExercise`, `Question`, `SousQuestion`,
   etc.) qui valident et typent le contenu JSON au moment de la lecture. Ces
   schémas sont alignés sur ceux utilisés dans `scripts/extract_french_exercises.py`
   — volontairement, pour que les JSON produits par l'extraction soient
   directement consommables par l'app sans transformation.

3. Une représentation « flattened » (`ExerciseItem`) qui déplie les questions
   et sous-questions en une liste linéaire ordonnée, adaptée au parcours
   élève question par question.

Rappel contexte : `Session.subject_id` de `app.core.db` pointe ici vers
`FrenchExercise.id` quand `subject_kind == "francais_comprehension"`. Cette
relation n'est pas exprimée par une contrainte FK DB (SQLite ne les applique
pas par défaut et la colonne appartient à la table matière HG-EMC dans
l'intention d'origine), c'est une convention applicative.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field as PField
from sqlmodel import Field, SQLModel

SUBJECT_KIND = "francais_comprehension"


# ============================================================================
# Schémas Pydantic alignés sur le JSON produit par l'extraction Opus
# ============================================================================


class Source(BaseModel):
    annee: int
    session: str
    centre: str
    code_sujet: str | None = None


class Epreuve(BaseModel):
    intitule: str
    duree_minutes: int
    points_total: int
    points_comprehension: int
    points_grammaire: int


class Ligne(BaseModel):
    n: int
    texte: str


class TexteSupport(BaseModel):
    auteur: str
    auteur_note: str | None = None
    oeuvre: str
    partie: str | None = None
    annee_publication: int | None = None
    genre: str
    lignes: list[Ligne]


class NoteTexte(BaseModel):
    n: int
    terme: str
    definition: str


class Image(BaseModel):
    type: str
    auteur: str | None = None
    titre: str | None = None
    annee: int | None = None
    description_visuelle: str


class LignesCiblees(BaseModel):
    start: int
    end: int


class SousQuestion(BaseModel):
    lettre: str
    enonce: str
    points: float
    competence: str | None = None


class Question(BaseModel):
    numero: str
    partie: Literal["comprehension", "grammaire"]
    type: Literal["standard", "reecriture"] = "standard"
    enonce: str
    citation: str | None = None
    mots_soulignes: list[str] = PField(default_factory=list)
    lignes_ciblees: list[LignesCiblees] = PField(default_factory=list)
    passage_a_reecrire: str | None = None
    contraintes: list[str] = PField(default_factory=list)
    sous_questions: list[SousQuestion] = PField(default_factory=list)
    points: float
    competence: str | None = None
    necessite_image: bool = False


class ComprehensionExercise(BaseModel):
    """Contenu complet d'un sujet de compréhension, tel qu'extrait par Opus."""

    id: str
    source: Source
    epreuve: Epreuve
    paratexte: str | None = None
    texte_support: TexteSupport
    notes_texte: list[NoteTexte] = PField(default_factory=list)
    image: Image | None = None
    questions: list[Question]
    source_file: str | None = None

    def flatten_items(
        self,
        *,
        include_image_questions: bool = False,
        include_grammar: bool = False,
        include_reecriture: bool = False,
    ) -> list["ExerciseItem"]:
        """Déplie les questions/sous-questions en une liste linéaire.

        Pour le parcours élève, une question avec sous-questions compte pour
        N étapes (une par sous-question). Une question simple compte pour
        une étape. Les items sont numérotés `order` de 1 à N dans l'ordre
        officiel du sujet.

        Les filtres du MVP excluent par défaut :
        - les questions qui nécessitent l'image (pas encore rendu en UI) ;
        - les questions de grammaire (MVP = compréhension seule) ;
        - la question de réécriture (exercice pédagogiquement à part).
        """
        items: list[ExerciseItem] = []
        order = 0
        for q in self.questions:
            if q.partie == "grammaire" and not include_grammar:
                continue
            if q.type == "reecriture" and not include_reecriture:
                continue
            if q.necessite_image and not include_image_questions:
                continue

            if q.sous_questions:
                for sq in q.sous_questions:
                    order += 1
                    items.append(
                        ExerciseItem(
                            order=order,
                            question_numero=q.numero,
                            sous_question_lettre=sq.lettre,
                            partie=q.partie,
                            type=q.type,
                            enonce_complet=_build_enonce_complet(q, sq),
                            citation=q.citation,
                            lignes_ciblees=q.lignes_ciblees,
                            points=sq.points,
                            competence=sq.competence or q.competence,
                            necessite_image=q.necessite_image,
                        )
                    )
            else:
                order += 1
                items.append(
                    ExerciseItem(
                        order=order,
                        question_numero=q.numero,
                        sous_question_lettre=None,
                        partie=q.partie,
                        type=q.type,
                        enonce_complet=_build_enonce_complet(q, None),
                        citation=q.citation,
                        lignes_ciblees=q.lignes_ciblees,
                        points=q.points,
                        competence=q.competence,
                        necessite_image=q.necessite_image,
                    )
                )
        return items


class ExerciseItem(BaseModel):
    """Item présenté à l'élève : question ou sous-question atomique."""

    order: int
    question_numero: str
    sous_question_lettre: str | None
    partie: Literal["comprehension", "grammaire"]
    type: Literal["standard", "reecriture"]
    enonce_complet: str
    citation: str | None
    lignes_ciblees: list[LignesCiblees]
    points: float
    competence: str | None
    necessite_image: bool

    @property
    def label(self) -> str:
        """Libellé affichable : '4a', '7', '10'."""
        if self.sous_question_lettre:
            return f"{self.question_numero}{self.sous_question_lettre}"
        return self.question_numero


def _build_enonce_complet(q: Question, sq: SousQuestion | None) -> str:
    """Compose l'énoncé effectivement présenté à l'élève pour cet item.

    Si la question principale contient une introduction / citation / contexte
    commun aux sous-questions, on le préfixe à l'énoncé de la sous-question
    pour ne pas perdre d'information quand l'item est présenté seul.
    """
    if sq is None:
        return q.enonce
    # L'énoncé de la question principale peut contenir une citation de
    # contexte (ex. « En de certains endroits, elle était fort profonde. »
    # (ligne 3)) — on le garde en préfixe si la sous-question ne le répète pas.
    main = q.enonce.strip()
    sub = sq.enonce.strip()
    if main and main not in sub:
        return f"{main}\n\n{sub}"
    return sub


# ============================================================================
# Table SQLModel (catalogue)
# ============================================================================


class FrenchExercise(SQLModel, table=True):
    """Une annale de compréhension, chargée depuis les JSON extraits.

    Le contenu riche (texte, questions, image) vit dans `data_json` pour
    rester souple : les schémas évoluent sans migration DB, et l'app désérialise
    vers `ComprehensionExercise` au moment de la lecture.

    Nom de table explicite pour éviter toute collision avec d'éventuels
    futurs modèles d'autres sous-épreuves français (dictee, redaction).
    """

    __tablename__ = "french_exercise"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    source_file: str
    annee: int = Field(index=True)
    centre: str
    data_json: str

    def load(self) -> ComprehensionExercise:
        """Désérialise `data_json` en objet Pydantic validé."""
        raw = json.loads(self.data_json)
        return ComprehensionExercise.model_validate(raw)


__all__ = [
    "SUBJECT_KIND",
    "Source",
    "Epreuve",
    "Ligne",
    "TexteSupport",
    "NoteTexte",
    "Image",
    "LignesCiblees",
    "SousQuestion",
    "Question",
    "ComprehensionExercise",
    "ExerciseItem",
    "FrenchExercise",
]

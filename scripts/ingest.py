"""
Ingestion du corpus DNB dans les collections RAG d'Albert.

Ce script pousse les PDF du dépôt vers les collections Albert qui servent
de base de connaissances au tuteur. Les collections sont préfixées par
matière (`dnb_<matière>_<type>`) pour isoler les contextes et éviter qu'une
recherche en français ne remonte un corrigé d'histoire.

Collections histoire-géo-EMC :

  - dnb_hgemc_sujets      : sujets "développement construit" extraits des
                            annales (JSON produits par extract_subjects.py)
  - dnb_hgemc_corriges    : corrigés modèles des DNB
  - dnb_hgemc_methodo     : fiches méthodologiques
  - dnb_hgemc_programmes  : programmes officiels cycle 4 (garde-fou
                            anti-hallucination)

Collections français :

  - dnb_francais_programme        : programme cycle 4 + attendus fin de
                                    3e/4e/5e + repères de progression
  - dnb_francais_methodo          : fiches méthodologiques (classes
                                    grammaticales, propositions,
                                    conjugaison, compréhension…)
  - dnb_francais_redaction_sujets : consignes de rédaction (imagination /
                                    réflexion) extraites des annales DNB
                                    (JSON → markdown à la volée)

Principe :
- On laisse Albert faire le chunking (RecursiveCharacterTextSplitter côté serveur)
  via `POST /v1/documents` en multipart/form-data. On ne pré-chunk pas.
- On ne gère PAS les embeddings manuellement — Albert les produit avec son modèle
  d'embedding configuré pour la collection (bge-m3).
- Idempotence : on stocke un hash sha256 du fichier dans une petite table locale
  SQLite (data/ingest_state.db). Un re-run ne re-pousse que les fichiers modifiés.
- Les collections sont créées à la demande si elles n'existent pas.

Usage :
    source .env
    .venv/bin/python -m scripts.ingest                          # ingère tout
    .venv/bin/python -m scripts.ingest --only corriges          # une seule collection
    .venv/bin/python -m scripts.ingest --only fr_programme --only fr_methodo
    .venv/bin/python -m scripts.ingest --matiere francais       # toutes les français
    .venv/bin/python -m scripts.ingest --matiere hgemc          # toutes les HG-EMC
    .venv/bin/python -m scripts.ingest --force                  # re-pousse tout
    .venv/bin/python -m scripts.ingest --dry-run                # simule

Corpus attendu (depuis la racine du repo) :
    content/histoire-geo-emc/annales/*.pdf           →  dnb_hgemc_sujets (via .../subjects/*.json)
    content/histoire-geo-emc/corriges/*.pdf          →  dnb_hgemc_corriges
    content/histoire-geo-emc/methodologie/*.pdf,*.md →  dnb_hgemc_methodo
    content/histoire-geo-emc/programme/*.pdf         →  dnb_hgemc_programmes
    content/francais/programme/*.pdf                 →  dnb_francais_programme
    content/francais/methodologie/*.pdf,*.md         →  dnb_francais_methodo
    content/francais/redaction/subjects/*.json       →  dnb_francais_redaction_sujets
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import httpx

logger = logging.getLogger(__name__)

# ============================================================================
# Configuration
# ============================================================================

DEFAULT_BASE_URL = "https://albert.api.etalab.gouv.fr/v1"
STATE_DB = Path("data/ingest_state.db")

REPO_ROOT = Path(__file__).resolve().parent.parent
HGEMC_CONTENT = REPO_ROOT / "content" / "histoire-geo-emc"
FRANCAIS_CONTENT = REPO_ROOT / "content" / "francais"
MATH_CONTENT = REPO_ROOT / "content" / "mathematiques"
SCIENCES_CONTENT = REPO_ROOT / "content" / "sciences"


@dataclass(frozen=True)
class CollectionSpec:
    """Définition d'une des 4 collections cibles."""

    key: str  # identifiant court utilisé en CLI ex: "corriges"
    name: str  # nom complet côté Albert ex: "dnb_corriges"
    description: str
    sources: list[Path]  # dossiers/fichiers à ingérer
    file_patterns: tuple[str, ...] = ("*.pdf",)


# Correspondance matière → liste des clés de collections. Sert au CLI
# `--matiere <nom>` pour sélectionner d'un coup toutes les collections d'une
# matière.
MATIERE_COLLECTIONS: dict[str, tuple[str, ...]] = {
    "hgemc": ("corriges", "methodo", "programmes", "sujets"),
    "francais": ("fr_programme", "fr_methodo", "fr_redaction_sujets"),
    "mathematiques": ("math_programmes", "math_methodo", "math_sujets"),
    "sciences": (
        "sciences_programme",
        "sciences_methodo",
        "sciences_annales",
        "sciences_revision_questions",
    ),
}


COLLECTIONS: dict[str, CollectionSpec] = {
    "corriges": CollectionSpec(
        key="corriges",
        name="dnb_hgemc_corriges",
        description="Corrigés officiels et modèles de DNB histoire-géo-EMC.",
        sources=[HGEMC_CONTENT / "corriges"],
    ),
    "methodo": CollectionSpec(
        key="methodo",
        name="dnb_hgemc_methodo",
        description="Fiches méthodologiques pour le développement construit au DNB.",
        sources=[HGEMC_CONTENT / "methodologie"],
        file_patterns=("*.pdf", "*.md"),
    ),
    "programmes": CollectionSpec(
        key="programmes",
        name="dnb_hgemc_programmes",
        description="Programmes officiels cycle 4 histoire-géo-EMC. Source d'autorité anti-hallucination.",
        sources=[HGEMC_CONTENT / "programme"],
    ),
    "sujets": CollectionSpec(
        key="sujets",
        name="dnb_hgemc_sujets",
        description="Consignes de développement construit extraites des annales DNB.",
        sources=[HGEMC_CONTENT / "subjects"],
        file_patterns=("*.json",),
    ),
    "fr_programme": CollectionSpec(
        key="fr_programme",
        name="dnb_francais_programme",
        description=(
            "Programme officiel français cycle 4 + attendus fin de 3e/4e/5e + "
            "repères annuels de progression. Source d'autorité anti-hallucination "
            "pour les questions de compréhension, grammaire et réécriture."
        ),
        sources=[FRANCAIS_CONTENT / "programme"],
    ),
    "fr_methodo": CollectionSpec(
        key="fr_methodo",
        name="dnb_francais_methodo",
        description=(
            "Fiches méthodologiques français DNB : classes grammaticales, "
            "propositions et groupes de mots, liens logiques, fabrication des "
            "mots, conjugaison (modes et temps), poésie/théâtre et figures de "
            "style, compréhension du texte littéraire, orthographe."
        ),
        sources=[FRANCAIS_CONTENT / "methodologie"],
        file_patterns=("*.pdf", "*.md"),
    ),
    "fr_redaction_sujets": CollectionSpec(
        key="fr_redaction_sujets",
        name="dnb_francais_redaction_sujets",
        description=(
            "Sujets de rédaction DNB français (2018-2025) : pour chaque "
            "annale, les deux options proposées (imagination / réflexion) "
            "avec leur consigne, leurs contraintes et leur éventuelle "
            "référence au texte support de compréhension. Sert au RAG de "
            "la sous-épreuve rédaction (correction finale)."
        ),
        sources=[FRANCAIS_CONTENT / "redaction" / "subjects"],
        file_patterns=("*.json",),
    ),
    "math_programmes": CollectionSpec(
        key="math_programmes",
        name="dnb_math_programmes",
        description=(
            "Programmes officiels de mathématiques cycle 4 + attendus de "
            "fin de 3e/4e/5e + repères annuels de progression. Source "
            "d'autorité anti-hallucination pour les automatismes DNB 2026."
        ),
        sources=[MATH_CONTENT / "programme"],
    ),
    "math_methodo": CollectionSpec(
        key="math_methodo",
        name="dnb_math_methodo",
        description=(
            "Fiches méthodologiques mathématiques DNB : automatismes au "
            "collège, cadrage de l'épreuve 2026, modalités d'évaluation 3e, "
            "et fiches thématiques (calcul numérique, calcul littéral, "
            "géométrie plane, trigonométrie, géométrie dans l'espace, "
            "fonctions, statistiques et probabilités, algorithmique)."
        ),
        sources=[MATH_CONTENT / "methodologie"],
    ),
    "math_sujets": CollectionSpec(
        key="math_sujets",
        name="dnb_math_automatismes_sujets",
        description=(
            "Banque de questions d'automatismes DNB 2026 (sujets zéro "
            "officiels + questions générées ancrées sur la liste indicative "
            "d'octobre 2025). Convertie en markdown à la volée à l'upload "
            "(Albert ne parse pas les .json)."
        ),
        sources=[MATH_CONTENT / "automatismes" / "questions"],
        file_patterns=("*.json",),
    ),
    "sciences_programme": CollectionSpec(
        key="sciences_programme",
        name="dnb_sciences_programme",
        description=(
            "Programme officiel cycle 4 pour les trois disciplines "
            "scientifiques (Physique-Chimie, SVT, Technologie). Source "
            "d'autorité anti-hallucination pour les questions de "
            "révision sciences DNB 2026."
        ),
        sources=[SCIENCES_CONTENT / "programme"],
    ),
    "sciences_methodo": CollectionSpec(
        key="sciences_methodo",
        name="dnb_sciences_methodo",
        description=(
            "Fiches méthodologiques sciences DNB : huit fiches "
            "thématiques couvrant l'organisation de la matière, les "
            "mouvements et l'énergie, l'électricité et les signaux, "
            "l'univers et les mélanges, le corps humain et la santé, "
            "la Terre et l'évolution, la génétique et la technologie."
        ),
        sources=[SCIENCES_CONTENT / "methodologie"],
    ),
    "sciences_annales": CollectionSpec(
        key="sciences_annales",
        name="dnb_sciences_annales",
        description=(
            "Annales du DNB Sciences 2018-2025 (série générale). "
            "Inclut les sujets de métropole, Amérique du Nord/Sud, "
            "Asie, Polynésie, Nouvelle-Calédonie et centres étrangers. "
            "Utilisé pour ancrer les feedbacks Albert sur des exemples "
            "concrets d'énoncés proches."
        ),
        sources=[SCIENCES_CONTENT / "annales"],
    ),
    "sciences_revision_questions": CollectionSpec(
        key="sciences_revision_questions",
        name="dnb_sciences_revision_questions",
        description=(
            "Banque de questions de révision sciences DNB 2026, "
            "structurée par discipline (Physique-Chimie, SVT, "
            "Technologie) et par thème. Convertie en markdown à la "
            "volée à l'upload (Albert ne parse pas les .json)."
        ),
        sources=[SCIENCES_CONTENT / "revision" / "questions"],
        file_patterns=("*.json",),
    ),
}

# Fichiers à exclure d'office lors de l'itération (nom, pas glob).
# `_all.json` est l'aggrégat legacy côté HG-EMC. Tout autre fichier qui
# commence par « _ » est un méta-fichier (cf. `_liste_officielle.json` côté
# automatismes maths) et n'a pas vocation à être indexé non plus.
EXCLUDED_FILENAMES = {"_all.json"}
EXCLUDED_PREFIXES = ("_",)


# ============================================================================
# State local (idempotence)
# ============================================================================


def _init_state_db() -> sqlite3.Connection:
    """Crée la table de suivi d'ingestion si absente."""
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingested (
            collection_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            albert_document_id INTEGER,
            pushed_at INTEGER NOT NULL,
            PRIMARY KEY (collection_name, file_path)
        )
        """
    )
    conn.commit()
    return conn


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _already_ingested(
    conn: sqlite3.Connection, collection_name: str, file_path: Path, sha: str
) -> bool:
    row = conn.execute(
        "SELECT sha256 FROM ingested WHERE collection_name = ? AND file_path = ?",
        (collection_name, str(file_path)),
    ).fetchone()
    return row is not None and row[0] == sha


def _record_ingestion(
    conn: sqlite3.Connection,
    collection_name: str,
    file_path: Path,
    sha: str,
    document_id: int | None,
) -> None:
    import time

    conn.execute(
        """
        INSERT OR REPLACE INTO ingested
            (collection_name, file_path, sha256, albert_document_id, pushed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (collection_name, str(file_path), sha, document_id, int(time.time())),
    )
    conn.commit()


# ============================================================================
# Client HTTP Albert
# ============================================================================


class AlbertRagClient:
    """Mini-client HTTP pour les endpoints collections/documents/search d'Albert.

    On n'utilise pas le SDK OpenAI ici parce que ces endpoints sont propres à
    Albert (pas dans la spec OpenAI).
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0):
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._http = httpx.Client(timeout=timeout, headers=self._headers)

    def close(self) -> None:
        self._http.close()

    # ---- collections -------------------------------------------------------

    def list_collections(self) -> list[dict]:
        r = self._http.get(f"{self._base}/collections", params={"limit": 100})
        r.raise_for_status()
        return r.json().get("data", [])

    def get_collection_by_name(self, name: str) -> dict | None:
        collections = self.list_collections()
        return next((c for c in collections if c.get("name") == name), None)

    def create_collection(self, name: str, description: str) -> dict:
        payload = {"name": name, "description": description, "visibility": "private"}
        r = self._http.post(f"{self._base}/collections", json=payload)
        r.raise_for_status()
        return r.json()

    def ensure_collection(self, name: str, description: str) -> int:
        """Retourne l'ID de la collection, la crée si besoin."""
        existing = self.get_collection_by_name(name)
        if existing:
            logger.info("  ↪ collection %r existe déjà (id=%s)", name, existing["id"])
            return existing["id"]
        created = self.create_collection(name, description)
        logger.info("  ✓ collection %r créée (id=%s)", name, created["id"])
        return created["id"]

    # ---- documents ---------------------------------------------------------

    def upload_document(
        self,
        collection_id: int,
        file_path: Path,
        display_name: str | None = None,
        chunk_size: int = 2048,
        *,
        content_override: bytes | None = None,
        virtual_filename: str | None = None,
        mime_override: str | None = None,
    ) -> dict:
        """Pousse un fichier dans une collection. Albert fait le chunking côté serveur.

        Si content_override est fourni, on upload ce contenu plutôt que le fichier
        du disque (utile pour transformer un JSON en markdown avant upload).
        """
        if content_override is not None:
            filename = virtual_filename or file_path.name
            mime = mime_override or "text/markdown"
            files = {"file": (filename, io.BytesIO(content_override), mime)}
            data = {"collection_id": str(collection_id), "chunk_size": str(chunk_size)}
            if display_name:
                data["name"] = display_name
            r = self._http.post(f"{self._base}/documents", files=files, data=data)
            r.raise_for_status()
            return r.json()

        with file_path.open("rb") as f:
            files = {
                "file": (file_path.name, f, _guess_mime(file_path)),
            }
            data = {
                "collection_id": str(collection_id),
                "chunk_size": str(chunk_size),
            }
            if display_name:
                data["name"] = display_name
            r = self._http.post(
                f"{self._base}/documents", files=files, data=data
            )
        r.raise_for_status()
        return r.json()

    def delete_document(self, document_id: int) -> None:
        r = self._http.delete(f"{self._base}/documents/{document_id}")
        r.raise_for_status()


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".json": "application/json",
    }.get(ext, "application/octet-stream")


def _subject_json_to_markdown(json_path: Path) -> tuple[bytes, str]:
    """Transforme un JSON de sujet produit par extract_subjects.py en markdown.

    Albert n'ingère pas bien les JSON (parser interne échoue avec 422).
    On convertit chaque DC en un petit document markdown avec des sections
    claires — plus facile pour le modèle d'embedding et pour le RAG runtime.

    Retourne (contenu_markdown_utf8, nom_virtuel_md).
    """
    data = json.loads(json_path.read_text())
    year = data.get("year") or "?"
    serie = data.get("serie") or ""
    session_label = data.get("session_label") or data.get("session") or ""
    source = data.get("source_file") or json_path.stem

    lines: list[str] = []
    lines.append(f"# Sujets DC — DNB {year} {session_label} ({serie})".strip())
    lines.append("")
    lines.append(f"Source : {source}")
    lines.append("")

    for idx, dc in enumerate(data.get("developpements_construits", []), start=1):
        lines.append(f"## Développement construit {idx}")
        lines.append("")
        lines.append(f"**Discipline** : {dc.get('discipline','?')}")
        lines.append("")
        lines.append(f"**Thème** : {dc.get('theme','?')}")
        lines.append("")
        lines.append(f"**Consigne** : {dc.get('consigne','?')}")
        lines.append("")
        if dc.get("verbe_cle"):
            lines.append(f"**Verbe-clé** : {dc['verbe_cle']}")
            lines.append("")
        if dc.get("bornes_chrono"):
            lines.append(f"**Bornes chronologiques** : {dc['bornes_chrono']}")
            lines.append("")
        if dc.get("bornes_spatiales"):
            lines.append(f"**Bornes spatiales** : {dc['bornes_spatiales']}")
            lines.append("")
        notions = dc.get("notions_attendues") or []
        if notions:
            lines.append("**Notions attendues** :")
            for n in notions:
                lines.append(f"- {n}")
            lines.append("")
        if dc.get("bareme_points"):
            lines.append(f"**Barème** : {dc['bareme_points']} points")
            lines.append("")

    md = "\n".join(lines).encode("utf-8")
    md_name = json_path.stem + ".md"
    return md, md_name


def _math_questions_json_to_markdown(json_path: Path) -> tuple[bytes, str]:
    """Transforme un fichier JSON de questions d'automatismes maths en markdown.

    Le format JSON est celui produit en session Claude Code (cf. issue #21
    et content/mathematiques/automatismes/questions/*.json) : un objet avec
    une clé `questions` dont chaque entrée porte `id`, `theme`, `competence`,
    `enonce`, `scoring` (mode python ou albert), `source` et éventuellement
    des `indices`/`reveal_explication`.

    On produit un bloc markdown par question : énoncé + réponse attendue +
    critères s'il s'agit d'une question ouverte. Les indices pré-calculés
    et l'explication pédagogique sont volontairement OMIS — Albert les
    génère à la volée côté runtime, on n'a pas besoin de les indexer.

    Retourne (contenu_markdown_utf8, nom_virtuel_md).
    """
    data = json.loads(json_path.read_text(encoding="utf-8"))
    questions = data.get("questions") or []
    nom_batch = json_path.stem

    lines: list[str] = []
    lines.append(f"# Banque automatismes maths — batch « {nom_batch} »")
    lines.append("")
    lines.append(f"Source : {json_path.name}")
    lines.append("")

    for q in questions:
        qid = q.get("id", "?")
        theme = q.get("theme", "?")
        competence = q.get("competence", "")
        enonce = (q.get("enonce") or "").strip()
        scoring = q.get("scoring") or {}
        mode = scoring.get("mode", "?")

        lines.append(f"## Question {qid}")
        lines.append("")
        lines.append(f"**Thème** : {theme}")
        if competence:
            lines.append(f"**Compétence** : {competence}")
        lines.append("")
        lines.append(f"**Énoncé** : {enonce}")
        lines.append("")

        if mode == "python":
            type_rep = scoring.get("type_reponse", "?")
            rep = scoring.get("reponse_canonique", "?")
            unite = scoring.get("unite") or ""
            rep_label = f"{rep} {unite}".strip()
            lines.append(f"**Réponse attendue** : {rep_label} (type : {type_rep})")
        elif mode == "albert":
            modele = scoring.get("reponse_modele", "?")
            lines.append(f"**Réponse modèle** : {modele}")
            criteres = scoring.get("criteres_validation") or []
            if criteres:
                lines.append("")
                lines.append("**Critères de validation** :")
                for c in criteres:
                    lines.append(f"- {c}")
        else:
            lines.append(f"**Mode de scoring** : {mode}")

        lines.append("")
        lines.append("---")
        lines.append("")

    md = "\n".join(lines).encode("utf-8")
    md_name = json_path.stem + ".md"
    return md, md_name


def _sciences_questions_json_to_markdown(json_path: Path) -> tuple[bytes, str]:
    """Transforme un fichier JSON de questions de révision sciences en markdown.

    Format attendu : un objet avec une clé `questions` dont chaque entrée
    porte `id`, `discipline`, `theme`, `competence`, `enonce`, `scoring`
    (mode python ou albert), `source` et éventuellement des `indices` /
    `reveal_explication`.

    Convention identique à `_math_questions_json_to_markdown` : un bloc
    markdown par question (énoncé + réponse attendue ou modèle + critères
    pour les ouvertes), les indices pré-calculés et l'explication de
    révélation sont volontairement OMIS — Albert les régénère côté
    runtime, pas besoin de les indexer.

    Retourne (contenu_markdown_utf8, nom_virtuel_md).
    """
    data = json.loads(json_path.read_text(encoding="utf-8"))
    questions = data.get("questions") or []
    nom_batch = json_path.stem

    lines: list[str] = []
    lines.append(f"# Banque révision sciences — batch « {nom_batch} »")
    lines.append("")
    lines.append(f"Source : {json_path.name}")
    lines.append("")

    for q in questions:
        qid = q.get("id", "?")
        discipline = q.get("discipline", "?")
        theme = q.get("theme", "?")
        competence = q.get("competence", "")
        enonce = (q.get("enonce") or "").strip()
        scoring = q.get("scoring") or {}
        mode = scoring.get("mode", "?")

        lines.append(f"## Question {qid}")
        lines.append("")
        lines.append(f"**Discipline** : {discipline}")
        lines.append(f"**Thème** : {theme}")
        if competence:
            lines.append(f"**Compétence** : {competence}")
        lines.append("")
        lines.append(f"**Énoncé** : {enonce}")
        lines.append("")

        if mode == "python":
            type_rep = scoring.get("type_reponse", "?")
            rep = scoring.get("reponse_canonique", "?")
            unite = scoring.get("unite") or ""
            rep_label = f"{rep} {unite}".strip()
            lines.append(f"**Réponse attendue** : {rep_label} (type : {type_rep})")
        elif mode == "albert":
            modele = scoring.get("reponse_modele", "?")
            lines.append(f"**Réponse modèle** : {modele}")
            criteres = scoring.get("criteres_validation") or []
            if criteres:
                lines.append("")
                lines.append("**Critères de validation** :")
                for c in criteres:
                    lines.append(f"- {c}")
        else:
            lines.append(f"**Mode de scoring** : {mode}")

        lines.append("")
        lines.append("---")
        lines.append("")

    md = "\n".join(lines).encode("utf-8")
    md_name = json_path.stem + ".md"
    return md, md_name


def _redaction_subject_json_to_markdown(json_path: Path) -> tuple[bytes, str]:
    """Transforme un JSON de sujet de rédaction (français) en markdown.

    Le format JSON est celui produit par scripts/extract_french_redactions.py :
    deux options ``sujet_imagination`` / ``sujet_reflexion`` avec leurs
    consignes et contraintes, plus une éventuelle référence au texte support
    de compréhension.

    Retourne (contenu_markdown_utf8, nom_virtuel_md).
    """
    data = json.loads(json_path.read_text())
    annee = data.get("source", {}).get("annee", "?")
    centre = data.get("source", {}).get("centre", "?")
    code = data.get("source", {}).get("code_sujet")
    source = data.get("source_file") or json_path.stem
    texte_ref = data.get("texte_support_ref")

    lines: list[str] = []
    lines.append(f"# Sujet de rédaction — DNB {annee} {centre}")
    lines.append("")
    lines.append(f"Source : {source}")
    if code:
        lines.append(f"Code sujet : {code}")
    if texte_ref:
        lines.append(f"Texte support : {texte_ref}")
    lines.append("")
    lines.append("**Épreuve** : Rédaction (40 points, 1 h 30)")
    lines.append("")

    for key, label in (
        ("sujet_imagination", "Sujet d'imagination"),
        ("sujet_reflexion", "Sujet de réflexion"),
    ):
        opt = data.get(key) or {}
        lines.append(f"## {label}")
        lines.append("")
        if opt.get("numero"):
            lines.append(f"**Étiquette** : {opt['numero']}")
            lines.append("")
        if opt.get("amorce"):
            lines.append(f"**Amorce** : {opt['amorce']}")
            lines.append("")
        if opt.get("consigne"):
            lines.append(f"**Consigne** : {opt['consigne']}")
            lines.append("")
        contraintes = opt.get("contraintes") or []
        if contraintes:
            lines.append("**Contraintes** :")
            for c in contraintes:
                lines.append(f"- {c}")
            lines.append("")
        if opt.get("longueur_min_lignes"):
            lines.append(
                f"**Longueur indicative** : ~{opt['longueur_min_lignes']} lignes minimum"
            )
            lines.append("")
        if opt.get("reference_texte_support"):
            lines.append(
                f"**Renvoi au texte support** : {opt['reference_texte_support']}"
            )
            lines.append("")

    md = "\n".join(lines).encode("utf-8")
    md_name = json_path.stem + ".md"
    return md, md_name


# ============================================================================
# Itération sur les fichiers d'une collection
# ============================================================================


def _is_excluded(name: str) -> bool:
    if name in EXCLUDED_FILENAMES:
        return True
    return any(name.startswith(p) for p in EXCLUDED_PREFIXES)


def _iter_files(spec: CollectionSpec) -> Iterator[Path]:
    """Itère tous les fichiers à ingérer pour une collection donnée."""
    for source in spec.sources:
        if not source.exists():
            logger.warning("  ⚠ source introuvable: %s", source)
            continue
        if source.is_file():
            if not _is_excluded(source.name):
                yield source
        else:
            for pattern in spec.file_patterns:
                for f in sorted(source.glob(pattern)):
                    if not _is_excluded(f.name):
                        yield f


# ============================================================================
# Ingestion d'une collection
# ============================================================================


def ingest_collection(
    spec: CollectionSpec,
    client: AlbertRagClient,
    state: sqlite3.Connection,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Ingère tous les fichiers d'une collection. Retourne (pushed, skipped, errors)."""
    logger.info("\n[collection %s → %s]", spec.key, spec.name)

    if dry_run:
        collection_id = -1
        logger.info("  (dry-run) création de collection simulée")
    else:
        collection_id = client.ensure_collection(spec.name, spec.description)

    pushed = skipped = errors = 0
    files = list(_iter_files(spec))
    logger.info("  %d fichier(s) candidat(s)", len(files))

    for file_path in files:
        rel = file_path.relative_to(REPO_ROOT)
        try:
            sha = _sha256(file_path)
        except OSError as e:
            logger.error("  ✗ %s : erreur lecture %s", rel, e)
            errors += 1
            continue

        if not force and _already_ingested(state, spec.name, file_path, sha):
            logger.info("  ↪ skip %s (déjà ingéré, sha identique)", rel)
            skipped += 1
            continue

        logger.info("  ↑ %s (%d ko)", rel, file_path.stat().st_size // 1024)
        if dry_run:
            pushed += 1
            continue

        try:
            # Spécial sujets : JSON → markdown avant upload (Albert ne parse
            # pas JSON). Deux convertisseurs distincts selon la collection :
            # - "sujets"               → DC histoire-géo
            # - "fr_redaction_sujets"  → rédaction française
            if spec.key == "sujets" and file_path.suffix.lower() == ".json":
                md_bytes, md_name = _subject_json_to_markdown(file_path)
                resp = client.upload_document(
                    collection_id=collection_id,
                    file_path=file_path,
                    display_name=str(rel.with_suffix(".md")),
                    content_override=md_bytes,
                    virtual_filename=md_name,
                    mime_override="text/markdown",
                )
            elif spec.key == "fr_redaction_sujets" and file_path.suffix.lower() == ".json":
                md_bytes, md_name = _redaction_subject_json_to_markdown(file_path)
                resp = client.upload_document(
                    collection_id=collection_id,
                    file_path=file_path,
                    display_name=str(rel.with_suffix(".md")),
                    content_override=md_bytes,
                    virtual_filename=md_name,
                    mime_override="text/markdown",
                )
            elif spec.key == "math_sujets" and file_path.suffix.lower() == ".json":
                md_bytes, md_name = _math_questions_json_to_markdown(file_path)
                resp = client.upload_document(
                    collection_id=collection_id,
                    file_path=file_path,
                    display_name=str(rel.with_suffix(".md")),
                    content_override=md_bytes,
                    virtual_filename=md_name,
                    mime_override="text/markdown",
                )
            elif (
                spec.key == "sciences_revision_questions"
                and file_path.suffix.lower() == ".json"
            ):
                md_bytes, md_name = _sciences_questions_json_to_markdown(file_path)
                resp = client.upload_document(
                    collection_id=collection_id,
                    file_path=file_path,
                    display_name=str(rel.with_suffix(".md")),
                    content_override=md_bytes,
                    virtual_filename=md_name,
                    mime_override="text/markdown",
                )
            else:
                resp = client.upload_document(
                    collection_id=collection_id,
                    file_path=file_path,
                    display_name=str(rel),
                )
            # La doc dit que la réponse est un DocumentResponse
            document_id = resp.get("id") if isinstance(resp, dict) else None
            _record_ingestion(state, spec.name, file_path, sha, document_id)
            pushed += 1
            logger.info("    ✓ document_id=%s", document_id)
        except httpx.HTTPStatusError as e:
            errors += 1
            logger.error(
                "  ✗ %s : HTTP %s %s",
                rel,
                e.response.status_code,
                e.response.text[:300],
            )
        except Exception as e:
            errors += 1
            logger.error("  ✗ %s : %s", rel, e)

    logger.info(
        "  résumé : %d poussés, %d ignorés, %d erreurs", pushed, skipped, errors
    )
    return pushed, skipped, errors


# ============================================================================
# Main
# ============================================================================


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        choices=sorted(COLLECTIONS.keys()),
        action="append",
        help="Ne traiter que cette/ces collection(s) (répétable). Défaut: toutes.",
    )
    parser.add_argument(
        "--matiere",
        choices=sorted(MATIERE_COLLECTIONS.keys()),
        action="append",
        help=(
            "Sélectionne toutes les collections d'une matière (répétable). "
            "Équivalent à `--only <key>` pour chaque clé de la matière. "
            "Compose avec `--only` : les deux sélections sont unionnées."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-pousser même les fichiers déjà ingérés (sha inchangé).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simuler sans faire d'appels HTTP.",
    )
    args = parser.parse_args()

    key = os.environ.get("ALBERT_API_KEY")
    if not key and not args.dry_run:
        sys.exit("ALBERT_API_KEY manquant. Source ton .env avant de lancer.")
    base_url = os.environ.get("ALBERT_BASE_URL") or DEFAULT_BASE_URL

    # Union des clés sélectionnées via --only et/ou --matiere. Si rien n'est
    # spécifié, on prend toutes les collections.
    selected: list[str] = []
    if args.matiere:
        for m in args.matiere:
            for k in MATIERE_COLLECTIONS[m]:
                if k not in selected:
                    selected.append(k)
    if args.only:
        for k in args.only:
            if k not in selected:
                selected.append(k)
    if not selected:
        selected = list(COLLECTIONS.keys())
    specs = [COLLECTIONS[k] for k in selected]
    selected_keys = selected

    logger.info("Cible Albert : %s", base_url)
    logger.info("Collections sélectionnées : %s", ", ".join(selected_keys))

    client = (
        AlbertRagClient(base_url, key) if not args.dry_run else None
    )  # type: ignore[arg-type]
    state = _init_state_db()

    try:
        totals = {"pushed": 0, "skipped": 0, "errors": 0}
        for spec in specs:
            p, s, e = ingest_collection(
                spec, client, state, force=args.force, dry_run=args.dry_run  # type: ignore[arg-type]
            )
            totals["pushed"] += p
            totals["skipped"] += s
            totals["errors"] += e

        logger.info("\n" + "=" * 60)
        logger.info(
            "TOTAL : %d poussés, %d ignorés, %d erreurs",
            totals["pushed"],
            totals["skipped"],
            totals["errors"],
        )
        return 0 if totals["errors"] == 0 else 2
    finally:
        if client is not None:
            client.close()
        state.close()


if __name__ == "__main__":
    sys.exit(main())

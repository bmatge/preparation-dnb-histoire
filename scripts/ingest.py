"""
Ingestion du corpus DNB dans les collections RAG d'Albert.

Ce script pousse les PDF du dépôt vers les 4 collections Albert qui servent
de base de connaissances au tuteur :

  - dnb_sujets      : sujets "développement construit" extraits des annales
                      (JSON produits par scripts/extract_subjects.py)
  - dnb_corriges    : corrigés modèles des DNB
  - dnb_methodo     : fiches méthodologiques
  - dnb_programmes  : programmes officiels cycle 4 (garde-fou anti-hallucination)

Principe :
- On laisse Albert faire le chunking (RecursiveCharacterTextSplitter côté serveur)
  via `POST /v1/documents` en multipart/form-data. On ne pré-chunk pas.
- On ne gère PAS les embeddings manuellement — Albert les produit avec son modèle
  d'embedding configuré pour la collection (bge-m3).
- Idempotence : on stocke un hash sha256 du fichier dans une petite table locale
  SQLite (data/ingest_state.db). Un re-run ne re-pousse que les fichiers modifiés.
- Les 4 collections sont créées à la demande si elles n'existent pas.

Usage :
    source .env
    .venv/bin/python -m scripts.ingest                    # ingère tout le corpus
    .venv/bin/python -m scripts.ingest --only corriges    # une seule collection
    .venv/bin/python -m scripts.ingest --force            # re-pousse tout
    .venv/bin/python -m scripts.ingest --dry-run          # simule sans appels réseau

Corpus attendu (depuis la racine du repo) :
    content/histoire-geo-emc/annales/*.pdf          →  dnb_sujets (via .../subjects/*.json)
    content/histoire-geo-emc/corriges/*.pdf         →  dnb_corriges
    content/histoire-geo-emc/methodologie/*.pdf,*.md →  dnb_methodo
    content/histoire-geo-emc/programme/*.pdf        →  dnb_programmes
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


@dataclass(frozen=True)
class CollectionSpec:
    """Définition d'une des 4 collections cibles."""

    key: str  # identifiant court utilisé en CLI ex: "corriges"
    name: str  # nom complet côté Albert ex: "dnb_corriges"
    description: str
    sources: list[Path]  # dossiers/fichiers à ingérer
    file_patterns: tuple[str, ...] = ("*.pdf",)


COLLECTIONS: dict[str, CollectionSpec] = {
    "corriges": CollectionSpec(
        key="corriges",
        name="dnb_corriges",
        description="Corrigés officiels et modèles de DNB histoire-géo-EMC.",
        sources=[HGEMC_CONTENT / "corriges"],
    ),
    "methodo": CollectionSpec(
        key="methodo",
        name="dnb_methodo",
        description="Fiches méthodologiques pour le développement construit au DNB.",
        sources=[HGEMC_CONTENT / "methodologie"],
        file_patterns=("*.pdf", "*.md"),
    ),
    "programmes": CollectionSpec(
        key="programmes",
        name="dnb_programmes",
        description="Programmes officiels cycle 4 histoire-géo-EMC. Source d'autorité anti-hallucination.",
        sources=[HGEMC_CONTENT / "programme"],
    ),
    "sujets": CollectionSpec(
        key="sujets",
        name="dnb_sujets",
        description="Consignes de développement construit extraites des annales DNB.",
        sources=[HGEMC_CONTENT / "subjects"],
        file_patterns=("*.json",),
    ),
}

# Fichiers à exclure d'office lors de l'itération (nom, pas glob)
EXCLUDED_FILENAMES = {"_all.json"}


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


# ============================================================================
# Itération sur les fichiers d'une collection
# ============================================================================


def _iter_files(spec: CollectionSpec) -> Iterator[Path]:
    """Itère tous les fichiers à ingérer pour une collection donnée."""
    for source in spec.sources:
        if not source.exists():
            logger.warning("  ⚠ source introuvable: %s", source)
            continue
        if source.is_file():
            if source.name not in EXCLUDED_FILENAMES:
                yield source
        else:
            for pattern in spec.file_patterns:
                for f in sorted(source.glob(pattern)):
                    if f.name not in EXCLUDED_FILENAMES:
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
            # Spécial sujets : JSON → markdown avant upload (Albert ne parse pas JSON)
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

    selected_keys = args.only or list(COLLECTIONS.keys())
    specs = [COLLECTIONS[k] for k in selected_keys]

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

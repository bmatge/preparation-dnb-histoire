"""
Wrapper RAG côté runtime — interroge les collections Albert pour récupérer
les passages pertinents qui seront injectés dans les prompts pédagogiques.

Principes :
- On ne fait AUCUN embedding local, AUCUN chunking local : Albert s'en charge.
- On n'utilise pas le SDK OpenAI (les endpoints /collections et /search sont
  propres à Albert), on tape directement /v1/search via httpx.
- On résout les ID de collections par leur nom au démarrage (cache mémoire),
  pour ne pas avoir à les hardcoder. Fallback hardcodé en dernier recours.
- Mini-cache mémoire (clé = (query, collections, limit)) pour éviter de
  rappeler Albert deux fois pour le même besoin dans la même session.
- Nettoyage des chunks : on supprime les artefacts d'extraction PDF (« picture
  intentionally omitted », `<br>`, gras isolés…) avant injection dans le prompt.

Utilisé par app/pedagogy.py qui orchestre les étapes du parcours élève.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Iterable

import httpx

from app.core.albert_client import DEFAULT_BASE_URL, Task

logger = logging.getLogger(__name__)


# ============================================================================
# RagPassage — unité de passage retrouvé dans une collection Albert
# ============================================================================
#
# Défini ici (côté core) parce que rag.py est le producteur. Les prompts des
# sous-modules de matière le consomment via `from app.core.rag import RagPassage`.


@dataclass
class RagPassage:
    """Un extrait retrouvé par Albert dans une collection."""

    source: str  # ex: "corrigé 2021 Berlin", "programme cycle 4", "méthodo MrDarras"
    content: str


# ============================================================================
# Étiquettes "courtes" pour les citations dans les prompts/évals
# ============================================================================
#
# Les prompts demandent au modèle de citer ses sources entre crochets sous la
# forme [programme], [corrigé], [méthodo]. Pour rendre cette citation possible,
# chaque collection est associée à une étiquette courte qu'on injecte dans le
# `source` du RagPassage. Le modèle voit donc « [programme] ... », ce qui
# correspond exactement à la regex de post-filtre côté albert_client.
# ============================================================================

COLLECTION_LABELS: dict[str, str] = {
    "dnb_programmes": "programme",
    "dnb_corriges": "corrigé",
    "dnb_methodo": "méthodo",
    "dnb_sujets": "sujet",
}

# Fallback si /v1/collections ne répond pas — IDs créés par scripts/ingest.py
# (cf HANDOFF.md §4.5). Mis à jour lors du dernier ingest 2026-04.
FALLBACK_COLLECTION_IDS: dict[str, int] = {
    "dnb_methodo": 184792,
    "dnb_corriges": 184795,
    "dnb_programmes": 184797,
    "dnb_sujets": 184809,
}


# ============================================================================
# Sélection des collections par étape pédagogique
# ============================================================================
#
# Toutes les étapes interrogent en priorité le programme (anti-hallucination),
# les corrigés modèles et la méthodologie. La correction finale ajoute les
# sujets pour pouvoir comparer avec d'autres consignes proches.
# ============================================================================

TASK_COLLECTIONS: dict[Task, tuple[str, ...]] = {
    Task.DECRYPT_SUBJECT: ("dnb_programmes", "dnb_methodo", "dnb_corriges"),
    Task.HELP_UNDERSTAND: ("dnb_programmes", "dnb_methodo"),
    Task.FIRST_EVAL: ("dnb_programmes", "dnb_corriges", "dnb_methodo"),
    Task.SECOND_EVAL: ("dnb_programmes", "dnb_corriges", "dnb_methodo"),
    Task.FINAL_CORRECTION: (
        "dnb_programmes",
        "dnb_corriges",
        "dnb_methodo",
        "dnb_sujets",
    ),
}


# ============================================================================
# Nettoyage des chunks
# ============================================================================

# Artefacts récurrents observés dans les sorties d'extraction PDF d'Albert.
_NOISE_PATTERNS = [
    re.compile(r"==>\s*picture[^<]*<==", re.IGNORECASE),
    re.compile(r"-{2,}\s*Start of picture text\s*-{2,}", re.IGNORECASE),
    re.compile(r"-{2,}\s*End of picture[^-]*-{2,}", re.IGNORECASE),
    re.compile(r"<br\s*/?>", re.IGNORECASE),
    re.compile(r"!\[\]\([^)]*\)"),  # images markdown vides
    re.compile(r"\*{3,}"),  # gras isolés massifs
    re.compile(r"\n{3,}"),  # collapse plus de 2 retours à la ligne
]


def _clean_chunk(text: str) -> str:
    """Strip les artefacts d'extraction PDF avant injection dans un prompt."""
    out = text
    for pat in _NOISE_PATTERNS[:-1]:
        out = pat.sub(" ", out)
    out = _NOISE_PATTERNS[-1].sub("\n\n", out)
    # Espaces multiples → un seul, mais préserve les retours à la ligne.
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


# ============================================================================
# Client RAG
# ============================================================================


@dataclass
class _SearchHit:
    score: float
    content: str
    collection_id: int
    document_id: int | None


class AlbertRagClient:
    """Client RAG minimal contre /v1/search d'Albert.

    Thread-safe pour le cache (dict + lock). Une seule instance partagée
    par l'app suffit, voir `get_default_rag_client()`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
    ):
        key = api_key or os.environ.get("ALBERT_API_KEY")
        if not key:
            raise RuntimeError(
                "ALBERT_API_KEY manquant. Source ton .env avant de lancer l'app."
            )
        url = (base_url or os.environ.get("ALBERT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._base = url
        self._http = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {key}"},
        )
        self._collection_ids: dict[str, int] = {}
        self._cache: dict[tuple, list[RagPassage]] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------
    # Résolution des IDs de collections
    # ------------------------------------------------------------------

    def _resolve_collection_ids(self, names: Iterable[str]) -> list[int]:
        """Résout les noms de collections en IDs. Cache mémoire + fallback."""
        missing = [n for n in names if n not in self._collection_ids]
        if missing:
            try:
                r = self._http.get(
                    f"{self._base}/collections", params={"limit": 100}
                )
                r.raise_for_status()
                for c in r.json().get("data", []):
                    name = c.get("name")
                    if name:
                        self._collection_ids[name] = int(c["id"])
            except (httpx.HTTPError, ValueError, KeyError) as e:
                logger.warning(
                    "Impossible de résoudre les collections via /v1/collections (%s) — fallback hardcodé",
                    e,
                )
                for name, cid in FALLBACK_COLLECTION_IDS.items():
                    self._collection_ids.setdefault(name, cid)

        ids: list[int] = []
        for name in names:
            cid = self._collection_ids.get(name)
            if cid is None:
                logger.warning("Collection inconnue côté Albert : %s", name)
                continue
            ids.append(cid)
        return ids

    # ------------------------------------------------------------------
    # Recherche bas niveau
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        collections: list[str],
        limit: int = 5,
        score_threshold: float = 0.5,
        method: str = "semantic",
    ) -> list[RagPassage]:
        """Recherche dans une ou plusieurs collections, renvoie une liste de RagPassage.

        Les résultats sous le `score_threshold` sont filtrés. L'étiquette de
        source est l'étiquette courte de la collection (programme, corrigé…),
        ce qui permet aux post-filtres de citation de matcher.
        """
        cache_key = (query.strip().lower(), tuple(sorted(collections)), limit)
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]

        collection_ids = self._resolve_collection_ids(collections)
        if not collection_ids:
            return []

        try:
            r = self._http.post(
                f"{self._base}/search",
                json={
                    "collection_ids": collection_ids,
                    "prompt": query,
                    "method": method,
                    "limit": limit,
                },
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("Recherche RAG échouée pour query=%r : %s", query[:80], e)
            return []

        # Mapping inverse id → nom pour étiqueter les passages
        id_to_name = {cid: name for name, cid in self._collection_ids.items()}

        passages: list[RagPassage] = []
        for item in r.json().get("data", []):
            score = float(item.get("score") or 0.0)
            if score < score_threshold:
                continue
            chunk = item.get("chunk") or {}
            content = _clean_chunk(chunk.get("content") or "")
            if not content:
                continue
            cid = chunk.get("collection_id")
            name = id_to_name.get(cid, "?")
            label = COLLECTION_LABELS.get(name, name)
            passages.append(RagPassage(source=label, content=content))

        with self._lock:
            self._cache[cache_key] = passages
        return passages

    # ------------------------------------------------------------------
    # Recherche orientée par tâche
    # ------------------------------------------------------------------

    def search_for_task(
        self,
        task: Task,
        query: str,
        limit: int = 5,
        score_threshold: float = 0.5,
    ) -> list[RagPassage]:
        """Recherche en sélectionnant automatiquement les bonnes collections."""
        collections = list(TASK_COLLECTIONS.get(task, ()))
        if not collections:
            return []
        return self.search(
            query=query,
            collections=collections,
            limit=limit,
            score_threshold=score_threshold,
        )

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()


# ============================================================================
# Singleton paresseux
# ============================================================================

_default_client: AlbertRagClient | None = None
_default_lock = threading.Lock()


def get_default_rag_client() -> AlbertRagClient:
    """Retourne (et crée à la demande) le client RAG partagé par l'app."""
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = AlbertRagClient()
        return _default_client


__all__ = [
    "AlbertRagClient",
    "COLLECTION_LABELS",
    "TASK_COLLECTIONS",
    "get_default_rag_client",
]

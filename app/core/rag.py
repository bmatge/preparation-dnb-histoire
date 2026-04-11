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
# Collections Albert — dicts indexés par matière
# ============================================================================
#
# La plateforme est multi-matières : chaque matière (histoire-géo-EMC, maths…)
# possède son propre jeu de collections côté Albert, avec un nommage préfixé
# `dnb_<matière>_*`. Tous les dicts qui décrivent les collections sont donc
# indexés par une clé de matière (`subject_kind`) qui correspond au nom du
# package Python — p. ex. `"histoire_geo_emc"` (cf. `app/histoire_geo_emc/
# __init__.py::SUBJECT_KIND`).
#
# Convention de nommage : `dnb_<matière>_<type>` où <type> ∈ {programmes,
# corriges, methodo, sujets}.
#
# Étiquettes courtes : les prompts demandent au modèle de citer ses sources
# entre crochets sous la forme [programme], [corrigé], [méthodo], [sujet].
# `COLLECTION_LABELS[subject_kind][collection_name]` donne l'étiquette courte
# à injecter dans le `source` du RagPassage.
# ============================================================================

# Nouveau nommage (cible post-refacto 2b).
COLLECTION_LABELS: dict[str, dict[str, str]] = {
    "histoire_geo_emc": {
        "dnb_hgemc_programmes": "programme",
        "dnb_hgemc_corriges": "corrigé",
        "dnb_hgemc_methodo": "méthodo",
        "dnb_hgemc_sujets": "sujet",
        # Anciens noms gardés dans le dict pour étiqueter correctement les
        # chunks qui remonteraient des anciennes collections pendant la
        # fenêtre de bascule (cf. LEGACY_COLLECTION_ALIASES ci-dessous).
        # À retirer une fois que les anciennes collections sont supprimées
        # côté Albert.
        "dnb_programmes": "programme",
        "dnb_corriges": "corrigé",
        "dnb_methodo": "méthodo",
        "dnb_sujets": "sujet",
    },
    # Français compréhension : les collections sont partagées par toutes les
    # sous-épreuves françaises (compréhension, grammaire, réécriture et à
    # terme dictée, rédaction), mais indexées par subject_kind pour coller à
    # l'API `search_for_task(subject_kind=...)`. Si d'autres sous-épreuves
    # françaises sont ajoutées, ajouter une clé `francais_<sous_epreuve>` qui
    # pointe vers les mêmes noms de collections.
    "francais_comprehension": {
        "dnb_francais_programme": "programme",
        "dnb_francais_methodo": "méthodo",
    },
    # Français rédaction : on cible les fiches méthodo français (qui
    # contiennent les attendus rédactionnels) et le programme. On ajoute la
    # collection dédiée `dnb_francais_redaction_sujets` pour pouvoir comparer
    # avec d'autres consignes proches lors de la correction finale (gérée
    # par scripts/ingest.py côté offline).
    "francais_redaction": {
        "dnb_francais_programme": "programme",
        "dnb_francais_methodo": "méthodo",
        "dnb_francais_redaction_sujets": "sujet",
    },
    # Mathématiques : trois collections, une par type de source.
    # `dnb_math_methodo` regroupe les fiches de méthode + cadrage de
    # l'épreuve. `dnb_math_programmes` couvre le programme cycle 4 + les
    # attendus 3e/4e/5e + les repères annuels. `dnb_math_automatismes_sujets`
    # contient les questions du corpus committé (converties en markdown
    # à la volée par scripts/ingest.py).
    "mathematiques": {
        "dnb_math_methodo": "méthodo",
        "dnb_math_programmes": "programme",
        "dnb_math_automatismes_sujets": "sujet",
    },
    # Sciences : quatre collections. `dnb_sciences_methodo` regroupe les
    # 8 fiches de méthode thématiques (couvrant PC/SVT/Techno).
    # `dnb_sciences_programme` couvre le programme cycle 4 (physique-chimie
    # + SVT + technologie). `dnb_sciences_annales` contient les 73 sujets
    # d'annales DNB Sciences 2018-2025 (utile en RAG pour la rév. ouverte).
    # `dnb_sciences_revision_questions` contient les questions du corpus
    # committé (converties en markdown à la volée par scripts/ingest.py).
    "sciences": {
        "dnb_sciences_methodo": "méthodo",
        "dnb_sciences_programme": "programme",
        "dnb_sciences_annales": "annale",
        "dnb_sciences_revision_questions": "sujet",
    },
}

# Fenêtre de bascule : si un nouveau nom de collection ne résout pas côté
# Albert (parce que le re-ingest n'a pas encore été fait), on retente avec
# l'ancien nom. À vider une fois que `scripts.ingest --force` a été lancé
# contre les nouveaux noms et que les anciennes collections sont supprimées.
LEGACY_COLLECTION_ALIASES: dict[str, dict[str, str]] = {
    "histoire_geo_emc": {
        "dnb_hgemc_programmes": "dnb_programmes",
        "dnb_hgemc_corriges": "dnb_corriges",
        "dnb_hgemc_methodo": "dnb_methodo",
        "dnb_hgemc_sujets": "dnb_sujets",
    },
}

# Fallback si /v1/collections ne répond pas — IDs créés par scripts/ingest.py
# (cf HANDOFF.md §4.5). Les IDs listés ici sont ceux du dernier ingest
# 2026-04 (anciens noms). Ils resteront valides via LEGACY_COLLECTION_ALIASES
# tant que le re-ingest sous les nouveaux noms n'est pas fait. Après re-ingest,
# remplacer par les nouveaux IDs.
FALLBACK_COLLECTION_IDS: dict[str, dict[str, int]] = {
    "histoire_geo_emc": {
        "dnb_methodo": 184792,
        "dnb_corriges": 184795,
        "dnb_programmes": 184797,
        "dnb_sujets": 184809,
    },
    # Mathématiques : IDs à renseigner après le premier run de
    # `python -m scripts.ingest --matiere mathematiques`. Tant que ce dict
    # est vide, le client RAG fait un fetch via `/v1/collections` (route
    # nominale, ce fallback ne sert que si l'API liste les collections est
    # cassée — situation extrêmement rare en prod).
    "mathematiques": {},
    # Sciences : idem, IDs à renseigner après le premier run de
    # `python -m scripts.ingest --matiere sciences`.
    "sciences": {},
}


# ============================================================================
# Sélection des collections par étape pédagogique (par matière)
# ============================================================================
#
# Toutes les étapes interrogent en priorité le programme (anti-hallucination),
# les corrigés modèles et la méthodologie. La correction finale ajoute les
# sujets pour pouvoir comparer avec d'autres consignes proches.
# ============================================================================

TASK_COLLECTIONS: dict[str, dict[Task, tuple[str, ...]]] = {
    "histoire_geo_emc": {
        Task.DECRYPT_SUBJECT: (
            "dnb_hgemc_programmes",
            "dnb_hgemc_methodo",
            "dnb_hgemc_corriges",
        ),
        Task.HELP_UNDERSTAND: (
            "dnb_hgemc_programmes",
            "dnb_hgemc_methodo",
        ),
        Task.FIRST_EVAL: (
            "dnb_hgemc_programmes",
            "dnb_hgemc_corriges",
            "dnb_hgemc_methodo",
        ),
        Task.SECOND_EVAL: (
            "dnb_hgemc_programmes",
            "dnb_hgemc_corriges",
            "dnb_hgemc_methodo",
        ),
        Task.FINAL_CORRECTION: (
            "dnb_hgemc_programmes",
            "dnb_hgemc_corriges",
            "dnb_hgemc_methodo",
            "dnb_hgemc_sujets",
        ),
    },
    # Français compréhension : la méthodo (fiches par thème grammatical) est
    # la source principale pour l'eval, les indices et la révélation — c'est
    # là que vivent les règles concrètes (« un adjectif de couleur dérivé
    # d'un nom reste invariable »). Le programme est ajouté pour la
    # révélation (pour renforcer l'autorité en citant les attendus) et pour
    # la synthèse de fin de session (pour renvoyer l'élève à un thème du
    # programme qu'il doit retravailler).
    "francais_comprehension": {
        Task.FR_COMP_EVAL: (
            "dnb_francais_methodo",
        ),
        Task.FR_COMP_HINT: (
            "dnb_francais_methodo",
        ),
        Task.FR_COMP_REVEAL: (
            "dnb_francais_methodo",
            "dnb_francais_programme",
        ),
        Task.FR_COMP_SYNTHESE: (
            "dnb_francais_programme",
            "dnb_francais_methodo",
        ),
    },
    # Français rédaction : la méthodo français contient les attendus de
    # rédaction (intro/conclusion, paragraphes argumentés, registres). Le
    # programme est ajouté pour la première éval et la correction finale.
    # La collection des sujets de rédaction est interrogée seulement à la
    # correction finale, pour pouvoir comparer avec d'autres consignes
    # proches.
    "francais_redaction": {
        Task.FR_REDACTION_HELP: (
            "dnb_francais_methodo",
        ),
        Task.FR_REDACTION_FIRST_EVAL: (
            "dnb_francais_methodo",
            "dnb_francais_programme",
        ),
        Task.FR_REDACTION_SECOND_EVAL: (
            "dnb_francais_methodo",
            "dnb_francais_programme",
        ),
        Task.FR_REDACTION_FINAL_CORRECTION: (
            "dnb_francais_methodo",
            "dnb_francais_programme",
            "dnb_francais_redaction_sujets",
        ),
    },
    # Mathématiques : la méthodo (cadrage + 8 fiches thématiques) est la
    # source principale pour les indices et la révélation. Le programme
    # est ajouté pour ancrer les explications dans les attendus officiels
    # de fin de cycle 4. Pas d'interrogation de la collection sujets en
    # V1 (les questions sont déjà portées par la base SQLite).
    "mathematiques": {
        Task.MATH_AUTO_HINT: (
            "dnb_math_methodo",
        ),
        Task.MATH_AUTO_REVEAL: (
            "dnb_math_methodo",
            "dnb_math_programmes",
        ),
        Task.MATH_AUTO_EVAL_OPEN: (
            "dnb_math_methodo",
            "dnb_math_programmes",
        ),
        # Raisonnement et résolution de problèmes : mêmes collections que
        # les automatismes. Les fiches de méthode (8 thématiques) couvrent
        # à la fois les automatismes et les techniques mobilisées dans les
        # exercices de raisonnement (Pythagore, Thalès, PGCD, fonctions,
        # probabilités…). Pas de collection dédiée aux énoncés de problèmes
        # en V1 — on pourra en ajouter une si le besoin apparaît.
        Task.MATH_PROB_HINT: (
            "dnb_math_methodo",
        ),
        Task.MATH_PROB_REVEAL: (
            "dnb_math_methodo",
            "dnb_math_programmes",
        ),
        Task.MATH_PROB_EVAL_OPEN: (
            "dnb_math_methodo",
            "dnb_math_programmes",
        ),
    },
    # Sciences : les 8 fiches de méthode (regroupées dans
    # `dnb_sciences_methodo`) sont la source principale pour les indices
    # et la révélation — elles couvrent les notions clés des trois
    # disciplines. Le programme cycle 4 est ajouté pour la révélation
    # et l'éval ouverte (ancrage dans les attendus officiels). Les
    # annales DNB Sciences ne sont interrogées qu'à l'éval ouverte,
    # pour permettre au modèle de citer un cadrage ou un exemple
    # proche quand c'est utile.
    "sciences": {
        Task.SCIENCES_REV_HINT: (
            "dnb_sciences_methodo",
        ),
        Task.SCIENCES_REV_REVEAL: (
            "dnb_sciences_methodo",
            "dnb_sciences_programme",
        ),
        Task.SCIENCES_REV_EVAL_OPEN: (
            "dnb_sciences_methodo",
            "dnb_sciences_programme",
            "dnb_sciences_annales",
        ),
    },
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

    def _ensure_collections_loaded(self, subject_kind: str) -> None:
        """Charge en cache les IDs de collections depuis Albert (une fois).

        Fallback hardcodé (FALLBACK_COLLECTION_IDS[subject_kind]) si
        /v1/collections échoue. Tous les noms retournés par Albert sont
        cachés, pas seulement ceux de la matière — ça limite les appels
        redondants si plusieurs matières tapent dans des collections
        différentes sur le même process.
        """
        if self._collection_ids:
            return  # déjà peuplé par un appel précédent
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
                "Impossible de résoudre les collections via /v1/collections (%s) "
                "— fallback hardcodé pour %s",
                e,
                subject_kind,
            )
            for name, cid in FALLBACK_COLLECTION_IDS.get(subject_kind, {}).items():
                self._collection_ids.setdefault(name, cid)

    def _resolve_collection_ids(
        self, subject_kind: str, names: Iterable[str]
    ) -> list[int]:
        """Résout les noms de collections en IDs pour une matière donnée.

        Pendant la fenêtre de bascule 2b, si un nouveau nom (p. ex.
        `dnb_hgemc_programmes`) n'existe pas encore côté Albert, on retombe
        sur l'ancien nom via `LEGACY_COLLECTION_ALIASES`. Ça permet de
        déployer le code avant d'avoir re-ingéré les collections sous leur
        nouveau nom.
        """
        self._ensure_collections_loaded(subject_kind)
        aliases = LEGACY_COLLECTION_ALIASES.get(subject_kind, {})

        ids: list[int] = []
        for name in names:
            cid = self._collection_ids.get(name)
            if cid is None:
                legacy_name = aliases.get(name)
                if legacy_name and legacy_name in self._collection_ids:
                    logger.info(
                        "Collection %r introuvable côté Albert, fallback sur "
                        "l'ancien nom %r. Lance `scripts.ingest --force` pour "
                        "créer les nouvelles collections et supprimer les "
                        "anciennes.",
                        name,
                        legacy_name,
                    )
                    cid = self._collection_ids[legacy_name]
            if cid is None:
                logger.warning(
                    "Collection inconnue côté Albert : %s (matière=%s)",
                    name,
                    subject_kind,
                )
                continue
            ids.append(cid)
        return ids

    # ------------------------------------------------------------------
    # Recherche bas niveau
    # ------------------------------------------------------------------

    def search(
        self,
        subject_kind: str,
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
        cache_key = (subject_kind, query.strip().lower(), tuple(sorted(collections)), limit)
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]

        collection_ids = self._resolve_collection_ids(subject_kind, collections)
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
        labels = COLLECTION_LABELS.get(subject_kind, {})

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
            label = labels.get(name, name)
            passages.append(RagPassage(source=label, content=content))

        with self._lock:
            self._cache[cache_key] = passages
        return passages

    # ------------------------------------------------------------------
    # Recherche orientée par tâche
    # ------------------------------------------------------------------

    def search_for_task(
        self,
        subject_kind: str,
        task: Task,
        query: str,
        limit: int = 5,
        score_threshold: float = 0.5,
    ) -> list[RagPassage]:
        """Recherche en sélectionnant automatiquement les bonnes collections
        pour la matière et la tâche données."""
        task_map = TASK_COLLECTIONS.get(subject_kind, {})
        collections = list(task_map.get(task, ()))
        if not collections:
            return []
        return self.search(
            subject_kind=subject_kind,
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
    "RagPassage",
    "COLLECTION_LABELS",
    "LEGACY_COLLECTION_ALIASES",
    "FALLBACK_COLLECTION_IDS",
    "TASK_COLLECTIONS",
    "get_default_rag_client",
]

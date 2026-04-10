"""Doublures de test pour les clients Albert (chat + RAG).

Vague 2 : on ne teste pas l'API Albert elle-même (déjà couvert par les
post-filtres en vague 1 et par les tests live en vague 3). On veut juste
tester l'orchestration des routes / pedagogy autour de ce client.

Les fakes :
- ne touchent JAMAIS le réseau ;
- comptent les appels (introspection facile dans les tests) ;
- exposent une file de réponses canned (``queued_responses``) ;
- exposent une file d'exceptions à lever (``queued_exceptions``) pour
  tester les chemins d'erreur gracieuse de ``_safe_chat``.

Tous les fakes sont créés frais par fixture function-scoped : pas de
fuite d'état d'un test à l'autre.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.albert_client import ChatResult, Task
from app.core.rag import RagPassage


# ============================================================================
# Fake AlbertClient
# ============================================================================


@dataclass
class FakeAlbertCall:
    task: Task | None
    messages: list[dict] | None


class FakeAlbertClient:
    """Stub d'``AlbertClient`` qui ne fait aucun appel réseau.

    L'interface mime celle du vrai client uniquement sur la méthode
    ``chat`` (la seule utilisée par le runtime). Les arguments sont
    acceptés en positionnel ou en kw, parce que les call sites varient
    (DC HG passe positionnel, comprehension passe kw).
    """

    DEFAULT_RESPONSE = (
        "Tu as bien identifié l'idée centrale. Et si tu creusais comment "
        "ça se relie à la question posée ? [programme]"
    )

    def __init__(self) -> None:
        self.calls: list[FakeAlbertCall] = []
        self.queued_responses: list[str] = []
        self.queued_exceptions: list[Exception] = []

    def queue_response(self, content: str) -> None:
        self.queued_responses.append(content)

    def queue_exception(self, exc: Exception) -> None:
        self.queued_exceptions.append(exc)

    def chat(
        self,
        task: Task | None = None,
        messages: list[dict] | None = None,
        *,
        retry_on_missing_citations: bool = True,
        **_kwargs: Any,
    ) -> ChatResult:
        self.calls.append(FakeAlbertCall(task=task, messages=messages))
        if self.queued_exceptions:
            raise self.queued_exceptions.pop(0)
        content = (
            self.queued_responses.pop(0)
            if self.queued_responses
            else self.DEFAULT_RESPONSE
        )
        return ChatResult(
            content=content,
            task=task,  # type: ignore[arg-type]
            model="fake-model",
            prompt_tokens=0,
            completion_tokens=0,
        )

    def chat_stream(self, task: Task, messages: list[dict]):  # pragma: no cover
        # Pas utilisé par le runtime au MVP. Stub minimal au cas où.
        yield self.DEFAULT_RESPONSE


# ============================================================================
# Fake RAG client
# ============================================================================


@dataclass
class FakeRagCall:
    subject_kind: str
    task: Task | None
    query: str
    limit: int


class FakeRagClient:
    """Stub d'``AlbertRagClient``.

    Par défaut renvoie ``[]`` (les prompts gèrent un contexte vide). Les
    tests qui veulent vérifier l'injection RAG dans le prompt peuvent
    pré-remplir ``next_passages`` avant l'appel.
    """

    def __init__(self) -> None:
        self.calls: list[FakeRagCall] = []
        self.next_passages: list[RagPassage] = field(default_factory=list)  # type: ignore[assignment]
        # NB : on ne peut pas mettre `field(default_factory=list)` sur un
        # attribut de classe non-dataclass ; on remplace après __init__.
        self.next_passages = []

    def search_for_task(
        self,
        subject_kind: str,
        task: Task,
        query: str,
        limit: int = 5,
        score_threshold: float = 0.5,
    ) -> list[RagPassage]:
        self.calls.append(
            FakeRagCall(
                subject_kind=subject_kind, task=task, query=query, limit=limit
            )
        )
        return list(self.next_passages)

    def search(self, *_args: Any, **_kwargs: Any) -> list[RagPassage]:
        return list(self.next_passages)


__all__ = [
    "FakeAlbertClient",
    "FakeAlbertCall",
    "FakeRagClient",
    "FakeRagCall",
]

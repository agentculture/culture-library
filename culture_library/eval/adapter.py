"""The eval adapter seam: question generation and scoring behind one Protocol.

Only ``ragas_adapter.py`` may import ragas; everything else talks to this Protocol.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .models import Chunk, Passage, Sample, ScoreResult


@runtime_checkable
class ChatLLM(Protocol):
    """A minimal OpenAI-compatible chat client: messages in, assistant text out."""

    def complete(self, messages: list[dict]) -> str: ...


@runtime_checkable
class EvalAdapter(Protocol):
    """Generates a dataset from chunks and scores retrieval results."""

    name: str
    version: str

    def generate_samples(self, chunks: Sequence[Chunk], n: int, llm: ChatLLM) -> list[Sample]: ...

    def score(
        self,
        samples: Sequence[Sample],
        retrieved: Sequence[Sequence[Passage]],
        llm: ChatLLM | None,
    ) -> ScoreResult: ...

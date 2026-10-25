"""RAG evaluation support: adapter protocol, models, judge config, library API client.

Stdlib only. ragas may be imported solely by ``ragas_adapter.py``.
"""

from .adapter import ChatLLM, EvalAdapter
from .client import LibraryClient
from .config import JudgeConfig, OpenAIChat, guard_text_egress, is_local
from .errors import EvalApiError, EvalError, EvalPolicyError
from .models import Chunk, EvalRun, EvalSet, Passage, Sample, SampleResult, ScoreResult

__all__ = [
    "ChatLLM",
    "Chunk",
    "EvalAdapter",
    "EvalApiError",
    "EvalError",
    "EvalPolicyError",
    "EvalRun",
    "EvalSet",
    "JudgeConfig",
    "LibraryClient",
    "OpenAIChat",
    "Passage",
    "Sample",
    "SampleResult",
    "ScoreResult",
    "guard_text_egress",
    "is_local",
]

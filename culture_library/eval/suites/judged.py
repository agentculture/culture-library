"""Judged retrieval suite: LLM context precision/recall through the RAGAS adapter.

Keeps the runner's privacy rule: a non-public item with a non-local judge is skipped and
recorded as skipped (via ``guard_text_egress``), and, for a non-local judge, retrieved text of
non-public items is blanked before it leaves. ragas is imported lazily by the adapter, only
when this suite scores.
"""

from __future__ import annotations

import dataclasses
from typing import Callable

from ..config import guard_text_egress
from ..errors import EvalPolicyError
from .base import HIGHER, Metric, SuiteContext, SuiteScore


class JudgedSuite:
    name = "judged"
    version = "1"
    needs_judge = True
    metrics = (
        Metric(
            "context_precision",
            "LLM-judged (RAGAS, with reference): how much of the retrieved context, weighted "
            "toward the top ranks, is relevant to the reference answer.",
            (0.0, 1.0),
            HIGHER,
        ),
        Metric(
            "context_recall",
            "LLM-judged (RAGAS): the share of the reference answer supported by the "
            "retrieved context.",
            (0.0, 1.0),
            HIGHER,
        ),
    )

    def __init__(self, adapter_factory: Callable[..., object] | None = None) -> None:
        self._adapter_factory = adapter_factory

    def _adapter(self, judge):
        if self._adapter_factory is not None:
            return self._adapter_factory(judge)
        from ..ragas_adapter import RagasAdapter  # lazy: constructing it needs the [eval] extra

        return RagasAdapter(judge)

    def score(self, ctx: SuiteContext) -> SuiteScore:
        judge, llm = ctx.judge, ctx.llm
        if judge is None or llm is None:
            return SuiteScore(skipped="no judge configured")
        try:
            guard_text_egress([ctx.item], judge)
        except EvalPolicyError:
            iid = ctx.item.get("id", "?")
            return SuiteScore(
                skipped=f"item {iid} is not public and judge {judge.url!r} is not local"
            )
        retrieved = [list(ps) for ps in ctx.retrieved]
        if not judge.is_local:
            iid = ctx.item.get("id")
            public = ctx.is_public or (lambda _i: False)
            retrieved = [
                [
                    (
                        p
                        if (p.item_id == iid or public(p.item_id))
                        else dataclasses.replace(p, text="")
                    )
                    for p in ps
                ]
                for ps in retrieved
            ]
        result = self._adapter(judge).score(ctx.samples, retrieved, llm)
        keys = [m.name for m in self.metrics]
        per = [{k: row.get(k) for k in keys} for row in result.per_sample]
        return SuiteScore(per, {k: result.aggregate.get(k) for k in keys})


SUITE = JudgedSuite()

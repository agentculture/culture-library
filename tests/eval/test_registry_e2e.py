"""Registry end to end (plan t15): a suite registered only here reaches the runner, the
catalog route and the radar route, with no edit to runner, routes or site."""

from __future__ import annotations

import pytest

from culture_library.eval import suites
from culture_library.eval.models import EvalSet, Sample
from culture_library.eval.runner import run_set
from culture_library.eval.suites import HIGHER, Metric, SuiteScore

from .conftest import FakeAdapter

AXIS = "t15_shape"


class _Shape:
    name = "t15_fake"
    version = "7"
    needs_judge = False
    metrics = (Metric(AXIS, "fake 0-1 shape score", (0.0, 1.0), HIGHER),)

    def score(self, ctx):
        return SuiteScore([{AXIS: 0.5} for _ in ctx.samples], {AXIS: 0.5})


@pytest.fixture
def posted(library):
    from culture_library.core.indexer import Indexer, enqueue

    def embed(texts):
        return [[1.0, 0.0] for _ in texts]

    library.app.state.query_embedder = embed
    library.app.state.reranker = None
    item = library.add_item(
        "\n\n".join(f"Paragraph {i}. " + "words here. " * 30 for i in range(4)).encode()
    )
    with library.store.connect() as conn:
        enqueue(conn, item.id)
    Indexer(library.store, embed, model="fake-embed").run_once()
    chunks = library.client.get_chunks(item.id)
    stored = library.client.create_set(
        EvalSet(
            item_id=item.id,
            item_sha256=item.sha256,
            adapter="fake",
            adapter_version="0",
            samples=[
                Sample(question=f"q{i}", reference_chunk_shas=[c.chunk_sha])
                for i, c in enumerate(chunks)
            ],
        )
    )
    eval_set = library.client.get_set(stored["id"])
    with suites.registered(_Shape()):
        run = run_set(library.client, eval_set, FakeAdapter())
        body = library.client.create_run(run)
        catalog = library.http.get("/api/eval/suites")
        radar = library.http.get(f"/api/eval/radar?run={body['id']}")
    return run, body, catalog, radar


def test_runner_scores_the_fake_suite(posted):
    run, body, _, _ = posted
    assert {"name": "t15_fake", "version": "7"} in run.suites
    assert run.status == "complete"
    assert run.aggregates[AXIS] == 0.5
    assert all(s.scores[AXIS] == 0.5 for s in run.samples)
    assert body["aggregates"][AXIS] == 0.5


def test_catalog_route_lists_the_fake_suite(posted):
    _, _, catalog, _ = posted
    assert catalog.status_code == 200
    entry = next(s for s in catalog.json()["suites"] if s["name"] == "t15_fake")
    assert entry["version"] == "7"
    assert [m["name"] for m in entry["metrics"]] == [AXIS]


def test_unregistered_after_the_block(posted):
    assert "t15_fake" not in suites.names()


def test_radar_route_returns_the_fake_suite_axis(posted):
    _, _, _, radar = posted
    assert radar.status_code == 200
    assert radar.json()["axes"][AXIS] == 0.5

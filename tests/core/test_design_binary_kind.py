"""Deviation d1 (spec claim c44): binary assets under any pushed prefix are design."""

import pytest

from culture_library.core.repodocs import infer_kind


@pytest.mark.parametrize(
    "path",
    [
        "docs/deliveries/assets/flux-snow-leopard.png",
        "docs/specs/diagram.svg",
        "docs/plans/mock.HTML",
        ".devague/reviews/screenshot.jpg",
        "docs/deliveries/report.pdf",
    ],
)
def test_binary_assets_under_pushed_prefixes_are_design(path):
    assert infer_kind(path) == "design"


@pytest.mark.parametrize(
    "path, kind",
    [
        ("docs/specs/2026-10-30-x.md", "spec"),
        ("docs/plans/2026-10-30-x.md", "plan"),
        ("docs/deliveries/2026-10-30-x.md", "devague-record"),
        (".devague/frames/x.json", "devague-record"),
        (".devague/helpdump.py", "devague-record"),
        ("docs/design/library-site.md", "design"),
        ("README.png", "document"),
    ],
)
def test_text_files_keep_their_prefix_kind(path, kind):
    assert infer_kind(path) == kind

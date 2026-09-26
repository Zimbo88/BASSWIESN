"""Protect the author's approved wording without needing private source files."""
from hashlib import sha256
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_german_about_statement_preserves_approved_original():
    source = Path("basswiesn/app/static/js/about.js").read_text(encoding="utf-8")
    about = json.loads(source.split("window.BasswiesnAbout = ", 1)[1].rstrip(";\n"))
    blocks = about["stories"]["de"]
    assert len(blocks) == 32
    # Paragraph boundaries and prose are fixed; HTML styling is deliberately not.
    text = "\n\n".join(block["text"] for block in blocks)
    assert sha256(text.encode()).hexdigest() == "da5f0a1a478caf0c623a2574fe0ea3b1fd5672cd7048e88ca55080cfbd0cdda0"
    assert len(about["stories"]["en"]) == len(blocks)
    assert {block["kind"] for block in blocks} == {"heading", "paragraph"}

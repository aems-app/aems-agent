"""The agent's installed native writer retains C1/combining comment tails."""

from pathlib import Path
import re

import pymupdf
import pytest

from aems_agent.annotation_crud import add_annotation


@pytest.mark.parametrize("separator", [" ", "\x80", "\u0903", "\u20dd"])
def test_agent_paints_complete_comment(tmp_path: Path, separator: str) -> None:
    path = tmp_path / "synthetic-writer.pdf"
    text = ("First" + separator + "second" + separator + "third" + separator) * 15 + "FINALWORD"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(path)
    add_annotation(path, {"content": text, "type": "textbox", "page_index": 0,
                          "rect": [50, 770, 160, 792]})
    with pymupdf.open(path) as doc:
        page = doc[0]
        annotation = next(page.annots())
        assert annotation.info["content"] == text
        painted = annotation.get_text()
        assert re.sub("[^A-Za-z]", "", painted) == re.sub("[^A-Za-z]", "", text)
        assert "FINALWORD" in painted

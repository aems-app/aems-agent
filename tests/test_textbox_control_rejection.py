"""Agent rejects native C-string terminators before opening a PDF write."""
from pathlib import Path

import pymupdf
import pytest

from aems_agent.annotation_crud import add_annotation, update_annotation


@pytest.mark.parametrize("operation", ["create", "update"])
def test_nul_content_is_an_atomic_input_error(tmp_path: Path, operation: str) -> None:
    path = tmp_path / "synthetic-controls.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        annotation = page.add_freetext_annot((50, 50, 160, 72), "Intact FINALWORD", fontsize=8)
        doc.xref_set_key(annotation.xref, "NM", pymupdf.get_pdf_str("control-target"))
        doc.save(path)
    before = path.read_bytes()
    payload = {"content": "Accepted prefix\x00LOSTTAIL"}
    with pytest.raises(ValueError, match="NUL"):
        if operation == "create":
            add_annotation(path, {**payload, "type": "textbox", "page_index": 0,
                                  "rect": [50, 770, 160, 792]})
        else:
            update_annotation(path, "control-target", payload)
    assert path.read_bytes() == before

"""Tests du workflow html-to-pdf (validation sans WeasyPrint système)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from worker.workflows.html_to_pdf import ASSETS_DIR, HtmlToPdfInput


def test_assets_present() -> None:
    """Le dossier assets embarque le CSS et toutes les polices attendues."""
    assert (ASSETS_DIR / "fonts.css").is_file()
    for font in (
        "Lora.ttf",
        "Lora-Italic.ttf",
        "SourceSans3.ttf",
        "SourceSans3-It.ttf",
        "OpenDyslexic-400.woff",
        "OpenDyslexic-700.woff",
    ):
        assert (ASSETS_DIR / "fonts" / font).is_file(), f"police absente : {font}"


def test_input_requires_html() -> None:
    with pytest.raises(ValidationError):
        HtmlToPdfInput(html="")


def test_input_filename_default_and_validation() -> None:
    model = HtmlToPdfInput(html="<html><head></head><body><p>Bonjour</p></body></html>")
    assert model.filename == "document.pdf"
    with pytest.raises(ValidationError):
        HtmlToPdfInput(html="<p>x</p>", filename="livret")  # sans extension
    with pytest.raises(ValidationError):
        HtmlToPdfInput(html="<p>x</p>", filename="../escape.pdf")  # chemin

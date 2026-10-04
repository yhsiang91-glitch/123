"""PDFs are generated at test time; no binary fixtures are committed."""

import warnings
from pathlib import Path

import pytest
from pypdf import PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

pdfmetrics.registerFont(UnicodeCIDFont("MSung-Light"))  # built into reportlab, no font file needed


@pytest.fixture
def make_pdf(tmp_path):
    """make_pdf("a.pdf", ["page 1", "page 2"], cjk=False, parent=None) -> Path.
    An empty string makes a blank page."""

    def _make(name="doc.pdf", pages=("Hello World",), cjk=False, parent=None):
        path = Path(parent or tmp_path) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        c = canvas.Canvas(str(path))
        for text in pages:
            c.setFont("MSung-Light" if cjk else "Helvetica", 14)
            if text:
                c.drawString(72, 700, text)
            c.showPage()
        c.save()
        return path

    return _make


@pytest.fixture
def encrypt(tmp_path):
    """encrypt(src, user_pw="secret", algorithm="AES-256") -> Path of the encrypted copy."""

    def _encrypt(src, user_pw="secret", algorithm="AES-256"):
        if algorithm.startswith("AES"):
            pytest.importorskip("cryptography")
        writer = PdfWriter(clone_from=str(src))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            writer.encrypt(user_pw, owner_password="owner", algorithm=algorithm)
        out = tmp_path / f"enc-{algorithm}-{'empty' if not user_pw else 'pw'}.pdf"
        with out.open("wb") as fh:
            writer.write(fh)
        return out

    return _encrypt

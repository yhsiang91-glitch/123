"""Convert PDF files to plain text.

Usage:
    python pdf_to_txt.py input.pdf [output.txt]
    python pdf_to_txt.py input_dir/ output_dir/
"""

import argparse
import sys
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        sys.stderr.write(
            "Missing dependency. Install with: pip install pypdf\n"
        )
        sys.exit(1)


def pdf_to_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    parts = []
    for page in reader.pages:
        text = page.extract_text() or ""
        parts.append(text)
    return "\n".join(parts)


def convert_file(pdf_path: Path, out_path: Path) -> None:
    text = pdf_to_text(pdf_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"{pdf_path} -> {out_path}")


def convert_dir(src_dir: Path, dst_dir: Path) -> None:
    pdfs = sorted(src_dir.rglob("*.pdf"))
    if not pdfs:
        print(f"No PDF files found in {src_dir}")
        return
    for pdf in pdfs:
        rel = pdf.relative_to(src_dir).with_suffix(".txt")
        convert_file(pdf, dst_dir / rel)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert PDF files to TXT.")
    parser.add_argument("input", help="PDF file or directory containing PDFs")
    parser.add_argument(
        "output",
        nargs="?",
        help="Output TXT file or directory (defaults to input name with .txt)",
    )
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.stderr.write(f"Input not found: {src}\n")
        sys.exit(1)

    if src.is_dir():
        dst = Path(args.output) if args.output else src
        convert_dir(src, dst)
    else:
        dst = Path(args.output) if args.output else src.with_suffix(".txt")
        convert_file(src, dst)


if __name__ == "__main__":
    main()

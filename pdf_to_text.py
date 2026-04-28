"""Convert a PDF to a UTF-8 text file using pdfplumber.

Usage:  python pdf_to_text.py <input.pdf> [output.txt]

If no output path given, writes alongside input as <input>.txt.
Truncates to ~40,000 words (matches the experimental extractor cap).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pdfplumber


MAX_WORDS = 40_000


def pdf_to_text(pdf_path: Path, out_path: Path) -> int:
    parts: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t:
                parts.append(t)
    text = "\n\n".join(parts)
    words = text.split()
    if len(words) > MAX_WORDS:
        text = " ".join(words[:MAX_WORDS])
    out_path.write_text(text, encoding="utf-8")
    return len(words[:MAX_WORDS])


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python pdf_to_text.py <input.pdf> [output.txt]", file=sys.stderr)
        return 2
    pdf = Path(argv[0])
    out = Path(argv[1]) if len(argv) >= 2 else pdf.with_suffix(".txt")
    n = pdf_to_text(pdf, out)
    print(f"wrote {out} ({n} words)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

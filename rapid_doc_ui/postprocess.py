from __future__ import annotations

import re


def polish_markdown_layout(markdown: str) -> str:
    """Lightweight local cleanup for OCR markdown layout without changing content."""

    text = markdown.replace("\r\n", "\n").replace("\r", "\n")

    # Keep images visually separated from surrounding text.
    text = re.sub(r"(?<!\n)\n?(!\[[^\]]*]\([^)]+\))", r"\n\n\1", text)
    text = re.sub(r"(!\[[^\]]*]\([^)]+\))\n?(?!\n)", r"\1\n\n", text)

    # Chinese exam PDFs often come back as: stem（ ）A．...B．...
    text = re.sub(r"([。？！）)])\s*([A-H])([．.])", r"\1\n\n\2\3", text)
    text = re.sub(r"(?<!^)(?<!\n)([A-H])([．.])", r"\n\1\2", text)

    # Put each choice on its own line while keeping short option text compact.
    text = re.sub(r"\n([A-H][．.])", r"\n\n\1", text)

    # Avoid accidental excessive whitespace from repeated rules.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip() + "\n"

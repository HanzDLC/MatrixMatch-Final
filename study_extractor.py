# study_extractor.py
"""
Experimental: extract study metadata from an uploaded research PDF/DOCX
via a single LLM call. Called by the /admin/documents/upload-experimental
flow in app.py.

Kept deliberately small and fail-soft. If text extraction or the LLM parse
fails we return empty fields plus a list of admin-facing warnings so the
admin can paste the missing fields by hand on the review screen.

Module surface:
    extract_study(file_path: str) -> dict
        {
          "title": str,
          "authors": str,
          "abstract": str,
          "key_features": list[{label: str, description: str}],
          "warnings": list[str],
        }

The caller is expected to have saved the uploaded bytes to a staging
location and passes the staging path here; that lets the upload route
decide whether to keep the file (move to permanent) or delete it on
cancel without re-reading the user's upload stream.

Rule details for the prompt itself are documented in EXTRACTION_PROMPT.md.
"""

import json
import os
from typing import List

import llm_provider


MAX_WORDS = 40_000
ALLOWED_EXTENSIONS = (".pdf", ".docx")


def _read_pdf(path: str) -> str:
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError(
            "pdfplumber is required to parse PDFs. Install with: pip install pdfplumber"
        ) from exc
    parts: List[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                parts.append(t)
    return "\n".join(parts)


def _read_docx(path: str) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError(
            "python-docx is required to parse DOCX. Install with: pip install python-docx"
        ) from exc
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs if p.text)


def _read_text(file_path: str) -> str:
    """Extract document text, truncated to MAX_WORDS words."""
    lower = (file_path or "").lower()
    if lower.endswith(".pdf"):
        text = _read_pdf(file_path)
    elif lower.endswith(".docx"):
        text = _read_docx(file_path)
    else:
        raise ValueError(
            f"Unsupported file type: {os.path.basename(file_path)!r}. Accept .pdf or .docx."
        )
    words = text.split()
    if len(words) > MAX_WORDS:
        text = " ".join(words[:MAX_WORDS])
    return text


_EXTRACTION_PROMPT = """\
You are reading a completed research study from a university repository.

Return EXACTLY this JSON shape, and nothing else:
{{
  "title": "<the full, verbatim title of the study>",
  "authors": "<comma-separated list of student authors only>",
  "abstract": "<verbatim text of the study's Abstract section>",
  "key_features": [
    {{"label": "<2-5 word noun phrase>", "description": "<20-40 word capability-level description>"}}
  ]
}}

=== RULES FOR `title` ===
- The MAIN title of the study as it appears on the title page (first page / cover page).
- Must be a VERBATIM string copied from the document. Preserve case, punctuation, and any subtitle that appears on the same title-page block.
- Do NOT prepend things like "A thesis entitled" or "A capstone project titled" — strip those and return just the title itself.
- If no title is clearly identifiable, return an empty string.

=== RULES FOR `authors` ===
- ONLY the student(s) who wrote the study. These usually appear below the title under headings like "By", "Submitted by", "Prepared by", or "A thesis by".
- EXCLUDE advisers, panel members, chairpersons, dean, and any faculty — they are listed under "Adviser", "Panel Members", "Approved by", etc.
- Return as a single comma-separated string: "Juan Dela Cruz, Maria Santos". No trailing "and".
- Strip honorifics and academic suffixes ("Mr.", "Ms.", "Jr.", "PhD"). Keep "Dela Cruz" style multi-word surnames intact.
- If no student authors are clearly identifiable, return an empty string.

=== RULES FOR `abstract` ===
- Must be a VERBATIM substring copied from the study — preserve case, spacing, punctuation. Do NOT paraphrase or rewrite.
- Target length: 150-400 words.
- If multiple candidate abstracts exist (e.g. English + Filipino), pick the ENGLISH one.
- If no recognizable abstract section is present, return the first 200-word coherent paragraph from the study.

=== RULES FOR `key_features` (3 to 10 entries) ===
Each entry is an OBJECT with two fields: `label` and `description`.

`label` rules:
- A SHORT CONCEPT LABEL (2-5 words, noun phrase) that names a CONCRETE system CAPABILITY.
- GOOD: "QR code scanning", "Real-time GPS tracking", "E-wallet integration", "Arrival-time prediction".
- BAD (goal / outcome / benefit): "Reduce travel times", "Improve user satisfaction", "Help farmers sell products".
- BAD (commodity / generic): "System", "Database", "Application", "Web-based platform".

`description` rules — CRITICAL, READ CAREFULLY:
- **Capability voice, NOT implementation voice.** Describe what the feature lets a USER (passenger, driver, admin, customer, etc.) DO — NOT how the system is coded.
- The test: "Could a researcher writing a concept proposal (with NOTHING built yet) have written this same description?" If no, it's too implementation-leaning. The clusterer that consumes these descriptions has to match them against researcher concept descriptions, so the abstraction MUST match.
- **20-40 words, one sentence usually enough.** Two sentences allowed only when needed to clarify a non-obvious actor or trigger.
- **Voice cues:** start the sentence with the actor and the action — "Passenger pays...", "Driver shares...", "User sees...", "Admin sets up...", "System confirms...". Avoid system-only voice ("POSTs...", "Sends...", "Persists...").
- **Source.** Pull from anywhere in the study that explains what the capability does *for the user*. Often this is in Chapter 1 (problem framing) or the abstract itself; Chapter 3/4 may have implementation detail that should be RE-PHRASED at capability level, not copied verbatim.
- **Never echo the label.** Do not write "QR code scanning — QR code scanning lets the user...".

=== BANNED CONTENT IN DESCRIPTIONS ===
- Marketing language and benefit framing: "highly acceptable", "satisfactory", "easy and convenient", "improves productivity".
- Implementation jargon: HTTP verbs (POST, GET), REST endpoints, API path strings, database/table names, framework or library names (Flask, Django, Firebase, Spring), JSON keys, websocket/protocol details.
- Internal timing details ("every 10 seconds", "via webhook callback") UNLESS they are user-visible (e.g. "live, updated continuously" is fine; "every 10 seconds via WebSocket" is not).
- ALLOWED: user-facing product/service names like "GCash", "Maya", "Google Maps" — these are how researchers and end users would name them.

=== TWO WORKED GOOD/BAD EXAMPLES ===
EXAMPLE 1 — `"E-wallet integration"`:
  GOOD description: "Passenger pays the shuttle fare using e-wallets like GCash or Maya."
  BAD  description: "On confirmation, system redirects to GCash API with the booking reference; webhook receives payment status and marks the booking PAID." (too technical, leaks implementation)

EXAMPLE 2 — `"Real-time GPS tracking"`:
  GOOD description: "Shows the live location of the shuttle to passengers on a map as it moves along its route."
  BAD  description: "Driver's Android app sends latitude/longitude to the server every 10 seconds via HTTP POST; coordinates are persisted to the live_positions table and broadcast over WebSocket." (too technical)

=== HARD BANS (never include these anywhere) ===
- Dev lifecycles: Agile, Scrum, Waterfall, Design Thinking, IPO, RAD, Prototyping.
- Quality standards: ISO 25010, ISO 9126, McCall.
- Evaluation frameworks: TAM, UTAUT, Likert scale, purposive sampling, "user satisfaction evaluation".
- Non-functional qualities: functionality, reliability, usability, efficiency, maintainability, portability, security (as a label).
- Post-hoc results: mean scores, "Highly Acceptable", "Strongly Agree", "Very Satisfactory", sample sizes.

=== STUDY TEXT ===
{study_text}
"""


def _run_llm_extraction(study_text: str) -> dict:
    """Call the LLM once, parse JSON, normalize fields. Fail-soft."""
    result = {
        "title": "",
        "authors": "",
        "abstract": "",
        "key_features": [],
        "warnings": [],
    }

    if not study_text.strip():
        result["warnings"].append(
            "Document text came back empty after parsing — check the file contents."
        )
        return result

    prompt = _EXTRACTION_PROMPT.format(study_text=study_text)

    try:
        provider = llm_provider.get_llm_provider()
        raw = provider.generate(prompt, json_mode=True, temperature=0.1)
    except Exception as exc:
        result["warnings"].append(
            f"LLM call failed ({exc}). Please fill the fields manually."
        )
        return result

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        result["warnings"].append(
            f"LLM returned invalid JSON ({exc}). Please fill the fields manually."
        )
        return result

    result["title"] = str(parsed.get("title") or "").strip()
    result["abstract"] = str(parsed.get("abstract") or "").strip()

    raw_authors = parsed.get("authors") or ""
    if isinstance(raw_authors, list):
        result["authors"] = ", ".join(
            str(a).strip() for a in raw_authors if str(a).strip()
        )
    else:
        result["authors"] = str(raw_authors).strip()

    raw_features = parsed.get("key_features") or []
    seen = set()
    short_desc_count = 0
    long_desc_count = 0
    missing_desc_count = 0
    for feat in raw_features if isinstance(raw_features, list) else []:
        # Handle both new dict shape and legacy bare-string shape
        # (in case the LLM occasionally falls back).
        if isinstance(feat, dict):
            label = str(feat.get("label") or "").strip()
            description = str(feat.get("description") or "").strip()
        else:
            label = str(feat).strip()
            description = ""
        if not label:
            continue
        lc = label.lower()
        if lc in seen:
            continue
        seen.add(lc)
        if not description:
            missing_desc_count += 1
        else:
            word_count = len(description.split())
            if word_count < 10:
                short_desc_count += 1
            elif word_count > 60:
                long_desc_count += 1
        result["key_features"].append({"label": label, "description": description})

    if not result["title"]:
        result["warnings"].append("Title could not be extracted — please enter it manually.")
    if not result["authors"]:
        result["warnings"].append("Authors could not be extracted — please enter them manually.")
    if not result["abstract"]:
        result["warnings"].append("Abstract could not be extracted — please paste it manually.")
    if len(result["key_features"]) < 3:
        result["warnings"].append(
            "Fewer than 3 key features extracted — please review and add more."
        )
    if missing_desc_count:
        result["warnings"].append(
            f"{missing_desc_count} feature(s) are missing a description — please fill them in."
        )
    if short_desc_count:
        result["warnings"].append(
            f"{short_desc_count} description(s) look too short (<10 words). Expand them so the matrix can match accurately."
        )
    if long_desc_count:
        result["warnings"].append(
            f"{long_desc_count} description(s) look too long (>60 words). They may have leaked implementation detail — trim to capability voice."
        )

    return result


def extract_study(file_path: str) -> dict:
    """Top-level entry point called from the Flask route. `file_path` points
    to a saved copy of the user's upload (typically in studies/_staging/)."""
    try:
        study_text = _read_text(file_path)
    except Exception as exc:
        return {
            "title": "",
            "authors": "",
            "abstract": "",
            "key_features": [],
            "warnings": [f"Could not read file: {exc}"],
        }
    return _run_llm_extraction(study_text)

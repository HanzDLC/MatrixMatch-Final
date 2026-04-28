# Study Extraction Prompt

This document describes the single LLM prompt used by the experimental upload flow to extract metadata from a full uploaded research study (PDF or DOCX).

- **Defined in:** [study_extractor.py:94-140](study_extractor.py#L94-L140) as `_EXTRACTION_PROMPT`
- **Called from:** `_run_llm_extraction()` at [study_extractor.py:142-184](study_extractor.py#L142-L184)
- **Triggered by:** `POST /admin/documents/upload-experimental` ([app.py:534-619](app.py#L534-L619))
- **LLM mode:** `json_mode=True` — forces structured JSON output across Ollama / OpenAI / Gemini via `llm_provider.get_llm_provider()`

## What the prompt does

One LLM call per uploaded study. The model is given the full extracted text of the study (PDF via `pdfplumber`, DOCX via `python-docx`, truncated to 40 000 words) and asked to return four fields in strict JSON:

| Field | Type | What it is |
|---|---|---|
| `title` | string | Verbatim title from the title page |
| `authors` | string | Comma-separated student authors (no advisers / panel / faculty) |
| `abstract` | string | Verbatim Abstract section from the study |
| `key_features` | array of strings | 3–10 concrete system capability labels |

The admin reviews and edits every field before any row is written to the `documents` table.

## The full prompt, verbatim

Python's `.format()` fills `{study_text}` at call time with the extracted document text. The doubled `{{...}}` are literal braces, escaped because the string is a format template.

```
You are reading a completed research study from a university repository.

Return EXACTLY this JSON shape, and nothing else:
{
  "title": "<the full, verbatim title of the study>",
  "authors": "<comma-separated list of student authors only>",
  "abstract": "<verbatim text of the study's Abstract section>",
  "key_features": ["Feature label 1", "Feature label 2"]
}

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
Each entry is a SHORT CONCEPT LABEL (2-5 words, noun phrase) that names a CONCRETE system CAPABILITY the study describes as part of the built system.

- Prefer features from Chapter 3 (methodology) and Chapter 4 (system design / implementation) over features mentioned only in the introduction or conclusion. The built-system chapters describe what the study actually implemented; the intro describes motivation and the conclusion describes outcomes.
- GOOD: "QR code scanning", "Real-time GPS tracking", "Offline mode", "Arrival-time prediction", "Fingerprint authentication".
- BAD (goal / outcome / benefit): "Reduce travel times", "Improve user satisfaction", "Help farmers sell products".
- BAD (commodity / generic): "System", "Database", "Application", "Web-based platform".

=== HARD BANS (never include these anywhere) ===
- Dev lifecycles: Agile, Scrum, Waterfall, Design Thinking, IPO, RAD, Prototyping.
- Quality standards: ISO 25010, ISO 9126, McCall.
- Evaluation frameworks: TAM, UTAUT, Likert scale, purposive sampling, "user satisfaction evaluation".
- Non-functional qualities: functionality, reliability, usability, efficiency, maintainability, portability, security (as a label).
- Post-hoc results: mean scores, "Highly Acceptable", "Strongly Agree", "Very Satisfactory", sample sizes.

=== STUDY TEXT ===
{study_text}
```

## Why each rule exists

### `title` rules
- **Verbatim** — the LLM is reliable at reading the first-page title block but drifts when asked to "summarize" or "normalize" a title. Making it a quote-and-paste task eliminates rewording.
- **Strip thesis framing** — "A Capstone Project entitled X" is common boilerplate on ISAT U title pages; stripping it prevents titles from starting with the same five words.

### `authors` rules
- **Students only** — thesis title pages list students, advisers, panel members, chair, and dean in visually similar blocks. Without explicit direction, the LLM often returns faculty as "authors". The heading cues (`By`, `Submitted by`) are the reliable discriminator.
- **Comma-separated string** — matches the existing `documents.authors` schema column format, which stores a single VARCHAR(500).
- **Strip honorifics** — keeps the authors field visually consistent across records for the browse / edit pages.

### `abstract` rules
- **Verbatim** — lets Stage 1 retrieval embed the exact same text humans read, no paraphrasing drift. Also keeps Gap Analysis's "quote from the abstract" rule consistent — the quote has to come from the stored abstract.
- **English variant preference** — ISAT U theses often include an English and a Filipino abstract. The downstream LLM work is English-tuned; picking English keeps embeddings and matching consistent.
- **Fallback to first coherent paragraph** — some studies don't label the abstract section explicitly. Rather than returning an empty string and flagging it to the admin, pick something reasonable and let the admin verify on the review screen.

### `key_features` rules
The block carrying the most weight for the Stage 2 matrix.

- **2–5 word noun phrase** — forces concept labels rather than sentences or verb phrases. "QR code scanning" is comparable across studies; "Students scan QR codes at the kiosk" is not.
- **Chapter-3/4 preference** — introductions describe motivation, conclusions describe outcomes. Neither lists the system's actual built capabilities. Chapter 3 (methodology) and Chapter 4 (system design / implementation) are where features live. Without this hint, the LLM tends to over-weight the intro's framing language.
- **GOOD examples** — pattern-match prompting. LLMs match the shape of good examples faster than they follow abstract rules.
- **BAD examples (goal/outcome/benefit)** — blocks features like "Reduce travel times" that are actually the student's motivation, not a system capability.
- **BAD examples (commodity/generic)** — blocks "System", "Database", "Application" which are trivially true of every study.

### Hard bans
- **Dev lifecycles** — Agile / Scrum / RAD / Prototyping are development methodologies, not system features. They appear in almost every thesis abstract's methodology paragraph.
- **Quality standards** — ISO 25010 is the university's evaluation framework, not a feature. Every ISAT U capstone mentions it; treating it as a feature would flood the matrix with false overlap.
- **Evaluation frameworks** — TAM, UTAUT, Likert scale, purposive sampling, "user satisfaction evaluation" — same reason. They describe how the system was tested, not what the system does.
- **Non-functional qualities** — "Usability", "Reliability", "Efficiency" are ISO 25010 dimensions treated as features in abstract prose. They are not features in the architectural sense.
- **Post-hoc results** — "Highly Acceptable", mean scores, sample sizes — these describe evaluation outputs, not system capabilities.

Without these hard bans, the feature matrix would be dominated by boilerplate that's present in every row and carries no comparative signal.

## Expected output

Sample response for an ISAT U thesis titled *Library Management System*:

```json
{
  "title": "Library Management System",
  "authors": "Juan Dela Cruz, Maria Santos",
  "abstract": "The Library Management System (LMS) was developed to address inefficiencies in the traditional, manual operations of the Iloilo Science and Technology University (ISAT U) library…",
  "key_features": [
    "QR-based attendance tracking",
    "Library materials categorization",
    "Book reservation and status updates",
    "Damage reporting",
    "Statistical report generation",
    "Operational report generation"
  ]
}
```

## Post-processing applied to the response

Done in `_run_llm_extraction()` at [study_extractor.py:142-184](study_extractor.py#L142-L184):

1. `json.loads(raw)` — fails soft: a `json.JSONDecodeError` returns empty fields + a warning so the admin can paste manually.
2. `title`, `abstract` — `str(...).strip()`.
3. `authors` — accepts either a comma-separated string OR a JSON array from the LLM (some providers return a list despite the prompt); joins list form with `", "`.
4. `key_features` — iterated, case-insensitively deduplicated while preserving original-case first occurrence.
5. Length guard: the underlying text is truncated to `MAX_WORDS = 40000` in `_read_text()` before the prompt is built, so the LLM call never exceeds ~50k input tokens.

## When to tune this prompt

- **If the LLM returns features that feel too abstract** → add stricter GOOD/BAD examples in the `key_features` rules block.
- **If ISO 25010 / TAM start leaking into features** → extend the Hard Bans list.
- **If titles come back with thesis framing** ("A Capstone Project entitled …") → strengthen the `title` rule with one more example of what to strip.
- **If Filipino abstracts start getting selected** → add a tighter English-preference rule.

Keep the JSON shape declaration at the top of the prompt exactly as-is — changing the key names would break the Python parser in `_run_llm_extraction`.

## Related documents

- [FEATURES.md](FEATURES.md) — Stage 1 / Stage 2 / Gap Analysis architecture overview.
- [CLAUDE.md](CLAUDE.md) — whole-project context for new sessions.
- [study_extractor.py](study_extractor.py) — the module that owns this prompt.

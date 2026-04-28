# MatrixMatch — Session Context

This file is auto-loaded at session start. Read it first to pick up where the last session left off.

## Project overview

MatrixMatch is a Flask-based research-document matching platform that uses a two-stage pipeline:
- **Stage 1**: SBERT semantic similarity matching (cosine threshold ~0.6–0.7) over a corpus of academic papers.
- **Stage 2**: LLM-driven feature matrix comparison (capability-voice key features per document).

GitHub repo: https://github.com/HanzDLC/MatrixMatch-Final (branch: `main`)

## Tech stack

- **Backend**: Flask (`app.py`), Python
- **DB**: PostgreSQL (Docker: `matrixmatch-db-1`, user `matrixmatch`, db `matrixmatch`)
- **Schema**: [docker/postgres/init/01_schema.sql](docker/postgres/init/01_schema.sql)
- **Embeddings**: SBERT (sentence-transformers)
- **LLM extraction**: `study_extractor.py` + `llm_provider.py`
- **PDF parsing**: `pdfplumber`

## Key DB tables

- `users` — researchers/admins (was MySQL `user`)
- `documents` — academic papers (title, abstract, authors, research_field, source_file_path)
- `document_key_features` — normalized feature labels + descriptions per document
- `comparison_history` — per-user run history (keywords, top_matches JSON, feature_matrix JSON)
- `app_settings` — key/value config
- `password_reset_tokens` — sha256 hashed one-time tokens

## Research field whitelist

Defined at [app.py:1749](app.py) — 8 values:
1. Information Technology & Computing
2. Education
3. Sciences
4. Business & Management
5. Engineering & Architecture
6. Arts, Humanities & Social Sciences
7. Industrial Technology
8. Others (uses `research_field_other` for free-text)

## Database growth project (in progress)

Goal: grow corpus from 79 → **500 documents** (+421 new) using Claude-extracted open-access papers from arXiv, PubMed Central, ERIC, DOAJ, OpenAlex, Europe PMC.

**Pipeline:**
1. `bulk_download_studies.py` — per-cluster API queries → PDFs in `studies/_downloaded/batch-<N>/`, with extractability + dedupe + English filters → `manifest.csv`.
2. **Claude reads each PDF** in conversation and produces JSON extraction records (title/authors/abstract/research_field/key_features) following the **minimal-inference protocol**.
3. `bulk_upload_claude_extracted.py` — staging POST to `/admin/documents/upload-experimental` → save POST with Claude's fields (the auto-extracted output is ignored).
4. Verify via DB count + distribution + live Stage 1/Stage 2 spot-check.

**Minimal-inference rules** (from [EXTRACTION_PROMPT.md](EXTRACTION_PROMPT.md) and `study_extractor.py:84-157`):
- `label`: noun phrase from paper's own terminology, no synthesis
- `description`: 20–40 words, capability voice, light edits only
- Strip BANNED CONTENT: HTTP verbs, framework names, JSON keys, evaluation jargon, Agile/Scrum, ISO standards, TAM/UTAUT
- Skip features that require inventing the actor; if <3 features survive, replace the PDF
- `abstract`/`title`/`authors` are verbatim (strip "A thesis entitled" prefix)

**Batch plan**: 22 batches × ~20 PDFs each. Batches 1–2 complete; further batches pending.

**Validated test concepts** (saved as `concept_iot_vital_signs.docx`):
- Concept 2 (IoT Health Monitoring) — verified 70%+ Stage 1 similarity
- Concept 6 (Customer Churn predictive analytics)
- Concept 7 (GC-MS Plant Profiling)

## Recent UI work (committed and pushed)

Commit `6ac7dc5` — admin dashboards (`manage_documents` + `manage_researchers`):
- Server-side pagination + scoped search (ID/Authors/Research Field/Features)
- Sort dropdown (Newest/Oldest)
- Real-time client-side highlighting via TreeWalker DOM traversal (yellow `<mark>` wrapper)
- Vertical action buttons with palette colors: `.btn-action-edit` (navy), `.btn-action-file` (gold), Delete uses `btn-danger`
- Manage Users buttons rolled back to **horizontal** layout per explicit request

Commit `7754cee` — user-side `/history` page:
- Same pagination/search/sort/highlight pattern as admin pages
- Search across history_id, academic_program_filter, keywords

## Conventions and gotchas

- **DB authors column**: `authors` is `TEXT` (was `VARCHAR(500)` — caused silent truncation/insert failure for multi-author papers; fix is in schema).
- **Postgres ILIKE** for case-insensitive search (not LIKE).
- **`is_active` / `must_change_password`** are `SMALLINT` storing 0/1, not booleans.
- **PDF-to-text**: `pdf_to_text.py` takes a single file path (not a directory). Loop with bash if batching.
- **OpenAlex 403 fallback**: When OpenAlex returns publisher URLs that 403, the downloader auto-falls back to DOAJ, Europe PMC, and arXiv keyword search.
- **Policy-flagged papers**: ~18 cybersecurity-heavy papers triggered content filtering. They're quarantined in `studies/_downloaded/batch-*/_quarantine/` — skip rather than retry.
- **`.gitignore` excludes** `studies/_downloaded/`, `.claude/`, large per-field study dirs, generated SQL snapshots, `concept_*.py/.docx` (regenerable).

## Common verification commands

```bash
# Document count
docker exec matrixmatch-db-1 psql -U matrixmatch -d matrixmatch -c "SELECT COUNT(*) FROM documents;"

# Distribution by research field
docker exec matrixmatch-db-1 psql -U matrixmatch -d matrixmatch -c \
  "SELECT research_field, COUNT(*) FROM documents GROUP BY 1 ORDER BY 2 DESC;"

# Recent additions with feature counts
docker exec matrixmatch-db-1 psql -U matrixmatch -d matrixmatch -c \
  "SELECT d.document_id, d.title, d.research_field,
          (SELECT COUNT(*) FROM document_key_features WHERE document_id = d.document_id) AS feat_count
   FROM documents d ORDER BY d.document_id DESC LIMIT 10;"
```

## Run the app locally

```bash
python app.py            # Flask dev server
docker compose up -d     # Postgres
```

## Files to read on resume

For deeper context: [AGENT_CONTEXT.md](AGENT_CONTEXT.md), [EXTRACTION_PROMPT.md](EXTRACTION_PROMPT.md), [AI_PROMPTS_EXPLAINED.md](AI_PROMPTS_EXPLAINED.md).

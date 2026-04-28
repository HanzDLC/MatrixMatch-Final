# MatrixMatch Agent Context (Canonical)

Last updated: 2026-04-28

This file is the single onboarding document for AI/code agents working in this repository.
Prefer this over `docs/CONTEXT.md` (that file is MySQL-era and outdated).

## 1. What this system is

MatrixMatch is a Flask web app for comparing a proposed research abstract against a local repository of completed studies.

Core outputs:
- Stage 1: ranked semantic similarity matches (SBERT cosine similarity).
- Stage 2: feature matrix comparing user features vs matched documents.
- AI Gap Analysis: LLM-generated novelty/overlap explanation per matched document.

## 2. Current stack

- Backend: Python + Flask (`app.py`)
- Database: PostgreSQL (via `psycopg2`, wrapper in `db.py`)
- Semantic model: `sentence-transformers` (`all-mpnet-base-v2`) in `matcher.py`
- LLM abstraction: `llm_provider.py` (Ollama / OpenAI / Gemini)
- Frontend: Jinja templates + static JS/CSS
- File extraction: `study_extractor.py` (PDF/DOCX -> LLM extraction)

## 3. Run and deploy model

### Local Docker DB (recommended)
- `docker-compose.yml` runs Postgres 16 on host port `5440`.
- On a fresh volume, init scripts run from `docker/postgres/init`.
- Compose also mounts `matrixmatch-current-20260427-000705.sql` into init as:
  - `/docker-entrypoint-initdb.d/99_matrixmatch_snapshot.sql`
- Result on a fresh machine: schema + data match the snapshot.

Important:
- Postgres init scripts run only once per data volume.
- To reinitialize from scratch:
  - `docker compose down -v`
  - `docker compose up -d`

### App runtime
- `app.py` executes startup "ensure/migrate" helpers:
  - creates/ensures settings/tokens/columns
  - manages one-shot migration flags in `app_settings`

## 4. Database model (current)

Primary tables:
- `users`
  - auth identity, role (`Admin`/`Researcher`), active flag, force-change-password flag
- `documents`
  - study metadata (`title`, `authors`, `abstract`, `research_field`, `source_file_path`)
  - legacy `key_features` TEXT column retained for compatibility
- `document_key_features` (new normalized source of truth)
  - `feature_id` PK
  - `document_id` FK -> `documents.document_id` (ON DELETE CASCADE)
  - `sort_order`
  - `label`
  - `description`
- `comparison_history`
  - persisted comparison runs (`keywords`, `user_abstract`, `top_matches`, `feature_matrix`, etc.)
- `password_reset_tokens`
- `app_settings`

## 5. Feature storage design (important)

Current write/read source for document features:
- Use `document_key_features` (separate columns for `label` and `description`).

Legacy behavior:
- `documents.key_features` still exists, but app logic now reads/writes normalized rows.
- Startup migration (`_ensure_document_key_features_table` in `app.py`) backfills from legacy JSON once (guarded by `app_settings.document_key_features_migrated`).

## 6. Main workflows

### A) Admin: Upload Study (classic)
1. Route: `POST /documents/upload`
2. Form posts `key_features` JSON (`[{label, description}, ...]`)
3. Server validates with `_parse_features_form`
4. Insert into `documents`
5. Replace linked rows in `document_key_features`

### B) Admin: Upload Study (experimental)
1. Route: `POST /admin/documents/upload-experimental`
2. Uploaded PDF/DOCX staged to `studies/_staging`
3. `study_extractor.py` runs one LLM extraction call
4. Admin reviews fields
5. Save route inserts `documents`, writes `document_key_features`, archives source file

### C) Researcher: New comparison
1. Route: `POST /comparison/new`
2. Stage 1 (`matcher.run_stage1`):
  - embed user abstract and repository abstracts
  - compute cosine similarity
  - filter/sort
  - save `comparison_history`
3. History detail loads matches
4. Stage 2 (`matcher.evaluate_feature_matrix`):
  - user features from history keywords
  - doc features from `document_key_features`
  - LLM cluster -> matrix
  - cache matrix in `comparison_history.feature_matrix`

### D) AI gap analysis
- Route: `GET /api/history/<history_id>/gap_analysis/<doc_id>`
- Calls `matcher.generate_gap_analysis` through active LLM provider

## 7. Key files by responsibility

- `app.py`: routes, auth, admin features, migration helpers
- `matcher.py`: embeddings, Stage 1/Stage 2, gap analysis, feature highlight/compare
- `db.py`: Postgres connection wrapper (`dictionary=True` compatibility)
- `llm_provider.py`: provider adapter/factory
- `study_extractor.py`: experimental upload extraction prompt + parsing
- `docker/postgres/init/01_schema.sql`: base schema for fresh volumes
- `docker-compose.yml`: Postgres service + snapshot mount

## 8. Route map (high-value endpoints)

Auth/account:
- `/login`, `/register`, `/logout`
- `/forgot-password`, `/reset-password/<token>`
- `/account/change-password`

Researcher:
- `/comparison/new`
- `/history`
- `/history/<id>`
- `/history/<id>/heatmap`
- `/api/history/<id>/feature_matrix`
- `/api/history/<id>/reload_matrix`
- `/api/history/<id>/gap_analysis/<doc_id>`
- `/api/history/<id>/feature_highlight`
- `/api/history/<id>/feature_compare`

Admin:
- `/admin/dashboard`
- `/admin/documents`
- `/documents/upload` (classic)
- `/admin/documents/upload-experimental`
- `/admin/documents/<id>/edit`
- `/admin/documents/<id>/delete`
- `/admin/documents/<id>/source-file`
- `/admin/researchers`
- `/admin/researchers/<id>/history`
- `/admin/researchers/<id>/reset`
- `/admin/researchers/<id>/toggle-active`
- `/admin/researchers/<id>/change-role`
- `/admin/settings`

## 9. Environment notes

Typical `.env` values used by code:
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- `LLM_PROVIDER` = `ollama` | `openai` | `gemini`
- `LLM_MODEL`
- `OPENAI_API_KEY` (when provider is openai)
- `GEMINI_API_KEY` (when provider is gemini)
- `OLLAMA_URL` (optional override)
- SMTP vars for password reset email (`SMTP_USER`, `SMTP_PASSWORD`, etc.)

## 10. Known tech debt / risks

- Passwords are currently compared as plaintext in login flow.
- There is no automated test suite in this repo.
- Several docs under `docs/` reflect earlier architecture and may conflict with live code.
- First run of SBERT model may download model weights and be slow.

## 11. Agent guidelines for edits in this repo

- Treat `document_key_features` as canonical for document feature read/write logic.
- Keep `documents.key_features` unless an explicit migration/removal task is requested.
- If changing schema, update both:
  - `docker/postgres/init/01_schema.sql`
  - runtime ensure/migration helpers in `app.py` (if needed)
- If changing initial dataset behavior, verify `docker-compose.yml` snapshot mount behavior.
- When troubleshooting data mismatches, check whether the Postgres volume is fresh or reused.

## 12. Quick verification queries

How many docs and normalized features:
```sql
SELECT COUNT(*) FROM documents;
SELECT COUNT(*) FROM document_key_features;
```

Features for one document:
```sql
SELECT document_id, sort_order, label, description
FROM document_key_features
WHERE document_id = 1
ORDER BY sort_order, feature_id;
```

Compare docs missing normalized features:
```sql
SELECT d.document_id, d.title
FROM documents d
LEFT JOIN document_key_features f ON f.document_id = d.document_id
GROUP BY d.document_id, d.title
HAVING COUNT(f.feature_id) = 0
ORDER BY d.document_id;
```


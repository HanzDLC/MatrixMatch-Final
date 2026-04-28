   # MatrixMatch v2 — Objectives Evaluation

## 1. System Context

**MatrixMatch v2** is a Flask + MySQL web application that helps researchers (and admins) discover how a *new* research abstract relates to a *local repository* of completed studies. It uses a sentence-transformer model (`all-mpnet-base-v2`) for semantic similarity and a configurable LLM provider (OpenAI / Google Gemini / local Ollama — selected via `.env`) for feature extraction and AI-powered gap analysis.

### Core Components
- **[app.py](app.py)** — Flask routes for auth, dashboards, upload, comparison, history, admin actions, and an AI gap-analysis API.
- **[matcher.py](matcher.py)** — Semantic engine: SBERT encoding, cosine similarity, Stage 1 / Stage 2 matrix building, Ollama keyword extraction, and gap-analysis prompting.
- **[backfill_features.py](backfill_features.py)** — One-shot script that adds `key_features` (per document) and `gap_analysis` (per history) columns and populates `key_features` for legacy documents via Ollama.
- **[matrixmatch.sql](matrixmatch.sql)** — Schema with three tables: `user`, `documents`, `comparison_history`.
- **[templates/](templates/)** — Jinja2 views (login, register, dashboards, upload, comparison, history list/detail, heatmap, manage researchers).

### Database Schema (effective)
- `user(researcher_id, first_name, last_name, email, password, role[Admin|Researcher], registered_date)`
- `documents(document_id, title, abstract, academic_program, authors*, key_features*)`  *(starred columns added later)*
- `comparison_history(history_id, researcher_id, keywords, user_abstract, academic_program_filter, similarity_threshold, top_matches, gap_analysis*, created_at)`

### End-to-End Process (Step by Step)

1. **Account & Role**
   - Register/Login via [app.py:119-207](app.py#L119-L207). Roles: `Researcher` (default) or `Admin`.
   - Session-based auth via the `login_required` decorator.

2. **Upload a Finished Study** — [app.py:77-113](app.py#L77-L113), [templates/upload_document.html](templates/upload_document.html)
   - Researcher submits Title, Authors, Academic Program, Abstract, and (optional) Key Features.
   - Inserted into `documents` (Authors and Key Features stored alongside the abstract).

3. **Start a New Comparison** — [app.py:353-453](app.py#L353-L453), [templates/comparison_new.html](templates/comparison_new.html)
   - Researcher pastes their *own* abstract, optionally provides keywords (chip input), picks an academic-program filter, and sets a similarity threshold (default 60%).
   - If the keywords box is empty, [matcher.generate_unique_features()](matcher.py#L45-L76) calls Ollama to extract them automatically.

4. **Stage 1 — Abstract vs. Document Similarity** — [matcher.run_stage1()](matcher.py#L82-L193)
   - Loads documents from `documents`, optionally filtered by program.
   - Encodes the user abstract and all document abstracts with SBERT.
   - Computes cosine similarity; keeps documents above the threshold; sorts descending.
   - Persists the run to `comparison_history` (`top_matches` is `"docId|score,docId|score,..."`).

5. **History Detail / Stage 2 — Keyword vs. Abstract Matrix** — [app.py:504-574](app.py#L504-L574), [matcher.build_stage2_matrix()](matcher.py#L363-L406)
   - Reloads matches and keywords for the saved run.
   - Builds a `keywords × matched_documents` cosine-similarity matrix and renders an HTML heatmap ([templates/history_heatmap_table.html](templates/history_heatmap_table.html)).

6. **AI Gap Analysis (per-document)** — [app.py:898-934](app.py#L898-L934), [matcher.generate_gap_analysis()](matcher.py#L479-L511)
   - Async endpoint that asks Ollama to produce **Similarities / Differences / Summary** between the user's abstract and a chosen repository abstract.

7. **History & Admin**
   - Researchers see their own runs ([app.py:459-500](app.py#L459-L500)); Admins can browse all researchers, reset passwords, delete accounts, and view per-researcher histories ([app.py:581-806](app.py#L581-L806)).

---

## 2. Evaluation Against Stated Objectives

### General Objective
> Design and develop a system that organizes, filters, and compares local research studies from the local repository using **abstracts** and **key features**.

| Requirement | Status | Evidence |
|---|---|---|
| Organizes local research studies | **Met** | `documents` table groups studies by `academic_program`, `authors`, `key_features`. Admin/Researcher dashboards list/sort them. |
| Filters studies | **Met** | Comparison form exposes an Academic Program filter and a similarity-threshold slider; Stage 1 enforces both ([matcher.py:108-124](matcher.py#L108-L124), [matcher.py:142-151](matcher.py#L142-L151)). |
| Compares using abstracts | **Met** | SBERT cosine similarity between user abstract and each document abstract ([matcher.py:133-151](matcher.py#L133-L151)). |
| Compares using key features | **Partially Met** | Stage 2 uses *user-supplied keywords* against document **abstracts** ([matcher.py:363-406](matcher.py#L363-L406)). The `documents.key_features` column exists and is back-filled by [backfill_features.py](backfill_features.py), but the matrix never reads it — i.e. key features of stored documents are not yet a comparison input. |

**Verdict — General Objective: Mostly Met.** The plumbing for "key features" exists end-to-end (column added, Ollama extraction, upload form field), but the comparison stage compares *user keywords ↔ document abstracts*, not *document key features ↔ user key features*. To fully satisfy the wording, Stage 2 should also include a `key_features × key_features` matrix (or use `documents.key_features` as one of the comparison axes).

---

### Specific Objective 1 — *Compare similarities and unique features among the studies*

| Sub-requirement | Status | Evidence |
|---|---|---|
| Quantify similarity between two studies | **Met** | Stage 1 cosine similarity returned as a 0–1 score per document. |
| Highlight *unique* features (gaps) between two studies | **Met** | [matcher.generate_gap_analysis()](matcher.py#L479-L511) prompts Ollama for explicit **Similarities / Differences / Summary**, surfaced via [api_gap_analysis](app.py#L898-L934). |
| Visualize comparisons | **Met** | Heatmap matrix in [history_detail](app.py#L504-L574) and a dedicated full-page table in [history_heatmap](app.py#L808-L889). |
| Compare *across multiple* studies in one view | **Met** | Stage 2 builds a single matrix spanning every matched document at once. |
| Compare studies *to each other* (not just user vs. repo) | **Not Met** | All comparisons are anchored on the user's submitted abstract. There is no repo-vs-repo browsing/comparison view. |

**Verdict — Objective 1: Largely Met.** Strong on user-vs-repo similarity and AI-driven gap reporting. Missing a "study A vs. study B" mode if the intent was repository-internal comparisons.

---

### Specific Objective 2 — *Generate all possible similar and unique features of the study*

| Sub-requirement | Status | Evidence |
|---|---|---|
| Auto-extract key features from an abstract | **Met** | [matcher.generate_unique_features()](matcher.py#L45-L76) calls Ollama and returns a JSON array of methodological/thematic keywords. |
| Use extracted features to drive comparison | **Met** | Used as Stage 2 row labels and (when the user leaves keywords blank) as Stage 1 inputs ([app.py:425-430](app.py#L425-L430)). |
| Persist features for repository documents | **Met** | `documents.key_features` is added and back-filled by [backfill_features.py](backfill_features.py); upload form also lets researchers supply them manually. |
| Generate "similar AND unique" feature lists explicitly | **Partially Met** | The AI Gap Analysis produces narrative bullet lists of similarities and differences, but there is no structured side-by-side feature diff (e.g., set intersection / set difference of key-feature arrays) returned to the UI. |

**Verdict — Objective 2: Mostly Met.** Feature *generation* and *persistence* are solid. The "all possible similar and unique features" wording is only satisfied at the LLM-narrative level; a structured intersection/difference of `key_features` between two studies is not yet computed.

---

### Specific Objective 3 — *Upload finished studies in repository*

| Sub-requirement | Status | Evidence |
|---|---|---|
| Upload form for new studies | **Met** | [templates/upload_document.html](templates/upload_document.html) collects Title, Authors, Program, Abstract, optional Key Features. |
| Validation of required fields | **Met** | [app.py:91-93](app.py#L91-L93) rejects missing Title/Abstract/Authors/Program. |
| Persistence to repository | **Met** | `INSERT INTO documents (...)` in [app.py:98-104](app.py#L98-L104). |
| Login-gated | **Met** | `@login_required` on the route. |
| File upload (PDF/DOCX) for the actual finished study | **Not Met** | Only metadata + abstract text is stored. There is no file upload, file storage, or download of the original document. |
| Edit / delete of uploaded studies | **Not Met** | No update or delete routes for `documents`. Once uploaded, a study cannot be revised or removed through the UI. |

**Verdict — Objective 3: Met for textual metadata; Not Met if "upload finished studies" implies storing the actual research file (PDF/DOCX).** Worth clarifying with stakeholders which interpretation is intended.

---

## 3. Summary Scorecard

| Objective | Coverage |
|---|---|
| General — organize / filter / compare via abstracts & key features | **Mostly Met** (key-features column unused in the comparison itself) |
| Specific 1 — similarities & unique features among studies | **Largely Met** (no repo↔repo comparison) |
| Specific 2 — generate similar & unique features | **Mostly Met** (no structured feature-set diff) |
| Specific 3 — upload finished studies | **Met for metadata** / **Not Met for file uploads or edit-delete** |

## 4. Recommended Gaps to Close (in priority order)

1. **Use `documents.key_features` in Stage 2** — build a second `user_keywords × document_key_features` matrix (or replace abstract embeddings with feature embeddings) so the system literally compares "key features," matching the general objective wording.
2. **Structured feature diff** — alongside the LLM gap analysis, compute and display the set intersection and set difference of key-feature arrays between the user's study and each matched repository study (cheap, deterministic, easy to read).
3. **File upload + storage for finished studies** — accept PDF/DOCX, store on disk (or BLOB), and let researchers download the original from the history detail page.
4. **Edit / soft-delete uploaded studies** — admin or owning researcher should be able to correct metadata or retract a study.
5. **Repo-vs-repo comparison mode** — let a researcher pick any existing study from the repository as the "Study B" anchor, not only a freshly pasted abstract. This makes Objective 1 fully literal.
6. **Persist gap analyses** — `comparison_history.gap_analysis` already exists (added by [backfill_features.py:25-34](backfill_features.py#L25-L34)) but [api_gap_analysis](app.py#L898-L934) never writes to it. Caching avoids re-calling Ollama on every page view.
7. **Password hashing** — passwords are stored and compared in plaintext ([app.py:138-145](app.py#L138-L145), [app.py:194-200](app.py#L194-L200)). Not part of the stated objectives, but a critical security gap before deployment.

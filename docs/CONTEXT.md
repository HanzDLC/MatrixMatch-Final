# MatrixMatch — System Context

## 1. What MatrixMatch Is

MatrixMatch is a Flask + MySQL web application that helps researchers compare a *new* research abstract against a *local repository* of completed studies. Given a draft abstract and a few keywords, it returns:

1. A ranked list of the most semantically similar past studies (Stage 1).
2. A keyword-vs-document similarity heatmap (Stage 2).
3. An on-demand AI "peer-review" comparing the new study to any chosen repository study (AI Gap Analysis).

It is intended for academic settings — for example, a university computer-science department comparing student capstone projects against past theses to identify novelty and gaps.

---

## 2. Tech Stack

| Layer | Technology |
|---|---|
| Backend | **Python 3.11** (recommended) + **Flask** |
| Database | **MySQL** (`mysql-connector-python` driver) |
| Semantic similarity | **sentence-transformers** with the `all-mpnet-base-v2` SBERT model |
| Numerics | **PyTorch** (auto-installed by sentence-transformers), **NumPy**, **pandas** |
| LLM (pluggable) | **Ollama** (local), **OpenAI** API, or **Google Gemini** API — selected via `.env` |
| Static heatmap (legacy) | **matplotlib** |
| Interactive heatmap | **Plotly.js** (loaded from CDN in the template) |
| Templates | **Jinja2** |
| Config | **python-dotenv** loading a `.env` file at startup |

---

## 3. File / Folder Layout

```
MatrixMatch/
├── app.py                  # Flask routes + auth + admin
├── matcher.py              # SBERT engine: Stage 1, Stage 2, history loaders
├── llm_provider.py         # Pluggable LLM provider (Ollama / OpenAI / Gemini)
├── backfill_features.py    # One-shot script: adds key_features + gap_analysis columns,
│                           # then back-fills key_features for legacy documents via the LLM
├── matrixmatch.sql         # MySQL schema dump (3 tables + seed data)
├── .env                    # Local secrets and provider config (NOT committed)
├── templates/              # Jinja2 views
│   ├── base.html
│   ├── index.html / login.html / register.html
│   ├── dashboard_admin.html / dashboard_researcher.html
│   ├── upload_document.html
│   ├── comparison_new.html
│   ├── history.html / history_detail.html / history_heatmap_table.html / history_sentence_match.html
│   ├── manage_researchers.html
│   └── admin_reset_password.html
└── static/
    ├── css/main.css
    └── js/main.js, comparison.js, history.js
```

---

## 4. Database Schema

Three tables (see [matrixmatch.sql](matrixmatch.sql)):

### `user`
| column | type | notes |
|---|---|---|
| `researcher_id` | int (PK) | |
| `first_name`, `last_name` | varchar | |
| `email` | varchar | unique login identifier |
| `password` | varchar | **plaintext** (security debt — should be hashed) |
| `role` | enum('Admin', 'Researcher') | drives dashboard and permission checks |
| `registered_date` | timestamp | |

### `documents`
| column | type | notes |
|---|---|---|
| `document_id` | int (PK) | |
| `title` | varchar(500) | |
| `abstract` | text | the main field SBERT compares against |
| `academic_program` | varchar | e.g. BSCS, BSIS, BSIT |
| `authors` | varchar | added later |
| `key_features` | text (JSON array) | populated by [backfill_features.py](backfill_features.py) via the LLM |

### `comparison_history`
| column | type | notes |
|---|---|---|
| `history_id` | int (PK) | |
| `researcher_id` | int (FK → user) | who ran the comparison |
| `keywords` | text (JSON array) | the keywords used in this run |
| `user_abstract` | text | the abstract the researcher submitted |
| `academic_program_filter` | varchar | e.g. ALL / BSCS |
| `similarity_threshold` | decimal(5,2) | 0.0–1.0 |
| `top_matches` | text | encoded as `"docId|score,docId|score,..."` |
| `gap_analysis` | text | reserved column (not yet written by the API route) |
| `created_at` | timestamp | |

---

## 5. Two-Stage Comparison Engine

### Stage 1 — Abstract-level similarity

[matcher.run_stage1()](matcher.py)

1. Loads documents from the repository (filtered by `academic_program` if requested).
2. Encodes the user's abstract and every repository abstract using SBERT (`all-mpnet-base-v2`).
3. Computes **cosine similarity** between the user vector and each document vector.
4. Drops anything below the strictness threshold (default 60%).
5. Sorts the survivors descending and saves the run to `comparison_history`.

**Output:** a ranked list — *which* past studies are most similar overall.

### Stage 2 — Keyword × Document matrix

[matcher.build_stage2_matrix()](matcher.py)

1. Loads the abstracts of the Stage 1 matches.
2. Encodes each **keyword** and each **matched abstract** with SBERT.
3. Computes a `(num_keywords × num_documents)` cosine similarity matrix.
4. Wraps it in a `pandas.DataFrame` and returns it to the route.

**Output:** a heatmap rendered two ways:

- **Interactive Plotly.js heatmap** in [history_detail.html](templates/history_detail.html) — hover for exact percentages.
- **Static HTML-table heatmap** in [history_heatmap_table.html](templates/history_heatmap_table.html) — opens full-screen in a new tab; uses inline CSS `rgba()` gradients, no JS.

A row that stays pale across the whole grid = a *unique* feature of the new study. A row that lights up across many studies = a well-trodden idea.

---

## 6. AI Gap Analysis

When the researcher clicks **✨ AI Gap Analysis** on a row:

1. JavaScript on [history_detail.html](templates/history_detail.html) calls `GET /api/history/<history_id>/gap_analysis/<doc_id>`.
2. [app.py:api_gap_analysis](app.py) loads the user's abstract and the chosen repository abstract.
3. [matcher.generate_gap_analysis()](matcher.py) builds a peer-reviewer prompt asking for **Similarities / Differences / Summary** and routes the call through `get_llm_provider()`.
4. The text is lightly post-processed (markdown bullets → HTML) and returned as JSON.
5. The browser injects the result into a card on the same page.

This is the "in plain English" view that complements the numeric Stage 1 list and the visual Stage 2 heatmap.

---

## 7. LLM Provider Abstraction (the part you can swap)

[llm_provider.py](llm_provider.py) defines an `LLMProvider` interface and three implementations:

| Provider | Class | Default model | Activation |
|---|---|---|---|
| Local Ollama | `OllamaProvider` | `llama3.1` | `LLM_PROVIDER=ollama` |
| OpenAI API | `OpenAIProvider` | `gpt-4o-mini` | `LLM_PROVIDER=openai` + `OPENAI_API_KEY` |
| Google Gemini API | `GeminiProvider` | `gemini-2.5-flash` | `LLM_PROVIDER=gemini` + `GEMINI_API_KEY` |

The factory `get_llm_provider()` reads `LLM_PROVIDER` from the environment and returns a cached instance. `matcher.py` only calls `provider.generate(prompt, json_mode=...)` — it never imports any provider SDK directly. **Swapping providers requires zero code changes** — only an edit to `.env`.

OpenAI uses the official `openai` SDK; Gemini uses Google's **new** unified SDK `google-genai` (not the legacy `google-generativeai`, which is deprecated and only supports older models).

---

## 8. `.env` Configuration

Loaded by `load_dotenv()` at the very top of [app.py](app.py), before `import matcher` so the provider is initialized with the right credentials.

```env
# Required
LLM_PROVIDER=openai            # ollama | openai | gemini

# OpenAI (when LLM_PROVIDER=openai)
OPENAI_API_KEY=sk-proj-...

# Gemini (when LLM_PROVIDER=gemini)
GEMINI_API_KEY=AIza...

# Optional — overrides each provider's default model
LLM_MODEL=gpt-4o-mini

# Optional — override Ollama's URL
OLLAMA_URL=http://localhost:11434/api/generate
```

`.env` should be listed in `.gitignore` to keep API keys out of version control.

---

## 9. Routes (high level)

| Route | Method | Role | Purpose |
|---|---|---|---|
| `/` | GET | public | Landing page |
| `/login`, `/register`, `/logout` | GET/POST | public | Auth |
| `/dashboard` | GET | any | Routes to admin or researcher dashboard |
| `/admin/dashboard` | GET | Admin | Stats + recent comparisons across all users |
| `/researcher/dashboard` | GET | Researcher | The user's own recent runs |
| `/documents/upload` | GET/POST | any logged-in | Upload a finished study to the repository |
| `/comparison/new` | GET/POST | any logged-in | The Stage 1 entry point |
| `/history` | GET | any logged-in | List the current user's runs |
| `/history/<id>` | GET | owner / Admin | Run detail (Stage 1 list + Stage 2 Plotly heatmap) |
| `/history/<id>/heatmap` | GET | owner / Admin | Full-page HTML-table heatmap |
| `/api/history/<h>/gap_analysis/<d>` | GET (JSON) | owner / Admin | Async LLM gap analysis |
| `/admin/researchers` | GET | Admin | List researchers |
| `/admin/researchers/<id>/reset` | GET/POST | Admin | Reset a researcher's password |
| `/admin/researchers/<id>/delete` | POST | Admin | Delete a researcher (and their history) |
| `/admin/researchers/<id>/history` | GET | Admin | Browse one researcher's history |

---

## 10. Required Packages

```bash
pip install flask mysql-connector-python sentence-transformers pandas matplotlib requests python-dotenv
```

Plus one provider SDK depending on `LLM_PROVIDER`:

```bash
pip install openai           # if LLM_PROVIDER=openai
pip install google-genai     # if LLM_PROVIDER=gemini
# Ollama needs no Python SDK — uses requests against localhost
```

Recommended Python version: **3.11** (Python 3.9 is past end-of-life and triggers deprecation warnings from Google's libraries; 3.13 may have wheel-availability issues for parts of the ML stack).

---

## 11. Known Gaps / Tech Debt

These don't block the system from running but are worth knowing about:

- **Passwords are stored in plaintext** in the `user` table — should be hashed before any deployment.
- **`documents.key_features`** is back-filled by [backfill_features.py](backfill_features.py) but **not yet used** by Stage 2 — Stage 2 still compares user keywords against document *abstracts*, not against the stored feature list.
- **`comparison_history.gap_analysis`** column exists but is never written — every click of "✨ AI Gap Analysis" re-calls the LLM. Worth caching once the system is in heavier use.
- **No file upload** — `documents` only stores the abstract text, not the original PDF/DOCX.
- **No edit/delete for documents** — once uploaded, a study cannot be revised or removed through the UI.
- **All comparisons are anchored on a freshly pasted user abstract** — there is no "compare two existing repository studies to each other" mode.

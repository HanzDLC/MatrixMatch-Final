# MatrixMatch — Known Tech Debt & Flagged Items

A running list of items worth fixing or at least being aware of before touching the related code. None of these block the system from running today, but each is a foot-gun for a future change.

Severity legend:
- 🔴 **Critical** — security or correctness; fix before deployment
- 🟡 **Medium** — silent failure risk or performance waste; fix when convenient
- 🟢 **Low** — code quality / consistency; fix opportunistically

---

## 1. Caching gaps (LLM calls re-run on every page load)

### 🟡 `comparison_history.feature_matrix` column exists but is unused

- **Where:** schema added by [migrate.py](migrate.py); column never read/written from `app.py` or `matcher.py`
- **What's happening:** Every visit to `/history/<id>` re-calls `matcher.evaluate_feature_matrix()`, which re-runs the LLM over all matched abstracts. With the hard cap of 5 removed, this can now be expensive (one LLM call per page view, scaling with the number of matches).
- **Suggested fix:** In `history_detail`, before calling `evaluate_feature_matrix()`, check if `history["feature_matrix"]` is non-empty and parse-able. If yes, use it. If no, compute and `UPDATE comparison_history SET feature_matrix=%s WHERE history_id=%s`. The existing **Recalculate** button can also clear this cache so a manual recalc still works.
- **Note:** the `/api/history/<id>/reload_matrix` endpoint already exists for explicit cache invalidation — wire it to actually persist.

### 🟡 `comparison_history.gap_analysis` column exists but is unused

- **Where:** schema added by [backfill_features.py](backfill_features.py); column never written by [api_gap_analysis](app.py) (~line 994)
- **What's happening:** Every click of "✨ AI Gap Analysis" re-calls the LLM, even for the same `(history_id, doc_id)` pair the user already viewed.
- **Suggested fix:** Cache as a JSON object keyed by `doc_id`, e.g. `{"40": "<html>", "47": "<html>"}`. Read on click, miss → call LLM → write back.

---

## 2. Schema mismatches and silent failure points

### ~~🟡 `documents.authors` column may not exist in the base schema~~ — ✅ RESOLVED

- **Resolved by:** [_ensure_authors_column()](app.py) startup helper added during the Manage Documents work. Idempotently runs `ALTER TABLE documents ADD COLUMN authors VARCHAR(500) DEFAULT NULL` on Flask boot, with errno 1060 (duplicate column) treated as a no-op. The new admin edit form writes to it; the upload form continues to work as before.

### ~~🟡 `documents.key_features` is added by a one-shot script, not by the base schema~~ — ✅ RESOLVED

- **Resolved by:** [_ensure_key_features_column()](app.py) startup helper, idempotently runs `ALTER TABLE documents ADD COLUMN key_features TEXT DEFAULT NULL` on Flask boot. Same errno-1060 pattern as the other ensure helpers. Fresh installs no longer need to run `backfill_features.py` just to make the column exist (the script is still useful for *populating* it via the LLM, but the schema is now self-healing).

---

## 3. Security

### 🔴 Hardcoded Flask `secret_key`

- **Where:** [app.py:27](app.py#L27) — `app.secret_key = "supersecretkey_change_me"`
- **Why it matters:** Anyone with access to the source can forge session cookies and impersonate any user, including admins.
- **Suggested fix:** Read from `os.environ["FLASK_SECRET_KEY"]` (already loaded by `dotenv`), generate a random one if missing on first run, and persist it.

### ~~🔴 Hardcoded admin password reset value `"matrix123"`~~ — ✅ RESOLVED

- **Resolved by:** Replacing manual admin password entry with auto-generation.
- **What was done:**
  - Added `user.must_change_password TINYINT(1)` column via [migrate.py](migrate.py) plus an idempotent runtime ensure in [app.py](app.py) so it works without manual migrations.
  - [admin_reset_password](app.py) now generates a fresh random password via `secrets.token_urlsafe(8)`, sets `must_change_password = 1`, and renders the temporary password exactly once on [admin_reset_password.html](templates/admin_reset_password.html) with a copy button.
  - [login](app.py) reads `must_change_password` and routes affected users to `/account/change-password`. The `login_required` decorator was tightened to keep them locked on that page until they pick a new password.
  - New `/account/change-password` route + [force_change_password.html](templates/force_change_password.html) template provides the self-service change form. Saving clears the flag and unlocks the rest of the app.
- **Old commented-out hardcoded `"matrix123"` block** at [app.py:715-725](app.py#L715-L725) is dead code and can be deleted in a cleanup pass.

### 🔴 Plaintext passwords (verify!)

- **Where:** suspected at [app.py:138-145](app.py#L138-L145) and [app.py:194-200](app.py#L194-L200) — registration and login paths
- **Status:** **needs verification.** Earlier investigation in this codebase found plaintext storage; the most recent codebase sweep claimed werkzeug hashing is used. Confirm by reading the actual register/login bodies and inspecting a row of the `user` table.
- **If plaintext:** swap to `werkzeug.security.generate_password_hash()` and `check_password_hash()`. This is a one-shot migration: hash all existing passwords once.

---

## 4. Tuning / quality knobs

### 🟡 Guest mode has no abuse / rate-limit guardrails

- **Where:** [comparison_new()](app.py), [api_guest_gap_analysis()](app.py), [api_guest_feature_highlight()](app.py)
- **What's happening:** Guests can run unlimited comparisons without an account. Each comparison fires one full LLM call for `evaluate_feature_matrix`, plus one per "✨ AI Gap Analysis" click and one per feature-highlight click. If the system is exposed publicly, a guest hammering the form translates linearly into OpenAI/Gemini bills and burns through your rate-limit quota. There is currently no IP rate limit, no per-session cap, no captcha, and no daily budget.
- **Suggested fix (when needed):** Add a per-IP token-bucket rate limiter (e.g. `flask-limiter`) on `/comparison/new` POST and the two `/api/guest/...` endpoints — start with something generous like *5 comparisons per IP per hour, 30 LLM-backed clicks per hour*. For higher protection: a captcha on the first guest comparison of a session, or a small proof-of-work challenge. None of this is needed today (the system is local to the school) but worth wiring up before any public deployment.

---

### 🟢 Stage 1 hybrid weights are untuned

- **Where:** [matcher.py:149-150](matcher.py#L149-L150) — `final_sims = [(s * 0.5) + (b * 0.5) ...]`
- **What's happening:** The 50/50 SPECTER2 / BM25 split is arbitrary. For academic abstracts, 0.7/0.3 (favoring SPECTER2) is often a better baseline; production hybrid systems use **Reciprocal Rank Fusion** instead of weighted sums entirely.
- **Suggested fix:** When time allows, evaluate on a small handful of known-good queries and tune the weights, or switch to RRF (about 6 lines of code in `run_stage1`).

---

## 5. Possible duplicated UI

### 🟢 `history_sentence_match.html` already does paired sentence highlighting

- **Where:** [templates/history_sentence_match.html](templates/history_sentence_match.html)
- **What it does:** Two-column user-vs-repo view with `.has-match` chunks; mouseover pairs sentences across panels.
- **Why it's flagged:** The new feature-highlight modal (added in this session) overlaps conceptually. Before extending either, decide whether they should be merged into a single richer drill-down view, or whether they serve genuinely different purposes (matrix-cell drill-down vs. side-by-side review).

---

## 6. Code-quality observations

### 🟢 Heavy inline `<style>` blocks in templates

- **Where:** [comparison_new.html](templates/comparison_new.html), [history_heatmap_table.html](templates/history_heatmap_table.html), [history_sentence_match.html](templates/history_sentence_match.html), [history_detail.html](templates/history_detail.html)
- **Why it's flagged:** A lot of CSS lives inside `<style>` tags inside templates rather than in `static/css/main.css`. This makes it harder to maintain a consistent visual language and means theme tokens like `--color-primary` get re-declared in scattered places.
- **Suggested fix:** Long-term, migrate per-template styles into `main.css` under namespaced selectors (e.g. `.page--comparison-new ...`). Not urgent.

### 🟢 `app.py` is a single-file monolith (~1100+ lines)

- **Where:** [app.py](app.py)
- **Why it's flagged:** Auth, dashboards, comparisons, history, admin, and the API endpoints all live in one file. Any non-trivial feature requires scrolling through unrelated routes.
- **Suggested fix:** Eventually split into Flask blueprints (`auth_bp`, `comparison_bp`, `history_bp`, `admin_bp`, `api_bp`). Not urgent for a thesis project, but mentioning it for future maintainability.

---

## How to use this file

- Before making a change that touches one of the listed areas, **read the relevant entry first** so you know what the foot-guns are.
- When you fix an item, delete its entry from this file (or move it to a "Resolved" section at the bottom).
- New tech debt discovered later should be appended here, with a severity emoji and a `file:line` reference.

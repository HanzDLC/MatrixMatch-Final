# Feature Matrix — Current Logic & Prompt Redesign Plan

This doc covers (1) how Stage 2's feature matrix is generated today, and (2) the specific prompt/architecture changes we want to make so it actually answers the student's question.

---

## 1. Current generation flow

### Trigger

When a researcher opens a history detail page and the matrix isn't cached, the page renders immediately with a loading spinner. JavaScript then calls `GET /api/history/<id>/feature_matrix` in the background.

```
[history_detail page loads]
         ↓
  Is feature_matrix cached in DB?
         ↓
   YES → return it instantly, done.
    NO → set feature_matrix_pending=True, render page, let JS fetch.
         ↓
[JS calls /api/history/<id>/feature_matrix]
         ↓
   1. Load user_abstract + the Stage 1 matches
   2. Fetch the abstracts of those matched docs
   3. Build one big LLM prompt containing:
        - User Abstract
        - Repository Abstract 1 ... N
        - Instructions + hard bans + evidence rule
   4. Send prompt → LLM returns a JSON array
   5. Parse JSON into list of {feature, "User Abstract", "Abstract 1", ...}
   6. Cache the JSON into comparison_history.feature_matrix
   7. Return JSON to the browser
         ↓
[JS reloads the page → matrix renders from cache]
```

### The LLM prompt (two-step, one call)

**Step 1 — Feature extraction.** LLM reads all abstracts together and produces a **unified master list** of 5–10 concrete system features. Governed by:
- **Categories allowed:** buildable system modules, algorithms/tech stack, target domain/users.
- **Hard bans:** Agile/Scrum/Design Thinking/IPO, ISO 25010, UTAUT/TAM, surveys, Likert scales, ISO quality attributes (functionality/reliability/etc.), post-hoc results ("Highly Acceptable", mean scores).

**Step 2 — Presence check per abstract.** For each feature, the LLM outputs `true`/`false` for the User Abstract and each Repository Abstract. Governed by:
- **Evidence rule:** TRUE only if the feature is *literally stated* in the abstract text. No inference from domain.
- **Ideation rule:** user abstract features must be part of the *proposed system*, not the evaluation plan.
- **When in doubt:** mark FALSE.

### Output shape

```json
[
  {
    "feature": "Real-time GPS Tracking",
    "User Abstract": false,
    "Abstract 1": true,
    "Abstract 2": false
  }
]
```

### Click-a-✓ interaction

Clicking a ✓ in any cell calls a **separate** endpoint `/api/history/<id>/feature_highlight`. That endpoint runs an independent LLM call: given one abstract + one feature name, extract verbatim phrases, wrap in `<mark>` tags, return HTML for the modal.

**Known bug class:** the matrix LLM can mark a cell TRUE by inference (e.g. "Real-time GPS Tracking" for a commuter-app abstract that never says "GPS"), while the strict highlighter LLM correctly fails to find a phrase. Two different LLMs reaching different verdicts on the same question.

### Code pointers

- `evaluate_feature_matrix()` in [matcher.py](../matcher.py) — builds prompt, calls LLM, parses JSON.
- `highlight_feature_in_abstract()` in [matcher.py](../matcher.py) — click-a-✓ phrase finder.
- `api_history_feature_matrix()` in [app.py](../app.py) — lazy-load endpoint + DB caching.
- `api_history_feature_highlight()` in [app.py](../app.py) — click-a-✓ endpoint.
- [templates/history_detail.html](../templates/history_detail.html) — grid, spinner, click modal.

---

## 2. Prompt redesign — ranked by impact

The use case that should drive every prompt decision:

> A 3rd-year BSCS/BSIS/BSIT student pastes their **proposed** capstone abstract (ideation phase, nothing built yet) and wants to know: *"of the specific things I'm planning to build, which ones already exist in past capstones?"*

### #1 — Anchor extraction to the USER abstract, not "all abstracts together"

**Current:** `"extract a unified master list of 5–10 features across ALL these studies."`

**Problem:** pulls features from the repo that the user never proposed (QR scanning, smart locks, online payment). The student then stares at a grid asking *"did I mention QR scanning?"* — wrong question.

**Their real question:** *"Of the things I proposed, what's already been built?"*

**Fix:** extract features from the **user abstract first**, then optionally add at most 2–3 features that are distinctive in repo abstracts but absent from the user (labeled as "already built by others"). Re-centers the grid on the student's proposal.

### #2 — Require verbatim evidence as part of the output

**Current:** two-column JSON (`feature`, `true/false`). Evidence rule is in the prompt but the LLM doesn't have to *show its work*.

**Fix:** require a supporting phrase for every `true`:

```json
{
  "feature": "Real-time Tracking",
  "User Abstract": { "present": true,  "evidence": "time their walk to a stop or station" },
  "Abstract 1":   { "present": true,  "evidence": "real-time jeepney tracking" },
  "Abstract 2":   { "present": false, "evidence": null }
}
```

**Benefits:**
- If the LLM can't quote the abstract, it has to mark `false` → forces honest evaluation.
- The highlighter's job becomes trivial (phrase is pre-extracted).
- Output is auditable — student can see *why* each ✓ was given.

**Single biggest change.** Eliminates the GPS-hallucination class of bug.

### #3 — Ban generic commodity features, not just evaluation boilerplate

**Current:** bans methodologies/standards/surveys. Good.

**Problem:** still allows `"Mobile Application"` and `"Web-based Application"`. Every BSIT capstone has one. Marking both user and all repos ✓ on this gives the student zero novelty signal.

**Fix:** add a specificity rule — *features must be specific enough that a reasonable alternative exists*.

- `"Mobile Application"` → banned.
- `"Offline-first mobile app"` → allowed.
- `"Real-time GPS tracking for jeepneys"` → allowed.
- `"System"` / `"Database"` / `"Web Application"` → banned.

### #4 — Name features using the user abstract's own vocabulary

**Current:** LLM invents labels like `"Real-time GPS Tracking"` even when the user wrote only `"time their walk to a stop"`.

**Problem:** label mismatches the text → highlighter fails → confusing UX.

**Fix:** *"When the feature originates in the user's abstract, name it using words or phrases the user literally used. If the user wrote 'time their walk to a stop', call the feature 'Arrival time prediction' (paraphrase of user's words), not 'Real-time GPS Tracking' (your inference)."*

### #5 — Ban marking the user abstract TRUE for features from the evaluation section

**Current:** ideation-phase rule is there, but the LLM still slips.

**Fix:** *"Before marking the User Abstract TRUE for any feature, check: is this mentioned in the system description or in the evaluation plan? If only in the evaluation plan (sampling, survey, grading, testing criteria), mark FALSE — the student hasn't built anything yet."*

---

## 3. Architectural changes (beyond one prompt edit)

### #6 — Split the two-step into two separate LLM calls

Currently both extraction and evaluation happen in one call, so Step 2 truth values are anchored to whatever the LLM decided in Step 1.

Split:
- **Call A:** extract features from user abstract (+ optional distinctive repo features). Returns a list.
- **Call B:** for each abstract (user + each repo doc), independently check each feature with the evidence rule. Returns booleans + quotes.

Cost: ~5–7 LLM calls instead of 1. At gpt-4o-mini prices, still fractions of a cent per comparison. Quality gain: substantial — each evaluation judgment made in isolation, no anchoring.

### #7 — Add a "novelty signal" summary row above the grid

Three counts: **Present in user only** (potentially novel) / **Present in both** (overlap) / **Present in repo only** (already built without you). Turns a grid into an answer.

---

## 4. Recommended execution order

| Step | Scope | Risk | Priority |
|---|---|---|---|
| Prompt rewrite: #1 + #2 + #5 together | Single edit to `evaluate_feature_matrix` prompt | Low — reversible, easy to test | **Do first** |
| Observe output on the Pasahero test case | No code — just regenerate matrix | None | **Do next** |
| Add #3 (specificity ban) + #4 (user-vocabulary naming) | Another prompt edit | Low | After #1–#2 land |
| #6 split into two LLM calls | Refactor `evaluate_feature_matrix` + the endpoint | Medium — changes cost/latency profile | After prompt quality plateaus |
| #7 novelty summary row | Template + small Python aggregation | Low | UX polish |

---

## 5. Status

- [x] Current logic documented.
- [x] Known bug class (matrix-vs-highlighter disagreement) identified.
- [x] Prompt changes drafted.
- [ ] Implement #1 + #2 + #5 prompt rewrite.
- [ ] Validate on Pasahero test case.
- [ ] Implement #3 + #4.
- [ ] Consider #6 two-call split.
- [ ] Consider #7 novelty summary.

---

## Appendix — Original chat phrasing (verbatim reference)

Prompt design changes, ranked by impact
1. Anchor extraction to the USER abstract, not "all abstracts together"
Current: "extract a unified master list of 5–10 features across ALL these studies."

Problem: pulls features from the repo that the user never proposed (QR scanning, smart locks, online payment). The user then has to stare at a ✓/✗ grid asking "did I mention QR scanning?" — which isn't the question they came here to answer.

Their real question: "Of the things I proposed, what's already been built?"

Fix: extract features from the user abstract first, then add at most 2–3 features that are distinctive in repo abstracts but absent from the user (labeled as "already built by others"). This re-centers the grid on the student's proposal instead of padding it with irrelevant repo features.

2. Require verbatim evidence as part of the output
Current: two-column JSON (feature, true/false). The evidence rule is in the prompt but the LLM doesn't have to show its work.

Fix: require a supporting phrase for every true:


{
  "feature": "Real-time Tracking",
  "User Abstract": { "present": true,  "evidence": "time their walk to a stop or station" },
  "Abstract 1":   { "present": true,  "evidence": "real-time jeepney tracking" },
  "Abstract 2":   { "present": false, "evidence": null }
}
Benefits:

If the LLM can't quote the abstract, it has to mark false — forces honest evaluation.
The highlighter's job becomes trivial (the phrase is pre-extracted).
Output is auditable — the student can see why each ✓ was given.
This is the single biggest change. It eliminates the class of bug you just hit.

3. Ban generic commodity features, not just evaluation boilerplate
Current: bans methodologies, standards, surveys — good.

Problem: still allows "Mobile Application" and "Web-based Application" as features. Every BSIT capstone has one of these. Marking both user and all repo abstracts as ✓ for "Mobile Application" gives the student zero signal about novelty.

Fix: add a specificity rule — features must be specific enough that a reasonable alternative exists. "Mobile Application" → banned. "Offline-first mobile app" → allowed. "Real-time GPS tracking for jeepneys" → allowed. "System" / "Database" / "Web Application" → banned.

4. Name features using the user abstract's own vocabulary
Current: the LLM invents feature labels like "Real-time GPS Tracking" even when the user wrote only "time their walk to a stop."

Problem: the label mismatches the text → highlighter fails → confusing UX.

Fix: instruction like "When the feature originates in the user's abstract, name it using words or phrases the user literally used. If the user wrote 'time their walk to a stop', call the feature 'Arrival time prediction' (paraphrase of user's words), not 'Real-time GPS Tracking' (your inference)."

5. Ban marking the user abstract TRUE for features from the evaluation section
Current: the ideation-phase rule is there, but in practice the LLM still slips.

Fix: explicit rule — "Before marking the User Abstract TRUE for any feature, check: is this mentioned in the system description or in the evaluation plan? If only in the evaluation plan (sampling, survey, grading, testing criteria), mark FALSE — the student hasn't built anything yet."

Architecture changes (not prompt — beyond one message)
6. Split the two-step into two separate LLM calls
Right now both extraction and evaluation happen in one call, so the LLM's Step 2 truth values are anchored to whatever it decided in Step 1. Splitting:

Call A: extract features from user abstract + optionally supplement with distinctive repo features. Returns a list.
Call B: for each abstract (user + each repo doc), independently check each feature against the abstract text with the evidence rule. Returns booleans + quotes.
Cost: ~5–7 LLM calls instead of 1. At gpt-4o-mini prices, still fractions of a cent. Quality gain: substantial — each evaluation judgment is made in isolation, not anchored to the LLM's prior statements.

7. Add a "novelty signal" summary row above the grid
Three counts: Present in user only (potentially novel) / Present in both (overlap — not novel here) / Present in repo only (already built without you). This turns a grid into an answer.

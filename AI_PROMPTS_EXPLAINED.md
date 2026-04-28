# MatrixMatch — AI Prompts Explained

This document walks through the two LLM-powered components in MatrixMatch and the
prompt structure behind each of them:

1. **Stage 2 — AI Feature Matrix** (`matcher.py` → `evaluate_feature_matrix`)
2. **AI Gap Analysis** (`matcher.py` → `generate_gap_analysis`)

Both prompts share a common design discipline: **evidence-first output**. The LLM is
never allowed to assert that a feature is "present" or that two studies are "similar"
without quoting a verbatim substring from the underlying abstract. This turns the LLM
from a free-form summariser into a constrained extractor.

---

## 1. Stage 2 — AI Feature Matrix

### What it produces
A comparison table with:
- **Rows** = short feature labels (e.g. *"QR code scanning"*, *"Real-time GPS tracking"*).
- **Columns** = the User Abstract + each retrieved Repository Abstract (from Stage 1).
- **Cells** = `{ "present": true/false, "evidence": "<verbatim quote>" }`.

### Architecture: two-call design
Instead of one giant prompt that does everything, the matrix is built with **two
separate LLM calls** so each call has one clear job:

| Call | Purpose | Function |
|------|---------|----------|
| **Call A** | Extract the list of features to compare | `_extract_features_from_user_abstract` |
| **Call B** | For each abstract, independently check every feature | `_evaluate_features_against_abstract` (run once per abstract) |

This separation means a bad feature label can't poison the evidence check, and
each abstract is scored in isolation (no cross-contamination between columns).

---

### Prompt A — Feature Extraction

**Persona / framing**
> "You are extracting SYSTEM FEATURES to build a comparison matrix."

**Inputs injected into the prompt**
- `USER ABSTRACT` — the student's proposal (ideation phase, nothing built yet).
- `Repository Abstract 1..N` — the top-N matches from Stage 1.

**Required output shape (strict JSON)**
```json
{
  "user_features": ["Feature label 1", "Feature label 2"],
  "repo_only_features": ["Feature label 1", "Feature label 2"]
}
```

**Rules enforced by the prompt**

1. **Naming rules** — each entry must be a 2–5 word noun phrase (a *capability*,
   not a goal, not a sentence). Examples of good vs bad labels are embedded in
   the prompt so the model has concrete anchors.
2. **List A (`user_features`)** — 3–7 features the user's proposed system will
   *build*. An **IDEATION FILTER** excludes anything that only appears in the
   evaluation plan (surveys, grading scales, sampling methods), because the
   student hasn't built anything yet.
3. **List B (`repo_only_features`)** — up to 3 concrete, domain-relevant
   capabilities that appear in the repo abstracts but not in the user's. These
   surface as extra rows so the student can see what prior work adds.
4. **Hard bans** — categories that are always excluded from both lists:
   - Dev lifecycles (Agile, Scrum, IPO, RAD…)
   - Quality standards (ISO 25010, ISO 9126, McCall)
   - Evaluation frameworks (UTAUT, TAM, Likert, purposive sampling)
   - Non-functional qualities (reliability, usability, maintainability…)
   - Post-hoc results (mean scores, "Highly Acceptable", sample sizes)

**Post-processing** — the returned lists are merged (user features first),
deduped case-insensitively, and passed to Call B.

---

### Prompt B — Evidence Verification (run once per abstract)

**Persona / framing**
> "You are an academic text-analysis assistant. For EACH feature below, decide
> whether it is explicitly present in the abstract text."

**Inputs injected into the prompt**
- `ABSTRACT` — one abstract at a time (either the user's, or one repo abstract).
- `FEATURES TO CHECK` — the deduped list produced by Call A.

**Required output shape (strict JSON)**
```json
{
  "results": [
    {"feature": "<name>", "present": true,  "evidence": "exact quote"},
    {"feature": "<name>", "present": false, "evidence": null}
  ]
}
```

**The Evidence Rule (the heart of the prompt)**

- Mark `present: true` only if the abstract describes the **same concept** as
  the feature — synonyms and paraphrases are allowed.
- When `present: true`, the model MUST quote a **verbatim substring** of the
  abstract (3–15 words, preserving case/spacing/punctuation).
- The feature *name* does not need to literally appear. Example baked into the
  prompt: for feature *"Replacement of barcodes with QR codes"*, the quote
  *"integrates QR code technology"* counts.
- No domain inference. A commuter-app abstract does NOT automatically imply
  "Real-time GPS Tracking" unless the abstract actually describes it.
- When in doubt → `false`.

**Ideation-phase rule** (applies only when the abstract is the User Abstract):
if a feature only appears in the evaluation plan / grading scale, mark `false`
— the student hasn't built it yet.

**Defense-in-depth in code** — after the LLM returns, `_evaluate_features_against_abstract`
runs a substring check: if `evidence` is not a real substring of the abstract,
the cell is downgraded to `present: false`, `evidence: null`. This catches
hallucinated quotes even if the model slips past the instructions.

---

## 2. AI Gap Analysis

### What it produces
A structured, per-repository-match report telling the student how close their
proposal is to one specific prior study. The output is parsed by `app.py` into
four sections for display:

- **Problem Focus** — what each abstract is actually solving.
- **Verdict** — one of: `Duplicate`, `Substantial Overlap`, `Partial Overlap`, `Distinct`.
- **Similarities** — concrete feature-level overlaps, each with quotes from both sides.
- **What Your Proposal Adds** — concrete features the user builds that the repo study doesn't cover.

---

### Prompt structure (one call, highly structured)

**Persona / framing**
> "You are a prior-art search assistant helping a student check whether their
> PROPOSED research idea has already been built. You are NOT a peer reviewer,
> and you do NOT give recommendations."

This persona is deliberate — a "peer reviewer" persona drifted toward giving
advice and padding the output. "Prior-art search assistant" keeps the model on
the narrow question *"has this already been done?"*.

**Context block**
The prompt explicitly tells the model:
- `USER ABSTRACT` = ideation-phase proposal, nothing built yet. Forward-looking
  language about evaluation / surveys describes plans, not features.
- `REPOSITORY ABSTRACT` = completed study, may contain post-hoc artefacts
  (ratings, means, "Highly Acceptable") that are **irrelevant to novelty**.

Both abstracts are then injected between clear delimiter lines.

---

### The reasoning path encoded in the prompt

The prompt walks the model through **three explicit steps** before it writes
the output:

1. **Problem-focus read** — identify the core problem each abstract is solving.
   Are the two studies even aiming at the same kind of problem?
2. **Duplication judgment** — decide whether the user's abstract describes
   essentially the **same idea** (same problem + same system + same core
   methodology). Minor wording differences don't count.
3. **Output** — write the result in the exact format below, and nothing else.

---

### Required output format

```
**Problem Focus:**
- `Your abstract:` <one sentence + 3-15 word verbatim quote>
- `Repository study:` <one sentence + 3-15 word verbatim quote>
- `Alignment:` <SAME / OVERLAPPING / DIFFERENT problems, and why>

**Verdict:** <Duplicate | Substantial Overlap | Partial Overlap | Distinct>

**Similarities:**
- <brief label>: user says "<quote>"; repo says "<quote>".
- (or) `- No specific feature-level similarities beyond shared domain.`

**What Your Proposal Adds:**
- <feature label>: user says "<quote>"; not described in the repository abstract.
- (or) `- The user proposal does not describe any concrete system feature that the repository study does not already cover.`
```

---

### Why "What Your Proposal Adds" is one-directional

The bullet list is intentionally NOT a symmetric "Differences" section. The
student is in ideation phase — saying *"the repo has X, you don't mention X"*
is not useful (absence of mention ≠ absence of intent). So the section only
lists what the **user explicitly describes that the repo abstract does not**.

---

### Hard rules enforced in the prompt

- **Evidence rule** — every bullet MUST contain a 3–15 word verbatim quote
  wrapped in double-quotes. No quote → no bullet. "A short honest output is
  better than a padded speculative one."
- **Self-consistency clamp** — if "What Your Proposal Adds" contains any
  concrete feature bullet, the Verdict CANNOT be `Duplicate`. The model is
  told to downgrade to `Substantial Overlap` or lower. If the Verdict IS
  `Duplicate`, the adds section must contain only the boilerplate
  *"no concrete system feature"* bullet.
- **Exclusions** — must NOT include:
  - Evaluation results, ratings, mean scores, sample sizes, or phrases like
    "Highly Acceptable" / "Strongly Agree" / numeric averages.
  - Speculation about methodology, population, algorithms not literally
    stated in the abstracts.
  - Recommendations, suggestions, or peer-review padding.
  - A trailing Summary section.
- **Rationale vs feature distinction** — the prompt gives an explicit
  counter-example: *"QR code advantages over barcodes"* is a rationale for a
  tech choice, not a feature the user builds. If both abstracts use QR codes,
  that's a **Similarity**, not something the user "adds".

---

## Shared design principles (both prompts)

| Principle | Why it matters |
|---|---|
| **Evidence-first, verbatim quotes** | Turns LLM output into something the student can verify by opening the abstract. Removes hallucination as a plausible failure mode. |
| **Ideation vs completed framing** | Stops the model from penalising the student for not mentioning implementation details they might still add in the final build. |
| **Hard bans on non-functional fluff** | ISO standards, Likert scales, and "Highly Acceptable" phrases are repository noise — they'd otherwise dominate the matrix and drown out real feature comparisons. |
| **Strict JSON / strict markdown shapes** | The outputs are parsed downstream. A loose "just tell me what's similar" prompt would break the UI. |
| **Code-side defense in depth** | Substring checks on evidence (matrix) and regex parsing (gap analysis) catch the rare case where the model slips past the instructions. |

---

## File references

- Feature matrix prompts: [matcher.py](matcher.py) lines ~585 (Call A) and ~688 (Call B).
- Feature matrix orchestration: [matcher.py](matcher.py) `evaluate_feature_matrix` (~line 791).
- Gap analysis prompt: [matcher.py](matcher.py) `generate_gap_analysis` (~line 1055).
- LLM provider abstraction: [llm_provider.py](llm_provider.py).

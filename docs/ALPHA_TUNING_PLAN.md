# MatrixMatch — Alpha Tuning Plan (Stage 1 Hybrid Retrieval)

## Context

**MatrixMatch** is a capstone project for BSCS/BSIS/BSIT students. Stage 1 is a **Hybrid Semantic + Lexical Retrieval Engine**.

- **Data**: local repository of 64 past capstone abstracts (150–250 words each), stored in the project database (IDs 40–103).
- **Semantic Model**: `all-mpnet-base-v2` (local, via `sentence-transformers`) — 768-dim vectors, Cosine Similarity.
- **Lexical Model**: `rank-bm25` (local) — exact keyword / acronym matching.
- **Backend**: Flask + MySQL. Frontend calls the local Python engine via internal routes.

> Note: an earlier version of this plan referenced PostgreSQL + Next.js. The actual stack is MySQL + Flask. The *methodology* below is stack-agnostic.

---

## Goal

Empirically characterize how retrieval quality depends on the fusion weight α in:

```
final_score = (α · MPNet_score) + ((1 − α) · BM25_norm)
```

…on our domain corpus, and select a defensible α for deployment instead of guessing a 50/50 split.

> **Framing note:** we are NOT claiming to "mathematically prove the optimal α." With N=15 queries the data cannot support that claim. We are characterizing sensitivity and selecting a defensible operating point, with honest limitations.

---

## Step 1 — Ground Truth Queries (External Sourcing)

**Problem with earlier approach:** writing our own query abstracts leaks repo knowledge — we unconsciously pick phrasing that matches our own docs, inflating results.

**Revised approach: source queries from the public internet, not from our own writing.**

### Procedure

1. **Topic labels only, authored by project owner.** Produce 15 short topic labels (e.g., "Web-based inventory management system", "IoT smart agriculture", "Mobile GPS public transit app"). Full candidate list in [ALPHA_TUNING_QUERIES.md](ALPHA_TUNING_QUERIES.md).

2. **Second annotator sources the external abstracts.** For each topic label, the second annotator — *not* the project owner — searches the public web and captures **one real abstract** per topic. This prevents the owner from cherry-picking phrasings that match repo docs.

3. **Sourcing rule (commit to it before searching, no reshuffling after):**
   - Search **Google Scholar** (or ResearchGate / IEEE / ACM / university thesis archives) with the topic label as the query.
   - Take the **first result** that meets all of:
     - Full abstract publicly visible (not behind paywall snippet)
     - Length between 100 and 300 words (matches our repo shape)
     - Academic register (no blog posts, news articles, product pages)
     - Not byte-identical to any existing repo doc (sanity check against `documents.abstract`)
   - Record the **source URL** for every chosen abstract — goes in the paper appendix.

4. **Independent ground-truth labeling by both annotators.**
   - Project owner independently labels which repo doc IDs (from 40–103) are "relevant" to each external query.
   - Second annotator independently does the same — without seeing the owner's labels.
   - Compare labels, compute **percent agreement** (e.g., "82% of doc-query pairs received the same relevance label from both annotators").
   - Resolve disagreements by discussion → final ground truth.
   - Report the agreement rate in the methodology chapter.

5. **Pre-register.** Commit `queries_ground_truth.csv` to the repo *before* running any α tuning. Screenshot the commit hash for the paper. This kills the "tuned on test set" critique.

### What this buys us

| Original bias | Mitigation |
|---|---|
| Owner wrote queries knowing the repo contents | Second annotator sources abstracts from external sources |
| Owner alone defined "relevant" | Both annotators label independently; agreement rate reported |
| Owner could pick the most favorable abstract | Fixed sourcing rule: "first qualifying result" |
| No record that tuning happened after labeling | Pre-registration commit hash |

---

## Step 2 — Normalization Math

MPNet cosine scores live in **[0.0, 1.0]**. BM25 is **unbounded** and its scale varies per query (depends on term IDF and document length).

**Rule:** apply **Min-Max scaling per query** to BM25 scores *before* applying α:

```
bm25_norm_i = (bm25_i − min(bm25_query)) / (max(bm25_query) − min(bm25_query))
```

This puts both signals on a comparable [0, 1] scale so α has a meaningful interpretation. Without per-query normalization, α would conflate "weight" with "scale mismatch," and the tuning result would be meaningless.

**BM25 tokenization must be fixed and documented** (lowercase, stopword list, stemming choice). The BM25 score depends heavily on tokenization — record the exact config in the methodology chapter.

---

## Step 3 — Grid Search Script with Leave-One-Out CV

### Core sweep

Loop α from **0.0 → 1.0 in steps of 0.05** (21 values). For each α:

**Fusion formula:**
```
final_score = (α · MPNet_score) + ((1 − α) · BM25_norm)
```

For each query:
1. Compute MPNet cosine scores against every doc in the repo.
2. Compute BM25 scores against every doc, min-max normalize per query.
3. Fuse with the current α, sort descending, take Top-5.
4. Compare Top-5 against the ground-truth doc IDs.

### Leave-One-Out Cross-Validation (addresses "tuning on the test set")

For each of the 15 queries:
- **Train fold:** the other 14 queries. Pick the α that maximizes the chosen metric on those 14.
- **Test fold:** the held-out 1 query. Evaluate using the α picked from the train fold.
- Rotate through all 15 queries.
- Report **mean test-fold performance** — this is the honest number.
- Also report the α chosen in each of the 15 folds (shows stability: if α stays between 0.4–0.6 across folds, that's strong evidence).

### Metrics reported per α

- **MRR** (Mean Reciprocal Rank) — *primary metric, more sensitive than Top-5 Accuracy at this N.*
- **Top-5 Accuracy** (≥1 relevant doc in Top-5) — secondary, expect saturation.
- **Recall@5** (fraction of relevant docs retrieved) — sanity check.

### Baselines to include in the results table

| α | Description |
|---|---|
| 0.00 | Pure BM25 |
| 1.00 | Pure MPNet (SBERT) |
| 0.05…0.95 | Hybrid grid |
| RRF | Reciprocal Rank Fusion, no weights |

Include RRF so the panel's "why not RRF?" question has an answer ready.

### Reporting stance (commit to this)

- Report **the α range where performance is indistinguishable from the peak**, not a single "optimal α". Likely phrasing: *"MRR is stable within ±0.02 across α ∈ [0.4, 0.7]; we select α = 0.5 as the midpoint for deployment."*
- If α = 1.0 (pure SBERT) wins, **say so**. That is a legitimate finding — hybrid did not help on this corpus. This up-front commitment protects credibility.

---

## Critique Resolution (how this plan answers the earlier critique)

| # | Original critique | How the updated plan addresses it | Residual risk |
|---|---|---|---|
| 1 | **N=15 is tiny** — Top-5 Acc saturates, noise bumps mimic signal. | Promote MRR to primary metric (more sensitive than Top-5 Acc). Report α *ranges* where performance is indistinguishable from peak, not a single "optimal" value. Include bootstrap 95% CIs around MRR if time permits (optional polish). | Cannot be fully fixed without more data. Handled in Limitations — small N is acknowledged, not hidden. |
| 2 | **Tuning on the test set** (picking α from the same queries you report). | Leave-one-out cross-validation in Step 3: tune α on 14 queries, test on the held-out 1, rotate through all 15. Report mean test-fold performance. Pre-register `queries_ground_truth.csv` via a git commit *before* any tuning runs. | Resolved. LOO-CV + pre-registration are the textbook fix. |
| 3 | **Owner-authored queries leak repo knowledge.** | Step 1 rewritten: second annotator sources one real abstract per topic from public web (Google Scholar / IEEE / etc.) using a fixed rule ("first qualifying result"). Source URLs recorded in appendix. | Partial. Owner still picks the *topic labels*, so topic selection bias remains. Acknowledged in Limitations #3. |
| 4 | **Single annotator (= project owner) for ground truth.** | Second annotator (confirmed available) independently labels relevance. Inter-annotator agreement rate reported. Disagreements resolved by discussion → final ground truth. | Resolved for undergraduate standards. A third annotator would strengthen further but is not required at this level. |
| 5 | **Top-5 Accuracy too lenient as primary metric.** | MRR promoted to primary metric. Top-5 Acc and Recall@5 reported as secondary. | Resolved. |
| 6 | **Coarse grid (0.1 step) + noisy data → spurious "optimum".** | Grid tightened to 0.05 step (21 α values). Reporting stance committed to reporting *ranges*, not single points. Explicit phrasing: *"MRR stable within ±0.02 across α ∈ [X, Y]; we select α = Z as midpoint."* | Resolved in how results are framed. The underlying noise is inherent to small N. |

**Bottom-line shift:** the plan no longer claims to "mathematically prove the optimal α." It empirically characterizes α-sensitivity on the corpus and selects a defensible operating point, with every known bias either mitigated or disclosed.

---

## Limitations (write into the paper, do not hide)

1. **Small N.** 15 queries cannot detect small true differences between α values. Conclusions are about broad ranges, not precise optima.
2. **Domain-specific corpus.** 64 undergraduate capstone abstracts from one institution's programs (BSCS / BSIS / BSIT). Results may not generalize to other corpora.
3. **Topic labels authored by project owner.** Even with external abstract sourcing, the *choice of topics* reflects what the owner considered important. A different topic set could shift results.
4. **Ground truth uses binary relevance.** No graded relevance (highly / somewhat / not relevant) — precludes nDCG.
5. **BM25 tokenization sensitivity.** Results depend on stopword list and stemming choice; these are fixed and documented but not themselves tuned.
6. **No user study.** Real-world query quality (from 3rd-year students who have not read the repo) may differ from the external abstracts used here.

---

## Deliverables

1. **`queries_ground_truth.csv`** — topic labels, source URLs for external abstracts, final merged ground truth, committed to git before tuning.
2. **`alpha_tuning.py`** — implements Step 3 including leave-one-out CV and RRF baseline.
3. **Inter-annotator agreement report** — table showing per-query agreement between the two annotators on relevance labels.
4. **Results table** — α vs. MRR / Top-5 Acc / Recall@5 + RRF baseline row, plus per-fold α selections from LOO-CV.
5. **Methodology chapter** covering Steps 1–3, reporting stance, and the Limitations section above.

---

## Status

- [x] Repo inventoried — 64 abstracts across BSCS / BSIS / BSIT (IDs 40–103).
- [x] Candidate topic labels drafted — see [ALPHA_TUNING_QUERIES.md](ALPHA_TUNING_QUERIES.md).
- [x] Second annotator confirmed available.
- [ ] Finalize 15 topic labels (may trim or expand after review).
- [ ] Second annotator sources one external abstract per topic, records source URLs.
- [ ] Both annotators independently label relevance; compute agreement; resolve disagreements.
- [ ] Commit `queries_ground_truth.csv` to git (pre-registration).
- [ ] Write `alpha_tuning.py` with LOO-CV and RRF baseline.
- [ ] Run sweep, produce results table.
- [ ] Draft Limitations paragraph into the methodology chapter.

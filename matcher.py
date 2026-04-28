# matcher.py
import html as _html
import json
import re
from typing import List, Tuple, Dict, Optional

import pandas as pd
import torch
from sentence_transformers import SentenceTransformer, util
import requests
from matplotlib.figure import Figure

from llm_provider import get_llm_provider
from db import DB_CONFIG, get_db_connection  # noqa: F401 — DB_CONFIG re-exported for callers still importing it


# -----------------------------
# SBERT model (cached)
# -----------------------------
_model = None

def get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer("all-mpnet-base-v2")
    return _model


# -----------------------------
# Document embedding cache
# Avoids re-encoding all 64+ doc abstracts on every Stage 1 query.
# Keyed by document_id -> 1D torch.Tensor.
# Invalidated by upload / edit / delete in app.py.
# -----------------------------
_doc_embedding_cache: Dict[int, torch.Tensor] = {}


def _get_doc_embeddings(docs: List[Dict]) -> torch.Tensor:
    """
    Returns a 2D tensor of embeddings aligned with the order of `docs`.
    Uses the per-document cache; only encodes documents not already cached.
    """
    missing = [d for d in docs if d["document_id"] not in _doc_embedding_cache]
    if missing:
        new_embs = get_model().encode(
            [d["abstract"] for d in missing], convert_to_tensor=True
        )
        for d, emb in zip(missing, new_embs):
            _doc_embedding_cache[d["document_id"]] = emb
    return torch.stack([_doc_embedding_cache[d["document_id"]] for d in docs])


def invalidate_doc_embedding(document_id: int) -> None:
    """Drop a single document's cached embedding (call on edit or delete)."""
    _doc_embedding_cache.pop(document_id, None)


def clear_doc_embedding_cache() -> None:
    """Drop all cached embeddings (call on upload, or on bulk schema changes)."""
    _doc_embedding_cache.clear()

# -----------------------------
# LLM-backed feature extraction
# -----------------------------
def generate_unique_features(abstract: str) -> List[str]:
    """Uses the configured LLM provider to extract unique methodology/thematic features as a JSON array."""
    if not abstract.strip():
        return []

    prompt = (
        "You are an expert academic librarian. Read the following research abstract and "
        "identify all unique, high-level methodological or thematic keywords or features. "
        "Do not use generic terms. Return EXACTLY a json array of strings, and nothing else.\n\n"
        f"Abstract: {abstract}"
    )

    try:
        text = get_llm_provider().generate(prompt, json_mode=True)
        # Some providers may wrap the array in an object like {"keywords": [...]}; handle both.
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        if isinstance(parsed, dict):
            for value in parsed.values():
                if isinstance(value, list):
                    return [str(item) for item in value]
        return []
    except Exception as e:
        print(f"Error generating features: {e}")
        return []


# -----------------------------
# Stage 1: Abstract vs Documents
# -----------------------------
def run_stage1(
    researcher_id: int,
    keywords,
    user_abstract: str,
    research_field_filter: str = "ALL",
    similarity_threshold: float = 0.6,
) -> Tuple[Optional[int], List[Dict]]:
    """
    Stage 1:
    - Load documents from DB (optionally filter by research_field)
    - Compute similarity between user_abstract and each document abstract
    - Keep docs >= similarity_threshold
    - Save comparison_history row and return (history_id, matches)

    `keywords` is the researcher's feature list — list of {label, description}
    dicts. Stored as JSON in comparison_history.keywords.

    matches is a list of dicts:
      {
        "document_id": int,
        "title": str,
        "research_field": str,
        "similarity": float
      }
    """
    # 1) Load docs
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if research_field_filter and research_field_filter != "ALL":
            cursor.execute(
                """
                SELECT document_id, title, research_field, abstract
                FROM documents
                WHERE research_field = %s
                """,
                (research_field_filter,),
            )
        else:
            cursor.execute(
                """
                SELECT document_id, title, research_field, abstract
                FROM documents
                """
            )
        docs = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    if not docs:
        return None, []

    # 2) Semantic Similarity (MPNet + cosine)
    model = get_model()
    user_emb = model.encode(user_abstract, convert_to_tensor=True)
    doc_embs = _get_doc_embeddings(docs)

    sims_tensor = util.cos_sim(user_emb, doc_embs)[0]
    final_sims = sims_tensor.cpu().tolist()  # same order as docs

    matches: List[Dict] = []
    for doc, sim_val in zip(docs, final_sims):
        if sim_val >= similarity_threshold:
            matches.append(
                {
                    "document_id": doc["document_id"],
                    "title": doc["title"],
                    "research_field": doc["research_field"],
                    "similarity": float(sim_val),
                }
            )

    # Sort desc by similarity
    matches.sort(key=lambda m: m["similarity"], reverse=True)

    # 3) Save history
    if not matches:
        # still save a history row so it shows up in list
        top_matches_str = ""
    else:
        # store as "docID|similarity"
        top_matches_str = ",".join(
            f"{m['document_id']}|{m['similarity']:.4f}" for m in matches
        )

    keywords_json = json.dumps(keywords, ensure_ascii=False)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO comparison_history
            (researcher_id, keywords, user_abstract,
             academic_program_filter, similarity_threshold, top_matches)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING history_id
            """,
            (
                researcher_id,
                keywords_json,
                user_abstract,
                research_field_filter,
                float(similarity_threshold),
                top_matches_str,
            ),
        )
        history_id = cursor.fetchone()[0]
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    return history_id, matches


def recalculate_history(history_id: int) -> Tuple[int, List[Dict]]:
    """
    Re-runs Stage 1 scoring (MPNet + cosine) for an EXISTING history row,
    using the saved user_abstract / threshold / research_field filter (stored
    in the legacy academic_program_filter column), and UPDATES the row's
    `top_matches` field in place. Returns (match_count, matches).

    The history_id stays the same so existing bookmarks / links keep working.
    """
    # 1) Load the existing history row. The column is still named
    # academic_program_filter for storage compatibility, but new rows store
    # research-field labels in it.
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT user_abstract, similarity_threshold, academic_program_filter
            FROM comparison_history
            WHERE history_id = %s
            """,
            (history_id,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if not row:
        return 0, []

    user_abstract = row["user_abstract"] or ""
    similarity_threshold = float(row["similarity_threshold"] or 0.6)
    research_field_filter = row["academic_program_filter"] or "ALL"

    if not user_abstract.strip():
        return 0, []

    # 2) Load candidate documents (same query shape as run_stage1)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if research_field_filter and research_field_filter != "ALL":
            cursor.execute(
                """
                SELECT document_id, title, research_field, abstract
                FROM documents
                WHERE research_field = %s
                """,
                (research_field_filter,),
            )
        else:
            cursor.execute(
                """
                SELECT document_id, title, research_field, abstract
                FROM documents
                """
            )
        docs = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    if not docs:
        # Persist an empty result so the UI reflects the recalc
        _update_top_matches(history_id, "")
        return 0, []

    # 3) Same MPNet + cosine scoring as run_stage1
    model = get_model()
    user_emb = model.encode(user_abstract, convert_to_tensor=True)
    doc_embs = _get_doc_embeddings(docs)

    sims_tensor = util.cos_sim(user_emb, doc_embs)[0]
    final_sims = sims_tensor.cpu().tolist()

    # 4) Filter + sort
    matches: List[Dict] = []
    for doc, sim_val in zip(docs, final_sims):
        if sim_val >= similarity_threshold:
            matches.append(
                {
                    "document_id": doc["document_id"],
                    "title": doc["title"],
                    "research_field": doc["research_field"],
                    "similarity": float(sim_val),
                }
            )
    matches.sort(key=lambda m: m["similarity"], reverse=True)

    # 5) Persist back into the SAME history row
    if matches:
        top_matches_str = ",".join(
            f"{m['document_id']}|{m['similarity']:.4f}" for m in matches
        )
    else:
        top_matches_str = ""

    _update_top_matches(history_id, top_matches_str)
    return len(matches), matches


def _update_top_matches(history_id: int, top_matches_str: str) -> None:
    """Helper: write the new top_matches string back to comparison_history."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE comparison_history
            SET top_matches = %s
            WHERE history_id = %s
            """,
            (top_matches_str, history_id),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def run_stage2(keywords, stage1_matches, abstracts, show_heatmap=True):
    """
    Stage 2: Keyword vs Abstract matrix.

    keywords: list of keyword strings
    stage1_matches: list of (document_id, title, program, similarity)
    abstracts: list of document abstracts in SAME ORDER as stage1_matches

    Returns (fig, matrix)
    """
    from matplotlib.figure import Figure

    if not keywords or not stage1_matches or not abstracts:
        return None, None

    model = get_model()

    # Encode
    kw_embs = model.encode(keywords, convert_to_tensor=True)
    abs_embs = model.encode(abstracts, convert_to_tensor=True)

    sims = util.cos_sim(kw_embs, abs_embs).cpu().numpy()

    col_names = [f"{m[1]} (ID:{m[0]})" for m in stage1_matches]
    matrix = pd.DataFrame(sims, index=keywords, columns=col_names)

    if not show_heatmap:
        return None, matrix

    # Build heatmap
    fig = Figure(figsize=(1.2 * len(col_names), 0.5 * len(keywords)))
    ax = fig.add_subplot(111)
    im = ax.imshow(matrix, aspect='auto', interpolation='nearest')

    ax.set_xticks(range(len(col_names)))
    ax.set_xticklabels(col_names, rotation=45, ha='right', fontsize=8)
    ax.set_yticks(range(len(keywords)))
    ax.set_yticklabels(keywords, fontsize=8)

    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            val = matrix.iat[i, j]
            ax.text(j, i, f"{val*100:.1f}%", ha='center', va='center',
                    color='white' if val > 0.5 else 'black', fontsize=6)

    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    return fig, matrix


# -----------------------------
# Helpers to read history + matches from DB
# -----------------------------
def get_history_with_matches(history_id):
    """
    Load a single history entry and reconstruct Stage 1 matches from the DB.

    Returns:
        history: dict (row from comparison_history + researcher_name, etc.)
        matches: list[dict] with keys:
                 - document_id
                 - title
                 - research_field   (from documents.research_field)
                 - similarity
    """
    # --- Connect to DB ---
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    try:
        # 1) Load history row + researcher info
        cur.execute(
            """
            SELECT
                ch.*,
                CONCAT(u.first_name, ' ', u.last_name) AS researcher_name
            FROM comparison_history ch
            JOIN users u ON ch.researcher_id = u.researcher_id
            WHERE ch.history_id = %s
            """,
            (history_id,),
        )
        history = cur.fetchone()
        if not history:
            return None, []

        # 2) Parse top_matches -> list of (doc_id, similarity)
        raw_top = history.get("top_matches") or ""
        doc_pairs = []  # list of (doc_id, similarity)

        # formats we expect:
        #   "41|0.9350,8|0.8912"
        #   or "41,8,12"
        for entry in raw_top.split(","):
            entry = entry.strip()
            if not entry:
                continue

            if "|" in entry:
                doc_id_str, sim_str = entry.split("|", 1)
                doc_id_str = doc_id_str.strip()
                sim_str = sim_str.strip()
            else:
                doc_id_str = entry
                sim_str = None

            if doc_id_str.isdigit():
                doc_id = int(doc_id_str)
                try:
                    similarity = float(sim_str) if sim_str is not None else 0.0
                except ValueError:
                    similarity = 0.0
                doc_pairs.append((doc_id, similarity))

        if not doc_pairs:
            return history, []

        doc_ids = [dp[0] for dp in doc_pairs]

        # 3) Load documents from `documents` table, including research_field and abstract
        placeholders = ", ".join(["%s"] * len(doc_ids))
        cur.execute(
            f"""
            SELECT document_id, title, research_field, research_field_other,
                   abstract, source_file_path
            FROM documents
            WHERE document_id IN ({placeholders})
            """,
            tuple(doc_ids),
        )
        docs = cur.fetchall()

        if not docs:
            return history, []

        docs_by_id = {row["document_id"]: row for row in docs}

        # 4) Build matches list with "research_field" and "abstract" populated
        matches = []
        for doc_id, sim in doc_pairs:
            d = docs_by_id.get(doc_id)
            if not d:
                continue

            matches.append(
                {
                    "document_id": d["document_id"],
                    "title": d["title"],
                    "research_field": d.get("research_field") or "",
                    "research_field_other": d.get("research_field_other") or "",
                    "abstract": d.get("abstract") or "No abstract available.",
                    "source_file_path": d.get("source_file_path") or "",
                    "similarity": sim,
                }
            )

        return history, matches

    finally:
        cur.close()
        conn.close()


# -----------------------------
# Stage 2: LLM-Clustered Feature Matrix
#
# Both sides ship pre-extracted key feature lists:
#   - user_keywords: from the chip input on /comparison/new
#                    (persisted to comparison_history.keywords)
#   - documents.key_features: populated on upload / admin edit / backfill
#
# Stage 2 makes ONE LLM call that takes those lists and returns clusters of
# labels that describe the same system capability (e.g. "Real-time shuttle
# tracking" and "Real-time jeepney tracking" collapse into one row because the
# model understands shuttle ≈ jeepney for this purpose). The matrix is then
# built deterministically from the clusters. If the LLM call fails or returns
# unparseable JSON, we fall back to a normalized exact-string grouping so the
# matrix never breaks — it's just less smart.
#
# Cell schema (unchanged):
#   row["User Abstract"] = {"present": bool, "evidence": str or None}
#   row["Abstract N"]    = {"present": bool, "evidence": str or None}
# -----------------------------

_PUNCT_STRIP_RE = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _normalize_feature_label(label: str) -> str:
    """Lowercase, trim, collapse whitespace, strip leading/trailing punctuation."""
    if not label:
        return ""
    s = _WS_RE.sub(" ", str(label).strip().lower())
    s = _PUNCT_STRIP_RE.sub("", s)
    return s


def _parse_stored_key_features(raw) -> List[Dict[str, str]]:
    """Parse the document's key_features column into a list of feature dicts:
    [{"label": str, "description": str}, ...]

    Accepts:
      - The current shape: JSON array of {"label", "description"} dicts.
      - Legacy shape: JSON array of plain strings (description -> "").
      - Legacy shape: comma-separated string (description -> "").
      - None / empty -> [].
    """
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        text = str(raw).strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                items = json.loads(text)
                if not isinstance(items, list):
                    items = []
            except json.JSONDecodeError:
                items = []
        else:
            items = [part.strip() for part in text.split(",") if part.strip()]

    out: List[Dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            description = str(item.get("description") or "").strip()
        else:
            label = str(item).strip()
            description = ""
        if label:
            out.append({"label": label, "description": description})
    return out


_FEATURE_CLUSTER_PROMPT = """You are grouping system features into clusters.

Each FEATURE has a short LABEL and a one-sentence DESCRIPTION of what it does for the user.
Two features belong in the SAME cluster IF AND ONLY IF they describe the SAME system capability —
even when their labels differ or their descriptions are at different levels of detail.

KEY RULE: judge by DESCRIPTION, not LABEL. The description is what tells you whether two features
do the same thing. Identical labels with different descriptions might be different capabilities;
different labels with similar descriptions are the same capability.

PRODUCT-NAMING RULE: A user feature labeled "E-wallet integration" with description
"passenger pays using GCash or Maya" SHOULD merge with a doc feature labeled "GCash payment"
with description "user pays for the booked seat using their GCash wallet". Generic-vs-specific
brand naming is not a reason to keep them apart when the underlying capability matches.

SAME-PURPOSE / DIFFERENT-ACTOR RULE: "Driver shares jeepney location" and "Commuters watch
jeepney's live position" describe the SAME capability seen from two angles (producer vs
consumer side of live location sharing) — they belong in the same cluster. Don't split a
single capability just because the descriptions were written from different actor perspectives.

INTENT-vs-IMPLEMENTATION RULE: USER features come from a research concept (intent — "what we
will build") while DOC features come from completed studies (implementation — "what they built").
Match user intent to repo implementation; don't penalize a user feature for being less detailed.

SAME (merge) examples:
- USER "Real-time GPS tracking — Shows the live location of the shuttle to passengers on a map."
  + DOC "GPS integration — Driver's app shares the jeepney's current location so commuters can see it."
  → one live-location-sharing capability.
- USER "QR code scanning — Passenger scans a QR code at the door to log boarding."
  + DOC "QR attendance — Student scans a QR code at the classroom to record attendance."
  → one QR-based identification capability.

DIFFERENT (keep separate) examples:
- USER "Real-time GPS tracking — Shows the live location of the shuttle to passengers on a map."
  vs DOC "GPS-based seat finder — Confirms the booked passenger is on the right bus by matching
  their phone's location to the bus's location, used to flag no-shows."
  → both mention GPS, but descriptions show different functions (live position viewing vs
  presence verification). Keep separate.
- "Real-time tracking" vs "Arrival-time prediction" — tracking shows current position;
  prediction estimates a future time. Different capabilities.

Return EXACTLY this JSON shape, and nothing else:
{{
  "clusters": [
    {{
      "canonical": "<short noun phrase, 2-5 words>",
      "user_labels": ["<original label>"],
      "doc_labels": {{"1": ["<original label>"], "3": ["<original label>"]}}
    }}
  ]
}}

RULES:
- Every INPUT label must appear in exactly one cluster. Do not drop or duplicate labels.
- Copy labels VERBATIM from the input (preserve case, spacing, punctuation). Do NOT invent
  new labels or paraphrase.
- `canonical`: when any USER label is in the cluster, use one of the user's labels unchanged
  (preserves the researcher's vocabulary). Otherwise pick the shortest, most readable doc
  label in the cluster.
- `user_labels`: may be omitted or an empty list if no user label is in this cluster.
- `doc_labels`: keys are 1-based document numbers exactly as provided below. Omit keys for
  docs that contribute nothing to this cluster. Never invent doc numbers.
- Cluster order: clusters containing user_labels come FIRST, then repo-only clusters.

INPUT:
USER FEATURES:
{user_block}

{doc_blocks}

Return the JSON now."""


def _format_feature_line(feature: Dict[str, str]) -> str:
    """Render one feature as `- Label — Description` for the prompt."""
    label = str(feature.get("label") or "").strip()
    description = str(feature.get("description") or "").strip()
    if description:
        return f"- {label} — {description}"
    return f"- {label}"


def _cluster_features_llm(
    user_features: List[Dict[str, str]],
    doc_features_lists: List[List[Dict[str, str]]],
) -> Optional[List[Dict]]:
    """One LLM call to group pre-extracted features (label + description) into
    semantic clusters.

    Returns a list of cluster dicts:
        {"canonical": str,
         "user_labels": List[str],
         "doc_labels": {doc_idx:int -> List[str]}}
    where doc_idx is 0-based (so it can be used directly to build "Abstract N+1"
    cell keys).

    Returns None on any call/parse failure so the caller can fall back."""
    if not user_features and not any(doc_features_lists):
        return []

    user_block = "\n".join(_format_feature_line(f) for f in user_features) or "(none)"
    doc_blocks_parts: List[str] = []
    for i, feats in enumerate(doc_features_lists):
        body = "\n".join(_format_feature_line(f) for f in feats) or "(none)"
        doc_blocks_parts.append(f"DOC {i+1} FEATURES:\n{body}")
    doc_blocks = "\n\n".join(doc_blocks_parts)

    prompt = _FEATURE_CLUSTER_PROMPT.format(
        user_block=user_block,
        doc_blocks=doc_blocks,
    )

    user_labels_list = [str(f.get("label") or "").strip() for f in user_features]
    user_labels_list = [x for x in user_labels_list if x]
    doc_labels_lists = [
        [str(f.get("label") or "").strip() for f in feats if str(f.get("label") or "").strip()]
        for feats in doc_features_lists
    ]

    try:
        raw = get_llm_provider().generate(prompt, json_mode=True, temperature=0.1)
    except Exception as e:
        print(f"[stage2] LLM cluster call failed: {e}")
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[stage2] LLM cluster JSON parse failed: {e}")
        return None

    raw_clusters = parsed.get("clusters") if isinstance(parsed, dict) else None
    if not isinstance(raw_clusters, list):
        print("[stage2] LLM response missing `clusters` array")
        return None

    clusters: List[Dict] = []
    for c in raw_clusters:
        if not isinstance(c, dict):
            continue

        user_part = c.get("user_labels") or []
        if not isinstance(user_part, list):
            user_part = []
        user_clean = [str(x).strip() for x in user_part if str(x).strip()]

        doc_part = c.get("doc_labels") or {}
        if not isinstance(doc_part, dict):
            doc_part = {}
        doc_clean: Dict[int, List[str]] = {}
        for k, v in doc_part.items():
            try:
                idx = int(k) - 1  # convert 1-based → 0-based
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(doc_labels_lists):
                continue
            if not isinstance(v, list):
                continue
            labels = [str(x).strip() for x in v if str(x).strip()]
            if labels:
                doc_clean[idx] = labels

        canonical = str(c.get("canonical") or "").strip()
        if not canonical:
            if user_clean:
                canonical = user_clean[0]
            else:
                for _, vals in sorted(doc_clean.items()):
                    if vals:
                        canonical = vals[0]
                        break
        if not canonical:
            continue

        clusters.append({
            "canonical": canonical,
            "user_labels": user_clean,
            "doc_labels": doc_clean,
        })

    # Safety net: if the LLM dropped any input label, append it as its own cluster
    # so no signal is lost. Case-insensitive membership is forgiving about trivial
    # whitespace / case differences.
    covered = set()
    for c in clusters:
        for x in c["user_labels"]:
            covered.add(_normalize_feature_label(x))
        for labels in c["doc_labels"].values():
            for x in labels:
                covered.add(_normalize_feature_label(x))

    for x in user_labels_list:
        n = _normalize_feature_label(x)
        if n and n not in covered:
            clusters.append({"canonical": x, "user_labels": [x], "doc_labels": {}})
            covered.add(n)
    for i, labels in enumerate(doc_labels_lists):
        for x in labels:
            n = _normalize_feature_label(x)
            if n and n not in covered:
                clusters.append({"canonical": x, "user_labels": [], "doc_labels": {i: [x]}})
                covered.add(n)

    return clusters


def _cluster_features_exact(
    user_features: List[Dict[str, str]],
    doc_features_lists: List[List[Dict[str, str]]],
) -> List[Dict]:
    """Deterministic fallback: group features by normalized label exact match.
    Same return shape as `_cluster_features_llm`. Used when the LLM is
    unavailable or returns unusable JSON. Descriptions are ignored here —
    this is the dumb-but-safe path."""
    by_norm: Dict[str, Dict] = {}
    order: List[str] = []

    def _add(source: Optional[int], label: str) -> None:
        norm = _normalize_feature_label(label)
        if not norm:
            return
        cluster = by_norm.get(norm)
        if cluster is None:
            cluster = {
                "canonical": label,
                "user_labels": [],
                "doc_labels": {},
                "_has_user": False,
            }
            by_norm[norm] = cluster
            order.append(norm)
        if source is None:
            cluster["_has_user"] = True
            cluster["user_labels"].append(label)
            cluster["canonical"] = label  # prefer user's wording
        else:
            cluster["doc_labels"].setdefault(source, []).append(label)

    for f in user_features:
        label = str(f.get("label") or "").strip()
        if label:
            _add(None, label)
    for i, feats in enumerate(doc_features_lists):
        for f in feats:
            label = str(f.get("label") or "").strip()
            if label:
                _add(i, label)

    user_first = [by_norm[n] for n in order if by_norm[n]["_has_user"]]
    repo_only = [by_norm[n] for n in order if not by_norm[n]["_has_user"]]
    out: List[Dict] = []
    for c in user_first + repo_only:
        c.pop("_has_user", None)
        out.append(c)
    return out


def evaluate_feature_matrix(
    user_keywords,
    matches: List[Dict],
    user_abstract: str = "",  # kept for backward-compat; unused now
) -> List[Dict]:
    """
    Stage 2 feature matrix, built from pre-extracted key_features on both sides.

    Inputs:
      user_keywords: the researcher's feature list from /comparison/new — list
                     of {label, description} dicts (legacy bare strings tolerated).
      matches:       Stage 1 matches (each must carry `document_id`)
      user_abstract: legacy parameter, ignored (kept so existing callers don't break).

    Returns rows of shape:
      {"feature": str,
       "User Abstract": {"present": bool, "evidence": str or None},
       "Abstract 1":    {"present": bool, "evidence": str or None}, ...}

    `evidence` for a doc cell is the document's own original label that mapped
    into the cluster for this row (useful for tooltips); `None` when the doc
    didn't contribute a label. User cells always have `evidence=None` — the
    user's wording already lives in `row["feature"]` when they contributed.
    """
    if not matches:
        return []

    doc_ids = [m["document_id"] for m in matches]
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT document_id, label, description, sort_order
            FROM document_key_features
            WHERE document_id = ANY(%s)
            ORDER BY document_id ASC, sort_order ASC, feature_id ASC
            """,
            (doc_ids,),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    kf_by_id: Dict[int, List[Dict[str, str]]] = {int(did): [] for did in doc_ids}
    for r in rows:
        did = int(r["document_id"])
        label = str(r.get("label") or "").strip()
        description = str(r.get("description") or "").strip()
        if not label:
            continue
        kf_by_id.setdefault(did, []).append({"label": label, "description": description})
    per_doc_features: List[List[Dict[str, str]]] = [kf_by_id.get(did, []) for did in doc_ids]

    # Normalize user_keywords into the dict shape. Tolerates legacy bare-string
    # rows from old comparison_history.keywords entries.
    user_features: List[Dict[str, str]] = []
    for k in (user_keywords or []):
        if isinstance(k, dict):
            label = str(k.get("label") or "").strip()
            description = str(k.get("description") or "").strip()
        else:
            label = str(k).strip()
            description = ""
        if label:
            user_features.append({"label": label, "description": description})

    doc_features = [
        [{"label": f["label"], "description": f.get("description", "")} for f in feats]
        for feats in per_doc_features
    ]

    if not user_features and not any(doc_features):
        return []

    clusters = _cluster_features_llm(user_features, doc_features)
    if clusters is None:
        clusters = _cluster_features_exact(user_features, doc_features)
    if not clusters:
        return []

    # User-containing clusters first (the LLM is told to do this too, but we
    # enforce it deterministically so the order never drifts).
    ordered = (
        [c for c in clusters if c.get("user_labels")]
        + [c for c in clusters if not c.get("user_labels")]
    )

    # Build label -> description lookups so each cell can carry the original
    # description that was fed to the LLM clusterer. The history-detail modal
    # uses this to show "user side vs doc side" feature pairs side by side.
    user_desc_by_label = {
        f.get("label", ""): f.get("description", "")
        for f in user_features if f.get("label")
    }
    doc_desc_by_label = [
        {f.get("label", ""): f.get("description", "") for f in feats if f.get("label")}
        for feats in doc_features
    ]

    matrix: List[Dict] = []
    for cluster in ordered:
        row: Dict = {"feature": cluster["canonical"]}
        user_labels_in_cluster = cluster.get("user_labels") or []
        user_label = user_labels_in_cluster[0] if user_labels_in_cluster else None
        row["User Abstract"] = {
            "present": bool(user_labels_in_cluster),
            "evidence": user_label,
            "description": user_desc_by_label.get(user_label, "") if user_label else "",
        }
        doc_labels_map = cluster.get("doc_labels") or {}
        for i in range(len(matches)):
            labels = doc_labels_map.get(i) or []
            doc_label = labels[0] if labels else None
            doc_desc = ""
            if doc_label and i < len(doc_desc_by_label):
                doc_desc = doc_desc_by_label[i].get(doc_label, "")
            row[f"Abstract {i+1}"] = {
                "present": bool(labels),
                "evidence": doc_label,
                "description": doc_desc,
            }
        matrix.append(row)
    return matrix



def cell_is_present(cell) -> bool:
    """Return True if a matrix cell indicates 'present'. Works for both the
    new dict schema {"present": bool, "evidence": ...} AND the old plain-bool
    schema stored in older cached matrices."""
    if isinstance(cell, dict):
        return bool(cell.get("present"))
    return bool(cell)


def cell_evidence(cell) -> Optional[str]:
    """Return the evidence phrase from a cell, or None. Old-schema bool cells
    have no evidence."""
    if isinstance(cell, dict):
        ev = cell.get("evidence")
        if isinstance(ev, str) and ev.strip():
            return ev
    return None


def cell_description(cell) -> Optional[str]:
    """Return the original capability description for a cell, or None.
    Older cached matrices won't have this field."""
    if isinstance(cell, dict):
        d = cell.get("description")
        if isinstance(d, str) and d.strip():
            return d
    return None



def _evaluate_feature_matrix_LEGACY_UNUSED(user_abstract: str, matches: List[Dict]) -> List[Dict]:
    """Legacy single-call implementation kept inline to reference the old
    prompt if needed. Not called anywhere."""
    if not user_abstract or not matches:
        return []

    doc_ids = [m["document_id"] for m in matches]

    # fetch abstracts
    placeholders = ",".join(["%s"] * len(doc_ids))
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""
            SELECT document_id, abstract
            FROM documents
            WHERE document_id IN ({placeholders})
            """,
            tuple(doc_ids),
        )
        docs = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    abs_by_id = {d["document_id"]: d["abstract"] for d in docs}

    # Build prompt
    prompt = (
        "You are an expert academic peer-reviewer helping a student check whether their PROPOSED research idea (the User Abstract) has already been built.\n\n"
        "IMPORTANT CONTEXT: The User Abstract is an IDEATION-PHASE proposal. The student has NOT built or evaluated anything yet. Forward-looking language about evaluation, surveys, or grading scales describes what they PLAN to do later — it is not a real feature of the system.\n\n"
        "I will provide you with a 'User Abstract' (the proposed idea) and several 'Repository Abstracts' (completed projects).\n"
        "Your task is to extract a comparative feature matrix focused on BUILDABLE SYSTEM FEATURES — the things the student would actually have to design, code, and implement.\n\n"
        "Step 1: Extract a unified master list of 5 to 10 highly specific SYSTEM features across the studies. ONLY include items from these categories:\n"
        "  - Concrete system features or user-facing modules that would be coded (e.g. 'Real-time GPS Tracking', 'QR Code Scanning', 'Location Pinning', 'Online Payment Integration', 'Push Notifications', 'Offline Mode')\n"
        "  - Specific algorithms or core tech stack that define HOW the system works (e.g. 'Random Forest Algorithm', 'Computer Vision', 'IoT Soil Sensors', 'Mobile Application', 'Web-based Application')\n"
        "  - Target domain or user scope that defines WHO/WHERE the system is for (e.g. 'Public Transit Commuters', 'Aquaculture Farmers', 'High School Students')\n\n"
        "HARD BANS — do NOT include any of the following as 'features'. They are research/evaluation boilerplate that every capstone mentions and tell you nothing about novelty:\n"
        "  - Software development lifecycles or methodologies (Agile, Scrum, Waterfall, Design Thinking, IPO model, Rapid Application Development)\n"
        "  - Software quality standards (ISO 25010, ISO 9126, McCall, etc.)\n"
        "  - Evaluation frameworks, acceptance models, or user satisfaction surveys (UTAUT, TAM, Likert scale, four-point scale, purposive sampling, 'survey data collection', 'user satisfaction evaluation', 'usability testing')\n"
        "  - Generic non-functional qualities that everyone claims (functionality, reliability, usability, efficiency, maintainability, portability)\n"
        "  - Post-hoc evaluation results (mean scores, 'Highly Acceptable', 'Strongly Agree', sample sizes)\n"
        "These items may appear in the abstracts but they are NOT features of the system — they are how the researchers wrote up or tested it. Exclude them from the matrix.\n\n"
        "Step 2: For each extracted SYSTEM feature, mark true/false for whether it is explicitly present in EACH abstract's described system.\n"
        "  - For the User Abstract: ONLY mark true if the feature is an actual part of the proposed system. If the feature only appears in the evaluation plan or the 'related work' framing, mark false.\n"
        "  - For Repository Abstracts: mark true only if the system built in that study actually implements the feature.\n\n"
        "Return EXACTLY and ONLY a JSON array of objects. Do not include markdown formatting or explanations. Use this exact structure:\n"
        "[\n"
        "  {\n"
        "    \"feature\": \"Name of the extracted feature\",\n"
        "    \"User Abstract\": true,\n"
    )
    
    for i, doc_id in enumerate(doc_ids):
        prompt += f'    "Abstract {i+1}": false'
        if i < len(doc_ids) - 1:
            prompt += ",\n"
        else:
            prompt += "\n"
        
    prompt += (
        "  }\n"
        "]\n\n---\n"
        f"USER ABSTRACT:\n{user_abstract}\n\n"
        "REPOSITORY ABSTRACTS:\n"
    )
    
    for i, doc_id in enumerate(doc_ids):
        abstract_text = abs_by_id.get(doc_id, "")
        prompt += f"Abstract {i+1} (ID: {doc_id}):\n{abstract_text}\n\n"
        
    try:
        text = get_llm_provider().generate(prompt, json_mode=True)
        # Parse JSON
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        elif isinstance(parsed, dict):
            # Sometimes models wrap arrays inside objects
            for val in parsed.values():
                if isinstance(val, list):
                    return val
        return []
    except Exception as e:
        print(f"Error generating feature matrix: {e}")
        return []


# ======================================================
# Feature Highlight (independent of feature-matrix generation)
# ======================================================

def highlight_feature_in_abstract(abstract: str, feature: str, evidence: Optional[str] = None) -> Dict:
    """
    Given an abstract and a feature label, return the abstract as HTML with
    <mark> tags around phrases that describe the feature.

    Fast path: if `evidence` is provided (pre-extracted by the feature matrix)
    AND is a real substring of `abstract`, skip the LLM entirely and just wrap
    that single phrase. This avoids a redundant LLM call and prevents the old
    bug where the matrix marked a cell TRUE but the highlighter couldn't find
    any phrase.

    Slow path (no evidence, or evidence missing/invalid): fall back to the LLM
    phrase extractor as before.
    """
    if not abstract or not feature:
        return {
            "abstract": abstract or "",
            "highlighted_html": _html.escape(abstract or ""),
            "phrases": [],
        }

    if evidence and isinstance(evidence, str) and evidence.strip() and evidence in abstract:
        return {
            "abstract": abstract,
            "highlighted_html": _build_highlighted_html(abstract, [evidence]),
            "phrases": [evidence],
        }

    prompt = (
        "You are an academic text-analysis assistant. I will give you a research "
        "abstract and a key feature/concept. Find every phrase in the abstract "
        "that mentions, describes, or directly relates to that feature.\n\n"
        f"Feature: {feature}\n\n"
        f"Abstract:\n{abstract}\n\n"
        "Return EXACTLY this JSON object and nothing else:\n"
        '{"phrases": ["exact phrase 1", "exact phrase 2"]}\n\n'
        "Rules:\n"
        "- Each phrase MUST be a verbatim substring of the abstract — preserve "
        "case, spacing, and punctuation exactly.\n"
        "- Prefer short focused phrases (3-15 words) over entire sentences.\n"
        "- Include every relevant occurrence.\n"
        '- If the feature is not mentioned at all, return {"phrases": []}.\n'
        "- Do not include commentary or markdown."
    )

    phrases: List[str] = []
    try:
        text = get_llm_provider().generate(prompt, json_mode=True)
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            raw = parsed.get("phrases", [])
            if isinstance(raw, list):
                phrases = [str(p) for p in raw if isinstance(p, str) and p.strip()]
        elif isinstance(parsed, list):
            phrases = [str(p) for p in parsed if isinstance(p, str) and p.strip()]
    except Exception as e:
        print(f"Error highlighting feature {feature!r}: {e}")
        phrases = []

    highlighted_html = _build_highlighted_html(abstract, phrases)
    return {
        "abstract": abstract,
        "highlighted_html": highlighted_html,
        "phrases": phrases,
    }


def _build_highlighted_html(abstract: str, phrases: List[str]) -> str:
    """
    HTML-escapes `abstract` and wraps every span matching any of `phrases` with
    <mark>...</mark>. Robust against overlapping / nested phrases via span merging.
    """
    if not phrases:
        return _html.escape(abstract)

    # Collect every (start, end) span where a phrase occurs in the abstract.
    spans: List[Tuple[int, int]] = []
    for phrase in phrases:
        if not phrase:
            continue
        start = 0
        while True:
            idx = abstract.find(phrase, start)
            if idx < 0:
                break
            spans.append((idx, idx + len(phrase)))
            start = idx + 1  # allow overlapping matches; merging fixes any overlap

    if not spans:
        return _html.escape(abstract)

    # Merge overlapping / adjacent spans so we never produce nested <mark> tags.
    spans.sort()
    merged: List[Tuple[int, int]] = [spans[0]]
    for s, e in spans[1:]:
        last_s, last_e = merged[-1]
        if s <= last_e:
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))

    # Stitch the output: alternating escaped plain text and escaped marked text.
    parts: List[str] = []
    cursor = 0
    for s, e in merged:
        if cursor < s:
            parts.append(_html.escape(abstract[cursor:s]))
        parts.append(f"<mark>{_html.escape(abstract[s:e])}</mark>")
        cursor = e
    if cursor < len(abstract):
        parts.append(_html.escape(abstract[cursor:]))

    return "".join(parts)


# ======================================================
# NEW: AI Gap Analysis (Objective 1)
# ======================================================

def generate_gap_analysis(user_abstract: str, repo_abstract: str) -> str:
    """
    Structured novelty comparison between the user's PROPOSED (ideation-phase)
    abstract and one COMPLETED repository abstract.

    Output format (post-processed by app.py):
        **Verdict:** <Duplicate | Substantial Overlap | Partial Overlap | Distinct>
        **Similarities:**
        - <bullet with verbatim quotes from both abstracts>
        **What Your Proposal Adds:**
        - <bullet citing verbatim quote from users abstract only>

    Design choices:
      - "Prior-art search assistant" persona (not peer reviewer) so the model
        doesn't drift toward giving recommendations.
      - Every bullet must carry verbatim quotes from the relevant abstracts,
        matching the evidence discipline used in the feature matrix.
      - "What Your Proposal Adds" replaces bidirectional "Differences" so the
        output answers the student's real question (is my idea distinct?)
        without penalising them for not mentioning features they might still
        include in the final build.
      - Hard bans on post-hoc evaluation artefacts (ratings, mean scores,
        "Highly Acceptable" phrases) are kept.
    """
    prompt = (
        "You are a prior-art search assistant helping a student check whether "
        "their PROPOSED research idea has already been built. You are NOT a "
        "peer reviewer, and you do NOT give recommendations.\n\n"
        "CONTEXT:\n"
        "- USER ABSTRACT = a research PROPOSAL in the IDEATION phase. The "
        "  student has not built anything yet. Forward-looking language about "
        "  evaluation, surveys, or grading scales describes what they PLAN to "
        "  do, not real features.\n"
        "- REPOSITORY ABSTRACT = a COMPLETED study. It may include post-hoc "
        "  evaluation artefacts (ratings, mean scores, 'Highly Acceptable', "
        "  sample sizes). These are IRRELEVANT to novelty.\n\n"
        "REPOSITORY ABSTRACT:\n"
        "----------------------------------------------------------------\n"
        f"{repo_abstract}\n"
        "----------------------------------------------------------------\n\n"
        "USER ABSTRACT:\n"
        "----------------------------------------------------------------\n"
        f"{user_abstract}\n"
        "----------------------------------------------------------------\n\n"
        "HARD EVIDENCE RULE — this is the most important rule:\n"
        "Every bullet MUST quote 3-15 words VERBATIM from the abstract(s) it "
        "refers to, wrapped in double-quotes. A quote must be a real, exact "
        "substring — preserve case, spacing, punctuation. If you cannot quote, "
        "DO NOT write the bullet. A short honest output is better than a "
        "padded speculative one.\n\n"
        "STEP 1 — Problem-focus read.\n"
        "Before comparing details, identify the CORE PROBLEM each abstract is "
        "trying to solve. This is the 'big-picture' framing the student cares "
        "about most: are the two studies even aiming at the same kind of "
        "problem?\n\n"
        "STEP 2 — Duplication judgment.\n"
        "Decide whether the USER ABSTRACT describes essentially the SAME IDEA "
        "as the REPOSITORY ABSTRACT (same problem, same proposed system, same "
        "core methodology — minor wording differences do not count). This "
        "drives the Verdict line.\n\n"
        "STEP 3 — Output. Use EXACTLY this format and nothing else:\n\n"
        "**Problem Focus:**\n"
        "- `Your abstract:` <one sentence describing the problem YOUR abstract "
        "  is solving + a 3-15 word verbatim quote in double-quotes>.\n"
        "- `Repository study:` <one sentence describing the problem the repo "
        "  abstract is solving + a 3-15 word verbatim quote in double-quotes>.\n"
        "- `Alignment:` one sentence stating whether the two studies address "
        "  the SAME problem, OVERLAPPING problems, or DIFFERENT problems, and "
        "  why. No quotes required in this bullet.\n\n"
        "**Verdict:** <one label on this line, nothing else>\n"
        "Choose ONE of the four labels below:\n"
        "- `Duplicate` — user's idea is essentially the same as the repo study.\n"
        "- `Substantial Overlap` — major pieces match (e.g. problem + approach, "
        "  or approach + methodology), with only minor distinctions.\n"
        "- `Partial Overlap` — some shared elements, but a clear distinct "
        "  contribution in the user abstract.\n"
        "- `Distinct` — different problem, approach, or methodology.\n\n"
        "**Similarities:**\n"
        "- 1-5 bullets citing shared SYSTEM FEATURES, APPROACHES, or CORE "
        "  METHODOLOGY — not shared domain or user group alone (the retrieval "
        "  step already confirmed domain overlap).\n"
        "- Format each bullet as:\n"
        '  `- <brief label>: user says "<3-15 word quote>"; repo says "<3-15 word quote>".`\n'
        "- If there are no concrete feature-level similarities, write exactly:\n"
        "  `- No specific feature-level similarities beyond shared domain.`\n\n"
        "**What Your Proposal Adds:**\n"
        "- List only CONCRETE SYSTEM FEATURES / MODULES / CAPABILITIES that "
        "  the USER ABSTRACT explicitly describes as part of the proposed "
        "  system, AND that the REPOSITORY ABSTRACT does NOT describe.\n"
        "- DO NOT list:\n"
        "  * Motivations, rationales, or justifications (e.g. why a technology "
        "    was chosen, advantages of one approach over another, background "
        "    context, problem statements, literature-review framing).\n"
        "  * Expected benefits or outcomes that aren't features themselves.\n"
        "  * Minor wording differences that describe the same capability.\n"
        "  * Anything that both abstracts actually share but one describes in "
        "    more detail than the other.\n"
        "- EXAMPLE of what NOT to include: 'QR code advantages over barcodes' "
        "  — this is rationale for the user's tech choice, not a feature the "
        "  user builds. Both abstracts use QR codes, so QR code usage is a "
        "  SIMILARITY, not something the user adds.\n"
        "- One-directional on purpose — the student is in ideation, so 'repo "
        "  has X, user doesn't mention X' is NOT useful (absence of mention != "
        "  absence of intent).\n"
        "- Format each bullet as:\n"
        '  `- <brief feature label>: user says "<3-15 word quote describing the feature>"; not described in the repository abstract.`\n'
        "- If the Verdict is `Duplicate`, OR if after applying the rules above "
        "  you find no concrete feature the user builds that the repo doesn't, "
        "  write exactly:\n"
        "  `- The user proposal does not describe any concrete system feature that the repository study does not already cover.`\n\n"
        "HARD RULES:\n"
        "- SELF-CONSISTENCY: if 'What Your Proposal Adds' contains any concrete "
        "  feature bullet, the Verdict CANNOT be `Duplicate`. Downgrade the "
        "  Verdict to `Substantial Overlap` or lower. If the Verdict is "
        "  `Duplicate`, the adds section must contain only the boilerplate "
        "  'no concrete system feature' bullet.\n"
        "- Every bullet must contain at least one verbatim quote from the "
        "  relevant abstract, wrapped in double-quotes.\n"
        "- DO NOT include evaluation results, ratings, mean scores, sample "
        "  sizes, or phrases like 'Highly Acceptable' / 'Strongly Agree' / "
        "  '4.23' anywhere in the output.\n"
        "- DO NOT speculate about methodology, population, environment, or "
        "  algorithms if they are not literally stated in the abstracts.\n"
        "- DO NOT give recommendations, suggestions, or peer-review padding.\n"
        "- DO NOT include a Summary section.\n"
        "- DO NOT output anything before **Problem Focus:** or after the last "
        "  bullet of **What Your Proposal Adds:**."
    )

    try:
        text = get_llm_provider().generate(prompt)
        return text
    except Exception as e:
        print(f"Error generating gap analysis: {e}")
        return "Error generating gap analysis. Please check your LLM provider configuration in .env."

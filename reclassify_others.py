"""Re-classify documents currently filed under research_field='Others'.

For each, send title+abstract to the LLM and ask it to choose one of the
seven named fields. Update the row in place. Anything the LLM still flags
as 'Others' is left as 'Others' with a label.
"""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from db import get_db_connection
from llm_provider import get_llm_provider

NAMED_FIELDS = (
    "Engineering & Architecture",
    "Information Technology & Computing",
    "Sciences",
    "Business & Management",
    "Education",
    "Industrial Technology",
    "Arts, Humanities & Social Sciences",
)
ALLOWED = NAMED_FIELDS + ("Others",)

PROMPT = """You are categorizing an academic study into ONE research field.

Choose exactly one of these labels:
  - Engineering & Architecture
  - Information Technology & Computing
  - Sciences            (chemistry, biology, physics, environmental, lab work)
  - Business & Management
  - Education           (schools, teaching, training programs, learning outcomes)
  - Industrial Technology
  - Arts, Humanities & Social Sciences
  - Others              (use ONLY if none of the above clearly apply)

Return strict JSON with this shape and nothing else:
  {{"field": "<one of the labels above>", "other": "<short label, only if field=Others, else empty>"}}

Title: {title}

Abstract:
{abstract}
"""


def classify(title: str, abstract: str) -> tuple[str, str]:
    prompt = PROMPT.format(title=title, abstract=abstract[:3500])
    raw = get_llm_provider().generate(prompt, json_mode=True, temperature=0.0)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return "Others", "Auto-imported"
    field = str(obj.get("field") or "").strip()
    other = str(obj.get("other") or "").strip()[:80]
    if field not in ALLOWED:
        return "Others", other or "Auto-imported"
    if field == "Others":
        return "Others", other or "Auto-imported"
    return field, ""


def main() -> int:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT document_id, title, abstract
        FROM documents
        WHERE research_field = 'Others'
        ORDER BY document_id
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    print(f"{len(rows)} documents currently in 'Others'.")
    moved = 0
    kept = 0
    for r in rows:
        did = r["document_id"]
        title = r["title"] or ""
        abstract = r["abstract"] or ""
        try:
            new_field, other = classify(title, abstract)
        except Exception as exc:
            print(f"  #{did} {title[:60]!r}  -> ERROR {exc!r}")
            continue

        if new_field == "Others":
            kept += 1
            print(f"  #{did}  KEEP Others ({other or '-'}) — {title[:70]}")
            # Still write back to make sure other label is set.
            up_conn = get_db_connection()
            up = up_conn.cursor()
            up.execute(
                "UPDATE documents SET research_field=%s, research_field_other=%s WHERE document_id=%s",
                ("Others", other or "Auto-imported", did),
            )
            up_conn.commit()
            up.close()
            up_conn.close()
            continue

        up_conn = get_db_connection()
        up = up_conn.cursor()
        try:
            up.execute(
                "UPDATE documents SET research_field=%s, research_field_other=NULL WHERE document_id=%s",
                (new_field, did),
            )
            up_conn.commit()
        finally:
            up.close()
            up_conn.close()
        moved += 1
        print(f"  #{did}  -> {new_field}  — {title[:70]}")

    print(f"\nDone. Re-classified {moved}/{len(rows)}.  {kept} stayed in Others.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

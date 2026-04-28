"""
One-shot backfill: populates `documents.key_features` via the LLM for every
document row where it's still NULL/empty. The `key_features` and
`gap_analysis` columns are already created by the Docker init SQL, so the
idempotent ALTERs here just use `ADD COLUMN IF NOT EXISTS` as a safety net
for dev machines that predate the Docker migration.
"""

import json
import time

from dotenv import load_dotenv

load_dotenv()

from db import get_db_connection  # noqa: E402 — must follow load_dotenv
from matcher import generate_unique_features  # noqa: E402


def run_backfill():
    print("Connecting to database...")
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Ensure the runtime columns exist. Postgres supports IF NOT EXISTS on
        # ADD COLUMN (9.6+), so no errno hacks needed — this is a no-op on a
        # fresh Docker volume where init SQL already created them.
        print("Ensuring key_features column on documents...")
        cursor.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS key_features TEXT")
        print("Ensuring gap_analysis column on comparison_history...")
        cursor.execute("ALTER TABLE comparison_history ADD COLUMN IF NOT EXISTS gap_analysis TEXT")
        conn.commit()

        print("\nFetching documents to process...")
        cursor.execute(
            "SELECT document_id, title, abstract FROM documents "
            "WHERE key_features IS NULL OR key_features = ''"
        )
        docs = cursor.fetchall()
        print(f"Found {len(docs)} documents needing feature extraction.")

        update_cursor = conn.cursor()
        try:
            success_count = 0
            for i, doc in enumerate(docs):
                doc_id = doc["document_id"]
                title = doc["title"]
                abstract = doc["abstract"]
                print(f"\n[{i+1}/{len(docs)}] Processing Document ID {doc_id} ('{title[:30]}...')...")

                features = generate_unique_features(abstract)
                if features:
                    features_json = json.dumps(features)
                    update_cursor.execute(
                        "UPDATE documents SET key_features = %s WHERE document_id = %s",
                        (features_json, doc_id),
                    )
                    conn.commit()
                    print(f"  -> Success! Extracted {len(features)} features: {features}")
                    success_count += 1
                else:
                    print("  -> Failed to extract features. Check your LLM provider config.")

                time.sleep(0.5)  # breather between heavy LLM inferences
        finally:
            update_cursor.close()

        print(f"\nBackfill complete! Successfully extracted features for {success_count} out of {len(docs)} documents.")
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    run_backfill()

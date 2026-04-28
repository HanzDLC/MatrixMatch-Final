"""
Retired. The MySQL-era standalone migration script has been replaced by
the Docker init SQL at `docker/postgres/init/01_schema.sql`, which creates
the final schema (including `feature_matrix`, `gap_analysis`, and
`must_change_password`) on a fresh Postgres volume.

If you ever need to re-apply those columns to an existing Postgres database
without nuking the volume, run them by hand via psql:

    ALTER TABLE comparison_history ADD COLUMN IF NOT EXISTS feature_matrix TEXT;
    ALTER TABLE comparison_history ADD COLUMN IF NOT EXISTS gap_analysis TEXT;
    ALTER TABLE users              ADD COLUMN IF NOT EXISTS must_change_password SMALLINT NOT NULL DEFAULT 0;
"""

if __name__ == "__main__":
    print(__doc__.strip())

"""
Shared DB layer for MatrixMatch.

Single place that owns the psycopg2 connection config + the `conn.cursor(dictionary=True)`
compat wrapper. Both `app.py` and `matcher.py` import `get_db_connection` from here;
keeping it in its own module avoids a circular import (app.py imports matcher, so
matcher can't import from app.py).

Configuration is read from environment variables at import time:
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
Sensible defaults match the docker-compose.yml in the project root.
"""

import os

import psycopg2
import psycopg2.extras


DB_CONFIG = {
    "host":     os.environ.get("DB_HOST", "localhost"),
    "port":     int(os.environ.get("DB_PORT", "5432")),
    "dbname":   os.environ.get("DB_NAME", "matrixmatch"),
    "user":     os.environ.get("DB_USER", "matrixmatch"),
    "password": os.environ.get("DB_PASSWORD", "matrixmatch"),
}


class _DbConn:
    """Thin wrapper around a psycopg2 connection.

    Lets every existing `conn.cursor(dictionary=True)` call site from the
    mysql-connector era keep working unchanged on Postgres — the kwarg is
    translated into psycopg2's `cursor_factory=RealDictCursor`.

    All other attribute lookups (`.commit()`, `.close()`, `.rollback()`,
    context-manager use, etc.) pass through to the real psycopg2 connection.
    """
    __slots__ = ("_real",)

    def __init__(self, real):
        self._real = real

    def cursor(self, *args, **kwargs):
        if kwargs.pop("dictionary", False):
            kwargs.setdefault("cursor_factory", psycopg2.extras.RealDictCursor)
        return self._real.cursor(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._real.commit()
        else:
            self._real.rollback()
        return False


def get_db_connection():
    """Open a new Postgres connection wrapped for dictionary-cursor compat."""
    return _DbConn(psycopg2.connect(**DB_CONFIG))

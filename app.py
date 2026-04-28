from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env (must run before importing matcher)

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    send_file,
    send_from_directory,
    jsonify,
)
import base64
import io
import matcher
import json
import os
import re
import secrets
import shutil
import time
import werkzeug.utils
from math import ceil
from typing import List, Dict, Optional
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from db import get_db_connection
# -----------------------------
# Flask app setup
# -----------------------------
app = Flask(__name__)
app.secret_key = "supersecretkey_change_me" # TODO: change in production

# Expose matrix cell helpers to Jinja so templates can treat old-bool and new
# dict cell schemas uniformly.
app.jinja_env.globals["cell_is_present"] = matcher.cell_is_present
app.jinja_env.globals["cell_evidence"] = matcher.cell_evidence
app.jinja_env.globals["cell_description"] = matcher.cell_description


def _ensure_app_settings_table():
    """
    Idempotently create the `app_settings` (key, value) table. The Docker
    init SQL already creates it on a fresh volume; this just keeps dev
    setups that predate the Docker migration safe.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    setting_key   VARCHAR(100) NOT NULL PRIMARY KEY,
                    setting_value VARCHAR(500) NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        print(f"[migrate] Skipped app_settings ensure: {e}")


_ensure_app_settings_table()


def _ensure_is_active_column():
    """
    Idempotently add the `is_active` column to the users table. The Docker
    init SQL already includes it for fresh volumes; this catches existing
    dev databases that predate the activate/deactivate feature.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active SMALLINT NOT NULL DEFAULT 1"
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        print(f"[migrate] Skipped users.is_active ensure: {e}")


_ensure_is_active_column()


def _ensure_password_reset_tokens_table():
    """
    Idempotently create the `password_reset_tokens` table used by the
    self-service forgot-password flow. The Docker init SQL creates it on
    fresh volumes; this catches existing dev databases.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS password_reset_tokens (
                    token_hash     CHAR(64) PRIMARY KEY,
                    researcher_id  INT NOT NULL REFERENCES users(researcher_id) ON DELETE CASCADE,
                    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at     TIMESTAMP NOT NULL,
                    used_at        TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_researcher "
                "ON password_reset_tokens(researcher_id)"
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        print(f"[migrate] Skipped password_reset_tokens ensure: {e}")


_ensure_password_reset_tokens_table()


def _ensure_research_field_columns():
    """
    Idempotently add `research_field` and `research_field_other` columns to
    documents. Existing rows had `academic_program` (BSIT/BSCS/BSIS), which
    are programs, not research fields — backfill them as 'Others' so the
    schema is honest until the admin re-uploads each study and picks the
    actual field. Gated on a one-shot app_settings flag.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS research_field TEXT"
            )
            cursor.execute(
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS research_field_other TEXT"
            )
            cursor.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key = %s",
                ("research_field_migrated",),
            )
            row = cursor.fetchone()
            if not row or str(row[0]).strip() != "1":
                cursor.execute(
                    "UPDATE documents SET research_field = 'Others' "
                    "WHERE research_field IS NULL OR research_field = ''"
                )
                cursor.execute(
                    """
                    INSERT INTO app_settings (setting_key, setting_value)
                    VALUES ('research_field_migrated', '1')
                    ON CONFLICT (setting_key) DO UPDATE
                        SET setting_value = EXCLUDED.setting_value
                    """
                )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        print(f"[migrate] Skipped research_field migration: {e}")


_ensure_research_field_columns()


def _ensure_source_file_path_column():
    """
    Idempotently add `source_file_path` to documents. Stores the path
    RELATIVE to the studies/ root (e.g. "Information_Technology_and_Computing/000042_thesis.pdf").
    NULL for legacy rows and for classic-upload rows that didn't go through
    the experimental flow.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_file_path TEXT"
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        print(f"[migrate] Skipped documents.source_file_path ensure: {e}")


_ensure_source_file_path_column()


def _migrate_clear_legacy_key_features():
    """
    NULL out documents.key_features for all rows on first run after the
    label+description migration. Old-shape rows (bare-label JSON arrays)
    would render as label-only in the new matrix path with empty
    descriptions — admin re-uploads each doc to repopulate properly.
    Cached comparison_history.feature_matrix rows are NOT touched, so old
    runs still display under the legacy schema. Idempotent.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key = %s",
                ("feature_descriptions_migrated",),
            )
            row = cursor.fetchone()
            if row and str(row[0]).strip() == "1":
                return  # already done
            cursor.execute(
                "UPDATE documents SET key_features = NULL WHERE key_features IS NOT NULL"
            )
            cursor.execute(
                """
                INSERT INTO app_settings (setting_key, setting_value)
                VALUES ('feature_descriptions_migrated', '1')
                ON CONFLICT (setting_key) DO UPDATE
                    SET setting_value = EXCLUDED.setting_value
                """
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        print(f"[migrate] Skipped clear-legacy-key_features: {e}")


_migrate_clear_legacy_key_features()


def _ensure_document_key_features_table():
    """
    Create normalized per-document feature rows:
      document_key_features(document_id -> documents.document_id, label, description)
    and one-time migrate legacy JSON from documents.key_features.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS document_key_features (
                    feature_id   SERIAL PRIMARY KEY,
                    document_id  INT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                    sort_order   INT NOT NULL DEFAULT 0,
                    label        VARCHAR(200) NOT NULL,
                    description  TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS ix_document_key_features_document_id "
                "ON document_key_features(document_id)"
            )
            # One-shot migration from legacy JSON column.
            cursor.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key = %s",
                ("document_key_features_migrated",),
            )
            row = cursor.fetchone()
            if not row or str(row[0]).strip() != "1":
                cursor.execute(
                    "SELECT document_id, key_features FROM documents WHERE key_features IS NOT NULL AND key_features <> ''"
                )
                legacy_rows = cursor.fetchall()
                for did, raw_kf in legacy_rows:
                    try:
                        parsed = json.loads(raw_kf) if isinstance(raw_kf, str) else raw_kf
                    except json.JSONDecodeError:
                        parsed = []
                    if not isinstance(parsed, list):
                        continue
                    for idx, item in enumerate(parsed):
                        if isinstance(item, dict):
                            label = str(item.get("label") or "").strip()
                            description = str(item.get("description") or "").strip()
                        else:
                            label = str(item).strip()
                            description = ""
                        if not label:
                            continue
                        cursor.execute(
                            """
                            INSERT INTO document_key_features (document_id, sort_order, label, description)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (did, idx, label, description),
                        )
                cursor.execute(
                    """
                    INSERT INTO app_settings (setting_key, setting_value)
                    VALUES ('document_key_features_migrated', '1')
                    ON CONFLICT (setting_key) DO UPDATE
                        SET setting_value = EXCLUDED.setting_value
                    """
                )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        print(f"[migrate] Skipped document_key_features ensure: {e}")


_ensure_document_key_features_table()


# Setting keys and defaults for admin-controlled comparison threshold.
# Researchers never write these — only the /admin/settings page does.
SETTING_COMPARISON_THRESHOLD = "comparison_threshold"
SETTING_COMPARISON_SLIDER_ENABLED = "comparison_slider_enabled"
DEFAULT_COMPARISON_THRESHOLD = 60         # percent (0–100), matches slider range
DEFAULT_COMPARISON_SLIDER_ENABLED = True  # when False, researchers can't adjust


def get_app_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Return the raw string value for a setting, or `default` if unset."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key = %s",
                (key,),
            )
            row = cursor.fetchone()
            return row[0] if row else default
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        print(f"[settings] Failed to read {key}: {e}")
        return default


def set_app_setting(key: str, value: str) -> None:
    """Upsert a setting. Callers are responsible for type serialization."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO app_settings (setting_key, setting_value)
            VALUES (%s, %s)
            ON CONFLICT (setting_key) DO UPDATE
                SET setting_value = EXCLUDED.setting_value
            """,
            (key, value),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def get_comparison_threshold_setting() -> int:
    """Return the admin-configured threshold as an int in [60, 100]."""
    raw = get_app_setting(SETTING_COMPARISON_THRESHOLD)
    if raw is None:
        return DEFAULT_COMPARISON_THRESHOLD
    try:
        val = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_COMPARISON_THRESHOLD
    return max(60, min(100, val))


def get_comparison_slider_enabled_setting() -> bool:
    """Return whether researchers may adjust the threshold slider."""
    raw = get_app_setting(SETTING_COMPARISON_SLIDER_ENABLED)
    if raw is None:
        return DEFAULT_COMPARISON_SLIDER_ENABLED
    return str(raw).strip().lower() in ("1", "true", "yes", "on")




def _parse_features_form(raw_json: str):
    """
    Parse the JSON array posted by the feature-card widget.
    Returns (parsed: list[{label, description}], error_msg: Optional[str]).
    error_msg is None on success.

    Validation:
      - Must be a JSON array.
      - At least one entry.
      - Every entry must be a dict with non-empty `label` and non-empty `description`.
    """
    if not raw_json or not raw_json.strip():
        return [], "Please add at least one key feature with a description."
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return [], "Key features payload is malformed. Please try again."
    if not isinstance(parsed, list):
        return [], "Key features payload is malformed. Please try again."
    cleaned = []
    for entry in parsed:
        if not isinstance(entry, dict):
            return [], "Each key feature must include a label and description."
        label = str(entry.get("label") or "").strip()
        description = str(entry.get("description") or "").strip()
        if not label or not description:
            return [], "Each key feature needs both a label and a description."
        cleaned.append({"label": label, "description": description})
    if not cleaned:
        return [], "Please add at least one key feature with a description."
    return cleaned, None


# Stage 2 reuses `comparison_history.keywords` (the researcher's original chip
# list) to build the feature matrix. New rows store JSON arrays of
# {label, description} dicts; legacy rows store bare-string arrays or
# comma-separated strings. Always return the dict shape so callers don't have
# to branch.
def _parse_history_keywords(raw):
    items = None
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    items = parsed
            except json.JSONDecodeError:
                pass
        if items is None:
            items = [part.strip() for part in text.split(",") if part.strip()]
    if not items:
        return []
    out = []
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


def _get_document_features_map(document_ids: List[int]) -> Dict[int, List[Dict[str, str]]]:
    """
    Fetch normalized key features for many docs in one query.
    Returns: {document_id: [{"label": str, "description": str}, ...], ...}
    """
    if not document_ids:
        return {}
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
            (document_ids,),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    out: Dict[int, List[Dict[str, str]]] = {int(did): [] for did in document_ids}
    for r in rows:
        did = int(r["document_id"])
        out.setdefault(did, []).append(
            {
                "label": str(r.get("label") or "").strip(),
                "description": str(r.get("description") or "").strip(),
            }
        )
    return out


def _replace_document_features(document_id: int, features: List[Dict[str, str]]) -> None:
    """Replace all normalized key-feature rows for one document."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM document_key_features WHERE document_id = %s", (document_id,))
        for idx, feat in enumerate(features):
            label = str(feat.get("label") or "").strip()
            description = str(feat.get("description") or "").strip()
            if not label or not description:
                continue
            cursor.execute(
                """
                INSERT INTO document_key_features (document_id, sort_order, label, description)
                VALUES (%s, %s, %s, %s)
                """,
                (document_id, idx, label, description),
            )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


# -----------------------------
# top_matches helpers (used by admin document delete + warnings)
# -----------------------------
def _parse_top_matches(top_matches_str: str) -> List[tuple]:
    """Parse 'docId|score,docId|score,...' into [(doc_id:int, score:str), ...]."""
    if not top_matches_str:
        return []
    parts = []
    for chunk in top_matches_str.split(","):
        chunk = chunk.strip()
        if not chunk or "|" not in chunk:
            continue
        try:
            did_str, score_str = chunk.split("|", 1)
            parts.append((int(did_str), score_str))
        except ValueError:
            continue
    return parts


def _serialize_top_matches(parts: List[tuple]) -> str:
    """Inverse of _parse_top_matches."""
    return ",".join(f"{did}|{score}" for did, score in parts)


def count_history_referencing_doc(doc_id: int) -> int:
    """
    Returns the number of comparison_history rows that reference the given
    document id in their top_matches string. Used to warn admins before edits
    or deletes.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT history_id, top_matches FROM comparison_history WHERE top_matches IS NOT NULL AND top_matches != ''"
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    count = 0
    for row in rows:
        for did, _ in _parse_top_matches(row["top_matches"]):
            if did == doc_id:
                count += 1
                break
    return count


def sweep_doc_from_history(doc_id: int) -> int:
    """
    Removes any reference to `doc_id` from every comparison_history row's
    top_matches string. Returns the number of rows that were modified.

    Called by the document-delete flow so old history runs don't keep dangling
    references to a document that no longer exists.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT history_id, top_matches FROM comparison_history WHERE top_matches IS NOT NULL AND top_matches != ''"
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    modified = 0
    update_conn = get_db_connection()
    update_cursor = update_conn.cursor()
    try:
        for row in rows:
            parts = _parse_top_matches(row["top_matches"])
            filtered = [(did, sc) for did, sc in parts if did != doc_id]
            if len(filtered) != len(parts):
                new_str = _serialize_top_matches(filtered)
                update_cursor.execute(
                    "UPDATE comparison_history SET top_matches = %s WHERE history_id = %s",
                    (new_str, row["history_id"]),
                )
                modified += 1
        update_conn.commit()
    finally:
        update_cursor.close()
        update_conn.close()

    return modified


# -----------------------------
# Helpers
# -----------------------------
def get_current_user():
    """Return a dict-like object for the current logged-in user (from session)."""
    if "user_id" not in session:
        return None
    return {
        "id": session.get("user_id"),
        "first_name": session.get("first_name"),
        "last_name": session.get("last_name"),
        "role": session.get("role"),
        "email": session.get("email"),
    }


def login_required(view_func):
    """Simple decorator to require login for certain routes."""
    from functools import wraps

    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        # If the user was issued a temporary password by an admin, lock them out
        # of every protected route except the change-password page itself and logout.
        if session.get("force_password_change") and view_func.__name__ not in (
            "force_change_password",
            "logout",
        ):
            flash("Please set a new password before continuing.", "warning")
            return redirect(url_for("force_change_password"))
        return view_func(*args, **kwargs)

    return wrapped


# -----------------------------
# Routes
# -----------------------------

# Landing page
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/documents/upload", methods=["GET", "POST"])
@login_required
def upload_document():
    user = get_current_user()

    # Document uploads are admin-only. Researchers who type the URL directly
    # get bounced back to their dashboard with an explanatory flash. The route
    # itself stays intact so the admin Manage Documents page can keep linking
    # to it.
    if not user or user.get("role") != "Admin":
        flash("Only admins can upload documents.", "warning")
        return redirect(url_for("dashboard"))

    if request.method == "GET":
        return render_template(
            "upload_document.html",
            user=user,
            research_fields=RESEARCH_FIELDS,
        )

    title = request.form.get("title", "").strip()
    abstract = request.form.get("abstract", "").strip()
    authors = request.form.get("authors", "").strip()
    research_field = request.form.get("research_field", "").strip()
    research_field_other = request.form.get("research_field_other", "").strip()
    key_features = request.form.get("key_features", "").strip()

    if not title or not abstract or not authors or not research_field:
        flash("Please fill in all required fields (Title, Authors, Abstract, Research Field).", "danger")
        return redirect(url_for("upload_document"))

    if research_field not in RESEARCH_FIELDS:
        flash(f"Invalid research field. Choose one of: {', '.join(RESEARCH_FIELDS)}.", "danger")
        return redirect(url_for("upload_document"))

    if research_field == "Others":
        if not research_field_other or len(research_field_other) > 80:
            flash("When 'Others' is selected, please specify the field (1-80 characters).", "danger")
            return redirect(url_for("upload_document"))
    else:
        research_field_other = ""  # ignore stray text when not Others

    parsed_features, err = _parse_features_form(key_features)
    if err:
        flash(err, "danger")
        return redirect(url_for("upload_document"))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO documents (title, abstract, research_field, research_field_other, authors)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING document_id
            """,
            (title, abstract, research_field, research_field_other or None, authors),
        )
        document_id = cursor.fetchone()[0]
        conn.commit()
        _replace_document_features(document_id, parsed_features)
        matcher.clear_doc_embedding_cache()
        flash("Study uploaded successfully!", "success")
    except Exception as e:
        flash(f"An error occurred: {e}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for("upload_document"))


# -----------------------------
# Auth: Login & Register
# -----------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    # GET -> show login form
    if request.method == "GET":
        return render_template("login.html")

    # POST -> process login
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()

    if not email or not password:
        flash("Please fill in all fields.", "danger")
        return redirect(url_for("login"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # v1 schema: single `users` table with role ENUM('Admin','Researcher').
        # Also fetch is_active so we can block deactivated accounts below.
        cursor.execute(
            """
            SELECT researcher_id, first_name, last_name, email, role,
                   must_change_password, is_active
            FROM users
            WHERE email = %s AND password = %s
            """,
            (email, password)
        )
        user = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if not user:
        flash("Invalid email or password.", "danger")
        return redirect(url_for("login"))

    # Deactivated researchers cannot log in until an admin re-enables them.
    # We keep the row (and their comparison_history) intact — it's just a gate.
    if not user.get("is_active"):
        flash("This account is deactivated. Contact an administrator.", "danger")
        return redirect(url_for("login"))

    # Save to session
    session["user_id"] = user["researcher_id"]
    session["first_name"] = user["first_name"]
    session["last_name"] = user["last_name"]
    session["role"] = user["role"]      # 'Admin' or 'Researcher'
    session["email"] = user["email"]

    # If an admin issued a temporary password, force a self-service change before
    # the user can use the rest of the app.
    if user.get("must_change_password"):
        session["force_password_change"] = True
        flash(
            "You are using a temporary password. Please choose a new password before continuing.",
            "warning",
        )
        return redirect(url_for("force_change_password"))

    flash(f"Welcome back, {user['first_name']}!", "success")
    return redirect(url_for("dashboard"))


@app.route("/register", methods=["GET", "POST"])
def register():
    # Show the registration form
    if request.method == "GET":
        return render_template("register.html")

    # Handle form submission
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()

    if not (first_name and last_name and email and password and confirm_password):
        flash("Please fill in all fields.", "danger")
        return redirect(url_for("register"))

    if password != confirm_password:
        flash("Passwords do not match.", "danger")
        return redirect(url_for("register"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Check if email already exists
        cursor.execute("SELECT researcher_id FROM users WHERE email = %s", (email,))
        existing = cursor.fetchone()
        if existing:
            flash("Email already registered.", "warning")
            return redirect(url_for("register"))

        # Insert new researcher (role = 'Researcher')
        cursor.execute(
            """
            INSERT INTO users (first_name, last_name, email, password, role)
            VALUES (%s, %s, %s, %s, 'Researcher')
            """,
            (first_name, last_name, email, password)
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    flash("Account created! You can now log in.", "success")
    return redirect(url_for("login"))


# -----------------------------
# Self-service forgot-password flow
# -----------------------------
# Two-step email-link reset:
#   1) /forgot-password — user submits email, we mint a one-time token,
#      email them a reset link.
#   2) /reset-password/<token> — user clicks the link, picks a new password.
#
# Tokens are stored hashed (sha256) so a DB leak doesn't leak live links.
# Single-use: the row's `used_at` is stamped on success and blocks replays.
PASSWORD_RESET_TTL_MINUTES = 60


def _hash_reset_token(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Email-submission form. Neutral response prevents email enumeration —
    we tell the user "if this email exists we sent a link" regardless, so an
    attacker can't probe which addresses are registered."""
    if request.method == "GET":
        return render_template("forgot_password.html")

    email = request.form.get("email", "").strip()
    neutral_message = (
        "If an account exists for that email, a reset link has been sent. "
        "Check your inbox (and spam folder) in the next minute or two."
    )

    if not email:
        flash("Please enter your email address.", "danger")
        return redirect(url_for("forgot_password"))

    # Look up the researcher. We short-circuit on any of: not found, admin,
    # deactivated — but the response is always the same neutral message.
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT researcher_id, first_name, email, role, is_active "
            "FROM users WHERE email = %s",
            (email,),
        )
        user_row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if not user_row or user_row["role"] != "Researcher" or not user_row.get("is_active"):
        flash(neutral_message, "info")
        return redirect(url_for("login"))

    # Mint the token, store only its hash, send the plaintext in the email.
    from datetime import datetime, timedelta
    token_plain = secrets.token_urlsafe(32)
    token_hash = _hash_reset_token(token_plain)
    expires_at = datetime.utcnow() + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO password_reset_tokens "
                "(token_hash, researcher_id, expires_at) VALUES (%s, %s, %s)",
                (token_hash, user_row["researcher_id"], expires_at),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        print(f"[forgot-password] Failed to store token: {e}")
        flash("Something went wrong. Please try again.", "danger")
        return redirect(url_for("forgot_password"))

    reset_url = url_for("reset_password", token=token_plain, _external=True)

    try:
        import mailer
        mailer.send_password_reset(
            to_addr=user_row["email"],
            first_name=user_row.get("first_name"),
            reset_url=reset_url,
            ttl_minutes=PASSWORD_RESET_TTL_MINUTES,
        )
    except Exception as e:
        # Token is already stored; if the email fails the user can just
        # request another. Log loudly so admins see SMTP issues.
        print(f"[forgot-password] Mail send failed for {email}: {e}")

    flash(neutral_message, "info")
    return redirect(url_for("login"))


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Validate the one-time token, then let the researcher pick a new password."""
    from datetime import datetime
    token_hash = _hash_reset_token(token)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT token_hash, researcher_id, expires_at, used_at "
            "FROM password_reset_tokens WHERE token_hash = %s",
            (token_hash,),
        )
        token_row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    # One generic error message covers invalid / expired / already-used —
    # no need to help attackers probe which case they hit.
    if (
        not token_row
        or token_row.get("used_at") is not None
        or token_row["expires_at"] < datetime.utcnow()
    ):
        flash(
            "This reset link is invalid or has expired. Request a new one.",
            "danger",
        )
        return redirect(url_for("forgot_password"))

    if request.method == "GET":
        return render_template("reset_password.html", token=token)

    # POST -> new password submission
    new_password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not new_password or not confirm_password:
        flash("Please fill in both password fields.", "danger")
        return redirect(url_for("reset_password", token=token))

    if new_password != confirm_password:
        flash("Passwords do not match.", "danger")
        return redirect(url_for("reset_password", token=token))

    if len(new_password) < 4:
        flash("Password must be at least 4 characters.", "danger")
        return redirect(url_for("reset_password", token=token))

    # Commit the new password AND burn the token in one transaction-ish pair.
    # Also clear must_change_password in case an admin had previously flagged
    # this user; the self-service flow is a fresh, chosen password.
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE users SET password = %s, must_change_password = 0 "
            "WHERE researcher_id = %s",
            (new_password, token_row["researcher_id"]),
        )
        cursor.execute(
            "UPDATE password_reset_tokens SET used_at = %s WHERE token_hash = %s",
            (datetime.utcnow(), token_hash),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    flash("Your password has been updated. You can log in now.", "success")
    return redirect(url_for("login"))


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/account/change-password", methods=["GET", "POST"])
@login_required
def force_change_password():
    """
    Forced self-service password change page.

    Reached automatically right after a user logs in with a temporary password
    issued by an admin via /admin/researchers/<id>/reset. Until the user picks
    a new password here, every other protected route bounces back to this page.
    """
    if request.method == "GET":
        return render_template("force_change_password.html", user=get_current_user())

    new_password = request.form.get("new_password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()

    if not new_password or not confirm_password:
        flash("Please fill in both password fields.", "danger")
        return redirect(url_for("force_change_password"))

    if new_password != confirm_password:
        flash("Passwords do not match.", "danger")
        return redirect(url_for("force_change_password"))

    if len(new_password) < 6:
        flash("Password must be at least 6 characters long.", "warning")
        return redirect(url_for("force_change_password"))

    user_id = session.get("user_id")
    if not user_id:
        flash("Session expired. Please log in again.", "warning")
        return redirect(url_for("login"))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE users
            SET password = %s,
                must_change_password = 0
            WHERE researcher_id = %s
            """,
            (new_password, user_id),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    # Clear the lockout flag and let the user into the rest of the app.
    session.pop("force_password_change", None)
    flash("Password updated successfully. Welcome back!", "success")
    return redirect(url_for("dashboard"))


# -----------------------------
# Dashboard routing
# -----------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    """
    Generic endpoint used in base.html via url_for('dashboard').
    Redirects to the appropriate dashboard based on role.
    """
    role = session.get("role", "")
    if role == "Admin":
        return redirect(url_for("admin_dashboard"))
    elif role == "Researcher":
        return redirect(url_for("researcher_dashboard"))
    else:
        # fallback to login if something weird happens
        session.clear()
        return redirect(url_for("login"))


@app.route("/admin/dashboard")
@login_required
def admin_dashboard():
    if session.get("role") != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Fetch admin user info
        cursor.execute(
            "SELECT researcher_id, first_name, last_name, email, role "
            "FROM users WHERE researcher_id = %s",
            (session["user_id"],)
        )
        user = cursor.fetchone()

        # ----- Stats -----
        cursor.execute(
            "SELECT COUNT(*) AS total_researchers FROM users WHERE role='Researcher'"
        )
        total_researchers = cursor.fetchone()["total_researchers"]

        cursor.execute(
            "SELECT COUNT(*) AS total_admins FROM users WHERE role='Admin'"
        )
        total_admins = cursor.fetchone()["total_admins"]

        cursor.execute(
            "SELECT COUNT(*) AS total_comparisons FROM comparison_history"
        )
        total_comparisons = cursor.fetchone()["total_comparisons"]

        stats = {
            "total_researchers": total_researchers,
            "total_admins": total_admins,
            "total_comparisons": total_comparisons
        }

        # ----- Load ALL recent comparison history with full researcher name -----
        cursor.execute(
            """
            SELECT ch.history_id,
                   CONCAT(u.first_name, ' ', u.last_name) AS researcher_name,
                   ch.academic_program_filter,
                   ch.similarity_threshold,
                   ch.created_at
            FROM comparison_history ch
            JOIN users u ON ch.researcher_id = u.researcher_id
            ORDER BY ch.created_at DESC
            LIMIT 10
            """
        )
        recent_history = cursor.fetchall()

    finally:
        cursor.close()
        conn.close()

    return render_template(
        "dashboard_admin.html",
        user=user,
        stats=stats,
        recent_history=recent_history
    )



@app.route("/researcher/dashboard")
@login_required
def researcher_dashboard():
    """
    Researcher dashboard showing their own recent comparison history.
    """
    user = get_current_user()
    if not user or user["role"] != "Researcher":
        flash("Researcher access only.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # ✅ Get recent comparison history for this researcher
        cursor.execute(
            """
            SELECT history_id,
                   keywords,
                   academic_program_filter,
                   similarity_threshold,
                   created_at
            FROM comparison_history
            WHERE researcher_id = %s
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (user["id"],)
        )
        recent_history = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    return render_template(
        "dashboard_researcher.html",
        user=user,
        recent_history=recent_history
    )


# -----------------------------
# Comparison (Stage 1 + 2 placeholder)
# -----------------------------
# Minimum abstract length (characters) required at form submission. Stops users
# from submitting a few-word stub that gives SBERT and the LLM nothing to work
# with. Mirrored client-side via `minlength` on the textarea.
MIN_ABSTRACT_LENGTH = 150


@app.route("/comparison/new", methods=["GET", "POST"])
@login_required
def comparison_new():
    """
    Stage 1 entry point. Logged-in researchers only — admins are redirected
    back to the admin dashboard, guests are bounced to the login screen by
    the @login_required decorator.
    """
    user = get_current_user()

    # Admins are blocked from creating comparisons via the UI. The backend
    # functionality (matcher.run_stage1) is unchanged and still callable from
    # other code paths if anything else ever needs it.
    if user and user.get("role") == "Admin":
        flash("Admins cannot create comparisons. Use the Manage Documents page instead.", "warning")
        return redirect(url_for("admin_dashboard"))

    admin_threshold = get_comparison_threshold_setting()
    slider_enabled = get_comparison_slider_enabled_setting()

    if request.method == "GET":
        return render_template(
            "comparison_new.html",
            user=user,
            min_abstract_length=MIN_ABSTRACT_LENGTH,
            admin_threshold=admin_threshold,
            slider_enabled=slider_enabled,
            research_fields=RESEARCH_FIELDS,
        )

    # -----------------------------
    # POST – form submission
    # -----------------------------
    user_abstract = request.form.get("abstract", "").strip()
    research_field_filter = request.form.get("research_field_filter", "ALL").strip() or "ALL"
    threshold_str = request.form.get("threshold", str(admin_threshold)).strip()

    # Abstract is the only free-text mandatory field. Key features are
    # validated separately below.
    if not user_abstract:
        flash("Please describe your concept.", "danger")
        return redirect(url_for("comparison_new"))

    if len(user_abstract) < MIN_ABSTRACT_LENGTH:
        flash(
            f"Your concept is too short. Please provide at least {MIN_ABSTRACT_LENGTH} characters "
            f"(you have {len(user_abstract)}).",
            "danger",
        )
        return redirect(url_for("comparison_new"))

    # -----------------------------
    # Parse threshold (percent -> 0–1)
    #
    # When the admin has disabled the slider, ignore whatever the client
    # posted and force the standardized admin value. This closes the loophole
    # of a scripted client POST bypassing the disabled UI.
    # -----------------------------
    if slider_enabled:
        try:
            threshold_pct = float(threshold_str)
        except ValueError:
            threshold_pct = float(admin_threshold)
    else:
        threshold_pct = float(admin_threshold)
    similarity_threshold = threshold_pct / 100.0

    # Validate research-field filter (must be ALL or one of the known fields)
    if research_field_filter != "ALL" and research_field_filter not in RESEARCH_FIELDS:
        research_field_filter = "ALL"

    # -----------------------------
    # Key features — required. Received as a JSON array string of
    # {label, description} objects from the feature-card widget.
    # -----------------------------
    raw_key_features = request.form.get("key_features", "").strip()
    parsed_features, err = _parse_features_form(raw_key_features)
    if err:
        flash(err, "danger")
        return redirect(url_for("comparison_new"))

    # matcher.run_stage1 is responsible for:
    # - computing similarities
    # - saving to comparison_history
    # - returning (history_id, matches)
    history_id, matches = matcher.run_stage1(
        researcher_id=user["id"],
        keywords=parsed_features,
        user_abstract=user_abstract,
        research_field_filter=research_field_filter,
        similarity_threshold=similarity_threshold,
    )

    if history_id is None:
        flash("No documents found for the selected research field.", "warning")
        return redirect(url_for("comparison_new"))

    # Redirect to history detail for this run (Stage 1 results)
    flash("Stage 1 comparison completed.", "success")
    return redirect(url_for("history_detail", history_id=history_id))


# -----------------------------
# History (list + detail placeholders)
# -----------------------------
@app.route("/history")
@login_required
def history():
    """
    History list view.

    For Researcher:
    - This page shows ONLY the currently logged-in user's own history
      (comparison_history rows where researcher_id = session user_id).

    For Admin:
    - This route is blocked. Admins don't run comparisons themselves and
      already see everyone's runs on the admin dashboard, plus per-researcher
      history via /admin/researchers/<id>/history.
    """
    user = get_current_user()

    if user and user.get("role") == "Admin":
        flash("Admins view comparison history from the admin dashboard or per-researcher pages.", "warning")
        return redirect(url_for("admin_dashboard"))


    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                history_id,
                keywords,
                academic_program_filter,
                similarity_threshold,
                created_at
            FROM comparison_history
            WHERE researcher_id = %s
            ORDER BY created_at DESC
            """,
            (user["id"],)
        )
        history_rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    return render_template(
        "history.html",
        user=user,
        history_rows=history_rows
    )


@app.route("/history/<int:history_id>/delete", methods=["POST"])
@login_required
def history_delete(history_id):
    """
    Researcher-initiated delete of one of their own comparison_history rows.
    Admins are blocked from /history and should not hit this route.
    """
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    if user.get("role") == "Admin":
        flash("Admins cannot delete comparisons from the researcher view.", "warning")
        return redirect(url_for("admin_dashboard"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT researcher_id FROM comparison_history WHERE history_id = %s",
            (history_id,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if not row:
        flash("Comparison not found.", "danger")
        return redirect(url_for("history"))
    if row["researcher_id"] != user["id"]:
        flash("You can only delete your own comparisons.", "danger")
        return redirect(url_for("history"))

    del_conn = get_db_connection()
    del_cursor = del_conn.cursor()
    try:
        del_cursor.execute(
            "DELETE FROM comparison_history WHERE history_id = %s",
            (history_id,),
        )
        del_conn.commit()
    finally:
        del_cursor.close()
        del_conn.close()

    flash(f"Comparison #{history_id} deleted.", "success")
    return redirect(url_for("history"))


def bucket_feature_matrix(feature_matrix, top_matches):
    """
    Group feature_matrix rows into novelty buckets for the Stage 2 summary card.

    Returns a dict with four lists:
      - novel:         user has it, NO repo has it.
      - partial:       user has it, SOME (but not all) repos have it.
      - already_built: user has it, ALL repos have it.
      - built_without: user does NOT have it, at least one repo does.

    Each item in a list is {"feature": str, "matched": [ {document_id, title}, ... ]}.
    `matched` is empty for the `novel` bucket and omitted for `already_built` UI purposes.
    """
    buckets = {"novel": [], "partial": [], "already_built": [], "built_without": []}
    if not feature_matrix or not top_matches:
        return buckets

    n_repos = len(top_matches)

    for row in feature_matrix:
        feature_name = row.get("feature") or ""
        if not feature_name:
            continue

        user_has = matcher.cell_is_present(row.get("User Abstract"))
        matched = []
        for i, m in enumerate(top_matches):
            key = f"Abstract {i + 1}"
            if matcher.cell_is_present(row.get(key)):
                matched.append(
                    {"document_id": m.get("document_id"), "title": m.get("title")}
                )

        entry = {"feature": feature_name, "matched": matched}

        if user_has:
            if not matched:
                buckets["novel"].append(entry)
            elif len(matched) < n_repos:
                buckets["partial"].append(entry)
            else:
                buckets["already_built"].append(entry)
        else:
            if matched:
                buckets["built_without"].append(entry)

    return buckets


@app.route("/history/<int:history_id>")
@login_required
def history_detail(history_id):
    """
    Single history detail page (Stage 1 list + Stage 2 heatmap).
    - Reloads keywords + Stage 1 matches from DB via matcher.get_history_with_matches().
    - Runs Stage 2 (matrix + heatmap) immediately when this page is loaded.
    - Embeds the heatmap as a base64 <img> in the template.
    """
    user = get_current_user()

    # Load history row + Stage 1 matches from DB
    history, matches = matcher.get_history_with_matches(history_id)
    if not history:
        flash("History entry not found.", "warning")
        return redirect(url_for("history"))

    # Permission: researchers can only see their own; admins can see all
    if user["role"] == "Researcher" and history["researcher_id"] != user["id"]:
        flash("You are not allowed to view that history entry.", "danger")
        return redirect(url_for("history"))

    keywords = _parse_history_keywords(history.get("keywords"))

    # -----------------------------
    # Stage 2: LLM Feature Matrix — LAZY LOAD
    # If a cached matrix exists in the DB row, use it immediately (instant render).
    # Otherwise, render the page with an empty matrix and let the browser fetch
    # it asynchronously via /api/history/<id>/feature_matrix after page load.
    # This prevents the 20-40s synchronous LLM wait on first view.
    # -----------------------------
    feature_matrix = []
    feature_matrix_pending = False
    top_matches = matches[:5]
    if matches:
        if "feature_matrix" in history and history["feature_matrix"]:
            try:
                feature_matrix = json.loads(history["feature_matrix"])
            except json.JSONDecodeError:
                feature_matrix = []
                feature_matrix_pending = True
        else:
            feature_matrix_pending = True

    # The "Key Features Detected in Your Study" card iterates this list — it's
    # the subset of feature-matrix rows where the User Abstract column is True
    # (i.e. unified features that actually apply to the researcher's study).
    user_key_features = [
        row.get("feature", "")
        for row in (feature_matrix or [])
        if matcher.cell_is_present(row.get("User Abstract")) and row.get("feature")
    ]

    # -----------------------------
    # Pagination of the matrix's visible columns
    # Slices the SAME `top_matches` list — does not recompute anything.
    # -----------------------------
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        limit = max(1, int(request.args.get("limit", 3)))
    except ValueError:
        limit = 3

    total_top = len(top_matches)
    total_pages = max(1, ceil(total_top / limit)) if total_top else 1
    if page > total_pages:
        page = total_pages

    start = (page - 1) * limit
    end = start + limit
    paginated_top = top_matches[start:end]
    # Absolute index of each visible column inside top_matches, used to look up
    # the matching "Abstract N" key in feature_matrix rows.
    paginated_indices = list(range(start, start + len(paginated_top)))

    novelty_buckets = bucket_feature_matrix(feature_matrix, top_matches)

    return render_template(
        "history_detail.html",
        user=user,
        history=history,
        matches=matches,
        keywords=keywords,
        feature_matrix=feature_matrix,
        feature_matrix_pending=feature_matrix_pending,
        user_key_features=user_key_features,
        novelty_buckets=novelty_buckets,
        top_matches=top_matches,
        paginated_top=paginated_top,
        paginated_indices=paginated_indices,
        page=page,
        total_pages=total_pages,
        total_top=total_top,
        limit=limit,
    )


@app.route("/api/history/<int:history_id>/feature_matrix")
@login_required
def api_history_feature_matrix(history_id):
    """
    Lazy-load endpoint for the Stage 2 feature matrix.

    Returns the cached matrix immediately if present, otherwise runs the
    deterministic Stage 2 pass (SBERT-clustered key_features) and caches the
    result to comparison_history.feature_matrix before returning.
    """
    user = get_current_user()
    if not user:
        return jsonify({"error": "auth required"}), 401

    history, matches = matcher.get_history_with_matches(history_id)
    if not history:
        return jsonify({"error": "history not found"}), 404
    if user["role"] != "Admin" and history["researcher_id"] != user["id"]:
        return jsonify({"error": "forbidden"}), 403

    if history.get("feature_matrix"):
        try:
            return jsonify({"feature_matrix": json.loads(history["feature_matrix"])})
        except json.JSONDecodeError:
            pass

    if not matches:
        return jsonify({"feature_matrix": []})

    user_keywords = _parse_history_keywords(history.get("keywords"))
    feature_matrix = matcher.evaluate_feature_matrix(
        user_keywords,
        matches,
        user_abstract=history.get("user_abstract") or "",
    )

    if feature_matrix:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE comparison_history SET feature_matrix = %s WHERE history_id = %s",
                (json.dumps(feature_matrix), history_id),
            )
            conn.commit()
        except Exception as e:
            print(f"Error caching feature matrix: {e}")
        finally:
            cursor.close()
            conn.close()

    return jsonify({"feature_matrix": feature_matrix})


@app.route("/history/<int:history_id>/recalculate", methods=["POST"])
@login_required
def history_recalculate(history_id):
    """
    Re-runs Stage 1 (MPNet + cosine) for an existing history entry against the
    CURRENT state of the documents repository, then updates the row in place.
    Useful after the embedding model is upgraded or new documents are added.
    """
    user = get_current_user()

    history, _ = matcher.get_history_with_matches(history_id)
    if not history:
        flash("History entry not found.", "warning")
        return redirect(url_for("history"))

    # Permission: researchers can only recalculate their own runs; admins can recalculate any
    if user["role"] == "Researcher" and history["researcher_id"] != user["id"]:
        flash("You are not allowed to recalculate that history entry.", "danger")
        return redirect(url_for("history"))

    try:
        match_count, _ = matcher.recalculate_history(history_id)
        flash(
            f"Recalculated similarity scores. Found {match_count} matching document"
            f"{'' if match_count == 1 else 's'}.",
            "success",
        )
    except Exception as e:
        print(f"Error recalculating history #{history_id}: {e}")
        flash("Failed to recalculate similarity scores. Check the server logs.", "danger")

    return redirect(url_for("history_detail", history_id=history_id))


# -----------------------------
# Manage Documents (Admin-only)
# -----------------------------
@app.route("/admin/documents")
@login_required
def manage_documents():
    """
    Admin view of every uploaded document in the repository, with edit/delete actions.
    For each row we also surface how many comparison_history rows reference it so admins
    know the blast radius before they edit the abstract or hard-delete.
    """
    user = get_current_user()
    if not user or user["role"] != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = max(5, min(100, int(request.args.get("per_page", 25))))
    except (TypeError, ValueError):
        per_page = 25
    q = (request.args.get("q") or "").strip()
    research_field_filter = (request.args.get("field") or "").strip()

    where_clauses = []
    params: list = []
    if q:
        like = f"%{q}%"
        where_clauses.append("(title ILIKE %s OR authors ILIKE %s OR abstract ILIKE %s)")
        params.extend([like, like, like])
    if research_field_filter and research_field_filter in RESEARCH_FIELDS:
        where_clauses.append("research_field = %s")
        params.append(research_field_filter)
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(f"SELECT COUNT(*) AS c FROM documents{where_sql}", params)
        total_documents = cursor.fetchone()["c"]
        offset = (page - 1) * per_page
        cursor.execute(
            f"""
            SELECT document_id, title, abstract, research_field, research_field_other,
                   authors, source_file_path
            FROM documents
            {where_sql}
            ORDER BY document_id DESC
            LIMIT %s OFFSET %s
            """,
            params + [per_page, offset],
        )
        documents = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    total_pages = max(1, (total_documents + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages

    # Build a single map { doc_id: reference_count } in one pass over comparison_history
    # so we don't fire one COUNT query per document.
    history_conn = get_db_connection()
    history_cursor = history_conn.cursor(dictionary=True)
    try:
        history_cursor.execute(
            "SELECT top_matches FROM comparison_history WHERE top_matches IS NOT NULL AND top_matches != ''"
        )
        history_rows = history_cursor.fetchall()
    finally:
        history_cursor.close()
        history_conn.close()

    reference_counts: Dict[int, int] = {}
    for hr in history_rows:
        seen_in_row = set()
        for did, _ in _parse_top_matches(hr["top_matches"]):
            if did in seen_in_row:
                continue
            seen_in_row.add(did)
            reference_counts[did] = reference_counts.get(did, 0) + 1

    features_map = _get_document_features_map([d["document_id"] for d in documents])

    for doc in documents:
        doc["reference_count"] = reference_counts.get(doc["document_id"], 0)
        # Truncate the abstract preview so the table stays readable
        abstract = doc.get("abstract") or ""
        doc["abstract_preview"] = (abstract[:140] + "…") if len(abstract) > 140 else abstract
        doc["research_field_display"] = _format_research_field_for_display(
            doc.get("research_field"), doc.get("research_field_other")
        )
        parsed_kf = features_map.get(doc["document_id"], [])
        doc["parsed_key_features"] = parsed_kf
        doc["needs_reupload"] = not bool(parsed_kf)

    return render_template(
        "manage_documents.html",
        user=user,
        documents=documents,
        total_documents=total_documents,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        q=q,
        research_field_filter=research_field_filter,
        research_fields=RESEARCH_FIELDS,
    )


# Allowed research fields — keep in sync with the upload form dropdowns.
# Order matters: this is the order the dropdown renders.
RESEARCH_FIELDS = (
    "Engineering & Architecture",
    "Information Technology & Computing",
    "Sciences",
    "Business & Management",
    "Education",
    "Industrial Technology",
    "Arts, Humanities & Social Sciences",
    "Others",
)


def _research_field_slug(field: str) -> str:
    """Slugify a research-field label for safe use as a folder name."""
    return re.sub(r"[^A-Za-z0-9]+", "_", str(field or "")).strip("_") or "Unknown"


def _format_research_field_for_display(field: Optional[str], other: Optional[str]) -> str:
    """Render the research field for the manage_documents table cell, etc."""
    if not field:
        return "—"
    if field == "Others" and other and str(other).strip():
        return f"Others ({str(other).strip()})"
    return field


@app.route("/admin/documents/<int:document_id>/edit", methods=["GET", "POST"])
@login_required
def admin_edit_document(document_id):
    """
    Edit a single document. All fields are editable, including the abstract.
    Surfaces a warning showing how many comparison_history runs reference this
    document so admins know that editing the abstract leaves those runs stale.
    """
    user = get_current_user()
    if not user or user["role"] != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT document_id, title, abstract, research_field, research_field_other,
                   authors, source_file_path
            FROM documents
            WHERE document_id = %s
            """,
            (document_id,),
        )
        document = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if not document:
        flash("Document not found.", "danger")
        return redirect(url_for("manage_documents"))

    reference_count = count_history_referencing_doc(document_id)

    if request.method == "GET":
        initial_features = _get_document_features_map([document_id]).get(document_id, [])
        document["key_features"] = json.dumps(initial_features, ensure_ascii=False)
        return render_template(
            "admin_edit_document.html",
            user=user,
            document=document,
            reference_count=reference_count,
            research_fields=RESEARCH_FIELDS,
        )

    # POST -> validate and persist
    title = request.form.get("title", "").strip()
    abstract = request.form.get("abstract", "").strip()
    authors = request.form.get("authors", "").strip()
    research_field = request.form.get("research_field", "").strip()
    research_field_other = request.form.get("research_field_other", "").strip()
    key_features = request.form.get("key_features", "").strip()

    if not title or not abstract or not research_field:
        flash("Title, Abstract, and Research Field are required.", "danger")
        return redirect(url_for("admin_edit_document", document_id=document_id))

    if research_field not in RESEARCH_FIELDS:
        flash(f"Invalid research field. Choose one of: {', '.join(RESEARCH_FIELDS)}.", "danger")
        return redirect(url_for("admin_edit_document", document_id=document_id))

    if research_field == "Others":
        if not research_field_other or len(research_field_other) > 80:
            flash("When 'Others' is selected, please specify the field (1-80 characters).", "danger")
            return redirect(url_for("admin_edit_document", document_id=document_id))
    else:
        research_field_other = ""

    parsed_features, err = _parse_features_form(key_features)
    if err:
        flash(err, "danger")
        return redirect(url_for("admin_edit_document", document_id=document_id))

    update_conn = get_db_connection()
    update_cursor = update_conn.cursor()
    try:
        update_cursor.execute(
            """
            UPDATE documents
            SET title = %s,
                abstract = %s,
                authors = %s,
                research_field = %s,
                research_field_other = %s
            WHERE document_id = %s
            """,
            (title, abstract, authors, research_field, research_field_other or None, document_id),
        )
        update_conn.commit()
    finally:
        update_cursor.close()
        update_conn.close()

    _replace_document_features(document_id, parsed_features)
    matcher.invalidate_doc_embedding(document_id)
    flash(f"Document #{document_id} updated.", "success")
    return redirect(url_for("manage_documents"))


@app.route("/admin/documents/<int:document_id>/delete", methods=["POST"])
@login_required
def admin_delete_document(document_id):
    """
    Hard-delete a document AND sweep its id out of every comparison_history
    row's top_matches string so we don't leave dangling references.
    """
    user = get_current_user()
    if not user or user["role"] != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT document_id, title FROM documents WHERE document_id = %s",
            (document_id,),
        )
        document = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if not document:
        flash("Document not found.", "danger")
        return redirect(url_for("manage_documents"))

    # 1) Sweep top_matches strings BEFORE deleting so admins see an honest count.
    swept = sweep_doc_from_history(document_id)

    # 2) Hard-delete the document
    del_conn = get_db_connection()
    del_cursor = del_conn.cursor()
    try:
        del_cursor.execute(
            "DELETE FROM documents WHERE document_id = %s",
            (document_id,),
        )
        del_conn.commit()
    finally:
        del_cursor.close()
        del_conn.close()

    matcher.invalidate_doc_embedding(document_id)

    if swept:
        flash(
            f'Deleted "{document["title"]}" and removed it from {swept} '
            f'past comparison run{"" if swept == 1 else "s"}.',
            "success",
        )
    else:
        flash(f'Deleted "{document["title"]}".', "success")

    return redirect(url_for("manage_documents"))


# -----------------------------
# Experimental: LLM-assisted Upload (Admin-only)
# -----------------------------
# Two-step flow:
#   1) POST the uploaded PDF/DOCX → study_extractor runs the LLM and returns
#      {title, authors, abstract, key_features, warnings}. The admin lands on
#      a review form prefilled with those fields.
#   2) The review form POSTs to /admin/documents/upload-experimental/save,
#      which validates and inserts into `documents` using the same columns
#      as the classic upload flow.
# Deliberately kept separate from upload_document() so the experimental
# feature can be tuned / disabled without risk to the existing admin flow.
# Studies storage layout:
#   studies/_staging/{token}.{ext}    — temp file held between extract and save
#   studies/{field_slug}/{doc_id:06d}_{name}.{ext}  — permanent, organized by research field
_STUDIES_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "studies")
_STAGING_DIR = os.path.join(_STUDIES_ROOT, "_staging")
_STAGING_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,32}$")


def _ensure_studies_dirs():
    os.makedirs(_STAGING_DIR, exist_ok=True)


_ensure_studies_dirs()


def _safe_studies_path(*parts) -> str:
    """Join paths under studies/, resolving symlinks, and reject any escape
    attempts. Returns the resolved absolute path or raises ValueError."""
    candidate = os.path.realpath(os.path.join(_STUDIES_ROOT, *parts))
    root = os.path.realpath(_STUDIES_ROOT)
    if not (candidate == root or candidate.startswith(root + os.sep)):
        raise ValueError("Path escapes studies/ root")
    return candidate


@app.route("/admin/documents/upload-experimental", methods=["GET", "POST"])
@login_required
def upload_document_experimental():
    user = get_current_user()
    if not user or user.get("role") != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "GET":
        return render_template(
            "upload_document_experimental.html",
            user=user,
            stage="upload",
            research_fields=RESEARCH_FIELDS,
        )

    # POST -> save uploaded bytes to staging, run extractor on the staged
    # file, then render the review form. The staging file lives until either
    # the save handler moves it into place or the cancel endpoint deletes it.
    uploaded = request.files.get("study_file")
    if not uploaded or not uploaded.filename:
        flash("Please choose a .pdf or .docx file to upload.", "danger")
        return redirect(url_for("upload_document_experimental"))

    lower = uploaded.filename.lower()
    if lower.endswith(".pdf"):
        ext = ".pdf"
    elif lower.endswith(".docx"):
        ext = ".docx"
    else:
        flash("Unsupported file type. Upload a .pdf or .docx.", "danger")
        return redirect(url_for("upload_document_experimental"))

    staging_token = secrets.token_urlsafe(16)
    staging_path = _safe_studies_path("_staging", f"{staging_token}{ext}")
    try:
        uploaded.save(staging_path)
    except Exception as e:
        flash(f"Could not save uploaded file: {e}", "danger")
        return redirect(url_for("upload_document_experimental"))

    import study_extractor
    extracted = study_extractor.extract_study(staging_path)

    return render_template(
        "upload_document_experimental.html",
        user=user,
        stage="review",
        research_fields=RESEARCH_FIELDS,
        extracted=extracted,
        source_filename=uploaded.filename,
        staging_token=staging_token,
        staging_ext=ext,
    )


@app.route("/admin/documents/upload-experimental/save", methods=["POST"])
@login_required
def upload_document_experimental_save():
    user = get_current_user()
    if not user or user.get("role") != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    title = request.form.get("title", "").strip()
    abstract = request.form.get("abstract", "").strip()
    authors = request.form.get("authors", "").strip()
    research_field = request.form.get("research_field", "").strip()
    research_field_other = request.form.get("research_field_other", "").strip()
    key_features = request.form.get("key_features", "").strip()
    staging_token = request.form.get("staging_token", "").strip()
    staging_ext = request.form.get("staging_ext", "").strip().lower()
    original_filename = request.form.get("original_filename", "").strip()

    if not title or not abstract or not authors or not research_field:
        flash("Title, Authors, Abstract, and Research Field are required.", "danger")
        return redirect(url_for("upload_document_experimental"))

    if research_field not in RESEARCH_FIELDS:
        flash(
            f"Invalid research field. Choose one of: {', '.join(RESEARCH_FIELDS)}.",
            "danger",
        )
        return redirect(url_for("upload_document_experimental"))

    if research_field == "Others":
        if not research_field_other or len(research_field_other) > 80:
            flash("When 'Others' is selected, please specify the field (1-80 characters).", "danger")
            return redirect(url_for("upload_document_experimental"))
    else:
        research_field_other = ""

    parsed_features, err = _parse_features_form(key_features)
    if err:
        flash(err, "danger")
        return redirect(url_for("upload_document_experimental"))

    if staging_ext not in (".pdf", ".docx"):
        flash("Source file metadata missing — please re-upload the file.", "danger")
        return redirect(url_for("upload_document_experimental"))
    if not _STAGING_TOKEN_RE.match(staging_token):
        flash("Source file token is invalid — please re-upload the file.", "danger")
        return redirect(url_for("upload_document_experimental"))

    try:
        staging_path = _safe_studies_path("_staging", f"{staging_token}{staging_ext}")
    except ValueError:
        flash("Source file path is invalid — please re-upload the file.", "danger")
        return redirect(url_for("upload_document_experimental"))

    if not os.path.isfile(staging_path):
        flash("Staging file not found — please re-upload the file.", "danger")
        return redirect(url_for("upload_document_experimental"))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO documents (title, abstract, research_field, research_field_other, authors)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING document_id
            """,
            (title, abstract, research_field, research_field_other or None, authors),
        )
        new_id = cursor.fetchone()[0]
        conn.commit()
    except Exception as e:
        conn.rollback()
        flash(f"Insert failed: {e}", "danger")
        return redirect(url_for("upload_document_experimental"))
    finally:
        cursor.close()
        conn.close()

    _replace_document_features(new_id, parsed_features)

    # Move staging file to permanent location. DB row already exists; if the
    # move fails we surface a warning but keep the row — metadata is the more
    # important record.
    field_slug = _research_field_slug(research_field)
    base_name = werkzeug.utils.secure_filename(original_filename) or f"study{staging_ext}"
    if len(base_name) > 80:
        # Preserve extension while clipping the body.
        stem, dot, ext_part = base_name.rpartition(".")
        stem = (stem or base_name)[:75]
        base_name = f"{stem}.{ext_part}" if dot else stem
    permanent_name = f"{new_id:06d}_{base_name}"
    try:
        permanent_dir = _safe_studies_path(field_slug)
        os.makedirs(permanent_dir, exist_ok=True)
        permanent_path = _safe_studies_path(field_slug, permanent_name)
        shutil.move(staging_path, permanent_path)
        rel_path = f"{field_slug}/{permanent_name}"
        update_conn = get_db_connection()
        update_cursor = update_conn.cursor()
        try:
            update_cursor.execute(
                "UPDATE documents SET source_file_path = %s WHERE document_id = %s",
                (rel_path, new_id),
            )
            update_conn.commit()
        finally:
            update_cursor.close()
            update_conn.close()
        matcher.clear_doc_embedding_cache()
        flash(f'Study "{title}" uploaded via experimental extractor.', "success")
    except Exception as e:
        matcher.clear_doc_embedding_cache()
        flash(
            f'Study "{title}" saved, but archiving the source file failed: {e}',
            "warning",
        )

    return redirect(url_for("manage_documents"))


@app.route("/admin/documents/upload-experimental/cancel", methods=["POST"])
@login_required
def upload_document_experimental_cancel():
    """Discard a staging file and return to the upload page."""
    user = get_current_user()
    if not user or user.get("role") != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    staging_token = request.form.get("staging_token", "").strip()
    staging_ext = request.form.get("staging_ext", "").strip().lower()
    if _STAGING_TOKEN_RE.match(staging_token) and staging_ext in (".pdf", ".docx"):
        try:
            path = _safe_studies_path("_staging", f"{staging_token}{staging_ext}")
            if os.path.isfile(path):
                os.remove(path)
        except (ValueError, OSError):
            pass  # ignore cleanup failures; admin can sweep later
    return redirect(url_for("upload_document_experimental"))


@app.route("/admin/documents/<int:document_id>/source-file")
@login_required
def admin_download_source_file(document_id):
    """Stream the archived original PDF/DOCX for a document. Admin-only."""
    user = get_current_user()
    if not user or user.get("role") != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT source_file_path FROM documents WHERE document_id = %s",
            (document_id,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    rel = (row or {}).get("source_file_path")
    if not rel:
        flash("This document has no archived source file.", "warning")
        return redirect(url_for("manage_documents"))

    try:
        full_path = _safe_studies_path(rel)
    except ValueError:
        flash("Source file path is invalid.", "danger")
        return redirect(url_for("manage_documents"))

    if not os.path.isfile(full_path):
        flash("Source file is missing on disk.", "warning")
        return redirect(url_for("manage_documents"))

    return send_from_directory(
        os.path.dirname(full_path),
        os.path.basename(full_path),
        as_attachment=True,
    )


@app.route("/documents/<int:document_id>/source-file")
@login_required
def view_source_file(document_id):
    """
    Serve the archived original PDF/DOCX for a repository document. Available
    to any logged-in user (researchers + admins) so researchers can open the
    full paper from a Stage 1 match. Streams inline by default so the browser
    can render PDFs in an <iframe>; pass ?download=1 to force the download
    dialog. Researchers without an existing comparison referencing this doc
    are still permitted — repository papers are public to logged-in users.
    """
    user = get_current_user()
    if not user:
        flash("Please log in.", "warning")
        return redirect(url_for("login"))

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT title, source_file_path FROM documents WHERE document_id = %s",
            (document_id,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not row:
        flash("Document not found.", "warning")
        return redirect(url_for("dashboard"))

    rel = row.get("source_file_path") or ""
    if not rel:
        flash("This document has no archived source file.", "warning")
        return redirect(url_for("dashboard"))

    try:
        full_path = _safe_studies_path(rel)
    except ValueError:
        flash("Source file path is invalid.", "danger")
        return redirect(url_for("dashboard"))

    if not os.path.isfile(full_path):
        flash("Source file is missing on disk.", "warning")
        return redirect(url_for("dashboard"))

    as_attachment = request.args.get("download", "").strip() in ("1", "true", "yes")
    return send_from_directory(
        os.path.dirname(full_path),
        os.path.basename(full_path),
        as_attachment=as_attachment,
    )


@app.route("/admin/documents/staging/clean", methods=["POST"])
@login_required
def admin_clean_staging():
    """Sweep staging files older than 24 hours."""
    user = get_current_user()
    if not user or user.get("role") != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    cutoff = time.time() - 24 * 3600
    removed = 0
    try:
        for name in os.listdir(_STAGING_DIR):
            path = os.path.join(_STAGING_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    except FileNotFoundError:
        pass
    flash(f"Cleaned up {removed} stale staging file(s).", "success")
    return redirect(url_for("manage_documents"))


# -----------------------------
# Admin Settings (Admin-only)
# -----------------------------
# Admin-configurable knobs that affect what researchers see on the comparison
# form. Currently: the standardized minimum-similarity threshold and whether
# researchers may adjust it themselves. Persisted in the app_settings table
# via the get_/set_app_setting helpers defined near the top of this file.
@app.route("/admin/settings", methods=["GET", "POST"])
@login_required
def admin_settings():
    user = get_current_user()
    if not user or user.get("role") != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        raw_threshold = request.form.get("comparison_threshold", "").strip()
        slider_enabled = request.form.get("comparison_slider_enabled") == "on"

        try:
            threshold = int(float(raw_threshold))
        except ValueError:
            flash("Threshold must be a number.", "danger")
            return redirect(url_for("admin_settings"))

        if threshold < 60 or threshold > 100:
            flash("Threshold must be between 60 and 100.", "danger")
            return redirect(url_for("admin_settings"))
        if threshold % 5 != 0:
            flash("Threshold must be a multiple of 5 (to match the slider steps).", "danger")
            return redirect(url_for("admin_settings"))

        set_app_setting(SETTING_COMPARISON_THRESHOLD, str(threshold))
        set_app_setting(SETTING_COMPARISON_SLIDER_ENABLED, "true" if slider_enabled else "false")

        flash("Settings saved.", "success")
        return redirect(url_for("admin_settings"))

    return render_template(
        "admin_settings.html",
        user=user,
        comparison_threshold=get_comparison_threshold_setting(),
        comparison_slider_enabled=get_comparison_slider_enabled_setting(),
    )


# -----------------------------
# Manage Researchers (Admin-only)
# -----------------------------
@app.route("/admin/researchers")
@login_required
def manage_researchers():
    """
    Admin view to manage researchers (matches url_for('manage_researchers')).
    Now wired to the `user` table with role='Researcher'.
    """
    user = get_current_user()
    if not user or user["role"] != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = max(5, min(100, int(request.args.get("per_page", 25))))
    except (TypeError, ValueError):
        per_page = 25
    q = (request.args.get("q") or "").strip()
    role_filter = (request.args.get("role") or "").strip()

    where_clauses = []
    params: list = []
    if q:
        like = f"%{q}%"
        where_clauses.append("(first_name ILIKE %s OR last_name ILIKE %s OR email ILIKE %s)")
        params.extend([like, like, like])
    if role_filter in ("Admin", "Researcher"):
        where_clauses.append("role = %s")
        params.append(role_filter)
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(f"SELECT COUNT(*) AS c FROM users{where_sql}", params)
        total_users = cursor.fetchone()["c"]
        offset = (page - 1) * per_page
        cursor.execute(
            f"""
            SELECT researcher_id, first_name, last_name, email, role,
                   registered_date, is_active
            FROM users
            {where_sql}
            ORDER BY CASE role WHEN 'Admin' THEN 0 ELSE 1 END,
                     researcher_id ASC
            LIMIT %s OFFSET %s
            """,
            params + [per_page, offset],
        )
        researcher_rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    total_pages = max(1, (total_users + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages

    return render_template(
        "manage_researchers.html",
        user=user,
        researchers=researcher_rows,
        total_users=total_users,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        q=q,
        role_filter=role_filter,
    )

# -----------------------------
# Admin: researcher actions
# -----------------------------
# @app.route("/admin/researchers/<int:researcher_id>/reset-password", methods=["POST"])
# @login_required
# def admin_reset_password(researcher_id):
#     """Admin: reset a researcher's password to a default value."""
#     user = get_current_user()
#     if user["role"] != "Admin":
#         flash("Admin access only.", "danger")
#         return redirect(url_for("dashboard"))
#
#     # Example: simple default password (you can improve this later)
#     new_password = "matrix123"  # TODO: generate a random secure password in future
#
#     conn = get_db_connection()
#     cursor = conn.cursor()
#
#     try:
#         cursor.execute(
#             """
#             UPDATE users
#             SET password = %s
#             WHERE researcher_id = %s AND role = 'Researcher'
#             """,
#             (new_password, researcher_id)
#         )
#         conn.commit()
#     finally:
#         cursor.close()
#         conn.close()
#
#     flash(f"Password has been reset to '{new_password}' for researcher ID {researcher_id}.", "success")
#     return redirect(url_for("manage_researchers"))


@app.route("/admin/researchers/<int:researcher_id>/toggle-active", methods=["POST"])
@login_required
def admin_toggle_researcher_active(researcher_id):
    """Admin: activate or deactivate any user account (researcher or admin).
    Deactivated users keep their comparison_history rows, but cannot log in
    until an admin activates them again. An admin cannot deactivate their
    own account — that would lock themselves out."""
    user = get_current_user()
    if user["role"] != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    if researcher_id == user["id"]:
        flash("You can't deactivate your own account.", "danger")
        return redirect(url_for("manage_researchers"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT first_name, last_name, is_active FROM users "
            "WHERE researcher_id = %s",
            (researcher_id,),
        )
        target = cursor.fetchone()
        if not target:
            flash(f"User ID {researcher_id} not found.", "danger")
            return redirect(url_for("manage_researchers"))

        new_state = 0 if target["is_active"] else 1
        cursor.execute(
            "UPDATE users SET is_active = %s WHERE researcher_id = %s",
            (new_state, researcher_id),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    verb = "activated" if new_state else "deactivated"
    flash(
        f"{target['first_name']} {target['last_name']} has been {verb}.",
        "success" if new_state else "info",
    )
    return redirect(url_for("manage_researchers"))


@app.route("/admin/researchers/<int:researcher_id>/change-role", methods=["POST"])
@login_required
def admin_change_researcher_role(researcher_id):
    """Admin: set a user's role from an explicit posted `new_role` field
    (typically a <select> on the Manage Users page). Guards:
      - Admin-only endpoint
      - Cannot change your own role (prevents self-lockout)
      - Only 'Admin' or 'Researcher' accepted
      - No-op if submitted role equals current role (stops duplicate flashes)
    """
    user = get_current_user()
    if user["role"] != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    if researcher_id == user["id"]:
        flash("You can't change your own role.", "danger")
        return redirect(url_for("manage_researchers"))

    new_role = request.form.get("new_role", "").strip()
    if new_role not in ("Admin", "Researcher"):
        flash("Invalid role.", "danger")
        return redirect(url_for("manage_researchers"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT first_name, last_name, role FROM users WHERE researcher_id = %s",
            (researcher_id,),
        )
        target = cursor.fetchone()
        if not target:
            flash(f"User ID {researcher_id} not found.", "danger")
            return redirect(url_for("manage_researchers"))

        if target["role"] == new_role:
            # No change — just bounce back silently.
            return redirect(url_for("manage_researchers"))

        cursor.execute(
            "UPDATE users SET role = %s WHERE researcher_id = %s",
            (new_role, researcher_id),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    flash(
        f"{target['first_name']} {target['last_name']} is now a {new_role}.",
        "success",
    )
    return redirect(url_for("manage_researchers"))


@app.route("/admin/researchers/<int:researcher_id>/history")
@login_required
def admin_view_history(researcher_id):
    """Admin: view history for a specific researcher."""
    user = get_current_user()
    if user["role"] != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Get researcher info
        cursor.execute(
            """
            SELECT researcher_id, first_name, last_name, email
            FROM users
            WHERE researcher_id = %s AND role = 'Researcher'
            """,
            (researcher_id,)
        )
        researcher = cursor.fetchone()

        if not researcher:
            flash("Researcher not found.", "warning")
            return redirect(url_for("manage_researchers"))

        # Get their history
        cursor.execute(
            """
            SELECT history_id,
                   keywords,
                   academic_program_filter,
                   similarity_threshold,
                   created_at
            FROM comparison_history
            WHERE researcher_id = %s
            ORDER BY created_at DESC
            """,
            (researcher_id,)
        )
        history_rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    # Reuse the history template (you can also create a separate admin-specific one later)
    return render_template(
        "history.html",
        user=user,                    # current admin
        history_rows=history_rows,
        selected_researcher=researcher
    )

@app.route("/admin/researchers/<int:researcher_id>/reset", methods=["GET", "POST"])
@login_required
def admin_reset_password(researcher_id):
    """
    Admin-triggered password reset.

    GET  → show a confirmation page with a "Generate New Password" button.
    POST → generate a one-time random password using `secrets.token_urlsafe(8)`,
           store it on the user, set `must_change_password = 1`, and re-render
           the same page with the temporary password displayed exactly once
           so the admin can communicate it to the researcher.
    """
    # Only admins can access this
    if session.get("role") != "Admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Get the researcher record
        cursor.execute(
            """
            SELECT researcher_id, first_name, last_name, email
            FROM users
            WHERE researcher_id = %s AND role = 'Researcher'
            """,
            (researcher_id,)
        )
        researcher = cursor.fetchone()

        if not researcher:
            flash("Researcher not found.", "danger")
            return redirect(url_for("manage_researchers"))

        # GET → show the confirmation page
        if request.method == "GET":
            return render_template(
                "admin_reset_password.html",
                user=get_current_user(),
                researcher=researcher,
                generated_password=None,
            )

        # POST → generate a fresh random password and persist it
        new_password = secrets.token_urlsafe(8)  # ~11 chars URL-safe (e.g. "Vh3Q-uK9zPq")

        cursor.execute(
            """
            UPDATE users
            SET password = %s,
                must_change_password = 1
            WHERE researcher_id = %s
            """,
            (new_password, researcher_id),
        )
        conn.commit()

        # Re-render the same page with the one-time password visible
        return render_template(
            "admin_reset_password.html",
            user=get_current_user(),
            researcher=researcher,
            generated_password=new_password,
        )

    finally:
        cursor.close()
        conn.close()

@app.route("/history/<int:history_id>/heatmap")
@login_required
def history_heatmap(history_id):
    """
    Show Stage 2 as a full-page HTML table with a color gradient.
    Opens in a new tab from the History Detail page.
    """
    user = get_current_user()

    # Load history + Stage 1 matches
    history, matches = matcher.get_history_with_matches(history_id)
    if not history:
        flash("History entry not found.", "danger")
        return redirect(url_for("history"))

    # Permission check: researchers see only their own
    if user["role"] == "Researcher" and history["researcher_id"] != user["id"]:
        flash("You are not allowed to view that history entry.", "danger")
        return redirect(url_for("history"))

    # ----- Parse keywords from history["keywords"] -----
    raw_keywords = history.get("keywords") or ""
    keywords = []

    if isinstance(raw_keywords, str):
        try:
            parsed = json.loads(raw_keywords)
            if isinstance(parsed, list):
                keywords = [str(k).strip() for k in parsed if str(k).strip()]
            else:
                keywords = [
                    part.strip()
                    for part in str(parsed).split(",")
                    if part.strip()
                ]
        except json.JSONDecodeError:
            keywords = [
                part.strip()
                for part in raw_keywords.split(",")
                if part.strip()
            ]
    elif isinstance(raw_keywords, list):
        keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]

    if not keywords or not matches:
        flash("Not enough data to build a heatmap for this entry.", "warning")
        return redirect(url_for("history_detail", history_id=history_id))

    # ----- Build Stage 2 matrix (keyword × document) -----
    matrix = matcher.build_stage2_matrix(keywords, matches)
    if matrix is None or matrix.empty:
        flash("Unable to build heatmap matrix for this entry.", "warning")
        return redirect(url_for("history_detail", history_id=history_id))

    col_labels = list(matrix.columns)         # documents
    row_labels = list(matrix.index)           # keywords
    values = matrix.values.tolist()           # 2D list of floats (0–1)

    min_val = float(matrix.values.min())
    max_val = float(matrix.values.max())

    # Build a friendlier structure for Jinja
    table_rows = []
    for i, kw in enumerate(row_labels):
        row = {
            "keyword": kw,
            "cells": [
                {"col_label": col_labels[j], "value": values[i][j]}
                for j in range(len(col_labels))
            ],
        }
        table_rows.append(row)

    return render_template(
        "history_heatmap_table.html",
        user=user,
        history=history,
        col_labels=col_labels,
        table_rows=table_rows,
        min_val=min_val,
        max_val=max_val,
    )



# -----------------------------
# AI Gap Analysis
# -----------------------------
from flask import jsonify

@app.route("/api/history/<int:history_id>/gap_analysis/<int:doc_id>")
@login_required
def api_gap_analysis(history_id, doc_id):
    """
    Asynchronous endpoint to generate the AI Gap Analysis.
    """
    user = get_current_user()

    history, matches = matcher.get_history_with_matches(history_id)
    if not history or (user["role"] == "Researcher" and history["researcher_id"] != user["id"]):
        return jsonify({"error": "Unauthorized or not found"}), 403

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT abstract FROM documents WHERE document_id = %s", (doc_id,))
        repo_doc = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not repo_doc:
        return jsonify({"error": "Repository document not found."}), 404

    user_abstract = history.get("user_abstract") or ""
    repo_abstract = repo_doc["abstract"] or ""

    analysis_text = matcher.generate_gap_analysis(user_abstract, repo_abstract)

    # Convert section markers to HTML
    analysis_html = analysis_text.replace("**Problem Focus:**", "<strong>Problem Focus:</strong>")
    analysis_html = analysis_html.replace("**Verdict:**", "<strong>Verdict:</strong>")
    analysis_html = analysis_html.replace("**Similarities:**", "<strong>Similarities:</strong>")
    analysis_html = analysis_html.replace("**What Your Proposal Adds:**", "<strong>What Your Proposal Adds:</strong>")
    # Backward compat with older output shapes
    analysis_html = analysis_html.replace("**Differences:**", "<strong>Differences:</strong>")
    analysis_html = analysis_html.replace("**Summary:**", "<strong>Summary:</strong>")
    analysis_html = analysis_html.replace("\n", "<br>")

    return jsonify({"gap_analysis": analysis_html})


# -----------------------------
# Feature Highlight (independent of feature matrix)
# -----------------------------
@app.route("/api/history/<int:history_id>/feature_highlight")
@login_required
def api_feature_highlight(history_id):
    """
    Returns the chosen abstract with HTML <mark> tags wrapping every phrase that
    mentions the requested feature. Triggered when the user clicks a checkmark
    cell in the Stage 2 feature matrix.

    Query string:
        feature  - the unified key feature label (required)
        target   - either "user" (use the history's user_abstract) or a numeric
                   document_id (use that document's abstract)
    """
    user = get_current_user()

    history, history_matches = matcher.get_history_with_matches(history_id)
    if not history or (user["role"] == "Researcher" and history["researcher_id"] != user["id"]):
        return jsonify({"error": "Unauthorized or not found"}), 403

    feature = (request.args.get("feature") or "").strip()
    target = (request.args.get("target") or "").strip()
    if not feature or not target:
        return jsonify({"error": "Missing 'feature' or 'target' query parameter."}), 400

    # Resolve which abstract we're highlighting
    abstract_text = ""
    source_label = ""
    doc_id_int: Optional[int] = None
    if target == "user":
        abstract_text = history.get("user_abstract") or ""
        source_label = "Your Study"
    else:
        try:
            doc_id_int = int(target)
        except ValueError:
            return jsonify({"error": "target must be 'user' or a numeric document id."}), 400

        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                "SELECT title, abstract FROM documents WHERE document_id = %s",
                (doc_id_int,),
            )
            row = cur.fetchone()
        finally:
            cur.close()
            conn.close()

        if not row:
            return jsonify({"error": "Repository document not found."}), 404
        abstract_text = row["abstract"] or ""
        source_label = row["title"] or f"Document #{doc_id_int}"

    if not abstract_text:
        return jsonify({"error": "Selected abstract is empty."}), 404

    # Fast path: pull the pre-extracted evidence from the cached feature matrix.
    # If present AND still a real substring of the abstract, this lets the
    # highlighter skip the LLM entirely.
    evidence_phrase: Optional[str] = None
    fm_raw = history.get("feature_matrix") or ""
    if fm_raw:
        try:
            fm = json.loads(fm_raw)
            for fm_row in fm:
                if fm_row.get("feature") != feature:
                    continue
                if target == "user":
                    evidence_phrase = matcher.cell_evidence(fm_row.get("User Abstract"))
                else:
                    for idx, m in enumerate(history_matches):
                        if m["document_id"] == doc_id_int:
                            evidence_phrase = matcher.cell_evidence(fm_row.get(f"Abstract {idx+1}"))
                            break
                break
        except json.JSONDecodeError:
            pass

    result = matcher.highlight_feature_in_abstract(abstract_text, feature, evidence=evidence_phrase)
    return jsonify(
        {
            "feature": feature,
            "source_label": source_label,
            "abstract": result["abstract"],
            "highlighted_html": result["highlighted_html"],
            "phrases": result["phrases"],
            "match_count": len(result["phrases"]),
        }
    )


@app.route("/api/history/<int:history_id>/feature_compare")
@login_required
def api_feature_compare(history_id):
    """
    Returns the side-by-side feature objects (label + description) that
    explain why a row's user-cell and doc-cell merged into the same Stage 2
    cluster. Powers the "click ✓ to see why" modal on history_detail.

    Query string:
      feature  - the row's unified key feature label (the cluster's canonical)
      target   - "user" for the user-only card, or a numeric document_id for
                 the user-vs-doc side-by-side comparison.
    """
    user = get_current_user()
    history, history_matches = matcher.get_history_with_matches(history_id)
    if not history or (user["role"] == "Researcher" and history["researcher_id"] != user["id"]):
        return jsonify({"error": "Unauthorized or not found"}), 403

    feature = (request.args.get("feature") or "").strip()
    target = (request.args.get("target") or "").strip()
    if not feature or not target:
        return jsonify({"error": "Missing 'feature' or 'target' query parameter."}), 400

    fm_raw = history.get("feature_matrix") or ""
    if not fm_raw:
        return jsonify({"error": "Feature matrix not yet generated for this comparison."}), 404
    try:
        fm = json.loads(fm_raw)
    except json.JSONDecodeError:
        return jsonify({"error": "Cached matrix is malformed."}), 500

    matched_row = None
    for r in fm:
        if r.get("feature") == feature:
            matched_row = r
            break
    if not matched_row:
        return jsonify({"error": "Feature row not found in matrix."}), 404

    user_cell = matched_row.get("User Abstract")
    user_card = None
    if matcher.cell_is_present(user_cell):
        user_card = {
            "label": matcher.cell_evidence(user_cell) or feature,
            "description": matcher.cell_description(user_cell) or "",
        }

    doc_card = None
    doc_title = None
    doc_id_int = None
    if target != "user":
        try:
            doc_id_int = int(target)
        except ValueError:
            return jsonify({"error": "target must be 'user' or a numeric document id."}), 400

        idx_in_matches = None
        for idx, m in enumerate(history_matches):
            if m["document_id"] == doc_id_int:
                idx_in_matches = idx
                break
        if idx_in_matches is None:
            return jsonify({"error": "Document not in this comparison's matches."}), 404

        doc_title = history_matches[idx_in_matches].get("title") or f"Document #{doc_id_int}"
        doc_cell = matched_row.get(f"Abstract {idx_in_matches + 1}")
        if matcher.cell_is_present(doc_cell):
            doc_card = {
                "label": matcher.cell_evidence(doc_cell) or feature,
                "description": matcher.cell_description(doc_cell) or "",
            }

    return jsonify({
        "feature": feature,
        "user": user_card,
        "doc": doc_card,
        "doc_title": doc_title,
        "doc_id": doc_id_int,
    })


# -----------------------------
# AI Feature Matrix Regeneration
# -----------------------------
@app.route("/api/history/<int:history_id>/reload_matrix", methods=["POST"])
@login_required
def api_reload_matrix(history_id):
    """
    Regenerates the Stage 2 feature matrix for a given history row, overwriting
    the database cache. Stage 2 is deterministic (SBERT clustering on stored
    key_features), so this is cheap; the main reason to call it is to refresh
    a cached matrix after a matched document's key_features changed.
    """
    user = get_current_user()
    history, matches = matcher.get_history_with_matches(history_id)
    if not history or (user["role"] == "Researcher" and history["researcher_id"] != user["id"]):
        return jsonify({"error": "Unauthorized or not found"}), 403

    if not matches:
        return jsonify({"error": "No matches to evaluate"}), 400

    user_keywords = _parse_history_keywords(history.get("keywords"))
    feature_matrix = matcher.evaluate_feature_matrix(
        user_keywords,
        matches,
        user_abstract=history.get("user_abstract") or "",
    )

    # Save the generation to the database
    if feature_matrix:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE comparison_history SET feature_matrix = %s WHERE history_id = %s",
                (json.dumps(feature_matrix), history_id)
            )
            conn.commit()
        except Exception as e:
            print(f"Error updating feature matrix: {e}")
            return jsonify({"error": "Database error"}), 500
        finally:
            cursor.close()
            conn.close()
            
    return jsonify({"success": True})


# -----------------------------
# Main entrypoint
# -----------------------------
if __name__ == "__main__":
    app.run(debug=True)

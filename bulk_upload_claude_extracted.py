"""Upload pre-extracted studies to MatrixMatch's experimental save route.

Reads `studies/_downloaded/batch-<N>/extractions.json` (an array of
records produced by Claude reading each PDF) plus the manifest CSV,
then for each entry:

  1. POSTs the PDF to /admin/documents/upload-experimental (purely to
     get a staging_token; the AI extraction it returns is ignored).
  2. POSTs Claude's extracted fields to
     /admin/documents/upload-experimental/save with that staging token.

extractions.json record shape:
  {
    "pdf_path":  "studies/_downloaded/batch-1/arxiv-2401.12345.pdf",
    "title":     "...verbatim...",
    "authors":   "Foo Bar, Baz Qux",
    "abstract":  "...verbatim english abstract...",
    "research_field": "Information Technology & Computing",
    "research_field_other": "",   # only when research_field == "Others"
    "key_features": [
      {"label": "...", "description": "..."},
      ...
    ]
  }

Run with the Flask app (python app.py) listening on :5000.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:5000"
ADMIN_EMAIL = "admin@gmail.com"
ADMIN_PASSWORD = "1234"

VALID_FIELDS = {
    "Engineering & Architecture",
    "Information Technology & Computing",
    "Sciences",
    "Business & Management",
    "Education",
    "Industrial Technology",
    "Arts, Humanities & Social Sciences",
    "Others",
}


class _ReviewParser(HTMLParser):
    """Pulls the staging-handle hidden inputs out of the review page."""

    WANT = {"staging_token", "staging_ext", "original_filename"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("name") in self.WANT:
            self.fields[a["name"]] = a.get("value", "")


def parse_review(html: str) -> dict:
    p = _ReviewParser()
    p.feed(html)
    return dict(p.fields)


# ---------------------------------------------------------------------------
# Logging (unicode-safe on Windows cp1252 console)
# ---------------------------------------------------------------------------

_log_fh = None


def log(msg: str) -> None:
    try:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        sys.stdout.write(msg.encode("ascii", "replace").decode("ascii") + "\n")
        sys.stdout.flush()
    global _log_fh
    if _log_fh is not None:
        _log_fh.write(msg + "\n")
        _log_fh.flush()


# ---------------------------------------------------------------------------
# HTTP flow
# ---------------------------------------------------------------------------

def login(s: requests.Session) -> None:
    r = s.post(
        f"{BASE}/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        allow_redirects=False,
        timeout=30,
    )
    if r.status_code not in (302, 303):
        raise SystemExit(f"Login failed: HTTP {r.status_code}\n{r.text[:300]}")
    loc = r.headers.get("Location", "")
    if "login" in loc:
        raise SystemExit(f"Login rejected (redirect to {loc})")


def ensure_logged_in(s: requests.Session) -> None:
    """Cheap probe; re-login if session expired."""
    r = s.get(f"{BASE}/admin/dashboard", allow_redirects=False, timeout=10)
    if r.status_code in (302, 303):
        loc = r.headers.get("Location", "")
        if "login" in loc:
            log("  session expired, re-logging in...")
            login(s)


def stage_pdf(s: requests.Session, pdf_path: Path) -> dict | None:
    """POST the PDF to upload-experimental, parse the staging info."""
    with pdf_path.open("rb") as fh:
        files = {"study_file": (pdf_path.name, fh, "application/pdf")}
        r = s.post(
            f"{BASE}/admin/documents/upload-experimental",
            files=files,
            timeout=600,
        )
    if r.status_code != 200:
        log(f"  STAGE FAIL HTTP {r.status_code}: {r.text[:200]}")
        return None
    info = parse_review(r.text)
    if not info.get("staging_token"):
        log("  STAGE FAIL: no staging_token in response")
        return None
    return info


def save_doc(s: requests.Session, rec: dict, staging: dict) -> tuple[bool, str]:
    """POST the save form. Returns (ok, msg)."""
    field = rec.get("research_field", "").strip()
    if field not in VALID_FIELDS:
        return False, f"invalid research_field {field!r}"
    field_other = rec.get("research_field_other", "").strip()
    if field == "Others" and not field_other:
        field_other = "Auto-imported"

    kf = rec.get("key_features") or []
    if not isinstance(kf, list) or len(kf) < 1:
        return False, "no key_features"
    for f in kf:
        if not isinstance(f, dict):
            return False, "feature is not an object"
        if not f.get("label") or not f.get("description"):
            return False, "feature missing label or description"

    payload = {
        "title": rec["title"].strip(),
        "authors": rec["authors"].strip(),
        "abstract": rec["abstract"].strip(),
        "research_field": field,
        "research_field_other": field_other,
        "key_features": json.dumps(kf, ensure_ascii=False),
        "staging_token": staging["staging_token"],
        "staging_ext": staging.get("staging_ext", ""),
        "original_filename": staging.get("original_filename", ""),
    }
    r = s.post(
        f"{BASE}/admin/documents/upload-experimental/save",
        data=payload,
        allow_redirects=False,
        timeout=120,
    )
    if r.status_code in (302, 303):
        loc = r.headers.get("Location", "")
        if "manage_documents" in loc or loc.endswith("/admin/documents"):
            return True, f"saved -> {loc}"
        return False, f"unexpected redirect {loc}"
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def process_batch(batch: int, extractions_path: Path, dry_run: bool) -> int:
    """Process one batch's extractions.json. Returns 0 on full success."""
    if not extractions_path.exists():
        log(f"extractions file not found: {extractions_path}")
        return 2
    with extractions_path.open("r", encoding="utf-8") as fh:
        records = json.load(fh)
    if not isinstance(records, list):
        log("extractions.json must be a JSON array")
        return 2

    log(f"\n=== Batch {batch}: {len(records)} records ===")

    s = requests.Session()
    login(s)
    log("logged in.")

    ok = 0
    fail: list[tuple[str, str]] = []
    for i, rec in enumerate(records, 1):
        pdf_path = ROOT / rec["pdf_path"]
        log(f"\n[{i}/{len(records)}] {pdf_path.name}")
        if not pdf_path.exists():
            log(f"  PDF MISSING: {pdf_path}")
            fail.append((rec["pdf_path"], "pdf missing"))
            continue

        if dry_run:
            log(f"  DRY-RUN: would stage and save (field={rec.get('research_field')}, "
                f"features={len(rec.get('key_features') or [])})")
            ok += 1
            continue

        ensure_logged_in(s)
        staging = stage_pdf(s, pdf_path)
        if not staging:
            fail.append((rec["pdf_path"], "stage failed"))
            continue

        success, msg = save_doc(s, rec, staging)
        if success:
            ok += 1
            log(f"  SAVED  {msg}")
        else:
            fail.append((rec["pdf_path"], msg))
            log(f"  SAVE FAIL: {msg}")
        time.sleep(0.5)

    log(f"\n=== Batch {batch} done: {ok}/{len(records)} saved ===")
    if fail:
        log("Failures:")
        for path, why in fail:
            log(f"  {path}  --  {why}")
    return 0 if not fail else 1


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--batch", type=int, required=True)
    p.add_argument("--extractions", type=Path,
                   help="Path to extractions.json (default: "
                        "studies/_downloaded/batch-<N>/extractions.json)")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate records without uploading")
    args = p.parse_args(argv)

    extractions = args.extractions or (
        ROOT / "studies" / "_downloaded" / f"batch-{args.batch}" / "extractions.json"
    )
    log_path = (ROOT / "studies" / "_downloaded" / f"batch-{args.batch}"
                / "upload.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    global _log_fh
    _log_fh = log_path.open("a", encoding="utf-8")
    return process_batch(args.batch, extractions, args.dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

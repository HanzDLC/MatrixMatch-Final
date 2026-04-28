"""One-off bulk uploader for the Experimental upload route.

Walks every .pdf in `Abstract for Repository/`, posts it to the running
Flask app's experimental upload endpoint (which runs the LLM extractor),
infers a research_field from the title, then submits the review form so
the document is saved to the DB.

Run while `python app.py` is up on http://127.0.0.1:5000.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import requests

BASE = "http://127.0.0.1:5000"
ADMIN_EMAIL = "admin@gmail.com"
ADMIN_PASSWORD = "1234"
SOURCE_DIR = Path(__file__).resolve().parent / "Abstract for Repository"
LOG_PATH = Path(__file__).resolve().parent / "bulk_upload_experimental.log"

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

# Order matters — first match wins, so put the most specific patterns first.
FIELD_RULES = [
    ("Sciences", [
        "ion chromatograph", "ion suppression", "carvone", "cashew",
        "bond valence", "topochemical", "spearmint", "fragrant",
        "iron content", "instrumental", "spectroscop", "natural resource",
        " gis ", "extraction of", "characterization", "waste isolation",
        "chemistry", "biology", "physics", "chemical",
    ]),
    ("Education", [
        "school", "education", "training program", "self-efficacy",
        "inclusive practices", "diversity equity", "learning culture",
        "financial aid leaders", "onboarding", "mentor", "teacher",
        "student", "scout", "civic engagement",
    ]),
    ("Information Technology & Computing", [
        "ransomware", "cyber", "cloud comput", "machine learning",
        "artificial intelligence", "ai ", "iot", "blockchain",
        "android", "windows phone", "computer vision", "robot",
        "data leak", "data migration", "hadoop", "search engine",
        "exam engine", "auto grader", "library management",
        "complaint management", "tracking system", "remote ringer",
        "extensive medical application", "face identification",
        "online toxic", "social engineering", "fundraiser",
        "management system", "hr connect", "event management",
        "defect tracking", "barangay", "portal", "media",
        "cure unity", "java",
    ]),
    ("Business & Management", [
        "business retention", "customer turnover", "labor management",
        "communication discipline", "marketing",
    ]),
    ("Arts, Humanities & Social Sciences", [
        "policing", "israeli", "palestinian", "historical analysis",
        "mental health", "sexually transmitted", "screening program",
        "communication and onboarding",
    ]),
    ("Engineering & Architecture", [
        "engineering notebook", "architecture", "civil ",
    ]),
    ("Industrial Technology", [
        "industrial", "manufactur",
    ]),
]


def infer_field(title: str, filename: str) -> str:
    haystack = f" {title.lower()} {filename.lower()} "
    for field, keywords in FIELD_RULES:
        for kw in keywords:
            if kw in haystack:
                return field
    return "Others"


# --- minimal HTML parsing for the review form -------------------------------

class _ReviewParser(HTMLParser):
    """Extracts the input/textarea values we need from the review HTML.

    The fields we care about are static (server-rendered Jinja), except
    `key_features` which is injected by JS from `const initial = ...`.
    We pull that one with a regex on the raw HTML separately.
    """

    WANT_INPUTS = {
        "staging_token", "staging_ext", "original_filename",
        "title", "authors",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self._in_textarea: str | None = None
        self._textarea_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("name") in self.WANT_INPUTS:
            self.fields[a["name"]] = a.get("value", "")
        elif tag == "textarea" and a.get("name") == "abstract":
            self._in_textarea = "abstract"
            self._textarea_buf = []

    def handle_endtag(self, tag):
        if tag == "textarea" and self._in_textarea:
            self.fields[self._in_textarea] = "".join(self._textarea_buf)
            self._in_textarea = None

    def handle_data(self, data):
        if self._in_textarea:
            self._textarea_buf.append(data)


_KF_RE = re.compile(r"const\s+initial\s*=\s*(\[.*?\]);", re.DOTALL)


def parse_review(html: str) -> dict:
    p = _ReviewParser()
    p.feed(html)
    out = dict(p.fields)
    m = _KF_RE.search(html)
    if m:
        try:
            out["key_features"] = json.loads(m.group(1))
        except json.JSONDecodeError:
            out["key_features"] = []
    else:
        out["key_features"] = []
    return out


# --- main flow --------------------------------------------------------------

def login(s: requests.Session) -> None:
    r = s.post(
        f"{BASE}/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        allow_redirects=False,
        timeout=30,
    )
    if r.status_code not in (302, 303):
        raise SystemExit(f"Login failed: HTTP {r.status_code}\n{r.text[:400]}")
    loc = r.headers.get("Location", "")
    if "login" in loc:
        raise SystemExit(f"Login rejected; redirected to {loc}")


def upload_one(s: requests.Session, pdf: Path, log) -> bool:
    title_guess = pdf.stem
    field_guess = infer_field(title_guess, pdf.name)
    log(f"\n=== {pdf.name}")
    log(f"  inferred field: {field_guess}")

    # Stage 1: extract
    with pdf.open("rb") as fh:
        files = {"study_file": (pdf.name, fh, "application/pdf")}
        r = s.post(
            f"{BASE}/admin/documents/upload-experimental",
            files=files,
            timeout=300,
        )
    if r.status_code != 200:
        log(f"  EXTRACT FAILED: HTTP {r.status_code}")
        return False
    review = parse_review(r.text)
    title = review.get("title", "")
    authors = review.get("authors", "")
    abstract = review.get("abstract", "")
    kf = review.get("key_features") or []
    token = review.get("staging_token", "")
    ext = review.get("staging_ext", "")
    orig = review.get("original_filename", pdf.name)

    log(f"  title:   {title[:80]!r}")
    log(f"  authors: {authors[:80]!r}")
    log(f"  abstract len: {len(abstract)}")
    log(f"  key_features: {len(kf)}")

    if not (title and authors and abstract and kf and token):
        log(f"  SKIP (missing fields). token={bool(token)} kf={len(kf)}")
        return False

    # If field landed on "Others", give it a sensible label so the row is searchable.
    other_label = "" if field_guess != "Others" else "Auto-imported"

    payload = {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "research_field": field_guess,
        "research_field_other": other_label,
        "key_features": json.dumps(kf),
        "staging_token": token,
        "staging_ext": ext,
        "original_filename": orig,
    }

    r2 = s.post(
        f"{BASE}/admin/documents/upload-experimental/save",
        data=payload,
        allow_redirects=False,
        timeout=120,
    )
    if r2.status_code in (302, 303):
        loc = r2.headers.get("Location", "")
        if "manage_documents" in loc or loc.endswith("/admin/documents"):
            log(f"  SAVED  -> {loc}")
            return True
        log(f"  SAVE redirected unexpectedly to {loc}")
        return False
    log(f"  SAVE FAILED: HTTP {r2.status_code}")
    return False


def main(argv: list[str]) -> int:
    if not SOURCE_DIR.is_dir():
        print(f"Source dir missing: {SOURCE_DIR}", file=sys.stderr)
        return 2

    pdfs = sorted(p for p in SOURCE_DIR.iterdir() if p.suffix.lower() == ".pdf")
    if argv and argv[0].isdigit():
        pdfs = pdfs[: int(argv[0])]

    s = requests.Session()
    login(s)
    print(f"Logged in. {len(pdfs)} files to process.")

    log_fh = LOG_PATH.open("w", encoding="utf-8")

    def log(msg: str):
        print(msg)
        log_fh.write(msg + "\n")
        log_fh.flush()

    ok = 0
    for i, pdf in enumerate(pdfs, 1):
        log(f"\n[{i}/{len(pdfs)}] {pdf.name}")
        try:
            if upload_one(s, pdf, log):
                ok += 1
        except Exception as exc:
            log(f"  EXCEPTION: {exc!r}")
        time.sleep(0.5)

    log(f"\nDONE. {ok}/{len(pdfs)} succeeded. Log: {LOG_PATH}")
    log_fh.close()
    return 0 if ok == len(pdfs) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

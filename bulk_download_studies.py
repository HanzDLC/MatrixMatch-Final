"""Topic-clustered bulk downloader for MatrixMatch.

Downloads open-access research PDFs from arXiv and OpenAlex per topic
cluster, filters out scanned/empty PDFs (pdfplumber word count >= 200),
dedupes by source ID and content hash, and writes a manifest CSV that
the upload-and-extract pipeline consumes.

Usage:
    python bulk_download_studies.py --batch 1 --target 20
    python bulk_download_studies.py --batch 1 --target 20 --cluster security
    python bulk_download_studies.py --status   # show progress vs total target

Manifest schema (studies/_downloaded/manifest.csv):
    pdf_path, source, source_id, source_url, intended_research_field,
    intended_cluster, batch, sha256, word_count, downloaded_at

Cluster targets (across the whole 421-doc campaign) are baked into
CLUSTERS below and align with the approved plan.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator
from xml.etree import ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parent
DOWNLOAD_ROOT = ROOT / "studies" / "_downloaded"
MANIFEST = DOWNLOAD_ROOT / "manifest.csv"
LOG = DOWNLOAD_ROOT / "download.log"

MIN_WORDS = 200
USER_AGENT = "MatrixMatchBulkDownloader/1.0 (research repository seeding)"
TIMEOUT = 60


# ---------------------------------------------------------------------------
# Cluster plan — total target counts per cluster across the entire campaign.
# Sums to 421. Sources listed by preference; first that yields a fresh PDF wins.
# ---------------------------------------------------------------------------

# field, cluster_id, total_target, sources (list of (source_name, query_or_category))
CLUSTERS: list[dict] = [
    # === Information Technology & Computing : 220 ===
    {"field": "Information Technology & Computing", "id": "it_websystems",
     "total": 45, "sources": [("arxiv", "cat:cs.SE"), ("arxiv", "cat:cs.IR")]},
    {"field": "Information Technology & Computing", "id": "it_security",
     "total": 35, "sources": [("arxiv", "cat:cs.CR")]},
    {"field": "Information Technology & Computing", "id": "it_ml_cv_nlp",
     "total": 50, "sources": [("arxiv", "cat:cs.LG"), ("arxiv", "cat:cs.CV"), ("arxiv", "cat:cs.CL")]},
    {"field": "Information Technology & Computing", "id": "it_iot_sensors",
     "total": 25, "sources": [("arxiv", "cat:cs.NI"), ("arxiv", "cat:eess.SP")]},
    {"field": "Information Technology & Computing", "id": "it_mobile",
     "total": 20, "sources": [("openalex", "mobile application Android system"), ("arxiv", "cat:cs.HC")]},
    {"field": "Information Technology & Computing", "id": "it_elearning",
     "total": 20, "sources": [("arxiv", "cat:cs.CY"), ("openalex", "e-learning platform online education system")]},
    {"field": "Information Technology & Computing", "id": "it_health_it",
     "total": 15, "sources": [("openalex", "health information system electronic medical records"), ("arxiv", "cat:cs.HC")]},
    {"field": "Information Technology & Computing", "id": "it_data_systems",
     "total": 10, "sources": [("arxiv", "cat:cs.DB"), ("arxiv", "cat:cs.DC")]},

    # === Education : 60 ===
    {"field": "Education", "id": "edu_student_outcomes",
     "total": 20, "sources": [("openalex", "student academic outcomes mentoring program intervention")]},
    {"field": "Education", "id": "edu_pedagogy_elearning",
     "total": 15, "sources": [("openalex", "e-learning pedagogy higher education classroom technology")]},
    {"field": "Education", "id": "edu_dei",
     "total": 15, "sources": [("openalex", "diversity equity inclusion education program")]},
    {"field": "Education", "id": "edu_leadership",
     "total": 10, "sources": [("openalex", "educational leadership school administration superintendent")]},

    # === Sciences : 55 ===
    {"field": "Sciences", "id": "sci_extraction_chem",
     "total": 20, "sources": [("openalex", "extraction essential oil plant compound solvent")]},
    {"field": "Sciences", "id": "sci_analytical",
     "total": 15, "sources": [("openalex", "spectroscopy analytical instrumentation chromatography")]},
    {"field": "Sciences", "id": "sci_gis_environ",
     "total": 10, "sources": [("openalex", "GIS natural resource mapping environmental monitoring")]},
    {"field": "Sciences", "id": "sci_materials",
     "total": 10, "sources": [("openalex", "materials characterization synthesis composite")]},

    # === Business & Management : 40 ===
    {"field": "Business & Management", "id": "biz_churn_analytics",
     "total": 20, "sources": [("openalex", "customer churn prediction predictive analytics retention")]},
    {"field": "Business & Management", "id": "biz_org_hr",
     "total": 15, "sources": [("openalex", "organizational behavior human resources employee engagement")]},
    {"field": "Business & Management", "id": "biz_retention",
     "total": 5, "sources": [("openalex", "customer retention loyalty marketing strategy")]},

    # === Engineering & Architecture : 25 ===
    {"field": "Engineering & Architecture", "id": "eng_civil",
     "total": 10, "sources": [("openalex", "civil engineering structural analysis construction")]},
    {"field": "Engineering & Architecture", "id": "eng_applied",
     "total": 10, "sources": [("arxiv", "cat:eess.SY"), ("openalex", "applied engineering design system")]},
    {"field": "Engineering & Architecture", "id": "eng_digital_tools",
     "total": 5, "sources": [("openalex", "digital engineering notebook design tool")]},

    # === Arts, Humanities & Social Sciences : 15 ===
    {"field": "Arts, Humanities & Social Sciences", "id": "arts_policy",
     "total": 5, "sources": [("openalex", "public policy governance regulation")]},
    {"field": "Arts, Humanities & Social Sciences", "id": "arts_public_health",
     "total": 5, "sources": [("openalex", "public health social work community intervention")]},
    {"field": "Arts, Humanities & Social Sciences", "id": "arts_history",
     "total": 5, "sources": [("openalex", "historical analysis policing conflict society")]},

    # === Industrial Technology : 5 ===
    {"field": "Industrial Technology", "id": "indus_manufacturing",
     "total": 5, "sources": [("arxiv", "cat:eess.SY"), ("openalex", "manufacturing automation industrial process")]},

    # === Others : 1 ===
    {"field": "Others", "id": "others_misc",
     "total": 1, "sources": [("openalex", "policy considerations community resource sharing")]},
]


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

MANIFEST_FIELDS = [
    "pdf_path", "source", "source_id", "source_url",
    "intended_research_field", "intended_cluster",
    "batch", "sha256", "word_count", "downloaded_at",
]


def load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    with MANIFEST.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def append_manifest(rows: list[dict]) -> None:
    new_file = not MANIFEST.exists()
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def already_have(rows: list[dict], source: str, source_id: str) -> bool:
    return any(r["source"] == source and r["source_id"] == source_id for r in rows)


def have_hash(rows: list[dict], sha: str) -> bool:
    return any(r["sha256"] == sha for r in rows)


def cluster_progress(rows: list[dict], cluster_id: str) -> int:
    return sum(1 for r in rows if r["intended_cluster"] == cluster_id)


# ---------------------------------------------------------------------------
# PDF utilities
# ---------------------------------------------------------------------------

def is_text_extractable(pdf_bytes: bytes) -> tuple[bool, int]:
    """Return (passes, word_count). Uses pdfplumber on the first ~10 pages
    for speed; full-document extraction happens later in the upload flow."""
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            words = 0
            for i, page in enumerate(pdf.pages):
                if i >= 10:
                    break
                t = page.extract_text() or ""
                words += len(t.split())
                if words >= MIN_WORDS:
                    return True, words
            return words >= MIN_WORDS, words
    except Exception as exc:
        log(f"  pdfplumber error: {exc!r}")
        return False, 0


def looks_english(pdf_bytes: bytes) -> bool:
    """Cheap English heuristic: at least 30% of common English stopwords
    appear in the first ~10 pages."""
    import pdfplumber
    common = {"the", "of", "and", "to", "in", "a", "is", "that", "for",
              "with", "this", "are", "by", "we", "on", "as", "an", "be"}
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            tokens: list[str] = []
            for i, page in enumerate(pdf.pages):
                if i >= 5:
                    break
                t = (page.extract_text() or "").lower()
                tokens.extend(re.findall(r"[a-z]+", t))
                if len(tokens) >= 500:
                    break
        if not tokens:
            return False
        hits = sum(1 for w in tokens if w in common)
        return (hits / max(1, len(tokens))) >= 0.06
    except Exception:
        return True  # fail-open; pdfplumber filter already gated us


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class ArxivSource:
    """Atom-XML query API. Returns iterator of candidates (source_id, pdf_url, title)."""

    BASE = "http://export.arxiv.org/api/query"
    NS = {"atom": "http://www.w3.org/2005/Atom"}

    def __init__(self, session: requests.Session):
        self.s = session

    def candidates(self, category: str, max_results: int = 50,
                   start: int = 0) -> Iterator[tuple[str, str, str]]:
        params = {
            "search_query": category,
            "start": str(start),
            "max_results": str(max_results),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = f"{self.BASE}?{urllib.parse.urlencode(params)}"
        log(f"  arxiv query: {category} start={start} n={max_results}")
        r = self.s.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            log(f"  arxiv non-200: {r.status_code}")
            return
        time.sleep(3)  # arXiv rate-limit politeness
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError as exc:
            log(f"  arxiv parse error: {exc}")
            return
        for entry in root.findall("atom:entry", self.NS):
            id_el = entry.find("atom:id", self.NS)
            title_el = entry.find("atom:title", self.NS)
            if id_el is None or not id_el.text:
                continue
            arxiv_url = id_el.text.strip()
            # http://arxiv.org/abs/2401.12345v1
            m = re.search(r"arxiv\.org/abs/([0-9.]+)(v\d+)?", arxiv_url)
            if not m:
                continue
            arxiv_id = m.group(1)
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            title = (title_el.text or "").strip().replace("\n", " ") if title_el is not None else ""
            yield (arxiv_id, pdf_url, title)


class OpenAlexSource:
    """JSON API. Filters by is_oa:true, returns OA PDF URL when available."""

    BASE = "https://api.openalex.org/works"

    def __init__(self, session: requests.Session):
        self.s = session

    def candidates(self, query: str, per_page: int = 50,
                   page: int = 1) -> Iterator[tuple[str, str, str]]:
        params = {
            "search": query,
            "filter": "is_oa:true,has_fulltext:true,type:article",
            "per-page": str(per_page),
            "page": str(page),
        }
        url = f"{self.BASE}?{urllib.parse.urlencode(params)}"
        log(f"  openalex query: {query!r} page={page}")
        r = self.s.get(url, timeout=TIMEOUT,
                       headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            log(f"  openalex non-200: {r.status_code}")
            return
        time.sleep(1)
        try:
            data = r.json()
        except json.JSONDecodeError:
            log(f"  openalex parse error")
            return
        for work in data.get("results", []):
            wid = (work.get("id") or "").rsplit("/", 1)[-1]
            if not wid:
                continue
            best = work.get("best_oa_location") or {}
            pdf_url = best.get("pdf_url") or work.get("open_access", {}).get("oa_url")
            if not pdf_url or not pdf_url.lower().endswith(".pdf"):
                # try primary_location
                primary = work.get("primary_location") or {}
                pdf_url = primary.get("pdf_url") or pdf_url
            if not pdf_url:
                continue
            title = work.get("title") or ""
            yield (wid, pdf_url, title)


class DoajSource:
    """DOAJ articles API. Open access journals, public, no auth."""

    BASE = "https://doaj.org/api/search/articles"

    def __init__(self, session: requests.Session):
        self.s = session

    def candidates(self, query: str, per_page: int = 50,
                   page: int = 1) -> Iterator[tuple[str, str, str]]:
        q = urllib.parse.quote(query)
        url = f"{self.BASE}/{q}?pageSize={per_page}&page={page}"
        log(f"  doaj query: {query!r} page={page}")
        try:
            r = self.s.get(url, timeout=TIMEOUT,
                           headers={"User-Agent": USER_AGENT,
                                    "Accept": "application/json"})
        except requests.RequestException as exc:
            log(f"  doaj request error: {exc!r}")
            return
        if r.status_code != 200:
            log(f"  doaj non-200: {r.status_code}")
            return
        time.sleep(1)
        try:
            data = r.json()
        except json.JSONDecodeError:
            log(f"  doaj parse error")
            return
        for hit in data.get("results", []):
            aid = hit.get("id") or ""
            biblio = hit.get("bibjson") or {}
            title = (biblio.get("title") or "").strip()
            pdf_url = None
            for link in biblio.get("link", []):
                if (link.get("type") or "").lower() == "fulltext":
                    url_ = link.get("url") or ""
                    if url_.lower().endswith(".pdf"):
                        pdf_url = url_
                        break
            if not pdf_url or not aid:
                continue
            yield (aid, pdf_url, title)


class EuropePmcSource:
    """Europe PMC fulltext PDF API. Open access biomed/life sciences."""

    BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    def __init__(self, session: requests.Session):
        self.s = session

    def candidates(self, query: str, per_page: int = 50,
                   page: int = 1) -> Iterator[tuple[str, str, str]]:
        params = {
            "query": f"({query}) AND OPEN_ACCESS:Y AND HAS_FT:Y",
            "format": "json",
            "pageSize": str(per_page),
            "cursorMark": "*" if page == 1 else "",
            "resultType": "lite",
            "page": str(page),
        }
        url = f"{self.BASE}?{urllib.parse.urlencode(params)}"
        log(f"  europepmc query: {query!r} page={page}")
        try:
            r = self.s.get(url, timeout=TIMEOUT,
                           headers={"User-Agent": USER_AGENT})
        except requests.RequestException as exc:
            log(f"  europepmc request error: {exc!r}")
            return
        if r.status_code != 200:
            log(f"  europepmc non-200: {r.status_code}")
            return
        time.sleep(1)
        try:
            data = r.json()
        except json.JSONDecodeError:
            log(f"  europepmc parse error")
            return
        for hit in (data.get("resultList") or {}).get("result", []):
            pmcid = hit.get("pmcid") or ""
            pmid = hit.get("pmid") or ""
            sid = pmcid or pmid
            if not sid:
                continue
            title = (hit.get("title") or "").strip()
            if pmcid:
                pdf_url = f"https://europepmc.org/articles/{pmcid}?pdf=render"
            else:
                continue
            yield (sid, pdf_url, title)


# ---------------------------------------------------------------------------
# Download core
# ---------------------------------------------------------------------------

def download_pdf(s: requests.Session, url: str) -> bytes | None:
    try:
        r = s.get(url, timeout=TIMEOUT, allow_redirects=True,
                  headers={"User-Agent": USER_AGENT})
    except requests.RequestException as exc:
        log(f"  download error: {exc!r}")
        return None
    if r.status_code != 200:
        log(f"  download non-200: {r.status_code}")
        return None
    ct = (r.headers.get("Content-Type") or "").lower()
    body = r.content
    if not (ct.startswith("application/pdf") or body[:4] == b"%PDF"):
        log(f"  not a pdf (content-type={ct!r})")
        return None
    return body


def safe_filename(stem: str, ext: str = ".pdf") -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-")
    return (s or "doc")[:80] + ext


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

_log_fh = None


def log(msg: str) -> None:
    # Print ASCII-safely (Windows cp1252 console chokes on most unicode).
    try:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        sys.stdout.write(msg.encode("ascii", "replace").decode("ascii") + "\n")
        sys.stdout.flush()
    global _log_fh
    if _log_fh is None:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        _log_fh = LOG.open("a", encoding="utf-8")
    _log_fh.write(msg + "\n")
    _log_fh.flush()


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def fetch_for_cluster(s: requests.Session, cluster: dict, manifest: list[dict],
                      batch: int, batch_dir: Path,
                      max_to_take: int) -> int:
    """Try to acquire up to max_to_take new PDFs for this cluster, append them
    to the manifest list (in-memory) AND the CSV (on-disk, one row at a time).
    Returns count of PDFs added."""
    arxiv = ArxivSource(s)
    openalex = OpenAlexSource(s)
    doaj = DoajSource(s)
    europepmc = EuropePmcSource(s)
    taken = 0
    new_rows: list[dict] = []

    # Build effective source list: configured sources + automatic fallbacks
    # so that openalex 403 deserts also try doaj/europepmc/arxiv-keyword.
    sources = list(cluster["sources"])
    primary_query = next((q for sname, q in sources
                          if sname == "openalex"), None)
    if primary_query:
        existing = {(sn, q) for sn, q in sources}
        for fb in (("doaj", primary_query),
                   ("europepmc", primary_query),
                   ("arxiv_kw", primary_query)):
            if fb not in existing:
                sources.append(fb)

    # Cycle through this cluster's configured sources, pulling pages of candidates
    # until we either hit max_to_take or run out of fresh items.
    for src_name, query in sources:
        if taken >= max_to_take:
            break
        # paginate up to ~5 pages
        for page in range(0, 5):
            if taken >= max_to_take:
                break
            if src_name == "arxiv":
                candidates = list(arxiv.candidates(query, max_results=50,
                                                   start=page * 50))
            elif src_name == "arxiv_kw":
                # keyword-based arxiv search (works across categories)
                candidates = list(arxiv.candidates(f"all:{query}",
                                                   max_results=50,
                                                   start=page * 50))
            elif src_name == "openalex":
                candidates = list(openalex.candidates(query, per_page=50,
                                                      page=page + 1))
            elif src_name == "doaj":
                candidates = list(doaj.candidates(query, per_page=50,
                                                  page=page + 1))
            elif src_name == "europepmc":
                candidates = list(europepmc.candidates(query, per_page=50,
                                                       page=page + 1))
            else:
                log(f"  unknown source: {src_name}")
                break
            if not candidates:
                break
            for source_id, pdf_url, title in candidates:
                if taken >= max_to_take:
                    break
                if already_have(manifest, src_name, source_id):
                    continue
                log(f"\n  [{cluster['id']}] try {src_name}:{source_id} — {title[:80]!r}")
                pdf_bytes = download_pdf(s, pdf_url)
                if pdf_bytes is None:
                    continue
                sha = hashlib.sha256(pdf_bytes).hexdigest()
                if have_hash(manifest, sha) or have_hash(new_rows, sha):
                    log(f"  duplicate hash, skip")
                    continue
                ok, words = is_text_extractable(pdf_bytes)
                if not ok:
                    log(f"  text-extract fail (words={words}); skip")
                    continue
                if not looks_english(pdf_bytes):
                    log(f"  not english heuristic; skip")
                    continue
                # save
                fname = safe_filename(f"{src_name}-{source_id}")
                out = batch_dir / fname
                out.write_bytes(pdf_bytes)
                row = {
                    "pdf_path": str(out.relative_to(ROOT)).replace("\\", "/"),
                    "source": src_name,
                    "source_id": source_id,
                    "source_url": pdf_url,
                    "intended_research_field": cluster["field"],
                    "intended_cluster": cluster["id"],
                    "batch": str(batch),
                    "sha256": sha,
                    "word_count": str(words),
                    "downloaded_at": datetime.now(timezone.utc).isoformat(),
                }
                new_rows.append(row)
                manifest.append(row)
                append_manifest([row])
                taken += 1
                log(f"  KEEP {out.name} (words~{words}+, sha={sha[:8]})")
                time.sleep(0.5)
            time.sleep(2)
    return taken


def cmd_status(_args) -> int:
    rows = load_manifest()
    by_field: dict[str, int] = {}
    by_cluster: dict[str, int] = {}
    for r in rows:
        by_field[r["intended_research_field"]] = by_field.get(r["intended_research_field"], 0) + 1
        by_cluster[r["intended_cluster"]] = by_cluster.get(r["intended_cluster"], 0) + 1
    print(f"\nTotal downloaded: {len(rows)} / 421 target")
    print("\nBy research field:")
    for f, n in sorted(by_field.items(), key=lambda x: -x[1]):
        print(f"  {f:40s} {n}")
    print("\nBy cluster (current / total target):")
    for c in CLUSTERS:
        print(f"  {c['id']:25s} {by_cluster.get(c['id'], 0):3d} / {c['total']}")
    return 0


def cmd_download(args) -> int:
    batch = args.batch
    target = args.target
    only_cluster = args.cluster
    batch_dir = DOWNLOAD_ROOT / f"batch-{batch}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    log(f"\n=== Batch {batch}: target {target} PDFs into {batch_dir.relative_to(ROOT)} ===")

    manifest = load_manifest()
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})

    # Determine cluster need order. For each cluster, remaining = total - currently_have.
    needs: list[tuple[dict, int]] = []
    for c in CLUSTERS:
        if only_cluster and c["id"] != only_cluster:
            continue
        rem = c["total"] - cluster_progress(manifest, c["id"])
        if rem > 0:
            needs.append((c, rem))
    if not needs:
        log("Nothing to do — all clusters at target.")
        return 0

    # Round-robin: each pass take floor(target/len(needs)) per cluster, with leftovers.
    obtained = 0
    while obtained < target and needs:
        per = max(1, (target - obtained) // len(needs))
        # randomize cluster order so we don't always front-load IT
        random.shuffle(needs)
        new_needs: list[tuple[dict, int]] = []
        for cluster, rem in needs:
            if obtained >= target:
                break
            take = min(per, rem, target - obtained)
            log(f"\n--- cluster {cluster['id']} ({cluster['field']}) "
                f"want {take} (rem total {rem}) ---")
            got = fetch_for_cluster(s, cluster, manifest, batch, batch_dir, take)
            obtained += got
            new_rem = rem - got
            if new_rem > 0 and got > 0:
                new_needs.append((cluster, new_rem))
            elif got == 0:
                log(f"  cluster {cluster['id']} produced 0 — drop from this pass")
        needs = new_needs

    log(f"\n=== Batch {batch} done. obtained {obtained}/{target} ===")
    return 0 if obtained > 0 else 1


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")

    pd = sub.add_parser("download", help="Download a batch of PDFs (default cmd)")
    pd.add_argument("--batch", type=int, required=True)
    pd.add_argument("--target", type=int, default=20)
    pd.add_argument("--cluster", type=str, default=None,
                    help="Restrict to a single cluster id")
    pd.set_defaults(func=cmd_download)

    ps = sub.add_parser("status", help="Show progress vs total targets")
    ps.set_defaults(func=cmd_status)

    # Allow plain `--batch N` form (defaults to download).
    if argv and argv[0] not in ("download", "status"):
        argv = ["download", *argv]

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""
slr_search_arxiv.py - Step 2 ("Run the searches"), arXiv only, for the SLR
"Coding Agents in the Wild: A Systematic Review of Empirical Evidence on
Commercial AI Coding Agents".

Uses the official arXiv API: free, no key, no account.
Only dependency:  pip install requests

COMMANDS
  search   run the arXiv search and save a frozen snapshot
  verify   check a snapshot is complete and unchanged (hashes vs manifest)
  compare  compare two team members' snapshots paper by paper
  query    print the exact search string

TEAM WORKFLOW (2 members)
  Member A:  python slr_search_arxiv.py search --cutoff 2026-10-20 --run-by "Muzi"
             -> share the whole snapshot folder on the shared drive
  Member B:  python slr_search_arxiv.py verify  <snapshot folder>
  Optional:  both search with the SAME --cutoff, then
             python slr_search_arxiv.py compare <folder A> <folder B>

OUTPUT (in coding-agents-slr-2026/02-search-exports/<cutoff>_<name>/)
  arxiv_<cutoff>.raw.jsonl   untouched API records ("never edit")
  arxiv_<cutoff>.ris         import into Zotero / Rayyan
  arxiv_<cutoff>.csv         one row per paper (opens in Excel / Google Sheets)
  manifest.json              exact query, counts and SHA-256 of every file
plus one row per run in 02-search-exports/search_log.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

# ---------------------------------------------------------------------------
# PROTOCOL SETTINGS - change only together with a new protocol version
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = "1.0"
START_DATE = dt.date(2023, 1, 1)
CATEGORY = "cs.SE"

# A trailing * is expanded to singular + plural, because the arXiv API has no
# truncation: "coding agent*" -> "coding agent" OR "coding agents".
TOOL_TERMS = ["coding agent*", "AI coding assistant*", "agentic coding",
              "Claude Code", "GitHub Copilot", "Codex", "Cursor"]
CONTEXT_TERMS = ["empirical", "study", "survey", "pull request*", "developer*"]

API_URL = "https://export.arxiv.org/api/query"
PAGE_SIZE = 500            # records per request (arXiv allows up to 2,000)
PAUSE_SECONDS = 3.1        # arXiv asks for at least 3 seconds between requests
BROAD_WARNING = 3000       # guide: more than 3,000 hits means the string is too broad
DB_NAME = "arXiv (cs.SE)"

NS = {"a": "http://www.w3.org/2005/Atom",
      "os": "http://a9.com/-/spec/opensearch/1.1/",
      "arxiv": "http://arxiv.org/schemas/atom"}

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Rowan-SLR-coding-agents/2.0 (systematic literature review)"


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------
def expand(term: str) -> list[str]:
    return [term[:-1], term[:-1] + "s"] if term.endswith("*") else [term]


def field_group(terms) -> str:
    """Each term searched in title (ti) and abstract (abs)."""
    return "(" + " OR ".join(p for t in terms for x in expand(t)
                             for p in (f'ti:"{x}"', f'abs:"{x}"')) + ")"


def arxiv_query(cutoff: dt.date) -> str:
    return (f"{field_group(TOOL_TERMS)} AND {field_group(CONTEXT_TERMS)} "
            f"AND cat:{CATEGORY} "
            f"AND submittedDate:[{START_DATE:%Y%m%d}0000 TO {cutoff:%Y%m%d}2359]")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def http_get(params, tries=6, timeout=90):
    for attempt in range(tries):
        try:
            r = SESSION.get(API_URL, params=params, timeout=timeout)
        except requests.RequestException as e:
            wait = 2 ** (attempt + 2)
            print(f"    network error ({e.__class__.__name__}); retry in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            wait = int(r.headers.get("Retry-After", "0") or 0) or 2 ** (attempt + 2)
            print(f"    HTTP {r.status_code}; retry in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"arXiv returned HTTP {r.status_code}: {r.text[:600]}")
        return r
    raise RuntimeError(f"arXiv: gave up after {tries} attempts")


def clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def parse_entry(en) -> tuple[dict, dict]:
    abs_url = en.findtext("a:id", "", NS)
    full_id = abs_url.split("/abs/")[-1]
    # Version-free ID, so a v2 uploaded between two runs is still the same paper.
    arxiv_id = re.sub(r"v\d+$", "", full_id)
    published = en.findtext("a:published", "", NS)
    updated = en.findtext("a:updated", "", NS)
    cats = [c.get("term") for c in en.findall("a:category", NS)]
    primary = en.find("arxiv:primary_category", NS)
    rec = {
        "arxiv_id": arxiv_id,
        "version": full_id[len(arxiv_id):] or "v1",
        "title": clean(en.findtext("a:title", "", NS)),
        "authors": [clean(a.findtext("a:name", "", NS)) for a in en.findall("a:author", NS)],
        "year": published[:4],
        "submitted": published[:10],
        "updated": updated[:10],
        "primary_category": primary.get("term") if primary is not None else "",
        "categories": cats,
        "journal_ref": clean(en.findtext("arxiv:journal_ref", "", NS)),
        "doi": clean(en.findtext("arxiv:doi", "", NS)).lower(),
        "comment": clean(en.findtext("arxiv:comment", "", NS)),
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "abstract": clean(en.findtext("a:summary", "", NS)),
    }
    raw = {"atom_entry": ET.tostring(en, encoding="unicode")}
    return rec, raw


def run_search(cutoff: dt.date, count_only: bool, cap: int | None):
    """Returns (query, total reported by arXiv, list of (record, raw))."""
    q = arxiv_query(cutoff)
    items, total, start, empty_retries = [], 0, 0, 0
    while True:
        params = {"search_query": q, "start": start,
                  "max_results": 1 if count_only else PAGE_SIZE,
                  "sortBy": "submittedDate", "sortOrder": "ascending"}
        root = ET.fromstring(http_get(params).content)
        total = int(root.findtext("os:totalResults", "0", NS))
        entries = root.findall("a:entry", NS)
        if count_only:
            break
        if not entries and start < total and empty_retries < 3:
            empty_retries += 1        # arXiv sometimes returns an empty page; retry
            print("    empty page from arXiv; retrying")
            time.sleep(10)
            continue
        empty_retries = 0
        items += [parse_entry(en) for en in entries]
        start += PAGE_SIZE
        print(f"    fetched {min(start, total)}/{total}")
        if not entries or start >= total or (cap is not None and len(items) >= cap):
            break
        time.sleep(PAUSE_SECONDS)
    return q, total, items


def finalize(items, cutoff: dt.date):
    """Keep the shared date window, drop repeats, sort by arXiv ID."""
    kept, seen, dropped = [], set(), 0
    lo, hi = START_DATE.isoformat(), cutoff.isoformat()
    for rec, raw in items:
        if not (lo <= rec["submitted"] <= hi):
            dropped += 1
            continue
        if rec["arxiv_id"] in seen:          # same paper returned on two pages
            continue
        seen.add(rec["arxiv_id"])
        kept.append((rec, raw))
    kept.sort(key=lambda p: p[0]["arxiv_id"])
    return kept, dropped


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
CSV_COLS = ["arxiv_id", "version", "title", "authors", "year", "submitted", "updated",
            "primary_category", "categories", "journal_ref", "doi", "comment",
            "url", "pdf_url", "abstract"]
LOG_COLS = ["database", "date_searched", "cutoff", "exact_string", "filters_applied",
            "results_reported", "dropped_outside_window", "records_exported", "run_by",
            "run_folder", "protocol_version", "notes"]


def to_ris(r: dict) -> str:
    lines = ["TY  - UNPB", f"TI  - {r['title']}"]
    lines += [f"AU  - {a}" for a in r["authors"] if a]
    lines += [f"PY  - {r['year']}", f"DA  - {r['submitted']}",
              f"T2  - {r['journal_ref'] or 'arXiv'}", f"AB  - {r['abstract']}"]
    if r["doi"]:
        lines.append(f"DO  - {r['doi']}")
    lines += [f"UR  - {r['url']}", f"L1  - {r['pdf_url']}", f"DB  - {DB_NAME}",
              f"AN  - arXiv:{r['arxiv_id']}",
              f"N1  - arXiv categories: {', '.join(r['categories'])}; version {r['version']}",
              "ER  - "]
    return "\n".join(lines) + "\n\n"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def write_snapshot(items, run_dir: Path, stamp: str) -> list[Path]:
    raw_p = run_dir / f"arxiv_{stamp}.raw.jsonl"
    ris_p = run_dir / f"arxiv_{stamp}.ris"
    csv_p = run_dir / f"arxiv_{stamp}.csv"
    with raw_p.open("w", encoding="utf-8", newline="\n") as f:
        for _, raw in items:
            f.write(json.dumps(raw, ensure_ascii=False, sort_keys=True) + "\n")
    with ris_p.open("w", encoding="utf-8", newline="\n") as f:
        f.writelines(to_ris(r) for r, _ in items)
    with csv_p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        for r, _ in items:
            w.writerow({**r, "authors": "; ".join(r["authors"]),
                        "categories": "; ".join(r["categories"])})
    return [raw_p, ris_p, csv_p]


def append_log(log_path: Path, row: dict) -> None:
    new = not log_path.exists()
    with log_path.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLS)
        if new:
            w.writeheader()
        w.writerow(row)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unknown"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_search(args) -> int:
    cutoff = args.cutoff
    if cutoff > dt.date.today():
        sys.exit(f"--cutoff {cutoff} is in the future")
    if cutoff < START_DATE:
        sys.exit(f"--cutoff must be on or after {START_DATE}")
    stamp = cutoff.isoformat()
    run_folder = f"{stamp}_{slug(args.run_by)}"
    run_dir = args.out / run_folder
    if not args.count_only:
        if run_dir.exists() and any(run_dir.iterdir()) and not args.force:
            sys.exit(f"{run_dir} already has a snapshot; snapshots are never overwritten "
                     f"(use --force only to replace a failed run)")
        run_dir.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    when = dt.datetime.now().astimezone().isoformat(timespec="minutes")
    filters = (f"title+abstract; cat:{CATEGORY} incl. cross-lists; "
               f"submittedDate {START_DATE} to {stamp}")

    print(f"== {DB_NAME} ==  window {START_DATE} to {stamp}")
    try:
        query, total, items = run_search(cutoff, args.count_only, args.max)
    except Exception as e:
        print(f"FAILED: {e}")
        return 1

    log_row = {"database": DB_NAME, "date_searched": when, "cutoff": stamp,
               "exact_string": query, "filters_applied": filters, "results_reported": total,
               "run_by": args.run_by, "protocol_version": PROTOCOL_VERSION}

    if args.count_only:
        append_log(args.out / "search_log.csv", {**log_row, "dropped_outside_window": "",
                   "records_exported": 0, "run_folder": "",
                   "notes": "COUNT-ONLY trial (nothing exported)"})
        flag = "  <-- over 3,000: string too broad, tell the lead" if total > BROAD_WARNING else ""
        print(f"\narXiv reports {total} papers{flag}")
        return 0

    kept, dropped = finalize(items, cutoff)
    notes = f"--max {args.max} cap applied (TEST RUN, incomplete)" if args.max else ""
    files = write_snapshot(kept, run_dir, stamp)
    manifest = {
        "review": "Coding Agents in the Wild (SLR)",
        "database": DB_NAME,
        "protocol_version": PROTOCOL_VERSION,
        "script_sha256": sha256_file(Path(__file__)),
        "run_by": args.run_by, "run_at": when,
        "date_window": [START_DATE.isoformat(), stamp],
        "query": query, "filters": filters,
        "results_reported": total, "dropped_outside_window": dropped,
        "records_exported": len(kept), "max_cap": args.max,
        "files": {p.name: sha256_file(p) for p in files},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    append_log(args.out / "search_log.csv", {**log_row, "dropped_outside_window": dropped,
               "records_exported": len(kept), "run_folder": run_folder, "notes": notes})

    print("\n================ SUMMARY ================")
    flag = "  <-- over 3,000: string too broad, tell the lead" if total > BROAD_WARNING else ""
    print(f"arXiv reported {total}, exported {len(kept)} "
          f"(dropped outside window: {dropped}){flag}")
    if len(items) < total and not args.max:
        print("WARNING: fewer records fetched than arXiv reported; arXiv may have returned "
              "empty pages. Rerun later with --force before using this snapshot.")
    print(f"Snapshot: {run_dir}  (share this folder; teammate runs 'verify')")
    print(f"Search log: {args.out / 'search_log.csv'}")
    return 0


def cmd_verify(args) -> int:
    mpath = args.folder / "manifest.json"
    if not mpath.exists():
        sys.exit(f"no manifest.json in {args.folder}")
    m = json.loads(mpath.read_text(encoding="utf-8"))
    print(f"Snapshot by {m['run_by']} at {m['run_at']}, window {m['date_window'][0]} to "
          f"{m['date_window'][1]}, {m['records_exported']} papers, protocol {m['protocol_version']}")
    bad = 0
    for name, digest in m["files"].items():
        p = args.folder / name
        status = "MISSING" if not p.exists() else ("ok" if sha256_file(p) == digest else "CHANGED")
        bad += status != "ok"
        print(f"  {status:<8} {name}")
    if m["script_sha256"] != sha256_file(Path(__file__)):
        print("  note: your copy of the script differs from the one that made this snapshot")
    if m.get("max_cap"):
        print("  note: this was a capped TEST run, not a complete search")
    print("VERIFIED: identical to the original run." if not bad
          else f"NOT VERIFIED: {bad} file(s) missing or changed.")
    return 1 if bad else 0


def load_snapshot(folder: Path):
    mpath = folder / "manifest.json"
    if not mpath.exists():
        sys.exit(f"no manifest.json in {folder} (is this a snapshot folder?)")
    m = json.loads(mpath.read_text(encoding="utf-8"))
    csv_name = next(n for n in m["files"] if n.endswith(".csv"))
    with (folder / csv_name).open(encoding="utf-8-sig", newline="") as f:
        return m, {row["arxiv_id"]: row for row in csv.DictReader(f)}


def cmd_compare(args) -> int:
    ma, a = load_snapshot(args.folder_a)
    mb, b = load_snapshot(args.folder_b)
    if ma["date_window"] != mb["date_window"]:
        print(f"WARNING: different date windows {ma['date_window']} vs {mb['date_window']}; "
              f"rerun with the same --cutoff")
    only_a, only_b = sorted(a.keys() - b.keys()), sorted(b.keys() - a.keys())
    print(f"A ({ma['run_by']}): {len(a)}   B ({mb['run_by']}): {len(b)}   "
          f"in both: {len(a.keys() & b.keys())}   only A: {len(only_a)}   only B: {len(only_b)}")
    if not only_a and not only_b:
        print("IDENTICAL: both runs found the same papers.")
        return 0
    report = args.folder_b.parent / f"compare_{args.folder_a.name}_vs_{args.folder_b.name}.csv"
    with report.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["found_in", "arxiv_id", "title", "submitted", "url"])
        w.writeheader()
        for label, ids, src in (("only A", only_a, a), ("only B", only_b, b)):
            for i in ids:
                w.writerow({"found_in": label, "arxiv_id": i, "title": src[i]["title"],
                            "submitted": src[i]["submitted"], "url": src[i]["url"]})
    print(f"DIFFERENT: see {report}\nAgree on ONE snapshot for screening (usually the earlier, "
          f"verified one) and record the decision in the search log.")
    return 1


def cmd_query(args) -> int:
    print(arxiv_query(args.cutoff))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="SLR Step 2: reproducible arXiv search (no API key).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="run the arXiv search and save a frozen snapshot")
    s.add_argument("--cutoff", type=dt.date.fromisoformat, required=True,
                   help="shared cutoff date YYYY-MM-DD; both members must use the same one")
    s.add_argument("--run-by", required=True, help="who ran it, e.g. 'Muzi'")
    s.add_argument("--out", type=Path, default=Path("coding-agents-slr-2026/02-search-exports"))
    s.add_argument("--count-only", action="store_true", help="only fetch the hit count")
    s.add_argument("--max", type=int, default=None, help="cap records (testing only)")
    s.add_argument("--force", action="store_true", help="replace an existing snapshot folder")
    s.set_defaults(func=cmd_search)

    v = sub.add_parser("verify", help="check a snapshot folder against its manifest")
    v.add_argument("folder", type=Path)
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("compare", help="compare two members' snapshot folders")
    c.add_argument("folder_a", type=Path)
    c.add_argument("folder_b", type=Path)
    c.set_defaults(func=cmd_compare)

    q = sub.add_parser("query", help="print the exact search string")
    q.add_argument("--cutoff", type=dt.date.fromisoformat, default=dt.date.today())
    q.set_defaults(func=cmd_query)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
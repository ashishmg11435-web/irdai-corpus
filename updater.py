"""
updater.py -- Incremental corpus update engine for IRDAI documents.

This module handles:
  1. Diffing newly crawled PDF links against the existing metadata.csv
  2. Detecting NEW documents (URLs not previously downloaded)
  3. Detecting UPDATED documents (same title, higher Liferay ?version=)
  4. Marking superseded documents in metadata.csv
  5. Logging every change to changelog.csv

Usage
-----
    from updater import run_update
    run_update(max_downloads=10)

    # Or via CLI:
    python main.py --update --max-downloads 10
"""

import csv
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from utils import extract_version

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_METADATA_PATH = "metadata.csv"
DEFAULT_CHANGELOG_PATH = "changelog.csv"

CHANGELOG_COLUMNS = [
    "run_date",
    "action",       # ADDED, UPDATED, SUPERSEDED
    "filename",
    "doc_type",
    "tier",
    "url",
    "reason",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_base_title(url: str, link_text: str = "") -> str:
    """
    Extract a normalized base title for supersession comparison.

    Two documents are considered versions of the same regulation if their
    base titles match (ignoring version numbers, amendment markers, etc.).

    Priority: link_text > URL path filename segment.
    """
    title = link_text.strip()

    if not title:
        # Extract from URL path: last .pdf segment
        parts = url.split("/")
        for part in reversed(parts):
            decoded = unquote(part).replace("+", " ")
            if decoded.lower().endswith(".pdf"):
                title = decoded[:-4]  # strip .pdf
                break

    if not title:
        return ""

    # Normalize: lowercase, strip amendment/version markers, extra whitespace
    title = title.lower()
    title = re.sub(r"\(amendment\)", "", title)
    title = re.sub(r"\([\d]+(?:st|nd|rd|th)\s+amendment\)", "", title)
    title = re.sub(r"version\s*[\d.]+", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _load_metadata_rows(metadata_path: str) -> list[dict]:
    """Load all rows from metadata.csv as a list of dicts."""
    if not os.path.exists(metadata_path) or os.path.getsize(metadata_path) == 0:
        return []

    rows = []
    with open(metadata_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def _write_metadata_rows(metadata_path: str, rows: list[dict], fieldnames: list[str]) -> None:
    """Overwrite metadata.csv with the given rows."""
    with open(metadata_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _append_changelog(changelog_path: str, entries: list[dict]) -> None:
    """Append entries to changelog.csv (creates header if new)."""
    if not entries:
        return

    file_exists = os.path.exists(changelog_path) and os.path.getsize(changelog_path) > 0

    with open(changelog_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CHANGELOG_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(entries)


# ---------------------------------------------------------------------------
# Core: Diff current links against existing metadata
# ---------------------------------------------------------------------------

def diff_corpus(
    current_links: list[dict],
    metadata_path: str = DEFAULT_METADATA_PATH,
) -> dict:
    """
    Compare newly crawled PDF links against the existing metadata.

    Parameters
    ----------
    current_links : list[dict]
        PDF link dicts from scraper.crawl() — each has: url, doc_type, link_text, source_page
    metadata_path : str
        Path to the existing metadata CSV.

    Returns
    -------
    dict with keys:
        new_links    : list[dict]  -- URLs not in metadata (never downloaded)
        updated_links: list[dict]  -- same base title, higher version
        unchanged    : int         -- already up to date
    """
    existing_rows = _load_metadata_rows(metadata_path)
    known_urls = {row["url"] for row in existing_rows if row.get("url")}

    # Build a map: base_title -> (version, url, row_index) for supersession
    title_version_map: dict[str, tuple[str, str, int]] = {}
    for idx, row in enumerate(existing_rows):
        base = _extract_base_title(row.get("url", ""), row.get("link_text", ""))
        if base:
            ver = row.get("liferay_version", "0.0")
            title_version_map[base] = (ver, row.get("url", ""), idx)

    new_links: list[dict] = []
    updated_links: list[dict] = []
    unchanged = 0

    for link in current_links:
        url = link["url"]

        if url in known_urls:
            unchanged += 1
            continue

        # Check if this is an update to an existing document
        base_title = _extract_base_title(url, link.get("link_text", ""))
        new_version, _ = extract_version(url)

        if base_title and base_title in title_version_map:
            old_version, old_url, old_idx = title_version_map[base_title]
            if new_version > old_version:
                link["_replaces_url"] = old_url
                link["_replaces_idx"] = old_idx
                link["_old_version"] = old_version
                updated_links.append(link)
                continue

        new_links.append(link)

    return {
        "new_links": new_links,
        "updated_links": updated_links,
        "unchanged": unchanged,
    }


# ---------------------------------------------------------------------------
# Core: Mark superseded documents
# ---------------------------------------------------------------------------

def detect_superseded(metadata_path: str = DEFAULT_METADATA_PATH) -> int:
    """
    Scan metadata.csv and mark older versions of the same document as superseded.

    Two documents are considered versions of the same regulation if their
    base titles match. The one with the lower Liferay version is marked
    is_superseded=true.

    Returns the number of documents newly marked as superseded.
    """
    rows = _load_metadata_rows(metadata_path)
    if not rows:
        return 0

    # Determine the fieldnames from the first row
    fieldnames = list(rows[0].keys())

    # Group by base title
    title_groups: dict[str, list[int]] = {}
    for idx, row in enumerate(rows):
        base = _extract_base_title(row.get("url", ""), row.get("link_text", ""))
        if base:
            title_groups.setdefault(base, []).append(idx)

    marked = 0
    for base_title, indices in title_groups.items():
        if len(indices) < 2:
            continue

        # Find the highest version in this group
        best_ver = "0.0"
        best_idx = indices[0]
        for idx in indices:
            ver = rows[idx].get("liferay_version", "0.0")
            if ver > best_ver:
                best_ver = ver
                best_idx = idx

        # Mark all others as superseded
        for idx in indices:
            if idx != best_idx and rows[idx].get("is_superseded") != "true":
                rows[idx]["is_superseded"] = "true"
                marked += 1

    if marked > 0:
        _write_metadata_rows(metadata_path, rows, fieldnames)

    return marked


# ---------------------------------------------------------------------------
# Main: run_update
# ---------------------------------------------------------------------------

def run_update(
    max_downloads: int = 10,
    max_pages: int = 200,
    dest_dir: str = "downloaded_pdfs",
    metadata_path: str = DEFAULT_METADATA_PATH,
    changelog_path: str = DEFAULT_CHANGELOG_PATH,
    force: bool = False,
) -> dict:
    """
    Run an incremental corpus update.

    Steps:
      1. Crawl all seed pages + pagination to get current PDF links
      2. Diff against metadata.csv to find new/updated documents
      3. Download new documents (capped at max_downloads)
      4. Mark superseded documents
      5. Log changes to changelog.csv

    Parameters
    ----------
    max_downloads  : cap on new downloads
    max_pages      : max HTML pages to crawl
    dest_dir       : directory for downloaded PDFs
    metadata_path  : path to metadata CSV
    changelog_path : path to changelog CSV
    force          : if True, re-download everything (ignore metadata)

    Returns
    -------
    dict with summary stats: new_found, updated_found, downloaded, superseded
    """
    from scraper import crawl
    from downloader import run_downloader

    run_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changelog_entries: list[dict] = []

    # -- Step 1: Crawl --
    print("[Updater] Step 1/4: Crawling IRDAI website...")
    pdf_links, pages_visited = crawl(max_pages=max_pages)
    print(f"[Updater] Found {len(pdf_links)} PDF link(s) across {pages_visited} page(s).\n")

    if force:
        print("[Updater] FORCE mode: downloading all links (ignoring metadata).")
        to_download = pdf_links
        diff_result = {"new_links": pdf_links, "updated_links": [], "unchanged": 0}
    else:
        # -- Step 2: Diff --
        print("[Updater] Step 2/4: Diffing against existing corpus...")
        diff_result = diff_corpus(pdf_links, metadata_path)
        n_new = len(diff_result["new_links"])
        n_upd = len(diff_result["updated_links"])
        n_unch = diff_result["unchanged"]
        print(f"[Updater] {n_new} new | {n_upd} updated | {n_unch} unchanged\n")

        to_download = diff_result["new_links"] + diff_result["updated_links"]

        if not to_download:
            print("[Updater] Corpus is already up to date. No downloads needed.")

    # -- Step 3: Download --
    if to_download:
        print(f"[Updater] Step 3/4: Downloading (cap = {max_downloads})...")
        n_downloaded = run_downloader(
            pdf_links=to_download,
            dest_dir=dest_dir,
            metadata_path=metadata_path,
            max_downloads=max_downloads,
        )

        # Log new downloads to changelog
        # (We read back the last N rows of metadata to get filenames)
        existing = _load_metadata_rows(metadata_path)
        recent = existing[-n_downloaded:] if n_downloaded > 0 else []
        for row in recent:
            action = "ADDED"
            reason = "New document"

            # Check if this was an update
            for upd in diff_result["updated_links"]:
                if row.get("url") == upd["url"]:
                    action = "UPDATED"
                    reason = f"version {upd.get('_old_version', '?')} -> {row.get('liferay_version', '?')}"
                    break

            changelog_entries.append({
                "run_date": run_date,
                "action": action,
                "filename": row.get("filename", ""),
                "doc_type": row.get("doc_type", ""),
                "tier": row.get("tier", ""),
                "url": row.get("url", ""),
                "reason": reason,
            })
    else:
        n_downloaded = 0

    # -- Step 4: Supersession detection --
    print("\n[Updater] Step 4/4: Detecting superseded documents...")
    n_superseded = detect_superseded(metadata_path)
    if n_superseded > 0:
        print(f"[Updater] Marked {n_superseded} document(s) as superseded.")

        # Log supersessions to changelog
        rows = _load_metadata_rows(metadata_path)
        for row in rows:
            if row.get("is_superseded") == "true":
                # Only log if not already in a previous changelog entry
                changelog_entries.append({
                    "run_date": run_date,
                    "action": "SUPERSEDED",
                    "filename": row.get("filename", ""),
                    "doc_type": row.get("doc_type", ""),
                    "tier": row.get("tier", ""),
                    "url": row.get("url", ""),
                    "reason": "Newer version exists in corpus",
                })
    else:
        print("[Updater] No superseded documents found.")

    # Write changelog
    _append_changelog(changelog_path, changelog_entries)
    if changelog_entries:
        print(f"\n[Updater] {len(changelog_entries)} change(s) logged to {changelog_path}")

    summary = {
        "new_found": len(diff_result["new_links"]),
        "updated_found": len(diff_result["updated_links"]),
        "downloaded": n_downloaded,
        "superseded": n_superseded,
    }

    print(f"\n[Updater] Summary: {summary}")
    return summary


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    run_update(max_downloads=10, max_pages=30)

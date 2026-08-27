"""
downloader.py -- Safe PDF downloader with streaming verification and metadata.

For each discovered PDF link the downloader:
  1. Opens a streaming GET request (HEAD is blocked on IRDAI's Liferay server).
  2. Validates the response is actually a PDF (Content-Type + %PDF magic bytes).
  3. If valid, streams the file to disk with a tqdm progress bar.
  4. Computes an MD5 hash and checks for duplicate content.
  5. Classifies the document tier (gold/silver) and records Liferay version info.
  6. Appends a metadata row to metadata.csv.
"""

import csv
import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from tqdm import tqdm

from utils import (
    classify_tier,
    compute_file_hash,
    create_session,
    extract_version,
    get_document_type,
    has_non_english_filename_keyword,
    is_predominantly_non_english,
    sanitize_filename,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

METADATA_COLUMNS: list[str] = [
    "filename",
    "doc_type",
    "tier",
    "url",
    "source_page",
    "link_text",
    "liferay_version",
    "liferay_timestamp",
    "date_downloaded",
    "file_size_kb",
    "file_hash_md5",
    "is_superseded",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _derive_filename(url: str, link_text: str = "") -> str:
    """
    Derive a safe, human-readable filename from the URL or the anchor text.

    Priority:
      1. Anchor text (cleaner, more descriptive)
      2. Last .pdf segment from the URL path
      3. Timestamp fallback
    """
    if link_text:
        base = sanitize_filename(link_text)
        if base:
            if not base.lower().endswith(".pdf"):
                base += ".pdf"
            return base

    # Try to extract filename from the URL path
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]

    for part in reversed(path_parts):
        decoded = unquote(part)
        if decoded.lower().endswith(".pdf"):
            return sanitize_filename(decoded)

    # Last resort
    return f"irdai_document_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"


def _is_important(tier: str, doc_type: str, url: str, link_text: str, timestamp: int) -> bool:
    """
    Determine if a document is important enough for the production RAG.

    Strategy (tiered cutoffs):
      - Gold tier (Regulations, Master Circulars, Acts, Rules, Guidelines):
            Always important — foundational law that stays in force until repealed.
      - Silver tier — cutoff depends on document type:
            Circulars, Notifications  → keep if >= 2020  (buffer for unconsolidated)
            Orders                    → keep if >= 2023  (case-specific, only recent)
            Exposure Drafts           → keep if >= 2024  (only current/upcoming)
            Others (FAQ, Discussion)  → keep if >= 2023
    """
    if tier == "gold":
        return True

    # Determine the year cutoff based on document type
    _CUTOFF_MAP = {
        "Circular": 2020,
        "Notification": 2020,
        "Order": 2023,
        "Exposure Draft": 2024,
    }
    cutoff_year = _CUTOFF_MAP.get(doc_type, 2023)

    # Try to extract the year from the Liferay timestamp
    year = 0
    if timestamp > 0:
        year = datetime.fromtimestamp(timestamp).year

    if year == 0:
        # Fallback to regex on link text and URL
        import re
        match = re.search(r"20\d{2}", link_text + " " + url)
        if match:
            year = int(match.group(0))

    # If we can't determine the year, keep the document to be safe
    if year > 0 and year < cutoff_year:
        return False

    return True


def _load_metadata(metadata_path: str) -> tuple[set[str], set[str]]:
    """
    Read existing metadata CSV and return:
      - known_urls  : set of already-downloaded URLs (skip these)
      - known_hashes: set of MD5 hashes (detect renamed duplicates)
    """
    known_urls: set[str] = set()
    known_hashes: set[str] = set()

    if not os.path.exists(metadata_path):
        return known_urls, known_hashes

    try:
        with open(metadata_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("url"):
                    known_urls.add(row["url"])
                if row.get("file_hash_md5"):
                    known_hashes.add(row["file_hash_md5"])
    except Exception:
        pass

    return known_urls, known_hashes


def _append_metadata(metadata_path: str, row: dict) -> None:
    """Append a single metadata row to the CSV file (creates header if new)."""
    file_exists = os.path.exists(metadata_path) and os.path.getsize(metadata_path) > 0

    with open(metadata_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=METADATA_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


# ---------------------------------------------------------------------------
# Core: verify + download in a single streaming GET
# ---------------------------------------------------------------------------

def verify_and_download(
    url: str,
    dest_dir: str,
    link_info: dict,
    session,
    known_hashes: set[str],
) -> dict | None:
    """
    Verify that *url* is a PDF and download it in one streaming GET request.

    IRDAI's Liferay blocks HEAD requests (403), so we open a streaming GET,
    check the Content-Type and first 4 bytes (%PDF magic), then continue
    streaming the body to disk if valid.

    Returns a metadata dict on success, or None if skipped/failed.
    """
    filename = _derive_filename(url, link_info.get("link_text", ""))
    filepath = Path(dest_dir) / filename

    # Avoid clobbering an existing file with the same name
    if filepath.exists():
        counter = 1
        stem = filepath.stem
        suffix = filepath.suffix
        while filepath.exists():
            filepath = Path(dest_dir) / f"{stem}_{counter}{suffix}"
            counter += 1

    try:
        resp = session.get(url, stream=True, timeout=60)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("GET failed for %s: %s", url, exc)
        return None

    # --- Verification step (before writing to disk) ---
    content_type = resp.headers.get("Content-Type", "").lower()
    is_pdf_type = "application/pdf" in content_type or "octet-stream" in content_type

    if not is_pdf_type:
        # Check magic bytes as a fallback
        first_chunk = next(resp.iter_content(4), b"")
        if first_chunk != b"%PDF":
            logger.info("Not a PDF (type=%s, magic=%s): %s", content_type, first_chunk[:4], url)
            resp.close()
            return None
        # It IS a PDF despite the wrong Content-Type -- write the magic bytes first
        initial_data = first_chunk
    else:
        initial_data = b""

    # --- Download to disk ---
    total_bytes = int(resp.headers.get("Content-Length", 0))
    chunk_size = 8192

    try:
        with (
            open(filepath, "wb") as f,
            tqdm(
                total=total_bytes or None,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=filename[:50],
                leave=False,
            ) as bar,
        ):
            # Write any initial data from verification
            if initial_data:
                f.write(initial_data)
                bar.update(len(initial_data))

            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    bar.update(len(chunk))
    except Exception as exc:
        logger.error("Download write failed for %s: %s", url, exc)
        if filepath.exists():
            filepath.unlink()
        resp.close()
        return None
    finally:
        resp.close()

    # --- Hash-based deduplication ---
    file_hash = compute_file_hash(str(filepath))
    if file_hash in known_hashes:
        logger.info("Duplicate content (hash match) -- removing %s", filename)
        filepath.unlink()
        return None
    known_hashes.add(file_hash)

    # --- Build metadata row ---
    file_size_kb = round(filepath.stat().st_size / 1024, 1)
    doc_type = link_info.get("doc_type", get_document_type(url, link_info.get("source_page", "")))
    tier = classify_tier(doc_type)
    version, timestamp = extract_version(url)

    metadata_row = {
        "filename": filepath.name,
        "doc_type": doc_type,
        "tier": tier,
        "url": url,
        "source_page": link_info.get("source_page", ""),
        "link_text": link_info.get("link_text", ""),
        "liferay_version": version,
        "liferay_timestamp": timestamp,
        "date_downloaded": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file_size_kb": file_size_kb,
        "file_hash_md5": file_hash,
        "is_superseded": "false",
    }

    return metadata_row


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_downloader(
    pdf_links: list[dict],
    dest_dir: str = "downloaded_pdfs",
    metadata_path: str = "metadata.csv",
    max_downloads: int = 10,
) -> int:
    """
    Download up to *max_downloads* PDFs from *pdf_links*.

    Parameters
    ----------
    pdf_links     : list of dicts from scraper.crawl()
    dest_dir      : directory to store downloaded PDFs
    metadata_path : path to the metadata CSV file
    max_downloads : cap on new downloads in this run

    Returns
    -------
    int : number of files successfully downloaded
    """
    os.makedirs(dest_dir, exist_ok=True)
    session = create_session()

    known_urls, known_hashes = _load_metadata(metadata_path)
    print(f"[Downloader] Found {len(known_urls)} previously downloaded URL(s) in metadata.")

    downloaded = 0
    skipped_dup = 0
    skipped_invalid = 0

    for link in pdf_links:
        if downloaded >= max_downloads:
            print(f"[Downloader] Reached cap of {max_downloads} download(s). Stopping.")
            break

        url = link["url"]

        # Skip already-downloaded URLs
        if url in known_urls:
            skipped_dup += 1
            continue

        # Safety net: never download non-English documents, even if a stale
        # pdf_links list (crawled before the language fix) slips past the scraper.
        # Bilingual "Hindi _ English" docs pass — their link_text is English and
        # their URL has no language keyword.
        if has_non_english_filename_keyword(urlparse(url).path) or \
                is_predominantly_non_english(link.get("link_text", "")):
            skipped_invalid += 1
            preview = link.get("link_text", "")[:55] or url.split("/")[-1][:55]
            print(f"  [skip non-English] {preview}")
            continue

        title_preview = link.get("link_text", "")[:55] or url.split("/")[-1][:55]
        
        # Pre-check tier and importance to avoid wasteful GET requests
        doc_type = link.get("doc_type", get_document_type(url, link.get("source_page", "")))
        tier = classify_tier(doc_type)
        _, timestamp = extract_version(url)
        
        if not _is_important(tier, doc_type, url, link.get("link_text", ""), timestamp):
            skipped_invalid += 1
            print(f"  [{doc_type}] {title_preview}")
            print(f"       -> SKIP ({doc_type}, below year cutoff for silver tier)")
            continue

        print(f"  [{doc_type}] {title_preview}")

        metadata_row = verify_and_download(
            url=url,
            dest_dir=dest_dir,
            link_info=link,
            session=session,
            known_hashes=known_hashes,
        )

        if metadata_row:
            _append_metadata(metadata_path, metadata_row)
            known_urls.add(url)
            downloaded += 1
            print(
                f"       -> OK: {metadata_row['filename'][:50]} "
                f"({metadata_row['file_size_kb']} KB, {metadata_row['tier']})"
            )
        else:
            skipped_invalid += 1
            print(f"       -> SKIP (not a valid PDF or duplicate content)")

    print(
        f"\n[Downloader] Summary: {downloaded} downloaded | "
        f"{skipped_dup} already had | {skipped_invalid} invalid/duplicate."
    )
    return downloaded


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    from scraper import crawl

    print("Running scraper to discover PDF links...")
    pdf_links, _ = crawl(max_pages=15)

    print(f"\nRunning downloader (cap = 10)...")
    run_downloader(pdf_links, max_downloads=10)

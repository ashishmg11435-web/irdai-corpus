"""
main.py -- IRDAI Document Crawler Pipeline Orchestrator.

This is the single entry point that wires together:
  scraper   -> discovers PDF links on the IRDAI website
  downloader -> downloads verified PDFs (capped at MAX_DOWNLOADS)
  cleaner    -> strips Hindi pages and blank pages from downloaded PDFs
  updater    -> incremental corpus updates with diff + supersession detection

Usage
-----
    # Full pipeline (crawl -> download -> clean)
    python main.py

    # Individual stages
    python main.py --crawl-only
    python main.py --download-only
    python main.py --clean-only

    # Incremental update (production mode)
    python main.py --update
    python main.py --update --force   # force re-download everything

    # Adjust crawl breadth and download cap
    python main.py --max-pages 50 --max-downloads 10
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Directories & paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
DOWNLOADED_PDFS_DIR = str(BASE_DIR / "downloaded_pdfs")
CLEANED_PDFS_DIR = str(BASE_DIR / "cleaned_pdfs")
METADATA_CSV = str(BASE_DIR / "metadata.csv")
CHANGELOG_CSV = str(BASE_DIR / "changelog.csv")
PDF_LINKS_CACHE = str(BASE_DIR / "pdf_links_cache.json")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Stage functions
# ---------------------------------------------------------------------------

def stage_crawl(max_pages: int) -> list[dict]:
    """
    Run the focused crawler and return a list of discovered PDF link dicts.
    Also saves results to a JSON cache so the download stage can be run
    independently without re-crawling.
    """
    from scraper import crawl

    print_banner("STAGE 1 -- CRAWL")
    t0 = time.time()

    pdf_links, pages_visited = crawl(max_pages=max_pages)

    elapsed = time.time() - t0
    print(
        f"\n[Crawl] Completed in {elapsed:.1f}s | "
        f"Pages visited: {pages_visited} | "
        f"PDF links found: {len(pdf_links)}\n"
    )

    # Save cache for later stages
    with open(PDF_LINKS_CACHE, "w", encoding="utf-8") as f:
        json.dump(pdf_links, f, ensure_ascii=False, indent=2)
    print(f"[Crawl] PDF links cached to: {PDF_LINKS_CACHE}\n")

    return pdf_links


def stage_download(pdf_links: list[dict], max_downloads: int) -> int:
    """
    Download verified PDFs up to the specified cap.
    Loads from the JSON cache if *pdf_links* is empty.
    """
    from downloader import run_downloader

    print_banner("STAGE 2 -- DOWNLOAD")

    if not pdf_links:
        if os.path.exists(PDF_LINKS_CACHE):
            print(f"[Download] Loading PDF links from cache: {PDF_LINKS_CACHE}")
            with open(PDF_LINKS_CACHE, encoding="utf-8") as f:
                pdf_links = json.load(f)
        else:
            print("[Download] No PDF links available and no cache found.")
            print("           Run with --crawl-only first, then retry.")
            return 0

    t0 = time.time()
    n_downloaded = run_downloader(
        pdf_links=pdf_links,
        dest_dir=DOWNLOADED_PDFS_DIR,
        metadata_path=METADATA_CSV,
        max_downloads=max_downloads,
    )
    elapsed = time.time() - t0
    print(f"\n[Download] Completed in {elapsed:.1f}s | Files saved: {n_downloaded}\n")
    return n_downloaded


def stage_clean() -> None:
    """Strip Hindi and blank pages from all PDFs in downloaded_pdfs/."""
    from cleaner import clean_all

    print_banner("STAGE 3 -- CLEAN")
    t0 = time.time()
    clean_all(input_dir=DOWNLOADED_PDFS_DIR, output_dir=CLEANED_PDFS_DIR)
    elapsed = time.time() - t0
    print(f"[Clean] Completed in {elapsed:.1f}s\n")


def stage_update(max_downloads: int, max_pages: int, force: bool = False) -> dict:
    """
    Run an incremental corpus update:
      1. Crawl to get current PDF links
      2. Diff against metadata.csv (find new/updated)
      3. Download only what changed
      4. Detect & mark superseded documents
      5. Clean new downloads
      6. Log to changelog.csv
    """
    from updater import run_update

    print_banner("INCREMENTAL UPDATE")
    t0 = time.time()

    summary = run_update(
        max_downloads=max_downloads,
        max_pages=max_pages,
        dest_dir=DOWNLOADED_PDFS_DIR,
        metadata_path=METADATA_CSV,
        changelog_path=CHANGELOG_CSV,
        force=force,
    )

    # Also clean newly downloaded PDFs
    if summary.get("downloaded", 0) > 0:
        print("\n[Update] Cleaning newly downloaded PDFs...")
        from cleaner import clean_all
        clean_all(input_dir=DOWNLOADED_PDFS_DIR, output_dir=CLEANED_PDFS_DIR)

    elapsed = time.time() - t0
    print(f"\n[Update] Total time: {elapsed:.1f}s\n")
    return summary


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_banner(title: str) -> None:
    """Print a section header to the console."""
    width = 60
    print("\n" + "=" * width)
    print(f"  {title}")
    print("=" * width)


def print_summary(start_time: float, pdf_links: list[dict], n_downloaded: int) -> None:
    """Print a final summary of the pipeline run."""
    elapsed = time.time() - start_time
    minutes, seconds = divmod(int(elapsed), 60)

    print_banner("PIPELINE SUMMARY")
    print(f"  Run completed at : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Total time       : {minutes}m {seconds}s")
    print(f"  PDF links found  : {len(pdf_links)}")
    print(f"  PDFs downloaded  : {n_downloaded}")
    print(f"  Metadata CSV     : {METADATA_CSV}")
    print(f"  Changelog CSV    : {CHANGELOG_CSV}")
    print(f"  Downloaded PDFs  : {DOWNLOADED_PDFS_DIR}/")
    print(f"  Cleaned PDFs     : {CLEANED_PDFS_DIR}/")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="IRDAI Document Crawler Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--crawl-only",
        action="store_true",
        help="Only run the crawler; do not download or clean.",
    )
    mode.add_argument(
        "--download-only",
        action="store_true",
        help="Only run the downloader (uses cached PDF links if available).",
    )
    mode.add_argument(
        "--clean-only",
        action="store_true",
        help="Only run the PDF cleaner on already-downloaded files.",
    )
    mode.add_argument(
        "--update",
        action="store_true",
        help="Incremental update: crawl, diff, download new/updated, clean, log.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="With --update: force re-download everything (rebuild corpus).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=200,
        metavar="N",
        help="Maximum number of HTML pages to crawl (default: 200).",
    )
    parser.add_argument(
        "--max-downloads",
        type=int,
        default=10,
        metavar="N",
        help="Maximum number of PDFs to download in this run (default: 10).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging output.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    print_banner("IRDAI DOCUMENT CRAWLER")
    print(f"  Started at : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if args.update:
        mode_str = "Incremental update" + (" (FORCE)" if args.force else "")
    elif args.crawl_only:
        mode_str = "Crawl only"
    elif args.download_only:
        mode_str = "Download only"
    elif args.clean_only:
        mode_str = "Clean only"
    else:
        mode_str = "Full pipeline (crawl -> download -> clean)"

    print(f"  Mode       : {mode_str}")
    print(f"  Max pages  : {args.max_pages}")
    print(f"  Max DLs    : {args.max_downloads}")

    pipeline_start = time.time()
    pdf_links: list[dict] = []
    n_downloaded: int = 0

    try:
        # ---- Update mode (production) ----
        if args.update:
            summary = stage_update(
                max_downloads=args.max_downloads,
                max_pages=args.max_pages,
                force=args.force,
            )
            pdf_links = [{}] * summary.get("new_found", 0)  # placeholder for count
            n_downloaded = summary.get("downloaded", 0)

        # ---- Stage 1: Crawl ----
        elif not args.download_only and not args.clean_only:
            pdf_links = stage_crawl(max_pages=args.max_pages)

            # ---- Stage 2: Download ----
            if not args.crawl_only:
                n_downloaded = stage_download(
                    pdf_links=pdf_links,
                    max_downloads=args.max_downloads,
                )

                # ---- Stage 3: Clean ----
                stage_clean()

        elif args.download_only:
            n_downloaded = stage_download(
                pdf_links=[],
                max_downloads=args.max_downloads,
            )

        elif args.clean_only:
            stage_clean()

    except KeyboardInterrupt:
        print("\n\n[!] Pipeline interrupted by user.")
        sys.exit(0)

    # Print final summary
    if not args.clean_only:
        print_summary(pipeline_start, pdf_links, n_downloaded)


if __name__ == "__main__":
    main()

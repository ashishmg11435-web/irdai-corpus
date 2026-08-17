"""
cleaner.py — PDF cleaning module using PyMuPDF (fitz).

What it does
------------
1. Removes pages that are written entirely (or predominantly) in Hindi /
   Devanagari script — since the RAG system only processes English.
2. Removes blank or near-blank pages (no meaningful text content).
3. Saves cleaned PDFs to a separate output directory, leaving the originals
   untouched.

Dependencies
------------
    pip install pymupdf

Usage
-----
    # Clean a single file
    from cleaner import clean_pdf
    clean_pdf("downloaded_pdfs/my_doc.pdf", "cleaned_pdfs/my_doc.pdf")

    # Batch-clean everything in downloaded_pdfs/
    from cleaner import clean_all
    clean_all()
"""

import logging
import os
import re
from pathlib import Path

try:
    # pyrefly: ignore [missing-import]
    import fitz  # PyMuPDF
except ImportError as exc:
    raise ImportError(
        "PyMuPDF is required for PDF cleaning.  "
        "Install it with:  pip install pymupdf"
    ) from exc

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# A Unicode range covering Devanagari script (Hindi, Marathi, etc.)
_DEVANAGARI_PATTERN = re.compile(r"[\u0900-\u097F]")

# If this fraction of characters on a page are Devanagari, treat page as Hindi
HINDI_THRESHOLD: float = 0.60

# If a page has fewer than this many printable characters, treat it as blank
BLANK_PAGE_MIN_CHARS: int = 30

DEFAULT_INPUT_DIR: str = "downloaded_pdfs"
DEFAULT_OUTPUT_DIR: str = "cleaned_pdfs"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Page-level detectors
# ---------------------------------------------------------------------------

def is_hindi_page(page: "fitz.Page") -> bool:
    """
    Return True if the page is predominantly written in Hindi (Devanagari).

    Strategy: extract the raw text, count Devanagari vs total characters,
    and compare against HINDI_THRESHOLD.
    """
    text = page.get_text("text")
    if not text.strip():
        return False  # blank pages are handled separately

    total_chars = len(text.replace(" ", "").replace("\n", ""))
    if total_chars == 0:
        return False

    devanagari_chars = len(_DEVANAGARI_PATTERN.findall(text))
    ratio = devanagari_chars / total_chars
    return ratio >= HINDI_THRESHOLD


def is_blank_page(page: "fitz.Page") -> bool:
    """
    Return True if the page contains negligible text content.

    Note: purely image-based pages with no OCR text also appear blank here.
    We intentionally keep those (they may contain scanned English documents).
    """
    text = page.get_text("text").strip()
    printable = re.sub(r"\s+", "", text)
    return len(printable) < BLANK_PAGE_MIN_CHARS


# ---------------------------------------------------------------------------
# Single-file cleaner
# ---------------------------------------------------------------------------

def clean_pdf(input_path: str, output_path: str) -> dict:
    """
    Clean a single PDF by removing Hindi and blank pages.

    Parameters
    ----------
    input_path  : path to the source PDF
    output_path : where to save the cleaned PDF

    Returns
    -------
    dict with keys:
        total_pages   : int   — original page count
        removed_hindi : int   — pages removed (Hindi)
        removed_blank : int   — pages removed (blank)
        kept_pages    : int   — pages in the output file
        skipped       : bool  — True if the whole document was Hindi/blank
    """
    result = {
        "total_pages": 0,
        "removed_hindi": 0,
        "removed_blank": 0,
        "kept_pages": 0,
        "skipped": False,
    }

    try:
        doc = fitz.open(input_path)
    except Exception as exc:
        logger.error("Cannot open %s: %s", input_path, exc)
        result["skipped"] = True
        return result

    result["total_pages"] = len(doc)
    pages_to_keep: list[int] = []

    for page_num in range(len(doc)):
        page = doc[page_num]

        if is_hindi_page(page):
            result["removed_hindi"] += 1
            logger.debug("Page %d of %s → Hindi, removing.", page_num + 1, input_path)
            continue

        if is_blank_page(page):
            result["removed_blank"] += 1
            logger.debug("Page %d of %s → Blank, removing.", page_num + 1, input_path)
            continue

        pages_to_keep.append(page_num)

    result["kept_pages"] = len(pages_to_keep)

    if not pages_to_keep:
        logger.warning(
            "All pages removed from %s — document not saved.", input_path
        )
        doc.close()
        result["skipped"] = True
        return result

    # Build a new PDF containing only the kept pages
    output_doc = fitz.open()
    output_doc.insert_pdf(doc, from_page=0, to_page=-1)

    # Delete pages in reverse order to avoid index shifting
    pages_to_delete = sorted(
        set(range(len(doc))) - set(pages_to_keep), reverse=True
    )
    for page_num in pages_to_delete:
        output_doc.delete_page(page_num)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    output_doc.save(output_path, garbage=4, deflate=True)
    output_doc.close()
    doc.close()

    return result


# ---------------------------------------------------------------------------
# Batch cleaner
# ---------------------------------------------------------------------------

def clean_all(
    input_dir: str = DEFAULT_INPUT_DIR,
    output_dir: str = DEFAULT_OUTPUT_DIR,
) -> None:
    """
    Batch-process all PDFs in *input_dir* and save cleaned versions to
    *output_dir*. Already-cleaned files are skipped.

    Parameters
    ----------
    input_dir  : folder containing the raw downloaded PDFs
    output_dir : folder to save cleaned PDFs
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    pdf_files = list(input_path.glob("*.pdf"))
    if not pdf_files:
        print(f"[Cleaner] No PDF files found in '{input_dir}'.")
        return

    print(f"[Cleaner] Processing {len(pdf_files)} PDF(s) from '{input_dir}'…\n")

    total_removed_hindi = 0
    total_removed_blank = 0
    total_skipped = 0
    total_processed = 0

    for pdf_file in pdf_files:
        out_file = output_path / pdf_file.name

        # Skip if already cleaned (idempotent)
        if out_file.exists():
            print(f"  [SKIP — already cleaned] {pdf_file.name}")
            continue

        print(f"  Cleaning: {pdf_file.name}")
        stats = clean_pdf(str(pdf_file), str(out_file))

        if stats["skipped"]:
            total_skipped += 1
            print(
                f"  [SKIPPED — all pages removed] "
                f"{pdf_file.name} ({stats['total_pages']} pages)"
            )
        else:
            total_processed += 1
            total_removed_hindi += stats["removed_hindi"]
            total_removed_blank += stats["removed_blank"]
            print(
                f"  [OK] {pdf_file.name}: "
                f"{stats['total_pages']} pages -> {stats['kept_pages']} kept "
                f"(-{stats['removed_hindi']} Hindi, -{stats['removed_blank']} blank)"
            )

    print(
        f"\n[Cleaner] Done.\n"
        f"  Processed : {total_processed}\n"
        f"  Skipped   : {total_skipped}\n"
        f"  Hindi pages removed : {total_removed_hindi}\n"
        f"  Blank pages removed : {total_removed_blank}\n"
    )


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    clean_all()

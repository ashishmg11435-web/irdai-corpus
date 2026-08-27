"""
scraper.py — Focused BFS crawler for IRDAI regulatory documents.

Strategy
--------
1. Start from a fixed set of *seed URLs* — the known IRDAI document listing
   pages (Regulations, Circulars, Guidelines, etc.).
2. On each listing page, collect:
     - All PDF links in the current page view (/documents/37343/ paths)
     - All pagination links for that listing page (Liferay portlet pagination
       uses query params like ?..._cur=2, ..._cur=3, etc.)
3. Perform a shallow BFS (depth ≤ 1) to follow pagination only — we do NOT
   deep-crawl the whole IRDAI website.
4. Skip pages/documents that match *exclusion* patterns (recruitment, etc.),
   skip *predominantly* Hindi (pure-Devanagari) documents, and skip files whose
   name carries a non-English keyword. Bilingual "Hindi _ English" documents are
   KEPT (their English portion is used as the title).

Returns a list of PDF info dicts:
    {
        "url":         str,   # direct PDF download URL
        "source_page": str,   # listing page where the link was found
        "doc_type":    str,   # e.g. "Regulation", "Circular", ...
        "link_text":   str,   # anchor text (document title)
    }
"""

import logging
from collections import deque
from urllib.parse import unquote, urljoin, urlparse, urlunparse

# pyrefly: ignore [missing-import]
from bs4 import BeautifulSoup

from utils import (
    contains_devanagari,
    create_session,
    english_portion,
    get_document_type,
    has_non_english_filename_keyword,
    is_pdf_url,
    is_predominantly_non_english,
    normalize_url,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DOMAIN = "irdai.gov.in"

# Seed URLs: all known IRDAI document listing pages
SEED_URLS: list[str] = [
    # Primary regulatory documents
    "https://irdai.gov.in/consolidated-gazette-notified-regulations",
    "https://irdai.gov.in/updated-regulations",
    "https://irdai.gov.in/circulars",
    "https://irdai.gov.in/guidelines",
    "https://irdai.gov.in/guidelines1",
    "https://irdai.gov.in/acts",
    "https://irdai.gov.in/rules",
    "https://irdai.gov.in/rules2",
    "https://irdai.gov.in/notifications",
    "https://irdai.gov.in/notifications1",
    "https://irdai.gov.in/exposure-drafts",
    "https://irdai.gov.in/orders1",
    "https://irdai.gov.in/regulations2",
]

# Liferay portlet ID for the document media portlet (used to detect pagination)
_PORTLET_ID = "com_irdai_document_media_IRDAIDocumentMediaPortlet"
_PORTLET_CUR_KEY = f"_{_PORTLET_ID}_cur"

# URL segments that indicate non-regulatory content — skip these entirely.
# NOTE: These are substring matches — keep patterns specific enough to avoid
# accidentally filtering legitimate regulatory documents.
EXCLUDED_PATTERNS: tuple[str, ...] = (
    "recruitment",
    "career",
    "tender",
    "press-release",
    "press_release",
    "/media",
    "speech",
    "annual-report",
    "annual_report",
    "e-service",
    "eservice",
    "grievance",
    "consumer",
    "/login",
    "register",
    "contact-us",
    "contact_us",
    "sitemap",
    "accessibility",
    "/hi/",
    "languageId=hi",
    "vacancies",
    "/archive",
    # "form" removed — too broad, matches "reform", "information", "performance"
    # "format" removed — substring of legitimate words
    # "annexure" removed — IRDAI regulation annexures contain critical content
    # "draft" removed — contradicts the exposure-drafts seed URL
    # "discussion" removed — blocks discussion papers (optional corpus content)
    # "notices" removed — blocks regulatory/public notices along with recruitment
)

# Filename keywords that indicate non-English documents (e.g.
# "Integrity_Pledge_Hindi-.pdf") now live in utils.NON_ENGLISH_FILENAME_KEYWORDS
# and are applied via utils.has_non_english_filename_keyword(), so the scraper,
# downloader and cleaner all stay consistent.

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_excluded(url: str) -> bool:
    """Return True if *url* should be skipped based on exclusion patterns."""
    url_lower = url.lower()
    return any(pat in url_lower for pat in EXCLUDED_PATTERNS)


def _is_internal(url: str) -> bool:
    """Return True if *url* belongs to the irdai.gov.in domain."""
    netloc = urlparse(url).netloc.lower()
    return netloc in (BASE_DOMAIN, f"www.{BASE_DOMAIN}")


def _clean_pdf_url(href: str, base_url: str) -> str:
    """Return an absolute PDF URL with query string preserved but fragment stripped."""
    absolute = urljoin(base_url, href)
    p = urlparse(absolute)
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", p.query, ""))


def _decode_url_name(url: str) -> str:
    """
    Return the decoded, human-readable document name embedded in a Liferay PDF
    URL. Liferay paths look like:

        /documents/37343/366029/<Name>.pdf/<uuid>

    where <Name> is percent-encoded (and uses '+' for spaces). We decode the
    segment that ends in '.pdf' so the caller can language-detect on the real
    (possibly bilingual) title even when the anchor text is empty — IRDAI renders
    each PDF as two <a> tags, one with text and one without.
    """
    path = urlparse(url).path
    segments = [seg for seg in path.split("/") if seg]
    for seg in segments:
        if seg.lower().endswith(".pdf"):
            return unquote(seg).replace("+", " ")
    return unquote(segments[-1]).replace("+", " ") if segments else ""


def _fetch_soup(url: str, session) -> BeautifulSoup | None:
    """
    Fetch *url* and return a BeautifulSoup object.
    Uses response.content (bytes) to safely handle Hindi/Devanagari characters.
    Returns None on any HTTP or parsing error.
    """
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()
        return BeautifulSoup(response.content, "html.parser")
    except Exception as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


def _extract_pagination_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    """
    Find all Liferay portlet pagination links for the document listing portlet.
    These are anchor tags with href containing '_cur=' parameters.

    Returns a list of absolute pagination URLs (query strings preserved).
    """
    pagination_urls: list[str] = []
    seen: set[str] = set()

    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if _PORTLET_CUR_KEY in href:
            absolute = urljoin(base_url, href)
            p = urlparse(absolute)
            # Keep full query string for pagination
            clean = urlunparse((p.scheme, p.netloc.lower(), p.path, "", p.query, ""))
            if clean not in seen:
                seen.add(clean)
                pagination_urls.append(clean)

    return pagination_urls


def _extract_pdf_links(soup: BeautifulSoup, source_url: str) -> list[dict]:
    """
    Extract English (and bilingual Hindi+English) PDF document links from a page.

    IRDAI publishes many important documents under a *bilingual* title:
        "<Hindi> _ <English>"
        e.g. "... दिशानिर्देश, 2025 _ IRDAI (...) Guidelines, 2025"
    These are genuine English documents and are KEPT. Language is judged on an
    "effective title" — the anchor text, or the decoded URL name when the anchor
    is empty — and only *predominantly* non-English (pure-Hindi) titles/URLs are
    skipped.

    Skips:
    - Predominantly non-English (pure-Hindi) titled documents
    - Documents whose filename carries a non-English keyword (English-looking
      name but non-English content, e.g. "Integrity_Pledge_Hindi-.pdf")

    For bilingual titles, only the English portion is stored as link_text so the
    downstream filename and metadata stay clean English.
    """
    pdf_links: list[dict] = []
    seen_urls: set[str] = set()

    # Determine the doc_type for this source page once
    source_doc_type = get_document_type("", source_page=source_url)

    for tag in soup.find_all("a", href=True):
        href: str = tag["href"].strip()

        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        if not is_pdf_url(href) and not is_pdf_url(urljoin(source_url, href)):
            continue

        clean_url = _clean_pdf_url(href, source_url)

        # Skip duplicate URLs on the same page
        if clean_url in seen_urls:
            continue

        link_text = tag.get_text(strip=True) or ""

        # Language-detection basis: prefer the anchor text; fall back to the
        # decoded URL name (IRDAI renders each PDF as two <a> tags, one of which
        # has empty text). This keeps bilingual "<Hindi> _ <English>" documents
        # while still skipping pure-Hindi ones.
        effective_title = link_text or _decode_url_name(clean_url)

        # Skip predominantly non-English (pure-Hindi) documents, but KEEP
        # bilingual titles that carry a real English half.
        if is_predominantly_non_english(effective_title):
            logger.debug("Skipping non-English (pure Hindi) document: %s", effective_title[:60])
            continue

        # Skip non-English documents flagged by a filename keyword — English
        # URLs/titles but non-English content (e.g. "Integrity_Pledge_Hindi-.pdf").
        if has_non_english_filename_keyword(urlparse(clean_url).path):
            logger.debug("Skipping non-English filename: %s", clean_url[:80])
            continue

        seen_urls.add(clean_url)

        # For bilingual titles (or empty anchors), store only the clean English
        # portion so the derived filename and metadata stay English.
        if contains_devanagari(link_text) or not link_text:
            title_for_metadata = english_portion(effective_title)
            if title_for_metadata.lower().endswith(".pdf"):
                title_for_metadata = title_for_metadata[:-4].rstrip(" -_.")
        else:
            title_for_metadata = link_text

        doc_type = get_document_type(clean_url, source_page=source_url)
        if doc_type == "Unknown" and source_doc_type != "Unknown":
            doc_type = source_doc_type

        pdf_links.append({
            "url": clean_url,
            "source_page": source_url,
            "doc_type": doc_type,
            "link_text": title_for_metadata,
        })

    return pdf_links


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def crawl(
    seed_urls: list[str] = SEED_URLS,
    max_pages: int = 200,
) -> tuple[list[dict], int]:
    """
    Crawl IRDAI document listing pages and collect PDF links.

    This is a *focused* crawler — it only visits:
      1. The seed listing pages (depth 0)
      2. Their pagination pages (depth 1, Liferay ?_cur= links only)

    No general BFS is performed; this keeps the crawl fast and targeted.

    Parameters
    ----------
    seed_urls : list[str]
        Starting URLs. Defaults to the known IRDAI document listing pages.
    max_pages : int
        Safety cap on total pages visited (avoids runaway pagination).

    Returns
    -------
    pdf_links : list[dict]
        All discovered English PDF links with metadata.
    pages_visited : int
        Total number of HTML pages fetched.
    """
    session = create_session()
    visited: set[str] = set()

    # Queue entries: (url, is_pagination)
    queue: deque[tuple[str, bool]] = deque()
    for url in seed_urls:
        norm = normalize_url(url)
        if norm not in visited:
            queue.append((norm, False))
            visited.add(norm)

    all_pdf_links: list[dict] = []
    seen_pdf_urls: set[str] = set()
    pages_visited: int = 0

    print(f"[Scraper] Starting focused crawl from {len(seed_urls)} seed URL(s)...")

    while queue and pages_visited < max_pages:
        current_url, is_pagination = queue.popleft()

        if _is_excluded(current_url):
            continue

        page_type = "pagination" if is_pagination else "seed"
        print(f"  [{page_type}] Fetching: {current_url[:90]}")

        soup = _fetch_soup(current_url, session)
        if soup is None:
            continue
        pages_visited += 1

        # Collect PDF links
        pdf_links = _extract_pdf_links(soup, current_url)
        for link in pdf_links:
            if link["url"] not in seen_pdf_urls:
                seen_pdf_urls.add(link["url"])
                all_pdf_links.append(link)

        # From seed pages only: discover pagination links
        if not is_pagination:
            pagination_urls = _extract_pagination_urls(soup, current_url)
            for pag_url in pagination_urls:
                if pag_url not in visited:
                    visited.add(pag_url)
                    queue.append((pag_url, True))
            if pagination_urls:
                print(f"  -> Found {len(pagination_urls)} pagination page(s)")

    print(
        f"[Scraper] Done. Pages visited: {pages_visited} | "
        f"PDF links found: {len(all_pdf_links)}"
    )
    return all_pdf_links, pages_visited


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    pdf_links, total_pages = crawl(max_pages=20)

    print(f"\nDiscovered {len(pdf_links)} PDF link(s) after visiting {total_pages} page(s).\n")
    for i, link in enumerate(pdf_links[:20], start=1):
        title = link["link_text"][:60] if link["link_text"] else "(no title)"
        print(f"  {i:02d}. [{link['doc_type']}] {title}")
        print(f"       {link['url'][:90]}")
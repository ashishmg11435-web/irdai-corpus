import hashlib
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import parse_qs, urlparse, urlunparse


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def normalize_url(url: str, keep_query: bool = False) -> str:
    """
    Normalize a URL so that duplicates (trailing slash, fragments, etc.)
    collapse to the same canonical form.

    Parameters
    ----------
    keep_query : bool
        If True, preserve the query string (needed for PDF download URLs
        whose parameters differ meaningfully, e.g. ?version=1.1&download=true).
        If False (default), query strings are stripped for HTML page dedup.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    query = parsed.query if keep_query else ""
    return urlunparse((
        parsed.scheme,
        parsed.netloc.lower(),
        path,
        "",      # params
        query,   # query string
        "",      # fragment
    ))


def is_pdf_url(url: str) -> bool:
    """
    Return True if the URL points to a downloadable PDF.

    IRDAI PDFs come in two forms:
      1. Direct .pdf extension  →  ends with .pdf (ignoring query string)
      2. Liferay document store  →  /documents/37343/…  (ALL such paths are PDFs
         on the IRDAI website, regardless of whether download=true is present)
    """
    parsed = urlparse(url)
    path_lower = parsed.path.lower()
    if path_lower.endswith(".pdf"):
        return True
    # Every Liferay document path under /documents/37343/ is a PDF on IRDAI
    if "/documents/37343/" in parsed.path:
        return True
    return False


# Map of known IRDAI Liferay folder IDs → document type labels.
# Updated as new folder IDs are discovered during crawling.
_FOLDER_ID_MAP: dict[str, str] = {
    "366405": "Regulation",
    "365525": "Circular",
    "366029": "Guideline",
    "366037": "Act",
    "366041": "Rule",
    "366033": "Notification",
    "366045": "Exposure Draft",
    "366049": "Master Circular",
    "365521": "Discussion Paper",
    "366053": "FAQ",
}

# Seed URL slug → document type (fallback when folder ID is not in the URL)
_SLUG_TYPE_MAP: dict[str, str] = {
    "consolidated-gazette-notified-regulations": "Regulation",
    "updated-regulations": "Regulation",
    "regulations2": "Regulation",
    "circulars": "Circular",
    "guidelines": "Guideline",
    "guidelines1": "Guideline",
    "acts": "Act",
    "rules": "Rule",
    "rules2": "Rule",
    "notifications": "Notification",
    "notifications1": "Notification",
    "exposure-drafts": "Exposure Draft",
    "master-circular": "Master Circular",
    "master-circulars": "Master Circular",
    "discussion-papers": "Discussion Paper",
    "orders1": "Order",
    "faqs": "FAQ",
    "faqs1": "FAQ",
}


def get_document_type(url: str, source_page: str = "") -> str:
    """
    Classify a PDF URL into a document type.

    1. Try to match the Liferay folder ID from the URL path.
    2. Fall back to the slug of the source listing page.
    3. Default to 'Unknown'.
    """
    parsed = urlparse(url)
    parts = parsed.path.split("/")

    # Liferay path looks like /documents/37343/<folderId>/...
    if "/documents/37343/" in parsed.path:
        try:
            idx = parts.index("37343")
            folder_id = parts[idx + 1]
            if folder_id in _FOLDER_ID_MAP:
                return _FOLDER_ID_MAP[folder_id]
        except (ValueError, IndexError):
            pass

    # Fallback: check the source listing page slug
    for slug, doc_type in _SLUG_TYPE_MAP.items():
        if slug in source_page:
            return doc_type

    return "Unknown"


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------

# Document types that form the primary regulatory corpus
_GOLD_TYPES: set[str] = {"Regulation", "Master Circular", "Act", "Rule", "Guideline"}

# Secondary corpus: recent circulars, drafts, orders
_SILVER_TYPES: set[str] = {
    "Circular", "Exposure Draft", "Order", "Notification",
    "Discussion Paper", "FAQ",
}


def classify_tier(doc_type: str) -> str:
    """
    Return 'gold' or 'silver' based on the document type.

    Gold  = foundational regulatory documents (always searched first)
    Silver = supplementary documents (searched on fallback)
    """
    if doc_type in _GOLD_TYPES:
        return "gold"
    if doc_type in _SILVER_TYPES:
        return "silver"
    return "silver"  # default unknown types to silver


def extract_version(url: str) -> tuple[str, int]:
    """
    Parse Liferay version and timestamp from a document URL.

    Liferay URLs contain query parameters like:
        ?version=1.1&t=1785549059768&download=true

    Returns
    -------
    (version, timestamp) : tuple[str, int]
        version   : e.g. "1.1"  ("0.0" if not found)
        timestamp : Unix timestamp in seconds (0 if not found)
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    version = qs.get("version", ["0.0"])[0]

    t_val = qs.get("t", ["0"])[0]
    try:
        # Liferay timestamps are in milliseconds
        timestamp = int(t_val) // 1000
    except (ValueError, TypeError):
        timestamp = 0

    return version, timestamp


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def compute_file_hash(filepath: str, chunk_size: int = 65536) -> str:
    """Return the MD5 hex-digest of the file at *filepath*."""
    md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        while chunk := f.read(chunk_size):
            md5.update(chunk)
    return md5.hexdigest()


def sanitize_filename(name: str, max_length: int = 180) -> str:
    """
    Convert an arbitrary string into a safe filename.

    - Strip leading/trailing whitespace
    - Replace problematic characters with underscores
    - Collapse multiple underscores
    - Truncate to *max_length* characters
    """
    name = name.strip()
    name = re.sub(r"[^\w\-.]", "_", name)   # keep word chars, dash, dot
    name = re.sub(r"_+", "_", name)          # collapse consecutive underscores
    return name[:max_length]


# ---------------------------------------------------------------------------
# HTTP Session
# ---------------------------------------------------------------------------

def create_session() -> requests.Session:
    """
    Create a requests Session with:
    - Browser-like User-Agent header
    - Automatic retry on transient server errors (5xx)
    - Exponential back-off between retries
    """
    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0 Safari/537.36"
        )
    })

    retry = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session
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
# Language detection (English vs Hindi / other Indic scripts)
# ---------------------------------------------------------------------------
#
# IRDAI publishes many important documents under a *bilingual* title of the form
#     "<Hindi text> _ <English text>"
# e.g.  "आईआरडीएआई (बीमा धोखाधड़ी निगरानी रूपरेखा) दिशानिर्देश, 2025 _ "
#       "IRDAI (Insurance Fraud Monitoring Framework) Guidelines, 2025"
#
# These are genuine English documents and MUST be kept. Only titles/URLs that
# are *predominantly* non-English (a pure-Hindi document) should be skipped.
#
# A second, orthogonal problem: some files have a fully English-looking name but
# non-English *content* (e.g. "Integrity_Pledge_Hindi-.pdf", a scanned Hindi
# pledge). Those cannot be caught by looking at the script of the title, so they
# are caught separately via a filename keyword list.

# Devanagari (Hindi / Marathi) Unicode block
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

# A "real English word": a run of >= 3 ASCII letters.  Used to tell a bilingual
# title (many English words alongside Hindi) from a pure-Hindi title (none).
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]{3,}")

# Minimum number of English words for a title containing Devanagari to still be
# treated as bilingual (and therefore kept).
_MIN_ENGLISH_WORDS_FOR_BILINGUAL = 2

# Filename keywords that mark a document as non-English even when its URL / title
# looks fully English (e.g. "Integrity_Pledge_Hindi-.pdf", "..._Marathi.pdf").
# Matched as case-insensitive substrings against the URL path / filename.
# NOTE: this is the canonical definition — scraper.py, downloader.py and
# cleaner.py all import it so the three pipeline stages stay consistent.
NON_ENGLISH_FILENAME_KEYWORDS: tuple[str, ...] = (
    "hindi",
    "marathi",
    "telugu",
    "kannada",
    "tamil",
    "bengali",
    "gujarati",
    "punjabi",
    "malayalam",
    "odia",
    "urdu",
    "assamese",
    # NOTE: the short ISO code forms "_hi_" / "_hi." were deliberately removed.
    # They collide with "HI" = Health Insurance, which appears in many legitimate
    # IRDAI filenames (e.g. "Circular_on_HI_returns_13_9_2022.pdf" -> "_hi_"),
    # producing false positives. Genuine Hindi files are already caught by the
    # full "hindi" keyword and by Devanagari detection, so nothing is lost.
)


def contains_devanagari(text: str) -> bool:
    """Return True if *text* contains any Devanagari (Hindi) character."""
    return bool(_DEVANAGARI_RE.search(text or ""))


def has_non_english_filename_keyword(path_or_name: str) -> bool:
    """
    Return True if a URL path or filename contains a language keyword that marks
    it as non-English content (even when the rest of the name looks English).

    This is what keeps files like "Integrity_Pledge_Hindi-.pdf" out of the
    corpus — their title/URL is otherwise indistinguishable from English.
    """
    lowered = (path_or_name or "").lower()
    return any(kw in lowered for kw in NON_ENGLISH_FILENAME_KEYWORDS)


def is_predominantly_non_english(text: str) -> bool:
    """
    Return True if *text* is predominantly non-English (a pure-Hindi title) and
    should therefore be skipped.

    Decision table:
      - No Devanagari at all                       -> False (keep; English/ASCII)
      - Devanagari + >= 2 English words (>=3 chars) -> False (bilingual, keep)
      - Devanagari + < 2 English words              -> True  (pure Hindi, skip)

    An English-looking title (no Devanagari) is treated as acceptable here; files
    whose *content* is Hindi but whose name is English are handled separately by
    has_non_english_filename_keyword().
    """
    if not contains_devanagari(text):
        return False
    english_words = _ENGLISH_WORD_RE.findall(text)
    return len(english_words) < _MIN_ENGLISH_WORDS_FOR_BILINGUAL


def english_portion(text: str) -> str:
    """
    Return the English / Latin portion of a possibly-bilingual title.

    Works by returning the run of Latin-script text (letters, digits, spaces and
    common title punctuation) that contains the most English words. For a
    bilingual "<Hindi> _ <English>" title this isolates the English half:

        "आईआरडीएआई (...) दिशानिर्देश, 2025 _ IRDAI (Insurance Fraud Monitoring "
        "Framework) Guidelines, 2025"
            -> "IRDAI (Insurance Fraud Monitoring Framework) Guidelines, 2025"

    If there is no Latin text at all, the original string is returned unchanged.
    """
    if not text:
        return text
    spans = re.findall(r"[A-Za-z0-9][A-Za-z0-9 ()\[\].,:;/&'\"‘’\-–—+]*", text)
    if not spans:
        return text
    best = max(spans, key=lambda s: len(_ENGLISH_WORD_RE.findall(s)))
    return best.strip(" -–—_/|.,:;")


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
    Convert an arbitrary string into a safe, ASCII-only filename.

    - Strip leading/trailing whitespace
    - Replace every character that is not an ASCII letter, digit, dash or dot
      with an underscore.  This is deliberately ASCII-only: it guarantees that
      Devanagari (or any non-Latin script) cannot survive into a filename, even
      if a stray Hindi character slips through upstream extraction.
    - Collapse multiple underscores
    - Trim stray leading/trailing separators
    - Truncate to *max_length* characters
    """
    name = name.strip()
    name = re.sub(r"[^A-Za-z0-9\-.]", "_", name)  # ASCII word chars, dash, dot only
    name = re.sub(r"_+", "_", name)               # collapse consecutive underscores
    name = name.strip("_")                         # trim leading/trailing underscores
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
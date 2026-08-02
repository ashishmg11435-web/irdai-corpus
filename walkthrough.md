# IRDAI Document Crawler — Walkthrough

## What Was Built

A production-ready document collection pipeline for an Agentic RAG system, covering automated discovery, download, cleaning, and incremental updates of IRDAI regulatory documents.

---

## Final Project Structure

```
irda_scraper/
├── .github/
│   └── workflows/
│       └── update_corpus.yml    # GitHub Actions daily update (02:00 UTC)
├── scraper.py                   # Focused crawler (seed pages + pagination)
├── downloader.py                # Streaming GET downloader with dedup & tier-filtering
├── cleaner.py                   # PyMuPDF Hindi/blank page removal
├── updater.py                   # Incremental diff + supersession engine
├── main.py                      # CLI orchestrator (all modes)
├── utils.py                     # Shared helpers (hashing, tier, versions)
├── metadata.csv                 # Document metadata (auto-generated)
├── changelog.csv                # Change log (auto-generated)
├── pdf_links_cache.json         # Crawl cache (auto-generated)
├── downloaded_pdfs/             # Raw PDFs from IRDAI
├── cleaned_pdfs/                # English-only PDFs (Hindi/blank removed)
└── project_description.txt      # Original project spec
```

---

## Files Modified / Created

| File | Action | Key Changes |
|---|---|---|
| [utils.py](file:///d:/Agentic%20RAG/irda_scraper/utils.py) | Rewritten | Added `classify_tier()`, `extract_version()`, `compute_file_hash()`, `sanitize_filename()`. |
| [scraper.py](file:///d:/Agentic%20RAG/irda_scraper/scraper.py) | Rewritten | Focused crawler with pagination. **Excludes "form", "draft", "annexure", and "discussion"** URLs to keep RAG chunks clean. |
| [downloader.py](file:///d:/Agentic%20RAG/irda_scraper/downloader.py) | Rewritten | **Corpus Filtering:** Only downloads `Gold` tier documents and recent `Silver` tier documents (≥ 2023). Uses streaming GET for verified PDFs. |
| [cleaner.py](file:///d:/Agentic%20RAG/irda_scraper/cleaner.py) | New | PyMuPDF-based Hindi page detection, blank page removal, batch processing. (Chunk-level Hindi filtering deferred to your notebook). |
| [updater.py](file:///d:/Agentic%20RAG/irda_scraper/updater.py) | New | Corpus diff engine: detects new/updated docs, version-based supersession marking, changelog.csv logging. |
| [main.py](file:///d:/Agentic%20RAG/irda_scraper/main.py) | Rewritten | CLI with `--update`, `--force`, `--max-pages`, `--max-downloads`. |
| [update_corpus.yml](file:///d:/Agentic%20RAG/irda_scraper/.github/workflows/update_corpus.yml) | New | GitHub Actions workflow: daily cron, manual trigger, auto-commit. |

---

## Corpus Filtering Strategy (Optimized for RAG)

To ensure the LLM doesn't hallucinate on outdated forms or 20-year-old superseded circulars, the pipeline now strictly enforces:

1. **Standalone Forms & Drafts**: Filtered out during the crawl (`scraper.py`).
2. **Gold Tier**: Regulations, Acts, Master Circulars, and Guidelines are **always downloaded** regardless of year.
3. **Silver Tier**: Circulars, Orders, and FAQs are **only downloaded if they are from 2023 or newer**. Anything older is skipped.

---

## CLI Usage

```bash
# Production incremental update
python main.py --update --max-downloads 50

# Rebuild entire corpus (force)
python main.py --update --force --max-downloads 500

# Individual stages
python main.py --crawl-only --max-pages 200
python main.py --download-only --max-downloads 50
python main.py --clean-only
```

---

## Next Steps for RAG Integration

Your RAG ingestion notebook should:
1. Read `cleaned_pdfs/` for document content. Note that `PyPDFDirectoryLoader` will mangle tables; if tables are critical to QA, consider upgrading to `LlamaParse` or `Unstructured` in your notebook.
2. Read `metadata.csv` for tier classification:
   - **Gold tier** docs → primary FAISS index
   - **Silver tier** docs → secondary FAISS index (fallback)
3. Skip rows where `is_superseded=true`.
4. Run Python-level Regex inside your text splitting pipeline to discard purely Hindi chunks (as you planned).

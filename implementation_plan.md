# Production IRDAI RAG Corpus — Architecture & Incremental Updates

## Problem Summary

Three things to build:
1. **Fix the download bug** — HEAD requests return 403; switch to streaming GET + PDF magic byte verification
2. **Tiered corpus architecture** — Gold (current regulations) vs Silver (recent circulars) metadata tagging
3. **Incremental update mechanism** — detect new/updated documents and refresh the corpus automatically

---

## 1. Fix: Download Verification (HEAD → GET)

### Root Cause
IRDAI's Liferay server blocks HTTP HEAD requests (returns 403 Forbidden). The `verify_pdf()` function in `downloader.py` relies entirely on HEAD.

### Fix
#### [MODIFY] [downloader.py](file:///d:/Agentic%20RAG/irda_scraper/downloader.py)

Replace `verify_pdf()` with a streaming GET approach:
- Open `session.get(url, stream=True)` — only reads headers, not the body
- Check `Content-Type` from the GET response
- If ambiguous, read first 4 bytes and check for `%PDF` magic
- If valid, continue streaming the body to disk (no second request)
- If invalid, close and discard

This means `verify_pdf()` and `download_pdf()` merge into a single `verify_and_download()` function — one GET request does both.

---

## 2. Tiered Corpus Architecture (Gold / Silver)

### Concept

```
                    ┌──────────────────────────┐
                    │     RAG Query Engine      │
                    │  (retrieves Gold first,   │
                    │   Silver on fallback)     │
                    └───────┬──────────┬────────┘
                            │          │
              ┌─────────────▼──┐  ┌────▼─────────────┐
              │   Gold Index   │  │   Silver Index    │
              │  (primary)     │  │  (fallback)       │
              └────────────────┘  └───────────────────┘

Gold (always searched):         Silver (searched on fallback):
 • Current Regulations           • Recent Circulars (< 3 yrs)
 • Master Circulars              • Exposure Drafts
 • Acts                          • Orders
 • Rules                         • Discussion Papers
 • Current Guidelines
```

### Implementation
#### [MODIFY] [metadata.csv](file:///d:/Agentic%20RAG/irda_scraper/metadata.csv)
Add two new columns:
- `tier` — `"gold"` or `"silver"` (auto-assigned by doc_type rules)
- `is_superseded` — `true` if a newer version of this document exists

#### [MODIFY] [utils.py](file:///d:/Agentic%20RAG/irda_scraper/utils.py)
Add `classify_tier(doc_type) -> str` function:
```python
GOLD_TYPES = {"Regulation", "Master Circular", "Act", "Rule", "Guideline"}
SILVER_TYPES = {"Circular", "Exposure Draft", "Order", "Notification", "Discussion Paper"}
```

---

## 3. Incremental Update Mechanism

### How It Works

```
┌──────────────────────────────────────────────────────┐
│                  Scheduled Run                       │
│              (daily / weekly cron)                   │
└───────────────────────┬──────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────┐
│  STEP 1: Crawl all seed pages + pagination           │
│  Output: current_links = [{url, doc_type, ...}]      │
└───────────────────────┬──────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────┐
│  STEP 2: Diff against metadata.csv                   │
│                                                      │
│  NEW      = current_links - known_urls               │
│  REMOVED  = known_urls - current_links (optional)    │
│  UPDATED  = same title, higher ?version= param       │
└───────┬────────────────┬─────────────────┬───────────┘
        │                │                 │
        ▼                ▼                 ▼
   Download NEW    Re-download       Mark old version
   documents       UPDATED docs     as superseded
        │                │                 │
        ▼                ▼                 ▼
┌──────────────────────────────────────────────────────┐
│  STEP 3: Clean new/updated PDFs                      │
│  STEP 4: Update metadata.csv + changelog.csv         │
│  STEP 5: Signal RAG system to re-index changed docs  │
└──────────────────────────────────────────────────────┘
```

### Key Mechanisms

**A. Version detection from Liferay URLs:**
```
?version=1.0&t=1631529760349   →  version 1.0, timestamp 1631529760
?version=1.1&t=1785549059768   →  version 1.1, timestamp 1785549059
```
We parse `version` and `t` from the query string. Higher version = updated document.

**B. Title-based supersession:**
If two documents share the same base title (e.g. "IRDAI (Investment) Regulations, 2016") but have different version numbers, the older one is marked `is_superseded=true`.

**C. Changelog tracking:**
#### [NEW] [changelog.csv](file:///d:/Agentic%20RAG/irda_scraper/changelog.csv)
Records every change per run:
```
run_date, action, filename, doc_type, reason
2026-08-01, ADDED, IRDAI_Investment_Regs_2024.pdf, Regulation, New document
2026-08-01, UPDATED, IRDAI_Agency_Regs.pdf, Regulation, version 1.0 -> 1.1
2026-08-01, SUPERSEDED, IRDAI_Agency_Regs_old.pdf, Regulation, Replaced by newer version
```

### New Files

#### [NEW] [updater.py](file:///d:/Agentic%20RAG/irda_scraper/updater.py)
Orchestrates the incremental update process:
- `diff_corpus(current_links, metadata_path)` → returns `{new, updated, removed}` sets
- `detect_superseded(metadata_path)` → identifies documents with newer versions
- `run_update(max_downloads)` → full update cycle: crawl → diff → download new → clean → log

#### [MODIFY] [main.py](file:///d:/Agentic%20RAG/irda_scraper/main.py)
Add new CLI mode:
```bash
python main.py --update          # incremental update (crawl + diff + download new only)
python main.py --update --force  # re-download everything (rebuild corpus)
```

---

## Deployment: How to Run This Automatically

### Option A: Windows Task Scheduler (simplest)
```
schtasks /create /sc DAILY /tn "IRDAI_Corpus_Update" /tr "python D:\path\to\main.py --update" /st 02:00
```
Runs at 2 AM daily. Output goes to `changelog.csv`.

### Option B: Cloud Deployment (production)
```
GitHub Actions / Cloud Scheduler → triggers main.py --update
  → New/updated PDFs downloaded
  → changelog.csv updated  
  → Webhook fires to RAG system → "re-index these files"
```

### Option C: Integrated with RAG Pipeline
Your RAG ingestion service calls `updater.run_update()` as a Python function before each re-index cycle.

---

## Proposed Changes Summary

| File | Action | What Changes |
|---|---|---|
| [downloader.py](file:///d:/Agentic%20RAG/irda_scraper/downloader.py) | MODIFY | Fix HEAD→GET, merge verify+download, add tier/version columns |
| [utils.py](file:///d:/Agentic%20RAG/irda_scraper/utils.py) | MODIFY | Add `classify_tier()`, `extract_version()` helpers |
| [updater.py](file:///d:/Agentic%20RAG/irda_scraper/updater.py) | NEW | Incremental update engine: diff, supersession, changelog |
| [main.py](file:///d:/Agentic%20RAG/irda_scraper/main.py) | MODIFY | Add `--update` mode that wires in the updater |
| [changelog.csv](file:///d:/Agentic%20RAG/irda_scraper/changelog.csv) | NEW | Created automatically on first update run |

---

## Verification Plan

1. Run `python main.py --update --max-downloads 10` — should download 10 PDFs with correct tier/version metadata
2. Run it again immediately — should detect 0 new documents ("already up to date")
3. Check `metadata.csv` — verify `tier` and `is_superseded` columns
4. Check `changelog.csv` — verify entries logged correctly
5. Run `python main.py --clean-only` — verify cleaned PDFs appear in `cleaned_pdfs/`

---

## Open Questions

> [!IMPORTANT]  
> **Deployment preference**: Which deployment method fits your setup?
> - A) Windows Task Scheduler (local, simplest)
> - B) GitHub Actions / Cloud (for when you deploy the RAG system)
> - C) Just the Python API for now (call from your RAG code)
> 
> This doesn't affect the core code — just determines whether I add a scheduler config file.

"""
publish_hf.py -- Publish the cleaned IRDAI corpus to the Hugging Face dataset.

The Hugging Face dataset (Ashishmg10/irdai-corpus) holds the actual cleaned
PDFs. The GitHub repo only tracks code + metadata.csv + changelog.csv (the PDFs
are gitignored), so this script is the single place that pushes PDFs to HF.

Two modes
---------
incremental (default)
    Steady-state / scheduled runs. ADD the newly-cleaned PDFs sitting in
    cleaned_pdfs/ and DELETE only the files that metadata.csv marks as
    superseded (is_superseded=true) and that actually exist on HF. HF therefore
    accumulates the live corpus and outdated versions are replaced -- WITHOUT
    ever doing a blanket wipe. Safe even though the CI runner starts with an
    (almost) empty cleaned_pdfs/.

mirror
    Deliberate full rebuilds only (triggered with force=true, after metadata was
    cleared and the whole corpus re-crawled). Makes HF exactly match the local
    cleaned_pdfs/ -- uploading everything and deleting anything on HF that is no
    longer present locally (this is what purges stray files such as the old
    Integrity_Pledge_Hindi-.pdf). Guarded: it REFUSES to run when cleaned_pdfs/
    looks partial, so a half-finished crawl can never wipe the dataset.

Auth
----
Reads HF_TOKEN from the environment (the huggingface_hub default). In CI it is
provided via the HF_TOKEN repository secret.

Usage
-----
    python publish_hf.py --mode incremental          # scheduled runs
    python publish_hf.py --mode mirror               # after a --force rebuild
    python publish_hf.py --mode mirror --dry-run     # preview, change nothing
"""

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ID = "Ashishmg10/irdai-corpus"
REPO_TYPE = "dataset"
DEFAULT_CLEANED_DIR = "cleaned_pdfs"
DEFAULT_METADATA = "metadata.csv"

# Mirror safety guard: refuse a full mirror (which deletes remote files) unless
# the local corpus looks complete. This is what prevents a partial run --
# e.g. a CI job that only downloaded a handful of docs -- from wiping HF.
MIRROR_MIN_FILES = 50            # absolute floor of cleaned PDFs required
MIRROR_MIN_FRACTION = 0.5        # and >= this fraction of live metadata rows

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("publish_hf")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def load_metadata(metadata_path: str) -> tuple[set[str], set[str]]:
    """
    Read metadata.csv and split filenames into (live, superseded).

    - live       : rows whose is_superseded is not 'true'  -> should be on HF
    - superseded : rows whose is_superseded == 'true'       -> should be removed

    Returns two sets of *filenames* (the cleaned PDF names, which match the paths
    used on HF).
    """
    live: set[str] = set()
    superseded: set[str] = set()

    if not os.path.exists(metadata_path) or os.path.getsize(metadata_path) == 0:
        return live, superseded

    with open(metadata_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            filename = (row.get("filename") or "").strip()
            if not filename:
                continue
            if (row.get("is_superseded") or "").strip().lower() == "true":
                superseded.add(filename)
            else:
                live.add(filename)

    # A filename that appears both live and superseded (e.g. a name reused across
    # versions) is treated as live -- never delete something we still want.
    superseded -= live
    return live, superseded


def list_cleaned(cleaned_dir: str) -> set[str]:
    """Return the set of *.pdf filenames currently in cleaned_dir."""
    return {p.name for p in Path(cleaned_dir).glob("*.pdf")}


# ---------------------------------------------------------------------------
# Hugging Face helpers
# ---------------------------------------------------------------------------

def _get_api():
    """Instantiate an HfApi, failing clearly if the token/library is missing."""
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:  # pragma: no cover
        logger.error("huggingface_hub is required:  pip install huggingface_hub")
        raise SystemExit(1) from exc

    token = os.environ.get("HF_TOKEN")
    if not token:
        logger.error("HF_TOKEN is not set in the environment; cannot authenticate.")
        raise SystemExit(1)

    return HfApi(token=token)


def _list_remote_pdfs(api) -> set[str]:
    """Return the set of *.pdf paths currently in the HF dataset (empty if new)."""
    try:
        files = api.list_repo_files(repo_id=REPO_ID, repo_type=REPO_TYPE)
    except Exception as exc:  # repo may not exist yet
        logger.warning("Could not list remote files (new/empty repo?): %s", exc)
        return set()
    return {f for f in files if f.lower().endswith(".pdf")}


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def publish_incremental(cleaned_dir: str, metadata_path: str, dry_run: bool) -> int:
    """
    Additive publish: upload cleaned_pdfs/ and delete superseded files from HF.

    Never performs a blanket delete, so it is safe to run when cleaned_pdfs/
    holds only the current run's new files.
    """
    cleaned = list_cleaned(cleaned_dir)
    live, superseded = load_metadata(metadata_path)

    api = _get_api()
    remote = _list_remote_pdfs(api)

    # Only delete superseded files that are actually on HF (idempotent).
    to_delete = sorted(superseded & remote)

    logger.info("Incremental publish plan:")
    logger.info("  cleaned PDFs to upload : %d", len(cleaned))
    logger.info("  superseded on HF to remove: %d", len(to_delete))
    for name in to_delete:
        logger.info("    - delete %s", name)

    if not cleaned and not to_delete:
        logger.info("Nothing to upload and nothing to delete. Done.")
        return 0

    if dry_run:
        logger.info("[dry-run] No changes made.")
        return 0

    api.create_repo(repo_id=REPO_ID, repo_type=REPO_TYPE, exist_ok=True)

    if cleaned:
        logger.info("Uploading %d file(s) from %s/ ...", len(cleaned), cleaned_dir)
        api.upload_folder(
            folder_path=cleaned_dir,
            path_in_repo=".",
            repo_id=REPO_ID,
            repo_type=REPO_TYPE,
            allow_patterns=["*.pdf"],
            ignore_patterns=sorted(superseded) or None,  # never publish superseded
            commit_message=f"Add/update {len(cleaned)} cleaned PDF(s)",
        )

    deleted = 0
    for name in to_delete:
        try:
            api.delete_file(
                path_in_repo=name,
                repo_id=REPO_ID,
                repo_type=REPO_TYPE,
                commit_message=f"Remove superseded document {name}",
            )
            deleted += 1
        except Exception as exc:
            logger.warning("Could not delete %s (already gone?): %s", name, exc)

    logger.info("Incremental publish complete: %d uploaded, %d removed.",
                len(cleaned), deleted)
    return 0


def publish_mirror(cleaned_dir: str, metadata_path: str, dry_run: bool) -> int:
    """
    Full mirror: make HF exactly match the LIVE corpus -- cleaned_pdfs/ minus any
    files metadata.csv marks superseded. Uploads the live set and deletes every
    remote file that is not part of it (strays AND outdated/superseded versions).

    Converges on the same target as incremental mode, so a force rebuild and a
    scheduled run never disagree about what belongs on HF.

    GUARDED. Refuses to run unless the live set looks like a complete corpus,
    so a partial/failed crawl can never wipe the dataset.
    """
    cleaned = list_cleaned(cleaned_dir)
    live, superseded = load_metadata(metadata_path)

    # Desired HF state = files we have locally that are NOT superseded.
    skip_upload = sorted(cleaned & superseded)   # superseded but present locally
    desired = cleaned - superseded

    # --- Safety guard --------------------------------------------------------
    n_desired = len(desired)
    n_live = len(live)
    floor_ok = n_desired >= MIRROR_MIN_FILES
    fraction_ok = (n_live == 0) or (n_desired >= MIRROR_MIN_FRACTION * n_live)

    if not (floor_ok and fraction_ok):
        logger.error(
            "MIRROR ABORTED -- the live set looks partial (%d live PDFs; metadata "
            "lists %d live). Refusing to mirror, because --delete would wipe HF "
            "down to this partial set. Run a full --force rebuild first, or use "
            "--mode incremental.", n_desired, n_live,
        )
        return 2

    api = _get_api()
    remote = _list_remote_pdfs(api)
    to_delete = sorted(remote - desired)   # strays + superseded + removed docs

    logger.info("Mirror publish plan:")
    logger.info("  live PDFs to upload    : %d", n_desired)
    logger.info("  superseded skipped     : %d", len(skip_upload))
    logger.info("  remote PDFs (HF)       : %d", len(remote))
    logger.info("  remote files to remove : %d", len(to_delete))
    for name in to_delete:
        logger.info("    - delete %s", name)

    if dry_run:
        logger.info("[dry-run] No changes made.")
        return 0

    api.create_repo(repo_id=REPO_ID, repo_type=REPO_TYPE, exist_ok=True)

    logger.info("Mirroring %d live file(s) to HF (removes strays + superseded) ...",
                n_desired)
    # delete_patterns=["*"] removes every pre-existing remote file that is NOT in
    # this upload set; ignore_patterns keeps superseded files out of that set, so
    # they get pruned too. One atomic reconcile commit.
    api.upload_folder(
        folder_path=cleaned_dir,
        path_in_repo=".",
        repo_id=REPO_ID,
        repo_type=REPO_TYPE,
        allow_patterns=["*.pdf"],
        ignore_patterns=skip_upload or None,
        delete_patterns=["*"],
        commit_message=f"Mirror corpus: {n_desired} live PDF(s)",
    )
    logger.info("Mirror complete.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="publish_hf.py",
        description="Publish the cleaned IRDAI corpus to Hugging Face.",
    )
    parser.add_argument(
        "--mode",
        choices=["incremental", "mirror"],
        default="incremental",
        help="incremental (add + replace superseded) or mirror (full rebuild).",
    )
    parser.add_argument("--cleaned-dir", default=DEFAULT_CLEANED_DIR)
    parser.add_argument("--metadata", default=DEFAULT_METADATA)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan but make no changes on Hugging Face.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger.info("publish_hf: mode=%s  cleaned_dir=%s  dry_run=%s",
                args.mode, args.cleaned_dir, args.dry_run)

    if args.mode == "mirror":
        code = publish_mirror(args.cleaned_dir, args.metadata, args.dry_run)
    else:
        code = publish_incremental(args.cleaned_dir, args.metadata, args.dry_run)

    sys.exit(code)


if __name__ == "__main__":
    main()

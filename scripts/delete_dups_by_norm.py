#!/usr/bin/env python3
"""Delete duplicates grouped by (chat_id, normalized filename) keeping newest per group.

Usage:
  python scripts/delete_dups_by_norm.py --preview 10    # preview duplicate groups (no deletes)
  python scripts/delete_dups_by_norm.py --delete --yes  # perform deletion (irreversible)
  python scripts/delete_dups_by_norm.py --delete --yes --limit 10000  # limit processed docs

This script computes a normalized filename using the same logic as `FileIndex._normalize_filename`.
It will set `norm_filename` for processed docs before deleting duplicates.
"""
import argparse
import sys
import pathlib
from pprint import pprint

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config.settings import settings
from app.services.mongo import MongoService
from app.models.file_index_impl import FileIndex
from pymongo import UpdateOne
from datetime import datetime


def parse_args():
    p = argparse.ArgumentParser(description="Delete duplicate file docs by normalized filename")
    p.add_argument("--preview", type=int, default=0, help="Show up to N sample duplicate groups (no deletes)")
    p.add_argument("--delete", action="store_true", help="Perform deletion")
    p.add_argument("--yes", action="store_true", help="Skip interactive confirmation when --delete is used")
    p.add_argument("--limit", type=int, default=0, help="Limit number of documents to process (0=all)")
    return p.parse_args()


def to_timestamp_val(ts):
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, datetime):
        return ts.timestamp()
    try:
        # try ISO format
        return datetime.fromisoformat(str(ts)).timestamp()
    except Exception:
        return 0.0


def main():
    args = parse_args()

    mongo = MongoService(settings.MONGO_URI, settings.DB_NAME)
    mongo.connect()
    coll = mongo.db.get_collection("files")
    fi = FileIndex(mongo.db)

    print("Scanning documents to compute normalized filename and find duplicates...")

    projection = {"_id": 1, "chat_id": 1, "filename": 1, "norm_filename": 1, "title_tokens": 1, "message_id": 1, "timestamp": 1}
    cursor = coll.find({}, projection=projection)

    groups = {}
    processed = 0

    for d in cursor:
        if args.limit and processed >= args.limit:
            break
        processed += 1
        chat_id = d.get("chat_id")
        # compute norm (prefer existing norm, then title_tokens, then file normalizer)
        norm = None
        try:
            if d.get("norm_filename"):
                norm = (d.get("norm_filename") or "").strip()
            elif d.get("title_tokens"):
                norm = " ".join([t.lower() for t in d.get("title_tokens") if t])
            else:
                norm = fi._normalize_filename(d.get("filename") or "")
        except Exception:
            norm = fi._normalize_filename(d.get("filename") or "")

        if not norm:
            continue

        key = (int(chat_id), norm)
        groups.setdefault(key, []).append(d)

    # collect duplicate groups
    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    total_groups = len(dup_groups)
    total_docs = sum(len(v) for v in dup_groups.values())
    total_to_delete = sum(len(v) - 1 for v in dup_groups.values())

    print(f"Processed documents: {processed}")
    print(f"Duplicate groups found: {total_groups}")
    print(f"Total docs in duplicate groups: {total_docs}")
    print(f"Estimated docs to delete (keeping 1 per group): {total_to_delete}")

    if args.preview:
        print(f"Showing up to {args.preview} sample duplicate groups:")
        i = 0
        for (chat_id, norm), docs in dup_groups.items():
            if i >= args.preview:
                break
            print("---")
            print(f"chat_id={chat_id} norm='{norm}' group_size={len(docs)}")
            for dd in sorted(docs, key=lambda x: (to_timestamp_val(x.get("timestamp")), int(x.get("message_id") or 0)), reverse=True):
                print({"_id": str(dd.get("_id")), "message_id": dd.get("message_id"), "filename": dd.get("filename"), "timestamp": dd.get("timestamp")})
            i += 1

    if not args.delete:
        print("No deletion requested. Rerun with --delete --yes to perform deletions.")
        return

    if not args.yes:
        confirm = input("Type DELETE to permanently remove duplicate documents: ")
        if confirm.strip() != "DELETE":
            print("Confirmation failed; aborting.")
            return

    # Step 1: ensure norm_filename is set for processed docs (bulk update)
    print("Updating `norm_filename` for processed docs (bulk write)...")
    updates = []
    for (chat_id, norm), docs in groups.items():
        for d in docs:
            if (d.get("norm_filename") or "") != norm:
                updates.append(UpdateOne({"_id": d.get("_id")}, {"$set": {"norm_filename": norm}}))

    applied_updates = 0
    try:
        BATCH = 1000
        for i in range(0, len(updates), BATCH):
            batch = updates[i : i + BATCH]
            if not batch:
                continue
            res = coll.bulk_write(batch, ordered=False)
            # bulk_write does not easily report modified_count per op; count requests
            applied_updates += len(batch)
        print(f"Patched norm_filename for {applied_updates} documents (approx)")
    except Exception as e:
        print("Bulk update for norm_filename failed:", e)

    # Step 2: delete duplicates (keep newest per group)
    print("Deleting duplicate documents (keeping newest per chat+norm group)...")
    to_delete_ids = []
    for (chat_id, norm), docs in dup_groups.items():
        # sort by timestamp desc then message_id desc -> keep first
        sorted_docs = sorted(docs, key=lambda x: (to_timestamp_val(x.get("timestamp")), int(x.get("message_id") or 0)), reverse=True)
        keep = sorted_docs[0]
        to_delete = [d.get("_id") for d in sorted_docs[1:]]
        to_delete_ids.extend(to_delete)

    deleted_count = 0
    if to_delete_ids:
        from bson import ObjectId
        BATCH = 1000
        for i in range(0, len(to_delete_ids), BATCH):
            batch = to_delete_ids[i : i + BATCH]
            try:
                res = coll.delete_many({"_id": {"$in": batch}})
                deleted_count += int(res.deleted_count or 0)
            except Exception as e:
                print("Delete batch failed:", e)

    print(f"Deleted {deleted_count} documents from duplicate groups.")

    # Step 3: ensure remaining documents have norm_filename populated
    print("Running full normalization pass to populate any remaining `norm_filename` fields (may take a while)...")
    # Call existing migration script to populate remaining norm_filename values
    try:
        import subprocess
        subprocess.Popen([sys.executable, "scripts/compute_norm_filenames.py", "--apply"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print("Started `compute_norm_filenames.py --apply` in background.")
    except Exception as e:
        print("Failed to start normalization script:", e)

    print("Operation complete. Summary:")
    print(f"  Processed docs scanned: {processed}")
    print(f"  Duplicate groups found: {total_groups}")
    print(f"  Documents deleted: {deleted_count}")


if __name__ == "__main__":
    main()

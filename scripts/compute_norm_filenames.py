#!/usr/bin/env python3
"""Compute and set `norm_filename` field for existing documents in `files` collection.

This migration fills missing `norm_filename` using `title_tokens` when available,
otherwise falls back to the filename normalizer used by FileIndex.

Usage:
  python scripts/compute_norm_filenames.py --preview 50  # show first 50 updates without writing
  python scripts/compute_norm_filenames.py --apply --limit 1000  # apply up to 1000 updates
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", type=int, default=0, help="Show sample updates (no write)")
    parser.add_argument("--apply", action="store_true", help="Apply updates")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of updates (0==no limit)")
    args = parser.parse_args()

    mongo = MongoService(settings.MONGO_URI, settings.DB_NAME)
    mongo.connect()
    coll = mongo.db.get_collection("files")
    fi = FileIndex(mongo.db)

    q = {"$or": [{"norm_filename": {"$exists": False}}, {"norm_filename": ""}]}
    total = coll.count_documents(q)
    print(f"Documents missing norm_filename: {total}")
    if total == 0:
        return

    limit = args.limit or total
    cursor = coll.find(q, {"_id": 1, "filename": 1, "title_tokens": 1}).limit(limit)

    to_update = []
    for d in cursor:
        fname = d.get("filename") or ""
        tt = d.get("title_tokens") or []
        if tt:
            norm = " ".join([t.lower() for t in tt if t])
        else:
            norm = fi._normalize_filename(fname)
        to_update.append((d.get("_id"), fname, norm))

    if args.preview:
        print(f"Previewing up to {args.preview} updates")
        for _id, fname, norm in to_update[: args.preview]:
            pprint({"_id": str(_id), "filename": fname, "norm_filename": norm})
        return

    if not args.apply:
        print("No --apply flag; exiting after preview. Use --apply to perform updates.")
        return

    applied = 0
    for _id, fname, norm in to_update:
        try:
            coll.update_one({"_id": _id}, {"$set": {"norm_filename": norm}})
            applied += 1
        except Exception as e:
            print("Failed to update", _id, e)
    print(f"Applied updates: {applied}")


if __name__ == "__main__":
    main()

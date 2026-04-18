#!/usr/bin/env python3
"""Preview and optionally delete video-like docs from the MongoDB `files` collection.

Usage:
  python scripts/delete_mp4_docs.py --preview 10         # show matches
  python scripts/delete_mp4_docs.py --preview 10 --delete # delete matches (non-reversible)

This script reads DB settings from `app.config.settings` which in turn loads `.env`.
"""
import argparse
import sys
import pathlib
from pprint import pprint

# Ensure project root is on sys.path so `import app` works when running the script
_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from app.config.settings import settings
except Exception as e:
    print("Failed to import project settings (are you running from project root?)", e)
    sys.exit(2)

from pymongo import MongoClient

EXTS = ["mp4", "mkv", "webm", "mov", "avi", "flv", "mpeg", "mpg"]


def build_query():
    filename_regex = r"\.({})$".format("|".join(EXTS))
    # Match by extension field, filename by extension or any occurrence of .mp4,
    # or codec indicating video/mp4 or audio/mp4
    return {
        "$or": [
            {"extension": {"$in": EXTS}},
            {"filename": {"$regex": filename_regex, "$options": "i"}},
            {"filename": {"$regex": r"\\.mp4", "$options": "i"}},
            {"codec": {"$regex": r"(video/|mp4)", "$options": "i"}},
        ]
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", type=int, default=10, help="How many example docs to show")
    parser.add_argument("--delete", action="store_true", help="Delete matching documents (irreversible)")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation when --delete is provided")
    args = parser.parse_args()

    uri = settings.MONGO_URI
    dbname = settings.DB_NAME
    print(f"Connecting to MongoDB: {dbname} (URI hidden)")

    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    db = client[dbname]
    coll = db.get_collection("files")

    q = build_query()

    try:
        count = coll.count_documents(q)
    except Exception as e:
        print("Failed to run count_documents:", e)
        sys.exit(3)

    print(f"Found {count} matching documents (video-like by extension/codec)")

    if count == 0:
        print("No documents to preview or delete.")
        return

    print(f"Showing up to {args.preview} sample documents:")
    try:
        cursor = coll.find(q, {"_id": 1, "chat_id": 1, "message_id": 1, "filename": 1, "extension": 1, "codec": 1, "timestamp": 1}).sort("timestamp", -1).limit(args.preview)
        for d in cursor:
            pprint(d)
    except Exception as e:
        print("Failed to fetch sample docs:", e)

    if not args.delete:
        print("No deletion requested. Rerun with --delete to remove these documents.")
        return

    # Confirm with the user one last time unless --yes provided
    if not args.yes:
        confirm = input("Type DELETE to permanently remove all matching documents: ")
        if confirm.strip() != "DELETE":
            print("Confirmation failed; aborting.")
            return

    try:
        res = coll.delete_many(q)
        print(f"Deleted {res.deleted_count} documents.")
    except Exception as e:
        print("Deletion failed:", e)
        sys.exit(4)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Dump a few sample documents from the files collection for inspection.
Usage: python scripts/inspect_files.py --limit 10
"""
import sys
import pathlib

# ensure project root on path
_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from app.config.settings import settings
except Exception as e:
    print("Failed to import settings:", e)
    sys.exit(2)

from pymongo import MongoClient
from pprint import pprint
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--limit", type=int, default=10)
args = parser.parse_args()

client = MongoClient(settings.MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[settings.DB_NAME]
coll = db.get_collection('files')

print("Total docs in files collection (approx):", coll.estimated_document_count())

for d in coll.find({}).sort('timestamp', -1).limit(args.limit):
    pprint(d)
    print('-'*80)

#!/usr/bin/env python3
"""Quick search preview script to show deduped results and norm_filename."""
import sys
import pathlib
import json

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config.settings import settings
from app.services.mongo import MongoService
from app.models.file_index_impl import FileIndex
from app.services.tokenizer import tokenize_query


def main():
    q = sys.argv[1] if len(sys.argv) > 1 else "sex"
    mongo = MongoService(settings.MONGO_URI, settings.DB_NAME)
    mongo.connect()
    fi = FileIndex(mongo.db)
    tokens = tokenize_query(q)
    res = fi.search_with_ranking(tokens, query=q, page=1, per_page=50)
    print(f"Query: {q} — total deduped results: {res.get('total')}")
    for r in res.get("results", []):
        print(json.dumps({
            "filename": r.get("filename"),
            "norm_filename": r.get("norm_filename"),
            "chat_id": r.get("chat_id"),
            "message_id": r.get("message_id"),
            "_score": r.get("_score"),
        }, default=str))


if __name__ == "__main__":
    main()

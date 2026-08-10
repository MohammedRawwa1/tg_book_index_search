# peer_manager.py
from typing import List
from pymongo import MongoClient
import os

try:
    from app.config.settings import resolve_mongo_uri

    MONGO_URI = resolve_mongo_uri()
except Exception:
    # peer_manager may be imported as a bare top-level module (e.g. by
    # ping_peers.py) without the project root on sys.path; fall back to a
    # robust env read that checks all accepted names, skips blank values
    # and strips quotes.
    def _resolve_mongo_uri_fallback() -> str:
        for _name in (
            "MONGO_URI",
            "mongo_uri",
            "MONGODB_URI",
            "mongodb_uri",
            "MONGODB_URL",
            "mongodb_url",
            "DATABASE_URL",
            "database_url",
        ):
            _raw = os.getenv(_name)
            if _raw:
                _val = _raw.strip()
                if len(_val) >= 2 and _val[0] == _val[-1] and _val[0] in "\"'":
                    _val = _val[1:-1].strip()
                if _val:
                    return _val
        return "mongodb://localhost:27017/tg_index"

    MONGO_URI = _resolve_mongo_uri_fallback()

DB_NAME = os.getenv("DB_NAME", "tg_index")
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
peers_col = db['peers']


def add_peer(chat_id: int):
    """Add a new peer to the DB if it doesn't exist yet."""
    peers_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id}},
        upsert=True
    )


def get_all_peers() -> List[int]:
    """Return list of all stored peer chat IDs."""
    return [doc['chat_id'] for doc in peers_col.find()]

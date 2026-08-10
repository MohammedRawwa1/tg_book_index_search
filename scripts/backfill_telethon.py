#!/usr/bin/env python3
"""Telethon-based backfill script.

Iterates chat history using Telethon which exposes `reply_to.reply_to_top_id`
making topic/thread-scoped backfills reliable. Writes to Mongo using
`app.services.mongo.MongoService` and uses FileIndex.bulk_upsert_files for
efficient upserts.

Usage:
  python scripts/backfill_telethon.py <chat_id> [--thread-id THREAD_ID] [--limit N] [--dry-run]

Requires: API_ID (or TELETHON_API_ID / telethon_api_id / TELEGRAM_APP_ID),
API_HASH (or TELETHON_API_HASH / telethon_api_hash / TELEGRAM_API_HASH), or an
existing .session file. The Mongo connection string is read from MONGO_URI /
MONGODB_URI / MONGODB_URL / DATABASE_URL (falling back to app settings).
"""

import argparse
import asyncio
import os
import sys
import json
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from telethon import TelegramClient
from telethon.tl.types import Message

from app.services.mongo import MongoService
from app.models.file_index_impl import FileIndex
from app.services.tokenizer import tokenize_filename
from app.config.settings import settings, resolve_mongo_uri


def _first_env(*names: str):
    """Return the first set, non-empty env var among the given names."""
    for name in names:
        val = os.getenv(name)
        if val and val.strip():
            return val.strip()
    return None


# Accepted env var names (priority order) for Telethon API credentials.
# API_ID/API_HASH (fully uppercase) are checked first, matching the rest of
# the app; the Telethon-prefixed names remain as fallbacks.
_API_ID_ENV_NAMES = ("API_ID", "TELETHON_API_ID", "telethon_api_id", "TELEGRAM_APP_ID")
_API_HASH_ENV_NAMES = ("API_HASH", "TELETHON_API_HASH", "telethon_api_hash", "TELEGRAM_API_HASH")
# Accepted env var names (priority order) for the MongoDB connection string
_MONGO_URI_ENV_NAMES = (
    "MONGO_URI",
    "mongo_uri",
    "MONGODB_URI",
    "mongodb_uri",
    "MONGODB_URL",
    "mongodb_url",
    "DATABASE_URL",
    "database_url",
)


def _choose_telethon_client():
    api_id = _first_env(*_API_ID_ENV_NAMES)
    api_hash = _first_env(*_API_HASH_ENV_NAMES)
    session = _first_env("TELETHON_SESSION", "SESSION_NAME") or "telethon_backfill"
    if not (api_id and api_hash):
        raise RuntimeError(
            "Telethon API credentials missing: set API_ID and API_HASH in env "
            "(or TELETHON_API_ID/TELETHON_API_HASH)"
        )
    try:
        api_id = int(api_id)
    except Exception:
        pass
    return TelegramClient(str(session), api_id, str(api_hash))


def _resolve_mongo_uri() -> str:
    """Resolve the MongoDB connection string from env, falling back to app settings.

    Delegates to the shared ``app.config.settings.resolve_mongo_uri`` so blank
    values (e.g. ``mongo_uri=""``) and literal quotes are handled consistently
    with the rest of the app.
    """
    return resolve_mongo_uri()


async def run_backfill(chat_id: int, thread_id: int | None, limit: int = 20000, dry_run: bool = False):
    client = _choose_telethon_client()

    mongo = MongoService(_resolve_mongo_uri(), settings.DB_NAME)
    try:
        mongo.connect()
        mongo.ensure_indexes()
    except Exception as exc:
        print("Mongo init failed:", exc)
        if not mongo.uri.startswith(("mongodb://", "mongodb+srv://")):
            print(
                f"Hint: the connection string {mongo.uri!r} is not a valid Mongo URI. "
                f"Set one of these env vars to a mongodb:// or mongodb+srv:// string: "
                f"{', '.join(_MONGO_URI_ENV_NAMES)}"
            )
        return

    file_index = FileIndex(mongo.db)

    stats = {
        "scanned": 0,
        "indexed": 0,
        "skipped_no_book": 0,
        "skipped_no_filename": 0,
        "skipped_bad_ext": 0,
    }

    batch = []
    BATCH_SIZE = int(os.getenv("BACKFILL_BATCH_SIZE", "500"))
    # Allowed document extensions (comma separated). Default = common ebook/doc formats.
    allowed_exts_raw = os.getenv(
        "ALLOWED_DOC_EXTENSIONS",
        "pdf,epub,mobi,azw3,azw,cbz,cbr,txt,rtf,doc,docx,odt",
    )
    try:
        ALLOWED_EXTS = {e.strip().lower() for e in allowed_exts_raw.split(",") if e.strip()}
    except Exception:
        ALLOWED_EXTS = {"pdf", "epub", "mobi", "azw3", "azw", "txt", "doc", "docx", "odt"}
    # Accept any filename-like documents; backfill should index documents (books)

    def _extract_filename_from_attachment(attachment, msg):
        # common direct attributes
        filename = getattr(attachment, "file_name", None) or getattr(attachment, "name", None)
        # Telethon Document often stores filename in attributes
        attrs = getattr(attachment, "attributes", None) or getattr(attachment, "attrs", None)
        if not filename and attrs:
            for a in attrs:
                fname = getattr(a, "file_name", None) or getattr(a, "fileName", None) or getattr(a, "filename", None)
                if fname:
                    filename = fname
                    break

        # try to find a filename-like token in caption/text (any extension)
        if not filename:
            caption = getattr(msg, "message", None) or getattr(msg, "text", None) or getattr(msg, "caption", None)
            if caption:
                # match any token that looks like name.ext (up to 8-char ext)
                pattern = r'([\w\-\s\(\)\[\]\.]+\.[A-Za-z0-9]{1,8})'
                m = re.search(pattern, caption, flags=re.IGNORECASE)
                if m:
                    filename = m.group(1).strip()

        # fallback: derive a filename from mime type
        if not filename:
            mime = getattr(attachment, "mime_type", None) or getattr(attachment, "mime", None)
            ext = None
            if mime and '/' in mime:
                subtype = mime.split('/')[-1].lower()
                # Try to use subtype as extension when reasonable
                if subtype and len(subtype) <= 8:
                    if subtype == 'x-matroska':
                        ext = 'mkv'
                    elif subtype == 'quicktime':
                        ext = 'mov'
                    else:
                        ext = subtype
            if ext:
                mid = getattr(msg, "id", getattr(msg, "message_id", None))
                filename = f"{mid}.{ext}" if mid else f"book.{ext}"

        return filename

    async with client:
        print("Telethon client started")
        # Telethon's iter_messages is efficient and yields newest->oldest by default
        async for msg in client.iter_messages(chat_id, limit=limit):
            stats["scanned"] += 1
            try:
                # telethon Message: check reply_to.reply_to_top_id or message.top_id
                belongs = False
                if thread_id is None:
                    belongs = True
                else:
                    # prefer top-level id
                    rt = None
                    try:
                        if getattr(msg, "reply_to_msg_id", None):
                            # telethon exposes reply_to_msg_id but nested object may be present
                            if getattr(msg, "reply_to", None) and getattr(msg.reply_to, "reply_to_top_id", None):
                                rt = msg.reply_to.reply_to_top_id
                    except Exception:
                        rt = None
                    # also check msg.top_id attribute (older Telethon versions)
                    try:
                        if rt is None and getattr(msg, "top_id", None):
                            rt = msg.top_id
                    except Exception:
                        pass
                    # finally, some messages may have msg.reply_to_msg_id equal to thread root
                    try:
                        if rt is None and getattr(msg, "reply_to_msg_id", None):
                            rt = getattr(msg, "reply_to_msg_id")
                    except Exception:
                        pass

                    if rt is not None:
                        try:
                            if int(rt) == int(thread_id):
                                belongs = True
                        except Exception:
                            belongs = False

                if not belongs:
                    continue

                # extract attachment (only document attachments; skip video/audio messages)
                attachment = getattr(msg, "document", None)
                if not attachment:
                    stats["skipped_no_book"] += 1
                    continue

                filename = _extract_filename_from_attachment(attachment, msg)
                # compute extension from discovered filename for indexing
                try:
                    ext = (filename.rsplit('.', 1)[1] or "") if filename and '.' in filename else ""
                    ext = ext.lower()
                except Exception:
                    ext = ""
                if not filename:
                    stats["skipped_no_filename"] += 1
                    continue

                # Enforce allowed extensions whitelist
                try:
                    if not ext or ext not in ALLOWED_EXTS:
                        stats["skipped_bad_ext"] += 1
                        continue
                except Exception:
                    stats["skipped_bad_ext"] += 1
                    continue

                token_struct = tokenize_filename(filename)
                file_id = getattr(attachment, "file_id", None)
                file_size = getattr(attachment, "file_size", None)
                duration = getattr(attachment, "duration", None)
                width = getattr(attachment, "w", None) or getattr(attachment, "width", None)
                height = getattr(attachment, "h", None) or getattr(attachment, "height", None)

                doc = {
                    "chat_id": int(chat_id),
                    "message_thread_id": int(thread_id) if thread_id is not None else None,
                    "message_id": int(msg.id),
                    "filename": filename,
                    "extension": ext,
                    "title_tokens": token_struct.get("title_tokens", []),
                    "quality_tokens": token_struct.get("quality_tokens", []),
                    "codec_tokens": token_struct.get("codec_tokens", []),
                    "year": token_struct.get("year"),
                    "other_tokens": token_struct.get("other", []),
                    "timestamp": msg.date,
                    "file_id": file_id,
                    "file_size": file_size,
                    "duration": duration,
                    "width": width,
                    "height": height,
                }

                batch.append(doc)
                if len(batch) >= BATCH_SIZE:
                    if not dry_run:
                        file_index.bulk_upsert_files(batch)
                        stats["indexed"] += len(batch)
                    else:
                        stats["indexed"] += len(batch)
                    batch = []

            except Exception:
                continue

        if batch:
            if not dry_run:
                file_index.bulk_upsert_files(batch)
                stats["indexed"] += len(batch)
            else:
                stats["indexed"] += len(batch)

    print(
        f"Telethon backfill complete: scanned={stats['scanned']} indexed={stats['indexed']} "
        f"skipped_no_book={stats['skipped_no_book']} skipped_no_filename={stats['skipped_no_filename']} "
        f"skipped_bad_ext={stats.get('skipped_bad_ext', 0)}"
    )


def main():
    parser = argparse.ArgumentParser(description="Telethon backfill for chat/topic")
    parser.add_argument("chat", help="Target chat id or username")
    parser.add_argument("--thread-id", type=int, default=None, help="Topic/thread id to limit to")
    parser.add_argument("--limit", type=int, default=20000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    chat = args.chat
    thread_id = args.thread_id
    limit = args.limit
    dry_run = args.dry_run

    # normalize chat id
    try:
        chat_id = int(chat)
    except Exception:
        chat_id = chat

    asyncio.run(run_backfill(chat_id, thread_id=thread_id, limit=limit, dry_run=dry_run))


if __name__ == "__main__":
    main()

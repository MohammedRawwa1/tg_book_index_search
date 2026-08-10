import asyncio
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from app.config.settings import settings
from motor.motor_asyncio import AsyncIOMotorClient
from app.handlers.indexer import backfill_history
from app.utils.logger import logger


def register_admin_handlers(client: Client, mongo):
    """
    Registers owner-only admin commands.
    """

    def _is_owner(user_id: Optional[int]) -> bool:
        return (
            settings.OWNER_ID is not None
            and user_id is not None
            and int(user_id) == int(settings.OWNER_ID)
        )

    # Ensure admin task storage
    if not hasattr(client, "_admin_tasks"):
        client._admin_tasks = {}

    # --- /stats ---
    @client.on_message(filters.command("stats"))
    async def _stats(client: Client, message: Message):
        if not _is_owner(message.from_user.id):
            return await message.reply_text("Unauthorized")

        files_col = mongo.db.get_collection("files")
        state = mongo.db.get_collection("index_state").find_one({}) or {}

        total = files_col.count_documents({})
        dups = files_col.count_documents({"is_duplicate": True})
        last = state.get("last_message_id", "N/A")

        await message.reply_text(
            f"Total files: {total}\nDuplicates: {dups}\nLast indexed: {last}"
        )

    # --- /health ---
    @client.on_message(filters.command("health"))
    async def _health(client: Client, message: Message):
        if not _is_owner(message.from_user.id):
            return await message.reply_text("Unauthorized")

        try:
            mongo.client.admin.command("ping")
            await message.reply_text("OK: MongoDB reachable")
        except Exception:
            await message.reply_text("ERROR: MongoDB ping failed")

    # --- /reindex ---
    @client.on_message(filters.command("reindex"))
    async def _reindex(client: Client, message: Message):
        if not _is_owner(message.from_user.id):
            return await message.reply_text("Unauthorized")

        args = message.text.split(maxsplit=1)
        if len(args) == 1:
            return await message.reply_text(
                "Usage: /reindex <chat_id>\nExample: /reindex -1001234567890"
            )

        try:
            target_chat_id = int(args[1].strip())
        except Exception:
            return await message.reply_text("Invalid chat ID")

        await message.reply_text(f"Starting backfill for {target_chat_id}")
        asyncio.get_running_loop().create_task(
            backfill_history(client, mongo, target_chat_id)
        )

    # --- /clear_all ---
    @client.on_message(filters.command("clear_all"))
    async def _clear_all(client: Client, message: Message):
        logger.info("ENTRY: /clear_all handler triggered. user_id={} chat_id={}", message.from_user.id if message.from_user else None, message.chat.id if message.chat else None)
        try:
            if not _is_owner(message.from_user.id):
                logger.warning("/clear_all unauthorized access attempt by user_id={}", message.from_user.id)
                return await message.reply_text("Unauthorized")

            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Delete", callback_data="C|delete"),
                        InlineKeyboardButton("Drop", callback_data="C|drop"),
                    ],
                    [InlineKeyboardButton("Cancel", callback_data="C|cancel")],
                ]
            )

            logger.info("Sending clear_all confirmation menu to chat_id={}", message.chat.id if message.chat else None)
            sent = await message.reply_text(
                "Clear ALL indexed files?\nDelete = safer, Drop = faster.",
                reply_markup=kb,
            )
            logger.info("clear_all confirmation menu sent successfully. message_id={}", sent.message_id if sent else None)
        except Exception as e:
            logger.exception("CATCH-ALL: Exception in /clear_all handler: {}", e)
            await message.reply_text("Error in clear_all handler. Check logs.")

    # --- /admin ---
    @client.on_message(filters.command("admin"))
    async def _admin_panel(client: Client, message: Message):
        if not _is_owner(message.from_user.id):
            return await message.reply_text("Unauthorized")

        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Start", callback_data="A|start"),
                    InlineKeyboardButton("Stop", callback_data="A|stop"),
                ],
                [InlineKeyboardButton("Stats", callback_data="A|stats")],
            ]
        )

        await message.reply_text("Admin panel", reply_markup=kb)


# --- CALLBACK HANDLER (OUTSIDE, not indented!) ---
async def handle_admin_callback(client: Client, cq: CallbackQuery):
    mongo = getattr(client, "_mongo", None)

    def _is_owner(user_id):
        return (
            settings.OWNER_ID is not None
            and user_id is not None
            and int(user_id) == int(settings.OWNER_ID)
        )

    data = cq.data or ""

    if not _is_owner(cq.from_user.id if cq.from_user else None):
        return await cq.answer("Unauthorized", show_alert=True)

    # --- CLEAR ACTIONS ---
    if data.startswith("C|"):
        action = data.split("|")[1]

        try:
            # Try using attached mongo instance (may be a sync PyMongo wrapper)
            performed = False
            if mongo is not None:
                try:
                    if action == "delete":
                        mongo.db.get_collection("files").delete_many({})
                        mongo.db.get_collection("index_state").delete_many({})
                    elif action == "drop":
                        try:
                            mongo.db.drop_collection("files")
                        except Exception:
                            pass
                        try:
                            mongo.db.drop_collection("index_state")
                        except Exception:
                            pass
                    performed = True
                except Exception:
                    logger.exception("Admin clear: attached mongo delete/drop failed, will fallback to async client")

            if not performed:
                # Fallback: create a temporary AsyncIOMotorClient and perform action
                try:
                    mclient = AsyncIOMotorClient(settings.MONGO_URI)
                    mdb = mclient[settings.DB_NAME]
                    if action == "delete":
                        await mdb.get_collection("files").delete_many({})
                        await mdb.get_collection("index_state").delete_many({})
                    elif action == "drop":
                        try:
                            await mdb.drop_collection("files")
                        except Exception:
                            pass
                        try:
                            await mdb.drop_collection("index_state")
                        except Exception:
                            pass
                    try:
                        mclient.close()
                    except Exception:
                        pass
                    performed = True
                except Exception:
                    logger.exception("Admin clear: fallback AsyncIOMotorClient operation failed")

            # Report results to user
            if action == "delete":
                await cq.message.edit_text("Documents deleted.")
                await cq.answer("Deleted")
            elif action == "drop":
                await cq.message.edit_text("Collections dropped.")
                await cq.answer("Dropped")
            else:
                await cq.message.edit_text("Cancelled.")
                await cq.answer()

        except Exception as e:
            logger.exception("Clear failed: {}", e)
            await cq.answer("Error", show_alert=True)

        return

    # --- ADMIN ACTIONS ---
    if not data.startswith("A|"):
        return

    cmd = data.split("|")[1]
    target = settings.TARGET_CHAT_ID or settings.OWNER_ID
    target = int(target) if target else None

    tasks = getattr(client, "_admin_tasks", {})

    if cmd == "start":
        if not target:
            return await cq.answer("No target", show_alert=True)

        if target in tasks and not tasks[target].done():
            return await cq.answer("Already running", show_alert=True)

        task = asyncio.get_running_loop().create_task(
            backfill_history(client, mongo, target)
        )
        tasks[target] = task
        await cq.answer("Started")

    elif cmd == "stop":
        task = tasks.get(target)
        if not task:
            return await cq.answer("No task", show_alert=True)

        task.cancel()
        await cq.answer("Stopped")

    elif cmd == "stats":
        files = mongo.db.get_collection("files")
        total = files.count_documents({})
        dups = files.count_documents({"is_duplicate": True})

        await cq.message.reply_text(
            f"Total: {total}\nDuplicates: {dups}"
        )
        await cq.answer()
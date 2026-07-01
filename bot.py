"""Telegram -> Immich uploader bot.

Listens (via long-polling, no inbound ports) for photos, image documents and
videos from allowed users, uploads them to Immich, and drops every asset into a
fixed album. Feedback is given as a message reaction, with a text fallback when
the reaction emoji is not accepted by Telegram.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import logging
import mimetypes
import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from PIL import Image
from telegram import Message, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
)
log = logging.getLogger("immich-telegram")

# --- Configuration (from environment / .env via docker-compose) --------------

# Load a local .env when running directly (e.g. `python bot.py`). Does NOT
# override variables already set in the environment, so docker-compose's
# `env_file` / real env vars always win.
load_dotenv()


def env(key: str, default: str = "") -> str:
    """Read an env var, stripping a single pair of surrounding quotes.

    python-dotenv strips quotes but Docker Compose's `env_file` keeps them
    literal, so we normalize here for consistent behavior in both modes.
    """
    val = os.environ.get(key, default).strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    return val


TELEGRAM_TOKEN = env("TELEGRAM_TOKEN") or os.environ["TELEGRAM_TOKEN"]
IMMICH_URL = env("IMMICH_URL").rstrip("/") or os.environ["IMMICH_URL"]
IMMICH_API_KEY = env("IMMICH_API_KEY") or os.environ["IMMICH_API_KEY"]
ALLOWED_USERS = {
    int(x) for x in env("ALLOWED_USERS").split(",") if x.strip()
}
IMMICH_ALBUM = env("IMMICH_ALBUM", "Telegram")
DEVICE_ID = env("IMMICH_DEVICE_ID", "telegram-bot")

REACT_OK = env("REACT_OK", "\U0001F44D")        # 👍
REACT_DUPLICATE = env("REACT_DUPLICATE", "\U0001F440")  # 👀
REACT_FAIL = env("REACT_FAIL", "\U0001F44E")    # 👎

# Telegram Bot API caps file downloads at 20 MB over long-polling.
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024

# Restrict which Pillow decoders run on attacker-influenced bytes (defense in
# depth: limits the image-parser attack surface to formats we expect).
SAFE_IMAGE_FORMATS = ["JPEG", "PNG", "WEBP", "GIF", "TIFF", "BMP", "MPO"]

IMMICH_HEADERS = {"x-api-key": IMMICH_API_KEY, "Accept": "application/json"}

# Album id is resolved once and cached; the lock prevents two concurrent
# uploads from each creating the album.
_album_id: Optional[str] = None
_album_lock = asyncio.Lock()


# --- Immich helpers -----------------------------------------------------------

async def get_album_id(client: httpx.AsyncClient) -> str:
    """Return the id of IMMICH_ALBUM, creating the album if it doesn't exist."""
    global _album_id
    if _album_id is not None:
        return _album_id
    async with _album_lock:
        if _album_id is not None:
            return _album_id
        resp = await client.get(f"{IMMICH_URL}/api/albums", headers=IMMICH_HEADERS)
        resp.raise_for_status()
        for album in resp.json():
            if album.get("albumName") == IMMICH_ALBUM:
                _album_id = album["id"]
                log.info("Using existing album %r (%s)", IMMICH_ALBUM, _album_id)
                return _album_id
        resp = await client.post(
            f"{IMMICH_URL}/api/albums",
            headers=IMMICH_HEADERS,
            json={"albumName": IMMICH_ALBUM},
        )
        resp.raise_for_status()
        _album_id = resp.json()["id"]
        log.info("Created album %r (%s)", IMMICH_ALBUM, _album_id)
        return _album_id


async def add_to_album(client: httpx.AsyncClient, asset_id: str) -> None:
    album_id = await get_album_id(client)
    resp = await client.put(
        f"{IMMICH_URL}/api/albums/{album_id}/assets",
        headers=IMMICH_HEADERS,
        json={"ids": [asset_id]},
    )
    resp.raise_for_status()


async def upload_to_immich(
    client: httpx.AsyncClient,
    data: bytes,
    filename: str,
    created_at: dt.datetime,
    content_type: str,
) -> dict:
    """Upload bytes to Immich. Returns the parsed JSON ({'id', 'status'})."""
    checksum = hashlib.sha1(data).hexdigest()
    iso = _iso_z(created_at)
    files = {
        "assetData": (filename, data, content_type),
    }
    form = {
        "deviceAssetId": f"telegram-{checksum}",
        "deviceId": DEVICE_ID,
        "fileCreatedAt": iso,
        "fileModifiedAt": iso,
    }
    resp = await client.post(
        f"{IMMICH_URL}/api/assets",
        headers=IMMICH_HEADERS,
        data=form,
        files=files,
    )
    resp.raise_for_status()
    return resp.json()


# --- Timestamp / content helpers ---------------------------------------------

def _iso_z(value: dt.datetime) -> str:
    """Format a datetime as Immich-friendly ISO8601 with millis and 'Z'."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    value = value.astimezone(dt.timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def exif_datetime(data: bytes) -> Optional[dt.datetime]:
    """Best-effort EXIF DateTimeOriginal (tag 36867) -> naive datetime."""
    try:
        with Image.open(io.BytesIO(data), formats=SAFE_IMAGE_FORMATS) as img:
            exif = img.getexif()
            # 36867 = DateTimeOriginal, 306 = DateTime (fallback)
            raw = exif.get(36867) or exif.get(306)
        if not raw:
            return None
        return dt.datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
    except Exception:  # noqa: BLE001 - EXIF parsing is strictly best-effort
        return None


def _guess_content_type(filename: str, fallback: Optional[str]) -> str:
    if fallback:
        return fallback
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


# --- Reactions ----------------------------------------------------------------

async def react(message: Message, emoji: str, fail_text: Optional[str] = None) -> None:
    """Set a reaction; fall back to a text reply if Telegram rejects the emoji."""
    try:
        await message.set_reaction(reaction=emoji)
    except (BadRequest, TelegramError) as exc:
        log.debug("Reaction %s rejected (%s); using text fallback", emoji, exc)
        if fail_text:
            try:
                await message.reply_text(fail_text)
            except TelegramError:
                log.warning("Could not send fallback reply", exc_info=True)


# --- Handler ------------------------------------------------------------------

def _extract_media(msg: Message):
    """Return (telegram_file_ref, filename, declared_mime, file_size, is_image)."""
    if msg.photo:  # list of sizes, last is largest
        photo = msg.photo[-1]
        return photo, f"{photo.file_unique_id}.jpg", "image/jpeg", photo.file_size, True
    if msg.video:
        v = msg.video
        name = v.file_name or f"{v.file_unique_id}.mp4"
        return v, name, v.mime_type or "video/mp4", v.file_size, False
    if msg.document:
        d = msg.document
        mime = d.mime_type or ""
        is_image = mime.startswith("image/")
        name = d.file_name or f"{d.file_unique_id}"
        return d, name, mime or None, d.file_size, is_image
    return None, None, None, None, False


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if msg is None:
        return
    # Fail-closed: an empty whitelist allows nobody.
    if not ALLOWED_USERS or user is None or user.id not in ALLOWED_USERS:
        log.warning("Ignoring media from unauthorized user %s", user.id if user else "?")
        return

    media, filename, declared_mime, file_size, is_image = _extract_media(msg)
    if media is None:
        return

    if file_size and file_size > TELEGRAM_DOWNLOAD_LIMIT:
        mb = file_size / 1024 / 1024
        log.warning("File %s too large for Bot API download: %.1f MB", filename, mb)
        await react(
            msg,
            REACT_FAIL,
            f"Too large ({mb:.0f} MB). Telegram bots can only download files "
            "up to 20 MB over polling.",
        )
        return

    client: httpx.AsyncClient = context.application.bot_data["client"]
    try:
        tg_file = await media.get_file()
        data = bytes(await tg_file.download_as_bytearray())

        created_at = exif_datetime(data) if is_image else None
        if created_at is None and msg.forward_origin is not None:
            # Forwarded media: prefer the original send time over the forward time.
            created_at = msg.forward_origin.date
        if created_at is None:
            created_at = msg.date
        content_type = _guess_content_type(filename, declared_mime)

        result = await upload_to_immich(client, data, filename, created_at, content_type)
        status = result.get("status", "created")
        asset_id = result.get("id")
        if asset_id:
            await add_to_album(client, asset_id)

        if status == "duplicate":
            await react(msg, REACT_DUPLICATE, "Already in Immich.")
        else:
            await react(msg, REACT_OK)
        log.info("Uploaded %s -> %s (%s)", filename, asset_id, status)
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:300]
        log.error("Immich rejected %s: %s %s", filename, exc.response.status_code, body)
        await react(msg, REACT_FAIL, f"Upload failed: {exc.response.status_code}")
    except Exception:  # noqa: BLE001 - keep detail in logs, not in the chat reply
        log.exception("Failed to handle %s", filename)
        await react(msg, REACT_FAIL, "Upload failed. Check the bot logs.")


# --- Lifecycle ----------------------------------------------------------------

async def _post_init(app: Application) -> None:
    app.bot_data["client"] = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
    if not ALLOWED_USERS:
        log.warning("ALLOWED_USERS is empty: the bot will reject everyone. "
                    "Set it to a comma-separated list of Telegram user IDs.")
    log.info("Bot started. Album=%r, allowed users=%s", IMMICH_ALBUM,
             sorted(ALLOWED_USERS) or "none")


async def _post_shutdown(app: Application) -> None:
    client = app.bot_data.get("client")
    if client is not None:
        await client.aclose()


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    media_filter = (
        filters.PHOTO
        | filters.VIDEO
        | filters.Document.IMAGE
        | filters.Document.VIDEO
    )
    app.add_handler(MessageHandler(media_filter, handle_media))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

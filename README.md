# Telegram → Immich uploader

A small bot that uploads photos, image documents and videos sent to a Telegram
bot straight into [Immich](https://immich.app), dropping every asset into a
fixed album. Uses long-polling, so it needs **no inbound ports** and works
behind Tailscale / Cloudflare.

## How it works

- Listens for **photos**, **image documents** (sent "as file" — full res, EXIF
  preserved) and **videos** (incl. video documents).
- Only users whose Telegram ID is in the `ALLOWED_USERS` whitelist are served; an empty or unset whitelist rejects everyone (fail-closed).
- Uploads to `POST /api/assets`. Immich dedupes by checksum, so re-sending an
  image returns `duplicate` instead of creating a copy.
- Adds every asset to the album named by `IMMICH_ALBUM` (auto-created).
- `fileCreatedAt` is taken from image **EXIF** when present, otherwise the
  Telegram message date.
- Feedback is a **message reaction**: 👍 uploaded, 👀 duplicate, 👎 failed. If
  Telegram rejects the reaction emoji, the bot replies with text instead.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Get your numeric Telegram user ID from [@userinfobot](https://t.me/userinfobot).
3. In Immich: **Account Settings → API Keys** → create a key.
4. Copy the env template and fill it in:

   ```bash
   cp .env.example .env
   # edit .env
   ```

   `ALLOWED_USERS` is the whitelist — comma-separated numeric IDs. The bot is
   fail-closed: if the value is empty or unset, **everyone is rejected**.

## Run with Docker (recommended)

```bash
docker compose up -d --build
docker compose logs -f
```

`docker-compose.yml` reads secrets from `.env` via `env_file`.

### Reaching Immich

`IMMICH_URL` must be resolvable **from inside the container**. Options:

- Attach this service to Immich's Docker network and use the service name, e.g.
  `IMMICH_URL=http://immich-server:2283`. Uncomment the `networks:` block in
  `docker-compose.yml` and set the external network name (find it with
  `docker network ls`).
- Or point at a host/Tailscale address the container can reach, e.g.
  `IMMICH_URL=http://100.x.y.z:2283`.

## Run locally (without Docker)

The bot loads `.env` automatically via `python-dotenv`, so:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

Real environment variables and Docker's `env_file` always take precedence over
`.env`.

## Notes & limits

- **Telegram Bot API caps downloads at 20 MB** over polling. Larger videos are
  rejected with a 👎 + message; lifting that needs a self-hosted Telegram Bot
  API server.
- **Send images "as file"** (document) when you care about quality — Telegram
  recompresses and strips EXIF from anything sent as a normal photo.
- `✅`/`❌` are usually **not** valid bot reactions in normal chats; the defaults
  (👍/👀/👎) are. Override via `REACT_OK` / `REACT_DUPLICATE` / `REACT_FAIL` —
  if an emoji is rejected the bot falls back to a text reply.

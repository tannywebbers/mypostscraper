#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
copybot.py - single-file interactive Telegram copy bot.

Run:  pip install telethon
Then: python copybot.py

Startup:
  - Logs in once as YOUR account (session saved to copybot_config.json).
  - Starts the bot and sends you a menu with buttons.

Menu / features:
  Read posts    - walks the saved posts one at a time.
  Each post message has:
      [ Copy ]   [ Delete ]     copy text / remove this post
      [ <  3/12  > ]            prev / position / next (number lives on the
                                buttons, never inside the content)
      [ Menu ]
  Scrape new    - fetch posts added since the last run.
  Polling       - background loop that keeps checking for new posts.
  Change source - switch the channel / id to fetch from.
  Stats         - post count, last scrape time, source.
  Delete all    - wipe saved posts (with confirmation).
  Log out       - forget the saved session.
  Close         - remove the menu message.

The bot deletes its previous screen every time it shows a new one, so the
chat only ever contains the message you are currently looking at.

Security note: copybot_config.json holds your session string and bot token.
Never commit or share it.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

try:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
    from telethon.errors import SessionPasswordNeededError
    from telethon.tl.types import (
        ReplyInlineMarkup,
        KeyboardInlineButton,
        KeyboardInlineButtonRow,
        KeyboardButtonStyle,
        InlineButtonTypeCopy,
        InlineButtonTypeCallback,
    )
except ImportError:
    sys.stderr.write("Missing dependency. Run:  pip install telethon\n")
    raise SystemExit(1)


SOURCE_URL = "https://web.telegram.org/k/#-2947341412"
DEFAULT_BOT_TOKEN = "7948436071:AAEu2OOnLbDvRuqmOPyOIXWuI8mWxjHCEuk"
DEFAULT_TARGET_USER = "1382903506"

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "copybot_config.json")
POSTS_FILE = os.path.join(HERE, "copybot_posts.json")
MAX_LEN = 4000


def log(msg):
    print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), msg))


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError) as exc:
            log("Could not read {} ({}); starting fresh.".format(os.path.basename(path), exc))
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def parse_source_id(value):
    text = str(value).strip()
    if "#" in text:
        text = text.split("#")[-1]
    return text.rstrip("/")


def cbtn(text, data, style=None):
    return KeyboardInlineButton(
        text=text,
        type=InlineButtonTypeCallback(data=data.encode("utf-8")),
        style=style,
    )


def copybtn(text):
    return KeyboardInlineButton(
        text="\U0001F4CB Copy",
        type=InlineButtonTypeCopy(copy_text=text),
        style=KeyboardButtonStyle(bg_primary=True),
    )


def markup(rows):
    return ReplyInlineMarkup(rows=[KeyboardInlineButtonRow(buttons=r) for r in rows])


def load_config():
    cfg = load_json(CONFIG_FILE, {})
    changed = False

    if not cfg.get("api_id") or not cfg.get("api_hash"):
        print("First run - enter your Telegram API credentials from https://my.telegram.org")
        cfg["api_id"] = cfg.get("api_id") or input("api_id: ").strip()
        cfg["api_hash"] = cfg.get("api_hash") or input("api_hash: ").strip()
        changed = True

    for key, value in (
        ("source", SOURCE_URL),
        ("bot_token", DEFAULT_BOT_TOKEN),
        ("target_user", DEFAULT_TARGET_USER),
    ):
        if not cfg.get(key):
            cfg[key] = value
            changed = True

    if "first_run_limit" not in cfg:
        cfg["first_run_limit"] = None
        changed = True
    if "poll_interval" not in cfg:
        cfg["poll_interval"] = 60
        changed = True

    if changed:
        save_json(CONFIG_FILE, cfg)
    return cfg


def fresh_store():
    return {
        "channel": None,
        "last_message_id": 0,
        "last_scraped_at": None,
        "last_read_index": 0,
        "posts": [],
    }


async def resolve_entity(client, source):
    raw = parse_source_id(source)
    digits = raw.lstrip("-")
    candidates = []
    if digits.isdigit():
        candidates.append(int("-100" + digits))
        candidates.append(int(digits))
        candidates.append(int("-" + digits))
    candidates.append(raw)
    for cand in candidates:
        try:
            return await client.get_entity(cand)
        except Exception:
            continue
    if digits.isdigit():
        for dialog in await client.get_dialogs():
            ent = dialog.entity
            if str(getattr(ent, "id", "")) == digits:
                return ent
    return None


async def login_user_client(cfg):
    while True:
        client = TelegramClient(
            StringSession(cfg.get("session_string") or ""),
            int(cfg["api_id"]),
            cfg["api_hash"],
        )
        await client.connect()

        if await client.is_user_authorized():
            log("Re-using saved session.")
            cfg["session_string"] = client.session.save()
            save_json(CONFIG_FILE, cfg)
            return client

        log("No valid session found, please log in.")
        phone = input("Phone (international format, e.g. +234...): ").strip()
        try:
            await client.send_code_request(phone)
        except Exception as exc:
            log("Could not send code: {}".format(exc))
            await client.disconnect()
            continue

        code = input("Login code: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            password = input("Two-factor password: ").strip()
            await client.sign_in(password=password)
        except Exception as exc:
            log("Login failed: {}. Let's try again.".format(exc))
            await client.disconnect()
            continue

        if await client.is_user_authorized():
            cfg["session_string"] = client.session.save()
            save_json(CONFIG_FILE, cfg)
            log("Session saved to {}.".format(os.path.basename(CONFIG_FILE)))
            return client
        await client.disconnect()


class CopyBot:
    def __init__(self, cfg, user_client, store):
        self.cfg = cfg
        self.user = user_client
        self.store = store
        self.owner = cfg["target_user"]
        if str(self.owner).lstrip("-").isdigit():
            self.owner = int(self.owner)
        self.bot = TelegramClient(StringSession(), int(cfg["api_id"]), cfg["api_hash"])
        self.polling = False
        self.poll_task = None
        self.awaiting = None
        self.screen_ids = []
        self.bot.add_event_handler(self.on_callback, events.CallbackQuery)
        self.bot.add_event_handler(self.on_message, events.NewMessage)

    # ---------------- screen / message management ----------------

    async def _delete_screen(self):
        ids = self.screen_ids
        self.screen_ids = []
        if ids:
            try:
                await self.bot.delete_messages(self.owner, ids)
            except Exception as exc:
                log("Cleanup skipped: {}".format(exc))

    async def _send_text(self, text, rows=None):
        await self._delete_screen()
        mk = markup(rows) if rows else None
        msg = await self.bot.send_message(self.owner, text, buttons=mk, link_preview=False)
        self.screen_ids.append(msg.id)
        return msg

    async def safe_answer(self, event, text=None, alert=False):
        try:
            await event.answer(message=text, alert=alert)
        except Exception:
            pass

    # ---------------- outgoing UI ----------------

    async def show_menu(self):
        src = self.cfg.get("source")
        last = self.store.get("last_scraped_at") or "never"
        text = (
            "\U0001F916 Copy Bot\n"
            "\u2022 Source: {}\n"
            "\u2022 Saved posts: {}\n"
            "\u2022 Last scraped: {}\n"
            "\u2022 Polling: {}"
        ).format(src, len(self.store["posts"]), last, "ON" if self.polling else "OFF")

        rows = [
            [cbtn("\u25B6\uFE0F Read posts", "read", KeyboardButtonStyle(bg_success=True))],
            [cbtn("\U0001F4E5 Scrape new", "scrape", KeyboardButtonStyle(bg_primary=True))],
            [cbtn("\U0001F501 Polling: {}".format("ON" if self.polling else "OFF"), "poll")],
            [cbtn("\U0001F4C2 Change source", "src")],
            [cbtn("\U0001F4CA Stats", "stats")],
            [cbtn("\U0001F5D1 Delete all posts", "del", KeyboardButtonStyle(bg_danger=True))],
            [cbtn("\U0001F6AA Log out", "logout", KeyboardButtonStyle(bg_danger=True))],
            [cbtn("\u2716 Close", "close")],
        ]
        await self._send_text(text, rows)

    async def send_post(self, idx):
        posts = self.store["posts"]
        total = len(posts)
        if total == 0:
            await self._send_text("No posts saved yet. Use Scrape first.")
            return
        idx = max(0, min(idx, total - 1))
        text = posts[idx]["text"]

        action_row = KeyboardInlineButtonRow(buttons=[
            copybtn(text),
            cbtn("\U0001F5D1 Delete", "delpost:{}".format(idx), KeyboardButtonStyle(bg_danger=True)),
        ])
        nav_row = KeyboardInlineButtonRow(buttons=[
            cbtn("\u25C0", "post:{}".format(idx - 1)),
            cbtn("{} / {}".format(idx + 1, total), "noop:{}".format(idx)),
            cbtn("\u25B6", "post:{}".format(idx + 1), KeyboardButtonStyle(bg_primary=True)),
        ])
        menu_row = KeyboardInlineButtonRow(buttons=[cbtn("\U0001F3E0 Menu", "menu")])
        full = ReplyInlineMarkup(rows=[action_row, nav_row, menu_row])
        no_copy = ReplyInlineMarkup(rows=[
            KeyboardInlineButtonRow(buttons=[action_row.buttons[1]]),
            nav_row,
            menu_row,
        ])
        await self._delete_screen()
        await self._emit(text, full, no_copy)
        self.store["last_read_index"] = idx
        save_json(POSTS_FILE, self.store)

    async def _emit(self, text, primary, fallback):
        chunks = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [""]
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            if not last:
                msg = await self.bot.send_message(self.owner, chunk, link_preview=False)
                self.screen_ids.append(msg.id)
                continue
            try:
                msg = await self.bot.send_message(self.owner, chunk, buttons=primary, link_preview=False)
            except Exception as exc:
                log("Primary markup rejected ({}); retrying without Copy button.".format(exc))
                try:
                    msg = await self.bot.send_message(self.owner, chunk, buttons=fallback, link_preview=False)
                except Exception as exc2:
                    log("Fallback markup failed ({}); sending plain.".format(exc2))
                    msg = await self.bot.send_message(self.owner, chunk, link_preview=False)
            self.screen_ids.append(msg.id)

    # ---------------- scraping ----------------

    async def do_scrape(self):
        if not self.cfg.get("session_string"):
            raise RuntimeError("Not logged in. Restart the script to log in.")
        source = parse_source_id(self.cfg.get("source"))
        entity = await resolve_entity(self.user, source)
        if entity is None:
            raise RuntimeError("Could not resolve source '{}'. Use Change source.".format(source))

        last_id = int(self.store.get("last_message_id") or 0)
        params = {}
        if last_id:
            params["min_id"] = last_id
        elif self.store.get("first_run_limit"):
            params["limit"] = int(self.store["first_run_limit"])

        internal = getattr(entity, "id", None)
        new = []
        async for msg in self.user.iter_messages(entity, **params):
            if not msg.message:
                continue
            link = "https://t.me/c/{}/{}".format(internal, msg.id) if internal is not None else None
            new.append({
                "id": msg.id,
                "date": msg.date.astimezone(timezone.utc).isoformat() if msg.date else None,
                "text": msg.message,
                "link": link,
            })
        new.reverse()

        self.store["channel"] = source
        self.store["last_scraped_at"] = datetime.now(timezone.utc).isoformat()
        if new:
            self.store["posts"].extend(new)
            self.store["last_message_id"] = max(
                int(self.store.get("last_message_id") or 0),
                max(p["id"] for p in new),
            )
        save_json(POSTS_FILE, self.store)
        return len(new), len(self.store["posts"])

    async def poll_loop(self):
        interval = int(self.cfg.get("poll_interval") or 60)
        while self.polling:
            await asyncio.sleep(interval)
            if not self.polling:
                break
            try:
                added, total = await self.do_scrape()
                if added:
                    await self.bot.send_message(
                        self.owner,
                        "\U0001F514 {} new post(s) saved. Total {}.".format(added, total),
                    )
            except Exception as exc:
                log("Poll error: {}".format(exc))

    # ---------------- handlers ----------------

    async def on_message(self, event):
        if event.sender_id != self.owner:
            return
        text = (event.raw_text or "").strip()

        if text in ("/start", "/menu"):
            await self.show_menu()
            return

        if self.awaiting == "source":
            self.awaiting = None
            if not text:
                return
            entity = await resolve_entity(self.user, text)
            if entity is None:
                await self._send_text(
                    "\u274C Could not resolve '{}'. Try @username or -100... id.".format(text),
                    rows=[[cbtn("\U0001F3E0 Menu", "menu")]],
                )
            else:
                self.cfg["source"] = text
                save_json(CONFIG_FILE, self.cfg)
                await self.show_menu()
            return

    async def on_callback(self, event):
        if event.sender_id != self.owner:
            await self.safe_answer(event, "Not allowed.", alert=True)
            return
        try:
            await self._handle(event, event.data.decode("utf-8", "replace"))
        except Exception as exc:
            log("Callback error: {}".format(exc))
            await self.safe_answer(event, "Error: {}".format(exc), alert=True)

    async def _handle(self, event, data):
        posts = self.store["posts"]
        total = len(posts)

        if data == "menu":
            await self.safe_answer(event)
            await self.show_menu()

        elif data == "read":
            await self.safe_answer(event)
            await self.send_post(int(self.store.get("last_read_index") or 0))

        elif data.startswith("post:"):
            idx = int(data.split(":", 1)[1])
            if total == 0:
                await self.safe_answer(event, "No posts saved.", alert=True)
                return
            if idx < 0 or idx >= total:
                await self.safe_answer(event, "Already at the {}.".format("start" if idx < 0 else "end"), alert=True)
                return
            await self.safe_answer(event)
            await self.send_post(idx)

        elif data.startswith("delpost:"):
            idx = int(data.split(":", 1)[1])
            if idx < 0 or idx >= total:
                await self.safe_answer(event, "Post not found.", alert=True)
                return
            del self.store["posts"][idx]
            save_json(POSTS_FILE, self.store)
            remaining = len(self.store["posts"])
            await self.safe_answer(event, "Post deleted.")
            if remaining == 0:
                await self.show_menu()
            else:
                await self.send_post(min(idx, remaining - 1))

        elif data.startswith("noop:"):
            idx = int(data.split(":", 1)[1])
            await self.safe_answer(event, "Post {} of {}".format(idx + 1, total), alert=True)

        elif data == "scrape":
            try:
                added, tot = await self.do_scrape()
                await self.safe_answer(event, "{} new post(s). Total {}.".format(added, tot), alert=True)
            except Exception as exc:
                await self.safe_answer(event, str(exc), alert=True)
            await self.show_menu()

        elif data == "poll":
            self.polling = not self.polling
            if self.polling:
                if self.poll_task:
                    self.poll_task.cancel()
                self.poll_task = asyncio.ensure_future(self.poll_loop())
            elif self.poll_task:
                self.poll_task.cancel()
                self.poll_task = None
            await self.safe_answer(event, "Polling {}.".format("ON" if self.polling else "OFF"), alert=True)
            await self.show_menu()

        elif data == "stats":
            last = self.store.get("last_scraped_at") or "never"
            await self.safe_answer(
                event,
                "Saved: {} posts\nSource: {}\nLast scraped: {}\nPolling: {}".format(
                    total, self.cfg.get("source"), last, "ON" if self.polling else "OFF"
                ),
                alert=True,
            )

        elif data == "src":
            self.awaiting = "source"
            await self.safe_answer(event)
            await self._send_text(
                "Send the new channel @username or -100... id as your next message.",
                rows=[[cbtn("\u21A9 Back", "menu")]],
            )

        elif data == "del":
            await self.safe_answer(event)
            await self._send_text(
                "Delete all {} saved posts?".format(total),
                rows=[[
                    cbtn("\u2705 Yes, delete", "delyes", KeyboardButtonStyle(bg_danger=True)),
                    cbtn("\u21A9 Cancel", "delno"),
                ]],
            )

        elif data == "delyes":
            self.store["posts"] = []
            self.store["last_message_id"] = 0
            self.store["last_read_index"] = 0
            save_json(POSTS_FILE, self.store)
            await self.safe_answer(event, "Deleted.")
            await self.show_menu()

        elif data == "delno":
            await self.safe_answer(event)
            await self.show_menu()

        elif data == "logout":
            self.cfg["session_string"] = ""
            save_json(CONFIG_FILE, self.cfg)
            try:
                await self.user.disconnect()
            except Exception:
                pass
            await self.safe_answer(event)
            await self._send_text(
                "Logged out. Restart the script to log in again.",
                rows=[[cbtn("\U0001F3E0 Menu", "menu")]],
            )

        elif data == "close":
            await self.safe_answer(event)
            await self._delete_screen()

        else:
            await self.safe_answer(event)

    async def run(self):
        await self.bot.start(bot_token=self.cfg["bot_token"])
        try:
            await self.show_menu()
        except Exception as exc:
            log("Could not send menu to owner ({}). Open the bot and press Start, then restart.".format(exc))
        log("Bot is running. Press Ctrl+C to stop.")
        await self.bot.run_until_disconnected()


async def main():
    cfg = load_config()
    store = load_json(POSTS_FILE, fresh_store())
    user = await login_user_client(cfg)
    try:
        await CopyBot(cfg, user, store).run()
    finally:
        await user.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Stopped.")

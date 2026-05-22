"""
CAD-BOT — Onshape Check-In / Check-Out bot for Easee Hardware
=============================================================
Manages the handoff of CAD files between Onshape (Engineering source of truth)
and Fusion 360 (used by Research). Tracks which parts are currently checked out,
by whom, and since when.

Trigger: @CAD-BOT <command>

Commands:
  checkout <part name/number> [onshape link]  — reserve a part for external editing
  checkin  <part name/number> [notes]          — return a part (attach modified file)
  status                                       — show all currently checked-out parts
  help                                         — explain the workflow
"""

import os
import re
import json
import logging
import sqlite3
from datetime import datetime, timezone
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from anthropic import Anthropic

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Clients ───────────────────────────────────────────────────────────────────
slack_app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)
anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
flask_app = Flask(__name__)
handler = SlackRequestHandler(slack_app)

# ── Database ──────────────────────────────────────────────────────────────────
# Use /data on Railway (persistent volume) or local cadbot.db for dev
DB_PATH = os.environ.get("DB_PATH", "/data/cadbot.db")


def init_db():
    """Create the checkouts table if it doesn't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checkouts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                part_name       TEXT    NOT NULL,
                part_number     TEXT,
                onshape_link    TEXT,
                user_id         TEXT    NOT NULL,
                user_name       TEXT,
                checkout_time   TEXT    NOT NULL,
                checkin_time    TEXT,
                notes           TEXT,
                slack_file_url  TEXT,
                status          TEXT    DEFAULT 'checked_out'
            )
        """)
        conn.commit()


init_db()


# ── Database helpers ──────────────────────────────────────────────────────────
def db_checkout(part_name, part_number, onshape_link, user_id, user_name, notes=None):
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO checkouts
               (part_name, part_number, onshape_link, user_id, user_name, checkout_time, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (part_name, part_number, onshape_link, user_id, user_name, now, notes),
        )
        conn.commit()


def db_checkin(part_name_or_number, user_id, notes=None, file_url=None):
    """Mark the most recent open checkout for this part as returned."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        # Find by part name or part number, checked out by this user (or anyone)
        row = conn.execute(
            """SELECT id FROM checkouts
               WHERE status = 'checked_out'
                 AND (LOWER(part_name) LIKE LOWER(?) OR LOWER(part_number) LIKE LOWER(?))
               ORDER BY checkout_time DESC LIMIT 1""",
            (f"%{part_name_or_number}%", f"%{part_name_or_number}%"),
        ).fetchone()

        if not row:
            return None  # Not found

        conn.execute(
            """UPDATE checkouts
               SET status = 'checked_in', checkin_time = ?, notes = ?, slack_file_url = ?
               WHERE id = ?""",
            (now, notes, file_url, row[0]),
        )
        conn.commit()
        # Return full row for confirmation message
        return conn.execute(
            "SELECT * FROM checkouts WHERE id = ?", (row[0],)
        ).fetchone()


def db_get_open_checkouts():
    """Return all currently checked-out parts."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """SELECT part_name, part_number, onshape_link, user_name, checkout_time
               FROM checkouts WHERE status = 'checked_out'
               ORDER BY checkout_time ASC"""
        ).fetchall()


def db_is_checked_out(part_name_or_number):
    """Return the active checkout row if the part is currently checked out."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """SELECT * FROM checkouts
               WHERE status = 'checked_out'
                 AND (LOWER(part_name) LIKE LOWER(?) OR LOWER(part_number) LIKE LOWER(?))
               ORDER BY checkout_time DESC LIMIT 1""",
            (f"%{part_name_or_number}%", f"%{part_name_or_number}%"),
        ).fetchone()


# ── Intent parsing ────────────────────────────────────────────────────────────
PARSE_PROMPT = """You are parsing a Slack message sent to @CAD-BOT, the Onshape check-in/out bot.
Extract the user's intent and return ONLY valid JSON — no explanation, no markdown.

JSON schema:
{
  "action": "checkout" | "checkin" | "status" | "help" | "unknown",
  "part_name": string | null,
  "part_number": string | null,
  "onshape_link": string | null,
  "notes": string | null
}

Rules:
- action = "checkout" when user wants to take/reserve/download a part for editing
- action = "checkin" when user is returning/uploading a modified file
- action = "status" when user asks what is checked out / currently in use
- action = "help" when user asks how to use the bot or what commands exist
- part_number usually looks like a product code (e.g. EV-1234, MECH-007, 100234)
- onshape_link is any URL containing "onshape.com"
- notes = any context about changes made or reason for checkout
"""


def parse_intent(text: str) -> dict:
    # Fast keyword pre-check — catches simple one-word commands without LLM
    lowered = text.lower().strip()
    if lowered in ("help", "?", "commands"):
        return {"action": "help"}
    if lowered in ("status", "list", "overview", "what's checked out", "whats checked out"):
        return {"action": "status"}
    if lowered.startswith("checkout ") or lowered.startswith("check out "):
        return {"action": "checkout", "part_name": text.split(" ", 1)[1].strip(),
                "part_number": None, "onshape_link": None, "notes": None}
    if lowered.startswith("checkin ") or lowered.startswith("check in "):
        return {"action": "checkin", "part_name": text.split(" ", 1)[1].strip(),
                "part_number": None, "onshape_link": None, "notes": None}

    # Fall back to LLM parsing for natural language
    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=PARSE_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        return json.loads(resp.content[0].text)
    except Exception as e:
        logger.warning("Intent parse failed: %s", e)
        return {"action": "unknown"}


# ── Response helpers ──────────────────────────────────────────────────────────
def format_status_table(rows) -> str:
    if not rows:
        return "✅ No parts are currently checked out. Onshape is the source of truth for everything."

    lines = ["*Currently checked-out parts:*", "```"]
    lines.append(f"{'Part':<30} {'Who':<20} {'Since (UTC)'}")
    lines.append("-" * 70)
    for r in rows:
        checkout_time = r["checkout_time"][:16].replace("T", " ")  # trim to YYYY-MM-DD HH:MM
        part = (r["part_number"] or "") + (" — " + r["part_name"] if r["part_name"] else "")
        lines.append(f"{part:<30} {(r['user_name'] or 'unknown'):<20} {checkout_time}")
    lines.append("```")
    return "\n".join(lines)


HELP_TEXT = """:wrench: *CAD-BOT — Onshape Check-In / Check-Out*

I manage the handoff of CAD files between *Onshape* (Engineering source of truth) and *Fusion 360* (Research team).

*Commands:*
• `@CAD-BOT checkout <part name or number> [Onshape link]`
  → Reserve a part for external editing. Download the file from Onshape, then work in Fusion 360.

• `@CAD-BOT checkin <part name or number> [notes]`
  → Return a part when you're done. Attach your modified file to the message.

• `@CAD-BOT status`
  → See all parts currently checked out.

• `@CAD-BOT help`
  → Show this message.

*Workflow:*
1️⃣  `checkout` the part → download it from Onshape
2️⃣  Edit in Fusion 360 (or any tool)
3️⃣  `checkin` with the modified file attached
4️⃣  Engineering reviews and imports back into Onshape"""


# ── Slack helpers ─────────────────────────────────────────────────────────────
def resolve_user_name(user_id: str) -> str:
    try:
        result = slack_app.client.users_info(user=user_id)
        return result["user"].get("real_name") or result["user"].get("name") or user_id
    except Exception:
        return user_id


def strip_mentions(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


# ── Main event handler ────────────────────────────────────────────────────────
@slack_app.event("app_mention")
def handle_mention(event, say):
    channel = event["channel"]
    event_ts = event["ts"]
    thread_ts = event.get("thread_ts", event_ts)
    user_id = event["user"]

    # 👀 reaction while processing
    try:
        slack_app.client.reactions_add(channel=channel, timestamp=event_ts, name="eyes")
    except Exception:
        pass

    try:
        raw_text = strip_mentions(event.get("text", ""))
        intent = parse_intent(raw_text)
        action = intent.get("action", "unknown")
        user_name = resolve_user_name(user_id)

        # ── CHECKOUT ──────────────────────────────────────────────────────────
        if action == "checkout":
            part_name = intent.get("part_name") or intent.get("part_number")
            part_number = intent.get("part_number")
            onshape_link = intent.get("onshape_link")
            notes = intent.get("notes")

            if not part_name:
                say(
                    text="I need a part name or number to check out. Try:\n`@CAD-BOT checkout <part name or number> [Onshape link]`",
                    thread_ts=thread_ts,
                )
                return

            # Warn if already checked out by someone else
            existing = db_is_checked_out(part_name)
            warning = ""
            if existing:
                warning = f"\n\n⚠️ *Note:* This part is already checked out by *{existing['user_name']}* since {existing['checkout_time'][:16].replace('T', ' ')} UTC. Proceeding anyway — make sure you coordinate!"

            db_checkout(part_name, part_number, onshape_link, user_id, user_name, notes)

            link_text = f"\n🔗 Onshape link: {onshape_link}" if onshape_link else "\n💡 Tip: include the Onshape document link next time so Engineering can find it easily."
            say(
                text=f"✅ *{part_name}* is now checked out by *{user_name}*."
                     f"{link_text}"
                     f"\n\nWhen you're done editing, return it with:\n`@CAD-BOT checkin {part_name} <notes about changes>`"
                     f"{warning}",
                thread_ts=thread_ts,
            )

        # ── CHECKIN ───────────────────────────────────────────────────────────
        elif action == "checkin":
            part_name = intent.get("part_name") or intent.get("part_number")
            notes = intent.get("notes")

            if not part_name:
                say(
                    text="I need a part name or number to check in. Try:\n`@CAD-BOT checkin <part name or number> [notes about changes]`",
                    thread_ts=thread_ts,
                )
                return

            # Check if a file was attached
            files = event.get("files", [])
            file_url = files[0].get("permalink") if files else None
            file_note = f"\n📎 File attached: {files[0].get('name', 'unnamed')}" if files else "\n⚠️ No file attached — remember to attach your modified file so Engineering can import it into Onshape."

            result = db_checkin(part_name, user_id, notes, file_url)

            if not result:
                say(
                    text=f"⚠️ I couldn't find an open checkout for *{part_name}*. Check the spelling or run `@CAD-BOT status` to see what's currently out.",
                    thread_ts=thread_ts,
                )
                return

            notes_text = f"\n📝 Notes: {notes}" if notes else ""
            say(
                text=f"✅ *{part_name}* has been checked back in by *{user_name}*."
                     f"{file_note}"
                     f"{notes_text}"
                     f"\n\n🔧 *Engineering:* please review and import the changes into Onshape.",
                thread_ts=thread_ts,
            )

        # ── STATUS ────────────────────────────────────────────────────────────
        elif action == "status":
            rows = db_get_open_checkouts()
            say(text=format_status_table(rows), thread_ts=thread_ts)

        # ── HELP ──────────────────────────────────────────────────────────────
        elif action == "help":
            say(text=HELP_TEXT, thread_ts=thread_ts)

        # ── UNKNOWN ───────────────────────────────────────────────────────────
        else:
            say(
                text=f"I'm not sure what you mean. Try `@CAD-BOT help` to see available commands.",
                thread_ts=thread_ts,
            )

    except Exception as e:
        logger.error("Error handling mention: %s", e, exc_info=True)
        say(text=f"⚠️ Something went wrong: `{e}`", thread_ts=thread_ts)

    finally:
        try:
            slack_app.client.reactions_remove(channel=channel, timestamp=event_ts, name="eyes")
        except Exception:
            pass


# ── Flask routes ──────────────────────────────────────────────────────────────
@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(host="0.0.0.0", port=port)

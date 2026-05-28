"""
CAD-BOT — Onshape Check-In / Check-Out bot for Easee Hardware
=============================================================
Manages the handoff of CAD files between Onshape (Engineering source of truth)
and Fusion 360 (used by Research). Integrates directly with Onshape API and
the Phoenix Check In / Out Notion database.

Trigger: @CAD-BOT <command>

Commands:
  checkout <part name/number>   — find part in Onshape, export .stp, share in thread
  checkin  [notes]              — return modified .stp (attach file); run in checkout thread
  done <part number>            — [Engineer] mark configured-part review complete
  status                        — show all currently checked-out parts (from Notion)
  help                          — explain the workflow
"""

import os
import re
import json
import logging
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone

import requests
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from anthropic import Anthropic

import onshape_client as oc
import notion_client as nc

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Point Onshape client at Easee enterprise instance ─────────────────────────
oc.BASE_URL = os.environ.get("ONSHAPE_BASE_URL", "https://easee.onshape.com")
oc.ACCESS_KEY = os.environ.get("ONSHAPE_ACCESS_KEY", "")
oc.SECRET_KEY = os.environ.get("ONSHAPE_SECRET_KEY", "")

# ── Clients ───────────────────────────────────────────────────────────────────
slack_app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)
anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
flask_app = Flask(__name__)
handler = SlackRequestHandler(slack_app)

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "/data/cadbot.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        # Legacy checkout tracking (kept for status command fallback)
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
        # Thread context: maps a Slack thread to an active checkout
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thread_context (
                thread_ts       TEXT    PRIMARY KEY,
                channel         TEXT    NOT NULL,
                part_number     TEXT    NOT NULL,
                part_name       TEXT,
                doc_id          TEXT,
                workspace_id    TEXT,
                branch_id       TEXT,
                blob_eid        TEXT,
                blob_name       TEXT,
                is_configured   INTEGER DEFAULT 0,
                configuration   TEXT,
                notion_page_id  TEXT,
                created_at      TEXT    NOT NULL
            )
        """)
        # Disambiguation state: pending Part# selection in a thread
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_disambiguation (
                thread_ts       TEXT    PRIMARY KEY,
                channel         TEXT    NOT NULL,
                user_id         TEXT    NOT NULL,
                candidates_json TEXT    NOT NULL,
                original_text   TEXT,
                created_at      TEXT    NOT NULL
            )
        """)
        # Engineer review queue
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_queue (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_ts       TEXT    NOT NULL,
                channel         TEXT    NOT NULL,
                part_number     TEXT    NOT NULL,
                notion_page_id  TEXT,
                blob_name       TEXT,
                branch_url      TEXT,
                created_at      TEXT    NOT NULL,
                status          TEXT    DEFAULT 'pending'
            )
        """)
        # Part index cache: maps Part# → Onshape document/element location.
        # Avoids slow full-workspace scans on repeat checkouts.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS part_index (
                part_number     TEXT    PRIMARY KEY,
                doc_id          TEXT    NOT NULL,
                doc_name        TEXT,
                workspace_id    TEXT    NOT NULL,
                element_id      TEXT    NOT NULL,
                part_id         TEXT,
                part_name       TEXT,
                feature_id      TEXT,
                configuration   TEXT,
                is_configured   INTEGER DEFAULT 0,
                cached_at       TEXT    NOT NULL
            )
        """)
        conn.commit()


init_db()


# ── SQLite helpers ────────────────────────────────────────────────────────────

def db_save_thread_context(thread_ts, channel, part_number, part_name,
                            doc_id, workspace_id, branch_id,
                            blob_eid, blob_name, is_configured, configuration,
                            notion_page_id):
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO thread_context
            (thread_ts, channel, part_number, part_name, doc_id, workspace_id,
             branch_id, blob_eid, blob_name, is_configured, configuration,
             notion_page_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (thread_ts, channel, part_number, part_name, doc_id, workspace_id,
              branch_id, blob_eid, blob_name, 1 if is_configured else 0,
              configuration, notion_page_id, now))
        conn.commit()


def db_get_thread_context(thread_ts):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM thread_context WHERE thread_ts = ?", (thread_ts,)
        ).fetchone()


def db_save_disambiguation(thread_ts, channel, user_id, candidates, original_text):
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO pending_disambiguation
            (thread_ts, channel, user_id, candidates_json, original_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (thread_ts, channel, user_id, json.dumps(candidates), original_text, now))
        conn.commit()


def db_get_disambiguation(thread_ts):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM pending_disambiguation WHERE thread_ts = ?", (thread_ts,)
        ).fetchone()


def db_clear_disambiguation(thread_ts):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM pending_disambiguation WHERE thread_ts = ?", (thread_ts,))
        conn.commit()


def db_save_review(thread_ts, channel, part_number, notion_page_id, blob_name, branch_url):
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO review_queue
            (thread_ts, channel, part_number, notion_page_id, blob_name, branch_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (thread_ts, channel, part_number, notion_page_id, blob_name, branch_url, now))
        conn.commit()


def db_get_cached_part(part_number: str) -> dict | None:
    """Return cached Onshape location for a Part#, or None if not cached."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM part_index WHERE LOWER(part_number) = LOWER(?)",
            (part_number,)
        ).fetchone()
        if not row:
            return None
        return {
            "documentId":    row["doc_id"],
            "documentName":  row["doc_name"] or "",
            "workspaceId":   row["workspace_id"],
            "elementId":     row["element_id"],
            "partId":        row["part_id"] or "",
            "partName":      row["part_name"] or "",
            "featureId":     row["feature_id"] or "",
            "configuration": row["configuration"] or "",
            "is_configured": bool(row["is_configured"]),
        }


def db_cache_part(part_number: str, part: dict):
    """Save an Onshape part location to the index cache."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO part_index
            (part_number, doc_id, doc_name, workspace_id, element_id,
             part_id, part_name, feature_id, configuration, is_configured, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            part_number,
            part.get("documentId", ""),
            part.get("documentName", ""),
            part.get("workspaceId", ""),
            part.get("elementId", ""),
            part.get("partId", ""),
            part.get("partName", ""),
            part.get("featureId", ""),
            part.get("configuration", ""),
            1 if part.get("is_configured") else 0,
            now,
        ))
        conn.commit()
    logger.info("Cached Onshape location for %s → %s", part_number, part.get("documentName"))


def db_clear_part_index():
    """Wipe the entire part index cache (used by @CAD-BOT refresh)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM part_index")
        conn.commit()
    logger.info("Part index cache cleared")


def db_get_pending_review(part_number):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("""
            SELECT * FROM review_queue
            WHERE LOWER(part_number) = LOWER(?) AND status = 'pending'
            ORDER BY created_at DESC LIMIT 1
        """, (part_number,)).fetchone()


def db_close_review(part_number):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE review_queue SET status = 'completed'
            WHERE LOWER(part_number) = LOWER(?) AND status = 'pending'
        """, (part_number,))
        conn.commit()


# ── Intent parsing ────────────────────────────────────────────────────────────

PART_NUMBER_RE = re.compile(r'\bM\d{4,6}\b', re.IGNORECASE)

PARSE_PROMPT = """You are parsing a Slack message sent to @CAD-BOT, the Onshape check-in/out bot.
Extract the user's intent and return ONLY valid JSON — no explanation, no markdown.

JSON schema:
{
  "action": "checkout" | "checkin" | "status" | "help" | "done" | "unknown",
  "part_description": string | null,
  "part_number": string | null,
  "notes": string | null
}

Rules:
- action = "checkout" when user wants to take/reserve/download/check out a part for editing
- action = "checkin" when user is returning/uploading a modified file or checking back in
- action = "status" when user asks what is checked out / currently in use / who has what
- action = "help" when user asks how to use the bot or what commands exist
- action = "done" when a Mechanical Engineer says a review is complete (e.g. "done M001432", "done reviewing M001432", "review complete for M001432")
- action = "refresh" when user asks to refresh, re-index, clear cache, or rebuild the part list
- part_number: extract if it looks like a product code starting with M followed by digits (e.g. M001432, M001505)
- part_description: the user's natural language description of the part if no clear part_number
- notes: any context about changes made or reason for checkout
- For "done" action, part_number is mandatory
"""


def parse_intent(text: str) -> dict:
    """Parse intent from cleaned message text. Fast keyword checks before LLM."""
    lowered = text.lower().strip()

    # One-word commands
    if lowered in ("help", "?", "commands"):
        return {"action": "help"}
    if lowered in ("status", "list", "overview"):
        return {"action": "status"}
    if lowered in ("refresh", "re-index", "reindex", "clear cache"):
        return {"action": "refresh"}

    # Explicit checkout / checkin prefix
    for prefix in ("checkout ", "check out ", "check-out "):
        if lowered.startswith(prefix):
            rest = text[len(prefix):].strip()
            pn = _extract_part_number(rest)
            return {"action": "checkout", "part_number": pn,
                    "part_description": None if pn else rest, "notes": None}

    for prefix in ("checkin ", "check in ", "check-in "):
        if lowered.startswith(prefix):
            rest = text[len(prefix):].strip()
            return {"action": "checkin", "part_number": _extract_part_number(rest),
                    "part_description": None, "notes": rest or None}

    if lowered in ("checkin", "check in", "check-in"):
        return {"action": "checkin", "part_number": None, "part_description": None, "notes": None}

    # "done M######"
    done_match = re.match(r'^done\s+(M\d+)', text, re.IGNORECASE)
    if done_match:
        return {"action": "done", "part_number": done_match.group(1).upper(), "notes": None}

    # Fall back to LLM
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


def _extract_part_number(text: str) -> str | None:
    m = PART_NUMBER_RE.search(text)
    return m.group(0).upper() if m else None


# ── Disambiguation helpers ────────────────────────────────────────────────────

DISAMBIG_PROMPT = """You are helping identify a CAD part from a natural language description.
The user said: "{description}"

Here is the list of available parts (JSON):
{parts_json}

Return ONLY valid JSON — no explanation:
{{
  "matches": [  // list of part_numbers that best match, ranked best first, max 5
    "M######", ...
  ],
  "confidence": "high" | "medium" | "low"
}}

Rules:
- "high" = only 1 clear match
- "medium" = 2–3 plausible matches
- "low" = no good match or too many candidates
- Consider part name, category, and module when matching
"""


def _text_score(description: str, part: dict) -> int:
    """
    Simple word-overlap score between a free-text description and a part's
    item_name / part_number. Returns the number of description words found
    in the part fields (case-insensitive). Used as a fast pre-filter before
    sending anything to Haiku.
    """
    haystack = " ".join([
        (part.get("item_name") or "").lower(),
        (part.get("part_number") or "").lower(),
        (part.get("item_category") or "").lower(),
        " ".join(part.get("part_of_module") or []).lower(),
    ])
    words = [w for w in description.lower().split() if len(w) > 2]
    return sum(1 for w in words if w in haystack)


def find_matching_parts(description: str) -> tuple[list[dict], str]:
    """
    Match a free-text description against all Notion parts.

    Strategy:
      1. Fast path — word-overlap text search on item_name / part_number.
         Handles obvious cases like "chargepack casing" → "ChargePack Casing"
         without an LLM call.
      2. Slow path — send the top text candidates (or full list if text finds
         nothing) to Claude Haiku for semantic/fuzzy matching.

    Returns (matched_parts, confidence).
    """
    all_parts = nc.get_all_parts()
    if not all_parts:
        return [], "low"

    # ── Fast path: word-overlap text search ───────────────────────────────────
    scored = [(p, _text_score(description, p)) for p in all_parts if p.get("part_number")]
    scored = [(p, s) for p, s in scored if s > 0]
    scored.sort(key=lambda x: -x[1])

    if scored:
        top_score   = scored[0][1]
        total_words = len([w for w in description.lower().split() if len(w) > 2])

        if total_words > 0 and top_score >= total_words:
            # All description words matched — high confidence
            confidence = "high" if len(scored) == 1 else "medium"
            return [p for p, _ in scored[:5]], confidence

        if top_score >= max(1, total_words // 2):
            # At least half the words matched — pass candidates to Haiku
            candidates = [p for p, _ in scored[:20]]
        else:
            candidates = None   # Text found nothing useful — send full list
    else:
        candidates = None

    # ── Slow path: Haiku semantic matching ────────────────────────────────────
    pool = candidates if candidates is not None else all_parts
    compact = [
        {
            "part_number":    p["part_number"],
            "item_name":      p["item_name"],
            "item_category":  p["item_category"],
            "part_of_module": p["part_of_module"],
        }
        for p in pool if p.get("part_number")
    ]

    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": DISAMBIG_PROMPT.format(
                    description=description,
                    parts_json=json.dumps(compact, ensure_ascii=False),
                ),
            }],
        )
        result     = json.loads(resp.content[0].text)
        matches_pn = result.get("matches", [])
        confidence = result.get("confidence", "low")

        part_map = {p["part_number"]: p for p in all_parts}
        matched  = [part_map[pn] for pn in matches_pn if pn in part_map]
        return matched, confidence

    except Exception as e:
        logger.warning("Disambiguation failed: %s", e)
        return [], "low"


# ── Changelog formatting ──────────────────────────────────────────────────────

CHANGELOG_PROMPT = """You are a technical writer formatting a CAD engineer's check-in notes
into a clean, concise changelog for an Onshape version description.

Rules:
- Output ONLY the formatted changelog — no intro, no explanation, no markdown fences
- Use bullet points (- ) for each distinct change
- Use clear, professional engineering language (e.g. "Adjusted hole diameter", not "made hole bigger")
- If the input is already well-structured, lightly clean it up and keep it
- If the input is vague or conversational, infer the most likely technical meaning
- Maximum 5 bullet points — combine minor related changes
- If no meaningful change notes are given, output a single line: "No detailed change notes provided."
"""

def format_changelog(raw_notes: str) -> str:
    """
    Use Claude Haiku to reformat raw Slack check-in notes into a clean
    engineering changelog. Falls back to raw_notes if Haiku fails.
    """
    if not raw_notes or not raw_notes.strip():
        return "No detailed change notes provided."
    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=CHANGELOG_PROMPT,
            messages=[{"role": "user", "content": raw_notes.strip()}],
        )
        formatted = resp.content[0].text.strip()
        return formatted if formatted else raw_notes
    except Exception as e:
        logger.warning("Changelog formatting failed, using raw notes: %s", e)
        return raw_notes


# ── Slack helpers ─────────────────────────────────────────────────────────────

def resolve_user(user_id: str) -> tuple[str, str]:
    """Return (display_name, email) for a Slack user."""
    try:
        result = slack_app.client.users_info(user=user_id)
        u = result["user"]
        name  = u.get("real_name") or u.get("name") or user_id
        email = u.get("profile", {}).get("email", "")
        return name, email
    except Exception:
        return user_id, ""


def strip_mentions(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


def upload_stp_to_slack(channel: str, thread_ts: str, stp_path: str,
                         part_number: str, part_name: str):
    """Upload a .stp file to a Slack thread."""
    try:
        slack_app.client.files_upload_v2(
            channel=channel,
            thread_ts=thread_ts,
            file=stp_path,
            filename=f"{part_number} - {part_name}.stp",
            title=f"{part_number} - {part_name}",
        )
    except Exception as e:
        logger.error("File upload to Slack failed: %s", e)
        raise


def download_slack_file(file_info: dict) -> str:
    """Download a Slack file to a temp path. Returns the local path."""
    url   = file_info.get("url_private_download") or file_info.get("url_private")
    name  = file_info.get("name", "upload.stp")
    token = os.environ["SLACK_BOT_TOKEN"]

    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()

    suffix = os.path.splitext(name)[1] or ".stp"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(resp.content)
    tmp.close()
    return tmp.name


# ── Response templates ────────────────────────────────────────────────────────

HELP_TEXT = """:wrench: *CAD-BOT — Onshape Check-In / Check-Out*

I handle the full CAD handoff between *Onshape* (Engineering) and *Fusion 360* (Research) — automatically exporting, uploading, and re-importing files.

*Commands:*
• `@CAD-BOT checkout <part name or number>`
  → I find the part in Onshape, create a checkout branch, export the .stp, and share it here.

• `@CAD-BOT checkin [notes]` _(reply in the checkout thread, attach the modified .stp)_
  → I upload your file back into Onshape and notify Engineering to review.

• `@CAD-BOT done <part number>` _(Mechanical Engineers only)_
  → Marks a configured-part review as complete in Notion.

• `@CAD-BOT status`
  → Shows all parts currently checked out (from Notion database).

• `@CAD-BOT help`
  → This message.

*Workflow:*
1️⃣  `checkout` the part → I share the .stp in this thread
2️⃣  Edit in Fusion 360
3️⃣  Reply in this thread: `@CAD-BOT checkin` with your modified file attached
4️⃣  Engineering reviews and merges the branch in Onshape"""


def format_status(parts: list[dict]) -> str:
    checked_out = [p for p in parts if p.get("status") == "Checked Out"]
    review      = [p for p in parts if p.get("status") == "Review Required"]

    if not checked_out and not review:
        return "✅ No parts are currently checked out."

    lines = []
    if checked_out:
        lines.append("*Currently checked out:*")
        for p in checked_out:
            lines.append(f"  • *{p['part_number']}* — {p['item_name']}")
    if review:
        lines.append("\n*⚠️ Awaiting engineer review:*")
        for p in review:
            lines.append(f"  • *{p['part_number']}* — {p['item_name']}")
    return "\n".join(lines)


# ── Core workflow handlers ────────────────────────────────────────────────────

def handle_checkout(event, say, part_number: str, part_name_hint: str | None,
                    notion_part: dict, user_id: str, user_name: str, user_email: str):
    """
    Full checkout flow:
    1. Verify not already checked out
    2. Search Onshape for Part#
    3. Create BOT### version + branch
    4. Export .stp
    5. Upload to Slack thread
    6. Update Notion → Checked Out
    7. Save thread context
    """
    channel   = event["channel"]
    event_ts  = event["ts"]
    thread_ts = event.get("thread_ts", event_ts)
    page_id   = notion_part["page_id"]
    part_name = notion_part.get("item_name") or part_name_hint or part_number

    # ① Check current status
    if notion_part.get("status") == "Checked Out":
        say(
            text=f"⚠️ *{part_number}* is already checked out. "
                 f"Run `@CAD-BOT status` to see who has it.",
            thread_ts=thread_ts,
        )
        return

    # ② Search Onshape — check cache first, then live scan
    cached = db_get_cached_part(part_number)
    if cached:
        logger.info("Cache hit for %s → %s", part_number, cached.get("documentName"))
        say(text=f"🔍 Found *{part_number} — {part_name}*. Located in Onshape (cached)…",
            thread_ts=thread_ts)
        part = cached
    else:
        say(text=f"🔍 Found *{part_number} — {part_name}*. Searching Onshape…",
            thread_ts=thread_ts)
        matches = oc.search_by_part_number(part_number, hint=part_name)
        if not matches:
            say(text=f"❌ Could not find *{part_number}* in Onshape. Check the Part# and try again.",
                thread_ts=thread_ts)
            return
        part = matches[0]
        db_cache_part(part_number, part)

    did           = part["documentId"]
    wid           = part["workspaceId"]
    eid           = part["elementId"]
    part_id       = part["partId"]
    configuration = part.get("configuration", "")
    is_configured = part.get("is_configured", False)

    config_note = f" _(config: {configuration})_" if is_configured else ""
    say(text=f"✅ Found in Onshape: *{part['documentName']}*{config_note}\n"
             f"Creating checkout branch…", thread_ts=thread_ts)

    # ③ Create version on main (branch point reference) + branch
    version_id = oc.create_version(
        did, wid, [part_number],
        label="Check out",
        description=f"CAD-BOT check-out reference. Checked out by {user_name}. Part(s): {part_number}",
    )
    if not version_id:
        say(text="❌ Failed to create checkout version in Onshape.", thread_ts=thread_ts)
        return

    branch_id = oc.create_branch(did, version_id, [part_number])
    if not branch_id:
        say(text="❌ Failed to create checkout branch in Onshape.", thread_ts=thread_ts)
        return

    branch_url = f"https://easee.onshape.com/documents/{did}/w/{branch_id}"

    # ④ Export .stp
    say(text="📦 Exporting .stp file (this can take 1–3 minutes)…", thread_ts=thread_ts)
    clean_name = oc.clean_filename(part.get("partName", part_number))
    stp_path   = f"/tmp/{clean_name.replace(' ', '_')}.stp"

    success = oc.export_step(did, branch_id, eid, part_id, stp_path, configuration)
    if not success:
        say(text="❌ STEP export failed. Branch was created — please export manually from Onshape.",
            thread_ts=thread_ts)
        return

    # ⑤ Upload to Slack
    say(text=f"⬆️ Uploading .stp to thread…", thread_ts=thread_ts)
    try:
        upload_stp_to_slack(channel, thread_ts, stp_path, part_number, part_name)
    except Exception as e:
        say(text=f"⚠️ File upload failed: `{e}`\nBranch is ready: {branch_url}",
            thread_ts=thread_ts)

    # ⑥ Update Notion — always update status; resolve person if possible
    notion_user_id = nc.resolve_notion_user(email=user_email, display_name=user_name)
    if not notion_user_id:
        logger.warning("Could not resolve Notion user for %s / %s — updating status only",
                       user_email, user_name)
    nc.set_checked_out(page_id, notion_user_id)

    # ⑦ Find the Import feature and blob element via the feature graph:
    #    part.featureId → Import feature → blobData.namespace → blob element ID
    #    This is API-driven and does not rely on element names at all.
    part_feature_id = part.get("featureId", "")
    _, blob_eid = oc.find_import_and_blob_for_part(did, branch_id, eid, part_feature_id)

    # Resolve blob element name for logging / reference
    blob_name = None
    if blob_eid:
        for elem in oc._get_elements(did, branch_id):
            if elem.get("id") == blob_eid:
                blob_name = elem.get("name") or ""
                break
        logger.info("Blob element for %s: '%s' (%s)", part_number, blob_name, blob_eid)
    else:
        logger.warning("No blob resolved for %s — check-in will use safe (new blob) path",
                       part_number)

    # ⑧ Save thread context
    db_save_thread_context(
        thread_ts=thread_ts, channel=channel,
        part_number=part_number, part_name=part_name,
        doc_id=did, workspace_id=wid, branch_id=branch_id,
        blob_eid=blob_eid, blob_name=blob_name,
        is_configured=is_configured, configuration=configuration,
        notion_page_id=page_id,
    )

    # ⑨ Final confirmation
    config_msg = (f"\n⚠️ _Configured part ({configuration}) — make sure you edit "
                  f"the correct variant in Fusion 360._") if is_configured else ""
    say(
        text=f"✅ *{part_number} — {part_name}* is checked out to *{user_name}*."
             f"{config_msg}"
             f"\n🔗 Onshape branch: {branch_url}"
             f"\n\nWhen you're done, reply in *this thread* with "
             f"`@CAD-BOT checkin` and attach your modified .stp file.",
        thread_ts=thread_ts,
    )
    try:
        os.unlink(stp_path)
    except Exception:
        pass


def handle_checkin(event, say, context: sqlite3.Row, stp_file: dict,
                   user_name: str, notes: str | None):
    """
    Full check-in flow:
    1. Download .stp from Slack
    2. Detect configured/shared Import feature
    3. Normal path: update blob + Import feature + version → Checked In
    4. Safe path: new blob + version with warning → Review Required + engineer ping
    """
    channel   = event["channel"]
    event_ts  = event["ts"]
    thread_ts = event.get("thread_ts", event_ts)

    part_number   = context["part_number"]
    part_name     = context["part_name"] or part_number
    did           = context["doc_id"]
    branch_id     = context["branch_id"]
    blob_eid      = context["blob_eid"]
    ps_eid        = context["workspace_id"]   # note: this is main WID; PS EID is separate
    is_configured = bool(context["is_configured"])
    configuration = context["configuration"] or ""
    notion_page_id = context["notion_page_id"]

    logger.info("Checkin context: part=%s did=%s branch=%s blob_eid=%s is_configured=%s",
                part_number, did, branch_id, blob_eid, is_configured)
    say(text=f"📥 Received your file — running check-in for *{part_number}*…",
        thread_ts=thread_ts)

    # Download file from Slack
    try:
        local_stp = download_slack_file(stp_file)
    except Exception as e:
        say(text=f"❌ Could not download your file: `{e}`", thread_ts=thread_ts)
        return

    # Find the Part Studio EID in the branch
    elements = oc._get_elements(did, branch_id)
    ps_eid_actual = None
    for elem in elements:
        etype = (elem.get("type") or "").replace(" ", "").upper()
        ename = elem.get("name") or ""
        if etype == "PARTSTUDIO" and part_number[:4].lower() in ename.lower():
            ps_eid_actual = elem["id"]
            break
    # fallback: use stored workspace_id as element hint (won't work but logs will show)
    if not ps_eid_actual:
        for elem in elements:
            if (elem.get("type") or "").replace(" ", "").upper() == "PARTSTUDIO":
                ps_eid_actual = elem["id"]
                break

    # Decide: normal path or safe path.
    # Safe path is used when: no blob was resolved during checkout (couldn't identify
    # the Import feature via the feature graph), OR the blob is genuinely shared across
    # multiple Import features (would overwrite geometry for other configured variants).
    # NOTE: is_configured alone does NOT trigger the safe path — find_import_and_blob_for_part
    # uses featureId matching and handles configured parts correctly.
    use_safe_path = not blob_eid
    if not use_safe_path and blob_eid and ps_eid_actual:
        use_safe_path = oc.detect_shared_import(did, branch_id, ps_eid_actual, blob_eid)

    if use_safe_path:
        # ── SAFE PATH (configured / shared Import) ────────────────────────────
        config_slug = re.sub(r'[^a-zA-Z0-9]', '_', configuration)[:20]
        new_blob_name = f"{part_number}_{config_slug}_checkin.step" if config_slug \
                        else f"{part_number}_checkin.step"

        say(text=f"⚠️ *Configured or shared part detected* — uploading as new blob "
                 f"(Import feature will need manual wiring by an engineer)…",
            thread_ts=thread_ts)

        new_eid = oc.create_new_blob_element(did, branch_id, local_stp, new_blob_name)
        if not new_eid:
            say(text="❌ Failed to upload new blob element to Onshape.", thread_ts=thread_ts)
            return

        changelog    = format_changelog(notes)
        version_desc = (
            f"Changelog - External Changes - {user_name}: {changelog}\n"
            f"⚠️ Configured part — new blob '{new_blob_name}' uploaded. "
            f"Import feature requires manual wiring by engineer before merge."
        )
        vid = oc.create_version(did, branch_id, [part_number],
                                label="Check in", description=version_desc)

        branch_url = f"https://easee.onshape.com/documents/{did}/w/{branch_id}"

        # Update Notion → Review Required
        if notion_page_id:
            nc.set_review_required(notion_page_id)

        # Save to review queue
        db_save_review(thread_ts, channel, part_number, notion_page_id,
                       new_blob_name, branch_url)

        say(
            text=f"⚠️ *Review required — configured part*\n"
                 f"New geometry for *{part_number}* has been uploaded to the checkout branch.\n"
                 f"🔗 {branch_url}\n\n"
                 f"A *Mechanical Engineer* must:\n"
                 f"1. Open the branch in Onshape\n"
                 f"2. Wire the new blob `{new_blob_name}` to the correct Import feature "
                 f"for its configuration\n"
                 f"3. Verify the geometry regenerates correctly\n"
                 f"4. Merge the branch to main\n\n"
                 f"Once done, reply: `@CAD-BOT done {part_number}`",
            thread_ts=thread_ts,
        )

    else:
        # ── NORMAL PATH ───────────────────────────────────────────────────────
        if not blob_eid:
            say(text="❌ No blob element found for this part in the checkout branch. "
                     "Please check in manually via Onshape.",
                thread_ts=thread_ts)
            return
        if not ps_eid_actual:
            say(text="❌ Could not find the Part Studio in the checkout branch.",
                thread_ts=thread_ts)
            return

        # Upload blob
        new_mv = oc.update_blob_element(did, branch_id, blob_eid, local_stp)
        if not new_mv:
            new_mv = oc.get_blob_microversion(did, branch_id, blob_eid)
        if not new_mv:
            say(text="❌ Blob upload to Onshape failed.", thread_ts=thread_ts)
            return

        # Get current Import feature
        resp = oc._request("GET",
            f"/api/v6/partstudios/d/{did}/w/{branch_id}/e/{ps_eid_actual}/features")
        if resp.status_code != 200:
            say(text="❌ Could not read Part Studio features.", thread_ts=thread_ts)
            return
        features = resp.json().get("features", [])
        import_feature = next(
            (f for f in features
             if f.get("featureType", "").lower() in ("import", "importforeign")), None
        )
        if not import_feature:
            say(text="⚠️ Import feature not found — blob uploaded but feature not updated. "
                     "Please update the Import feature manually in Onshape.",
                thread_ts=thread_ts)
        else:
            ok = oc.update_import_feature(
                did, branch_id, ps_eid_actual,
                import_feature["featureId"], blob_eid, new_mv, import_feature
            )
            if not ok:
                say(text="⚠️ Blob uploaded but Import feature update failed. "
                         "Please update it manually in Onshape.",
                    thread_ts=thread_ts)

        changelog = format_changelog(notes)
        vid = oc.create_version(
            did, branch_id, [part_number],
            label="Check in",
            description=f"Changelog - External Changes - {user_name}: {changelog}",
        )
        branch_url = f"https://easee.onshape.com/documents/{did}/w/{branch_id}"

        # Update Notion → Checked In
        if notion_page_id:
            nc.set_checked_in(notion_page_id)

        notes_text = f"\n📝 Notes: {notes}" if notes else ""
        say(
            text=f"✅ *{part_number} — {part_name}* has been checked back in by *{user_name}*."
                 f"{notes_text}"
                 f"\n🔗 Onshape branch ready for review: {branch_url}"
                 f"\n\n🔧 *Mechanical Engineer:* please review and merge the branch to main.",
            thread_ts=thread_ts,
        )

    try:
        os.unlink(local_stp)
    except Exception:
        pass


# ── Main event handler ────────────────────────────────────────────────────────

@slack_app.event("app_mention")
def handle_mention(event, say):
    channel   = event["channel"]
    event_ts  = event["ts"]
    thread_ts = event.get("thread_ts", event_ts)
    user_id   = event["user"]

    # 👀 react immediately so user knows we're working
    try:
        slack_app.client.reactions_add(channel=channel, timestamp=event_ts, name="eyes")
    except Exception:
        pass

    def _process():
        try:
            raw_text  = strip_mentions(event.get("text", ""))
            user_name, user_email = resolve_user(user_id)

            # ── CHECK: pending disambiguation in this thread ──────────────────
            disam = db_get_disambiguation(thread_ts)
            if disam:
                candidates = json.loads(disam["candidates_json"])
                lowered    = raw_text.strip().lower()

                # Accept "1", "2", … or a Part# directly
                selected = None
                if re.match(r'^\d+$', lowered):
                    idx = int(lowered) - 1
                    if 0 <= idx < len(candidates):
                        selected = candidates[idx]
                else:
                    pn = _extract_part_number(raw_text)
                    if pn:
                        selected = next(
                            (c for c in candidates if c["part_number"].upper() == pn), None
                        )

                if selected:
                    db_clear_disambiguation(thread_ts)
                    notion_parts = nc.search_part(selected["part_number"])
                    if notion_parts:
                        handle_checkout(event, say, selected["part_number"],
                                        selected.get("item_name"), notion_parts[0],
                                        user_id, user_name, user_email)
                    else:
                        say(text=f"⚠️ Could not find *{selected['part_number']}* in the Notion database.",
                            thread_ts=thread_ts)
                else:
                    say(text="I didn't catch that. Please reply with the number of the part "
                             "you want, e.g. `1` or `M001432`.",
                        thread_ts=thread_ts)
                return

            intent = parse_intent(raw_text)
            action = intent.get("action", "unknown")

            # ── CHECKOUT ─────────────────────────────────────────────────────
            if action == "checkout":
                part_number = intent.get("part_number")
                description = intent.get("part_description") or intent.get("part_name")

                if part_number:
                    # Direct Part# lookup
                    notion_parts = nc.search_part(part_number)
                    if not notion_parts:
                        say(text=f"⚠️ *{part_number}* is not in the Phoenix Check In / Out database. "
                                 f"Ask your engineer to add it before checking out.",
                            thread_ts=thread_ts)
                        return
                    handle_checkout(event, say, part_number, None, notion_parts[0],
                                    user_id, user_name, user_email)

                elif description:
                    # Disambiguation via Haiku + Notion
                    say(text=f"🔍 Looking up parts matching _{description}_…",
                        thread_ts=thread_ts)
                    matches, confidence = find_matching_parts(description)

                    if not matches:
                        say(text=f"❌ I couldn't find any parts matching _{description}_. "
                                 f"Try using the Part# directly (e.g. `checkout M001432`).",
                            thread_ts=thread_ts)
                        return

                    if confidence == "high" and len(matches) == 1:
                        # Single clear match — proceed directly
                        p = matches[0]
                        notion_parts = nc.search_part(p["part_number"])
                        if notion_parts:
                            handle_checkout(event, say, p["part_number"],
                                            p.get("item_name"), notion_parts[0],
                                            user_id, user_name, user_email)
                        return

                    # Multiple candidates — ask user to pick
                    lines = [f"I found {len(matches)} possible match(es) for _{description}_. "
                             f"Reply with the number to confirm:\n"]
                    for i, p in enumerate(matches[:5], 1):
                        module = ", ".join(p.get("part_of_module", []))
                        lines.append(
                            f"*{i}.* `{p['part_number']}` — {p['item_name']} "
                            f"({p.get('item_category', '')} | {module})"
                        )
                    say(text="\n".join(lines), thread_ts=thread_ts)
                    db_save_disambiguation(thread_ts, channel, user_id,
                                           [{"part_number": p["part_number"],
                                             "item_name": p["item_name"]}
                                            for p in matches[:5]],
                                           raw_text)
                else:
                    say(text="I need a part name or number. Try:\n"
                             "`@CAD-BOT checkout M001432` or\n"
                             "`@CAD-BOT checkout busbar in phase selector for N`",
                        thread_ts=thread_ts)

            # ── CHECKIN ──────────────────────────────────────────────────────
            elif action == "checkin":
                context = db_get_thread_context(thread_ts)
                if not context:
                    say(text="⚠️ I don't have a checkout context for this thread. "
                             "Make sure you reply in the *same thread* as the original checkout.",
                        thread_ts=thread_ts)
                    return

                files    = event.get("files", [])
                stp_file = next((f for f in files
                                 if f.get("name", "").lower().endswith((".stp", ".step"))), None)
                if not stp_file:
                    say(text="📎 Please attach your modified *.stp* or *.step* file to the message.",
                        thread_ts=thread_ts)
                    return

                notes = intent.get("notes")
                handle_checkin(event, say, context, stp_file, user_name, notes)

            # ── DONE (engineer review) ────────────────────────────────────────
            elif action == "done":
                part_number = intent.get("part_number")
                if not part_number:
                    say(text="Please specify the part number: `@CAD-BOT done M001432`",
                        thread_ts=thread_ts)
                    return

                review = db_get_pending_review(part_number)
                if not review:
                    say(text=f"⚠️ No pending review found for *{part_number}*.",
                        thread_ts=thread_ts)
                    return

                if review["notion_page_id"]:
                    nc.set_checked_in(review["notion_page_id"])

                db_close_review(part_number)

                say(
                    text=f"✅ Review for *{part_number}* marked complete.\n"
                         f"Notion updated → *Checked In*. "
                         f"Make sure the branch has been merged to main in Onshape.",
                    thread_ts=thread_ts,
                )

            # ── STATUS ────────────────────────────────────────────────────────
            elif action == "status":
                all_parts = nc.get_all_parts()
                active = [p for p in all_parts
                          if p.get("status") in ("Checked Out", "Review Required")]
                say(text=format_status(active), thread_ts=thread_ts)

            # ── REFRESH ───────────────────────────────────────────────────────
            elif action == "refresh":
                db_clear_part_index()
                say(text="🔄 Part index cache cleared. The next checkout for each part "
                         "will re-scan Onshape to rebuild the index.",
                    thread_ts=thread_ts)

            # ── HELP ──────────────────────────────────────────────────────────
            elif action == "help":
                say(text=HELP_TEXT, thread_ts=thread_ts)

            # ── UNKNOWN ───────────────────────────────────────────────────────
            else:
                say(text="I'm not sure what you mean. Try `@CAD-BOT help`.",
                    thread_ts=thread_ts)

        except Exception as e:
            logger.error("Error handling mention: %s", e, exc_info=True)
            say(text=f"⚠️ Something went wrong: `{e}`", thread_ts=thread_ts)
        finally:
            try:
                slack_app.client.reactions_remove(
                    channel=channel, timestamp=event_ts, name="eyes"
                )
            except Exception:
                pass

    # Run in background thread — Onshape export can take 30–90 s
    threading.Thread(target=_process, daemon=True).start()


# ── Flask routes ──────────────────────────────────────────────────────────────

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "onshape_base": oc.BASE_URL}, 200


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(host="0.0.0.0", port=port)

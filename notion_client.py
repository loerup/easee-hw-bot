"""
Notion REST API client for the CAD-BOT.

Uses the NOTION_TOKEN integration token (scoped to the Phoenix Check In / Out
database) for autonomous reads and writes — no MCP connector required at runtime.

DATABASE
────────
Phoenix Check In / Out
Database ID: 366db557-b8e3-803c-8c38-000bdc54767d
"""

import os
import json
import logging
import requests

logger = logging.getLogger(__name__)

NOTION_TOKEN  = os.environ.get("NOTION_TOKEN", "")
NOTION_BASE   = "https://api.notion.com/v1"
DB_ID         = "366db557-b8e3-8017-ac87-e2a9923b8b18"
NOTION_VER    = "2022-06-28"


def _headers() -> dict:
    return {
        "Authorization":  f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VER,
        "Content-Type":   "application/json",
    }


def _page_to_part(page: dict) -> dict:
    """Extract useful fields from a raw Notion page result."""
    props = page.get("properties", {})

    def select_val(key):
        s = props.get(key, {}).get("select")
        return s["name"] if s else None

    def multi_val(key):
        return [o["name"] for o in props.get(key, {}).get("multi_select", [])]

    def title_val(key):
        parts = props.get(key, {}).get("title", [])
        return "".join(p.get("plain_text", "") for p in parts)

    def text_val(key):
        parts = props.get(key, {}).get("rich_text", [])
        return "".join(p.get("plain_text", "") for p in parts)

    def people_val(key):
        return [p.get("id") for p in props.get(key, {}).get("people", [])]

    return {
        "page_id":        page["id"],
        "part_number":    title_val("Part number"),
        "item_name":      text_val("item name"),
        "status":         select_val("Checked In / Out"),
        "checked_out_by": people_val("Who checked out?"),
        "item_category":  select_val("item category"),
        "part_of_module": multi_val("Part of Module"),
    }


# ── Queries ───────────────────────────────────────────────────────────────────

def search_part(part_number: str) -> list[dict]:
    """
    Find parts in the database by exact Part# match.
    Returns list of part dicts (usually 0 or 1 result).
    """
    url  = f"{NOTION_BASE}/databases/{DB_ID}/query"
    body = {
        "filter": {
            "property": "Part number",
            "title":    {"equals": part_number},
        }
    }
    resp = requests.post(url, headers=_headers(), json=body)
    if resp.status_code != 200:
        logger.error("Notion search_part failed: %s — %s", resp.status_code, resp.text[:200])
        return []
    return [_page_to_part(p) for p in resp.json().get("results", [])]


def get_all_parts(max_pages: int = 5) -> list[dict]:
    """
    Fetch all parts from the database (paginated, up to max_pages × 100 items).
    Used for disambiguation when a user gives a description instead of a Part#.
    """
    url     = f"{NOTION_BASE}/databases/{DB_ID}/query"
    results = []
    cursor  = None

    for _ in range(max_pages):
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(url, headers=_headers(), json=body)
        if resp.status_code != 200:
            break
        data = resp.json()
        results.extend(_page_to_part(p) for p in data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return results


def get_part(page_id: str) -> dict | None:
    """Fetch a single part page by ID."""
    resp = requests.get(f"{NOTION_BASE}/pages/{page_id}", headers=_headers())
    if resp.status_code != 200:
        return None
    return _page_to_part(resp.json())


# ── State updates ─────────────────────────────────────────────────────────────

def set_checked_out(page_id: str, notion_user_id: str | None = None) -> bool:
    """Mark a part as Checked Out. Optionally records the Notion user."""
    props = {
        "Checked In / Out": {"select": {"name": "Checked Out"}},
    }
    if notion_user_id:
        props["Who checked out?"] = {"people": [{"object": "user", "id": notion_user_id}]}

    body = {"properties": props}
    resp = requests.patch(f"{NOTION_BASE}/pages/{page_id}", headers=_headers(), json=body)
    if resp.status_code != 200:
        logger.error("set_checked_out failed: %s — %s", resp.status_code, resp.text[:200])
    return resp.status_code == 200


def set_checked_in(page_id: str) -> bool:
    """Mark a part as Checked In and clear the Who checked out? field."""
    body = {
        "properties": {
            "Checked In / Out": {"select": {"name": "Checked In"}},
            "Who checked out?": {"people": []},
        }
    }
    resp = requests.patch(f"{NOTION_BASE}/pages/{page_id}", headers=_headers(), json=body)
    if resp.status_code != 200:
        logger.error("set_checked_in failed: %s — %s", resp.status_code, resp.text[:200])
    return resp.status_code == 200


def set_review_required(page_id: str) -> bool:
    """
    Mark a part as Review Required (configured/shared Import feature case).
    Preserves the 'Who checked out?' field so we know who submitted the file.
    """
    body = {
        "properties": {
            "Checked In / Out": {"select": {"name": "Review Required"}},
        }
    }
    resp = requests.patch(f"{NOTION_BASE}/pages/{page_id}", headers=_headers(), json=body)
    if resp.status_code != 200:
        logger.error("set_review_required failed: %s — %s", resp.status_code, resp.text[:200])
    return resp.status_code == 200


# ── User resolution ───────────────────────────────────────────────────────────

def resolve_notion_user(email: str = "", display_name: str = "") -> str | None:
    """
    Find a Notion user ID by email address, with a display-name fallback.

    Notion integrations often cannot read email addresses (requires the
    'Read user information including email addresses' capability). This function
    tries email first, then falls back to matching the Slack display name against
    the Notion user's name field — exact first, then first-name-only.
    """
    resp = requests.get(f"{NOTION_BASE}/users", headers=_headers())
    if resp.status_code != 200:
        logger.error("Notion users list failed: %s — %s", resp.status_code, resp.text[:200])
        return None

    people = [u for u in resp.json().get("results", []) if u.get("type") == "person"]

    # 1. Email match (works only if integration has email-read capability)
    if email:
        for u in people:
            if u.get("person", {}).get("email", "").lower() == email.lower():
                logger.info("Resolved Notion user by email: %s", u.get("name"))
                return u["id"]

    # 2. Exact name match
    if display_name:
        dn_lower = display_name.strip().lower()
        for u in people:
            if (u.get("name") or "").strip().lower() == dn_lower:
                logger.info("Resolved Notion user by exact name: %s", u.get("name"))
                return u["id"]

        # 3. First-name-only match (handles "Lars Loerup" → "Lars")
        first = dn_lower.split()[0] if dn_lower else ""
        if first:
            for u in people:
                notion_name = (u.get("name") or "").strip().lower()
                if notion_name.startswith(first + " ") or notion_name == first:
                    logger.info("Resolved Notion user by first name: %s", u.get("name"))
                    return u["id"]

    logger.warning("Could not resolve Notion user for email=%s name=%s", email, display_name)
    return None

"""
Onshape API client with HMAC-SHA256 authentication.
Covers all check-in/out operations: part search, STEP export, version/branch
creation, blob upload, Import feature update, configured-part detection.

Credentials are loaded from environment variables (set in Railway for production,
or a local .env file for development).
"""

import os
import hashlib
import hmac
import base64
import string
import random
import time
import datetime
import json
import logging
import requests
from urllib.parse import urlparse, urlencode

logger = logging.getLogger(__name__)

# ── Load credentials ──────────────────────────────────────────────────────────
# Try loading a local .env for development; in production Railway sets these directly.
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

ACCESS_KEY = os.environ.get("ONSHAPE_ACCESS_KEY", "")
SECRET_KEY = os.environ.get("ONSHAPE_SECRET_KEY", "")
BASE_URL   = os.environ.get("ONSHAPE_BASE_URL", "https://easee.onshape.com")


# ── Auth helper ───────────────────────────────────────────────────────────────
def _build_headers(method: str, path: str, query: str = "", content_type: str = "application/json") -> dict:
    """Return signed headers for an Onshape API request."""
    nonce     = "".join(random.choices(string.ascii_letters + string.digits, k=25))
    date      = datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
    query_str = query.lstrip("?") if query else ""

    # Onshape HMAC signing: path and query on separate lines, all lowercased
    string_to_sign = (
        method.lower()       + "\n" +
        nonce.lower()        + "\n" +
        date.lower()         + "\n" +
        content_type.lower() + "\n" +
        path.lower()         + "\n" +
        query_str.lower()    + "\n"
    )

    signature = base64.b64encode(
        hmac.new(
            SECRET_KEY.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
    ).decode("utf-8")

    auth = f"On {ACCESS_KEY}:HmacSHA256:{signature}"

    return {
        "Authorization": auth,
        "Date": date,
        "On-Nonce": nonce,
        "Content-Type": content_type,
        "Accept": "application/json;charset=UTF-8",
    }


def _request(method: str, endpoint: str, params: dict = None, body: dict = None, stream: bool = False):
    """Make a signed request to the Onshape API."""
    parsed   = urlparse(endpoint)
    path     = parsed.path
    query    = urlencode(params) if params else (parsed.query or "")
    url      = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
    ct       = "application/json"
    headers  = _build_headers(method, path, query, ct)

    resp = requests.request(
        method,
        url,
        headers=headers,
        params=params if not query else None,
        json=body,
        stream=stream,
        timeout=60,
    )
    return resp


# ── 1. Verify connectivity ────────────────────────────────────────────────────
def verify_auth() -> bool:
    """
    Verify credentials by fetching the current user profile.
    API key auth returns the user object at /api/v6/users/current.
    """
    resp = _request("GET", "/api/v6/users/current")
    if resp.status_code == 200:
        data = resp.json()
        print(f"✅ Authenticated as: {data.get('name', '?')} ({data.get('email', '?')})")
        return True
    # 204 on sessioninfo means auth works but no session (API key mode)
    # Fall back: try listing documents — 200/206 = auth OK
    resp2 = _request("GET", "/api/v6/documents", params={"limit": 1})
    if resp2.status_code in (200, 206):
        items = resp2.json().get("items", [])
        print(f"✅ Auth confirmed (API key mode). Documents visible: {len(items)}")
        return True
    print(f"❌ Auth failed: {resp.status_code} — {resp.text[:300]}")
    print(f"   Fallback also failed: {resp2.status_code} — {resp2.text[:300]}")
    return False


# ── 2. Search for part by Part# ───────────────────────────────────────────────
def search_by_part_number(part_number: str) -> list[dict]:
    """
    Search the Easee AS company workspace (ownerType=1) by Part# directly.
    Scans Part Studios for an exact partNumber match, stops as soon as found.
    Returns list of matches: [{documentId, workspaceId, elementId, partId,
                               partName, documentName, elementName, configuration,
                               is_configured}, ...]
    """
    resp = _request("GET", "/api/v6/documents", params={
        "q":         part_number,
        "ownerType": 1,           # Easee AS company workspace only
        "limit":     20,
    })
    if resp.status_code != 200:
        print(f"❌ Search failed: {resp.status_code} — {resp.text[:300]}")
        return []

    docs = resp.json().get("items", [])
    results = []
    for doc in docs:
        did  = doc["id"]
        name = doc["name"]
        if "Outdated" in name or "outdated" in name:
            continue
        wid = doc.get("defaultWorkspace", {}).get("id")
        if not wid:
            continue
        elems = _get_elements(did, wid)
        for elem in elems:
            # API returns "Part Studio" (with space)
            if (elem.get("type") or "").replace(" ", "").upper() == "PARTSTUDIO":
                parts = _get_parts(did, wid, elem["id"])
                for p in parts:
                    pnum = p.get("partNumber") or ""
                    if pnum.lower() == part_number.lower():
                        config = p.get("configuration") or ""
                        results.append({
                            "documentId":    did,
                            "documentName":  name,
                            "workspaceId":   wid,
                            "elementId":     elem["id"],
                            "elementName":   elem.get("name", ""),
                            "partId":        p.get("partId", ""),
                            "partName":      p.get("name", ""),
                            "configuration": config,
                            "is_configured": _is_configured_part(p),
                        })
    return results


def _get_elements(did: str, wid: str) -> list[dict]:
    resp = _request("GET", f"/api/v6/documents/d/{did}/w/{wid}/elements")
    if resp.status_code != 200:
        return []
    return resp.json()


def _get_parts(did: str, wid: str, eid: str) -> list[dict]:
    resp = _request("GET", f"/api/v6/parts/d/{did}/w/{wid}/e/{eid}")
    if resp.status_code != 200:
        return []
    return resp.json()


def _is_configured_part(part: dict) -> bool:
    """Return True if this part has a non-default configuration."""
    config = part.get("configuration") or ""
    return bool(config) and config.lower() not in ("", "default")


# ── 3. Export STEP ────────────────────────────────────────────────────────────
def export_step(did: str, wid: str, eid: str, part_id: str, output_path: str,
                configuration: str = "") -> bool:
    """
    Export a single part as STEP via the Translation API.
    Polls until the translation completes, then downloads the file.
    Pass `configuration` (e.g. 'Length=120 mm') to export a specific config.
    """
    body = {
        "formatName":         "STEP",
        "flattenAssemblies":  False,
        "yAxisIsUp":          False,
        "triggerAutoDownload": False,
        "storeInDocument":    False,
        "linkDocumentWorkspaceId": wid,
        "partIds": part_id,  # single part ID as string (not array)
    }
    params = {}
    if configuration:
        params["configuration"] = configuration
        print(f"  Exporting configuration: {configuration}")

    resp = _request("POST", f"/api/v6/partstudios/d/{did}/w/{wid}/e/{eid}/translations",
                    params=params, body=body)
    if resp.status_code not in (200, 202):
        print(f"❌ Translation request failed: {resp.status_code} — {resp.text[:400]}")
        return False

    translation_id = resp.json().get("id")
    print(f"  Translation started: {translation_id}")

    # Poll for completion
    for attempt in range(30):
        time.sleep(3)
        poll = _request("GET", f"/api/v6/translations/{translation_id}")
        if poll.status_code != 200:
            print(f"  Poll error: {poll.status_code}")
            continue
        state = poll.json().get("requestState", "")
        print(f"  [{attempt+1}] State: {state}")
        if state == "DONE":
            result_ids = poll.json().get("resultExternalDataIds", [])
            if not result_ids:
                print("❌ Translation done but no result file IDs found")
                return False
            # Download the file
            dl = _request("GET", f"/api/v6/documents/d/{did}/externaldata/{result_ids[0]}", stream=True)
            if dl.status_code != 200:
                print(f"❌ Download failed: {dl.status_code}")
                return False
            with open(output_path, "wb") as f:
                for chunk in dl.iter_content(chunk_size=8192):
                    f.write(chunk)
            size_kb = os.path.getsize(output_path) / 1024
            print(f"✅ Downloaded: {output_path} ({size_kb:.1f} KB)")
            return True
        elif state == "FAILED":
            print(f"❌ Translation failed: {poll.json().get('failureReason', 'unknown')}")
            return False

    print("❌ Timed out waiting for translation")
    return False


# ── 4. Create version ─────────────────────────────────────────────────────────
def create_version(did: str, wid: str, part_numbers: list[str]) -> str | None:
    """
    Create a named version in the document.
    Names it 'BOT### - Check out reference' and records the checked-out
    Part#(s) in the description for traceability.
    Returns the version ID or None on failure.
    """
    # Get existing versions to determine next number
    resp = _request("GET", f"/api/v6/documents/d/{did}/versions")
    if resp.status_code != 200:
        print(f"❌ Could not list versions: {resp.status_code}")
        return None
    existing = resp.json()
    next_num = len(existing) + 1
    version_name = f"BOT{next_num:03d} - Check out reference"
    parts_str    = ", ".join(part_numbers)

    body = {
        "documentId":  did,
        "workspaceId": wid,
        "name":        version_name,
        "description": f"Checked out by agent. Part(s): {parts_str}",
    }
    resp = _request("POST", f"/api/v6/documents/d/{did}/versions", body=body)
    if resp.status_code in (200, 201):
        vid = resp.json().get("id")
        print(f"✅ Version created: {version_name} (id: {vid})")
        return vid
    else:
        print(f"❌ Version creation failed: {resp.status_code} — {resp.text[:300]}")
        return None


# ── 5. Create branch from version ─────────────────────────────────────────────
def create_branch(did: str, version_id: str, part_numbers: list[str]) -> str | None:
    """
    Create a branch named 'Check out - Agent' from a version.
    Records the checked-out Part#(s) in the branch description.
    Returns the new workspace (branch) ID or None on failure.
    """
    parts_str = ", ".join(part_numbers)
    body = {
        "name":          "Check out - Agent",
        "description":   f"Checked out by agent. Part(s): {parts_str}",
        "fromVersionId": version_id,
        "isReadOnly":    False,
    }
    resp = _request("POST", f"/api/v6/documents/d/{did}/workspaces", body=body)
    if resp.status_code in (200, 201):
        branch_id = resp.json().get("id")
        print(f"✅ Branch created: 'Check out - Agent' (id: {branch_id})")
        return branch_id
    else:
        print(f"❌ Branch creation failed: {resp.status_code} — {resp.text[:300]}")
        return None


# ── 6. Update blob element with new .stp file ────────────────────────────────
def update_blob_element(did: str, wid: str, blob_eid: str, stp_path: str) -> str | None:
    """
    Upload a new .stp file over an existing blob element in a workspace.
    Uses a pre-computed multipart boundary so the exact Content-Type can be
    included in the HMAC signing string.
    Returns the new element microversion ID, or None on failure.
    """
    import uuid
    filename  = os.path.basename(stp_path)
    boundary  = uuid.uuid4().hex
    ct        = f"multipart/form-data; boundary={boundary}"
    path      = f"/api/v6/blobelements/d/{did}/w/{wid}/e/{blob_eid}"
    headers   = _build_headers("POST", path, "", ct)

    with open(stp_path, "rb") as f:
        file_data = f.read()

    # Build multipart body manually with the known boundary
    body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + file_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

    resp = requests.post(
        f"{BASE_URL}{path}",
        headers=headers,
        data=body,
        timeout=60,
    )

    if resp.status_code in (200, 201):
        mv = resp.json().get("microversionId", "")
        print(f"✅ Blob updated: {filename} → microversion: {mv}")
        return mv
    else:
        print(f"❌ Blob update failed: {resp.status_code} — {resp.text[:400]}")
        return None


def get_blob_microversion(did: str, wid: str, blob_eid: str) -> str | None:
    """Get the current microversion of a blob element."""
    resp = _request("GET", f"/api/v6/documents/d/{did}/w/{wid}/elements")
    if resp.status_code != 200:
        return None
    for elem in resp.json():
        if elem.get("id") == blob_eid:
            mv = elem.get("microversionId", "")
            print(f"  Blob microversion: {mv}")
            return mv
    return None


# ── 6b. Create a NEW blob element (for configured/shared Import cases) ─────────
def create_new_blob_element(did: str, wid: str, stp_path: str, blob_name: str) -> str | None:
    """
    POST a brand-new blob element to the document workspace.
    Used when the existing blob is shared across configurations — avoids
    overwriting geometry for other configured variants.
    Returns the new element ID, or None on failure.
    """
    import uuid
    filename  = os.path.basename(stp_path)
    boundary  = uuid.uuid4().hex
    ct        = f"multipart/form-data; boundary={boundary}"
    path      = f"/api/v6/blobelements/d/{did}/w/{wid}"
    headers   = _build_headers("POST", path, "", ct)

    with open(stp_path, "rb") as f:
        file_data = f.read()

    # Include element name so Onshape names it correctly in the element list
    body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"encodedFilename\"\r\n\r\n"
        f"{blob_name}\r\n"
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + file_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

    resp = requests.post(
        f"{BASE_URL}{path}",
        headers=headers,
        data=body,
        timeout=60,
    )

    if resp.status_code in (200, 201):
        new_eid = resp.json().get("id", "")
        print(f"✅ New blob element created: '{blob_name}' (id: {new_eid})")
        return new_eid
    else:
        print(f"❌ New blob creation failed: {resp.status_code} — {resp.text[:400]}")
        return None


# ── 6c. Detect shared Import features ──────────────────────────────────────────
def detect_shared_import(did: str, wid: str, eid: str, blob_eid: str) -> bool:
    """
    Return True if the blob element is referenced by more than one Import feature
    in the Part Studio — indicating a configured/shared setup where overwriting
    the blob would affect multiple parts.
    """
    resp = _request("GET", f"/api/v6/partstudios/d/{did}/w/{wid}/e/{eid}/features")
    if resp.status_code != 200:
        return False
    features = resp.json().get("features", [])
    import_features = [
        f for f in features
        if (f.get("featureType") or "").lower() in ("import", "importforeign")
    ]
    # Count how many Import features reference our blob element
    refs = 0
    for f in import_features:
        for param in f.get("parameters", []):
            if param.get("parameterId") == "blobData":
                ns = param.get("namespace", "")
                if blob_eid in ns:
                    refs += 1
    shared = refs > 1 or len(import_features) > 1
    if shared:
        print(f"  ⚠️  Shared Import detected: {len(import_features)} Import feature(s), "
              f"{refs} reference(s) to this blob")
    return shared


# ── 7. Update Import feature to reference new blob microversion ───────────────
def update_import_feature(did: str, wid: str, eid: str, feature_id: str,
                           blob_eid: str, blob_mv: str, full_feature: dict) -> bool:
    """
    Update the blobData namespace in an existing Import feature to point to
    a new blob microversion: 'e{blob_eid}::{blob_mv}'.
    Sends the full feature back with only the namespace swapped.
    """
    import copy
    updated = copy.deepcopy(full_feature)
    # Onshape microversion namespace format: e{elementId}::m{microversionId}
    mv_clean     = blob_mv.lstrip("m")   # strip any accidental prefix before re-adding
    new_namespace = f"e{blob_eid}::m{mv_clean}"

    for param in updated.get("parameters", []):
        if param.get("parameterId") == "blobData":
            param["namespace"] = new_namespace
            print(f"  Updated blobData namespace → {new_namespace}")

    # Endpoint expects BTFeatureDefinitionCall-1406 wrapping the BTMFeature object
    path = f"/api/v6/partstudios/d/{did}/w/{wid}/e/{eid}/features/featureid/{feature_id}"
    body = {
        "btType":  "BTFeatureDefinitionCall-1406",
        "feature": updated,
    }
    resp = _request("POST", path, body=body)
    if resp.status_code in (200, 201):
        print(f"✅ Import feature updated successfully")
        return True
    else:
        print(f"❌ Import feature update failed: {resp.status_code} — {resp.text[:400]}")
        return False


# ── Clean filename ─────────────────────────────────────────────────────────────
def clean_filename(raw_name: str) -> str:
    """
    Normalise Onshape auto-generated export names to 'Part# - Name' convention.
    e.g. 'M001505-Rev. Harvest Clip API Test - In progress' → 'M001505 - Harvest Clip API Test'
    """
    import re
    name = raw_name
    # Strip trailing status suffixes
    name = re.sub(r"\s*-\s*(In progress|In Progress|Done|WIP|Outdated)$", "", name)
    # Replace 'M001505-Rev.' or 'M001505-Rev' pattern with 'M001505 -'
    name = re.sub(r"(M\d+)-Rev\.?\s*", r"\1 - ", name)
    # Collapse multiple spaces
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name

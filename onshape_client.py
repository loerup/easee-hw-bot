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

# Module-level company ID cache — fetched once, reused across calls
_COMPANY_ID: str | None = None


def _get_company_id() -> str | None:
    """
    Return the Easee AS company ID, fetching it from the API on first call
    and caching it for the lifetime of the process.

    The API key is scoped to easee.onshape.com, so /api/v6/companies will only
    return Easee AS — no risk of picking up a different company.
    """
    global _COMPANY_ID
    if _COMPANY_ID:
        return _COMPANY_ID
    resp = _request("GET", "/api/v6/companies")
    if resp.status_code == 200:
        data = resp.json()
        # API may return a plain list or a paginated {"items": [...]} object
        companies = data if isinstance(data, list) else data.get("items", [])
        if companies:
            _COMPANY_ID = companies[0].get("id")
            company_name = companies[0].get("name", "unknown")
            print(f"  Easee AS company: '{company_name}' (id: {_COMPANY_ID})")
            return _COMPANY_ID
    print(f"  ⚠️ Could not fetch company ID: {resp.status_code} — {resp.text[:200]}")
    return None


def search_by_part_number(part_number: str, hint: str = "") -> list[dict]:
    """
    Search the Easee AS workspace for a part by its Part# attribute.

    Strategy:
      1. Global search  — POST /api/v6/documents/search with rawQuery="_all:<part#>"
                          Mirrors the Onshape UI search bar. Returns the matching
                          document(s) in ~1s; we then fetch elements+parts for the
                          matched doc to get the exact IDs. Total: ~4 API calls.
      2. Fallback scan  — Full workspace document scan (slow, last resort only).

    Returns list of matches: [{documentId, workspaceId, elementId, partId,
                               partName, documentName, elementName, configuration,
                               is_configured}, ...]
    """
    # ── Global search (primary) ────────────────────────────────────────────────
    result = _global_search_for_part(part_number)
    if result:
        return result

    # ── Fallback: brute-force scan (last resort) ───────────────────────────────
    print(f"  Global search found nothing — falling back to workspace scan…")
    return _scan_workspace_for_part(part_number)


def _global_search_for_part(part_number: str) -> list[dict]:
    """
    Use Onshape's global document search API to locate a part by its Part#.

    Uses rawQuery="_all:<part_number>" which searches all indexed fields,
    including part number metadata attributes — same index the UI search bar uses.
    Returns results in ~1 second for any workspace size.
    """
    company_id = _get_company_id()
    if not company_id:
        print("  Skipping global search — no company ID available")
        return []

    # ownerId scopes the search to Easee AS company documents only.
    # We try two query forms: "_all:<part#>" (Lucene all-fields) and plain "<part#>".
    # The plain query matches element/document names; _all also searches metadata.
    results_by_query: list = []
    for raw_query in [f"_all:{part_number}", part_number]:
        body = {
            "rawQuery":       raw_query,
            "ownerId":        company_id,
            "documentFilter": 0,
            "foundIn":        "ALL",
            "when":           "LATEST",
            "limit":          10,
            "offset":         0,
        }
        resp = _request("POST", "/api/v6/documents/search", body=body)
        if resp.status_code != 200:
            print(f"  Global search ({raw_query!r}) failed: {resp.status_code} — {resp.text[:200]}")
            continue
        items = resp.json().get("items", [])
        print(f"  Global search ({raw_query!r}) returned {len(items)} document(s)")
        results_by_query.extend(items)
        if items:
            break   # found something — no need to try the next query form

    items = results_by_query

    for item in items:
        did      = item.get("id")
        wid      = item.get("defaultWorkspace", {}).get("id")
        doc_name = item.get("name") or ""

        if not did or not wid:
            continue
        if "outdated" in doc_name.lower():
            continue

        # Scan Part Studios in this document for the exact part number
        elems = _get_elements(did, wid)
        for elem in elems:
            if (elem.get("type") or "").replace(" ", "").upper() == "PARTSTUDIO":
                parts = _get_parts(did, wid, elem["id"])
                for p in parts:
                    pnum = p.get("partNumber") or ""
                    if pnum.lower() == part_number.lower():
                        config = p.get("configuration") or ""
                        print(f"  ✅ Found {part_number} in '{doc_name}' via global search")
                        return [{
                            "documentId":    did,
                            "documentName":  doc_name,
                            "workspaceId":   wid,
                            "elementId":     elem["id"],
                            "elementName":   elem.get("name") or "",
                            "partId":        p.get("partId") or "",
                            "partName":      p.get("name") or "",
                            "featureId":     p.get("featureId") or "",
                            "configuration": config,
                            "is_configured": _is_configured_part(p),
                        }]

    return []


def _scan_workspace_for_part(part_number: str, max_pages: int = 5) -> list[dict]:
    """
    Fallback: paginate through workspace documents looking for an exact Part# match.
    Only used when the global search returns no results.
    Fetches 100 documents per page to minimise API round-trips.
    """
    offset       = 0
    docs_checked = 0
    for page in range(max_pages):
        params = {"ownerType": 1, "limit": 20, "offset": offset}  # max allowed is 20
        resp = _request("GET", "/api/v6/documents", params=params)
        if resp.status_code != 200:
            print(f"❌ Document list failed: {resp.status_code} — {resp.text[:200]}")
            break

        data = resp.json()
        docs = data.get("items", [])
        if not docs:
            break

        for doc in docs:
            did  = doc["id"]
            name = doc["name"]
            if "outdated" in name.lower():
                continue
            wid = doc.get("defaultWorkspace", {}).get("id")
            if not wid:
                continue

            docs_checked += 1
            elems = _get_elements(did, wid)
            for elem in elems:
                if (elem.get("type") or "").replace(" ", "").upper() == "PARTSTUDIO":
                    parts = _get_parts(did, wid, elem["id"])
                    for p in parts:
                        pnum = p.get("partNumber") or ""
                        if pnum.lower() == part_number.lower():
                            config = p.get("configuration") or ""
                            print(f"  ✅ Found {part_number} in '{name}' "
                                  f"(fallback scan, {docs_checked} docs checked)")
                            return [{
                                "documentId":    did,
                                "documentName":  name,
                                "workspaceId":   wid,
                                "elementId":     elem["id"],
                                "elementName":   elem.get("name") or "",
                                "partId":        p.get("partId") or "",
                                "partName":      p.get("name") or "",
                                "featureId":     p.get("featureId") or "",
                                "configuration": config,
                                "is_configured": _is_configured_part(p),
                            }]

        print(f"  Fallback scan page {page+1}: {docs_checked} docs checked so far…")
        if not data.get("next"):
            break
        offset += 20

    return []


def find_import_and_blob_for_part(did: str, wid: str, eid: str,
                                   part_feature_id: str) -> tuple[dict | None, str | None]:
    """
    Resolve the Import feature and blob element ID for a specific part using
    the Onshape feature graph — no name matching required.

    Flow:
      part.featureId  →  Import/importForeign feature  →  blobData.namespace
                                                          → e{blob_eid}::m{mv}

    Returns (import_feature_dict, blob_eid) or (None, None) on failure.
    Falls back to the sole Import feature if featureId matching is unavailable.
    """
    resp = _request("GET", f"/api/v6/partstudios/d/{did}/w/{wid}/e/{eid}/features")
    if resp.status_code != 200:
        print(f"❌ Could not fetch features: {resp.status_code}")
        return None, None

    features = resp.json().get("features", [])
    import_features = [
        f for f in features
        if (f.get("featureType") or "").lower() in ("import", "importforeign")
    ]

    if not import_features:
        print("  No Import features found in Part Studio")
        return None, None

    # 1. Exact featureId match — most reliable
    target = None
    if part_feature_id:
        for f in import_features:
            if f.get("featureId") == part_feature_id:
                target = f
                print(f"  Matched Import feature by featureId: {f.get('featureId')}")
                break

    # 2. Fallback: only one Import feature exists — unambiguous
    if not target and len(import_features) == 1:
        target = import_features[0]
        print(f"  Single Import feature found — using it: {target.get('featureId')}")

    if not target:
        ids = [f.get("featureId") for f in import_features]
        print(f"  ⚠️ Multiple Import features, no featureId match. "
              f"part_feature_id={part_feature_id!r}, available={ids}")
        return None, None

    # 3. Extract blob element ID from blobData namespace: e{eid}::m{mv}
    for param in target.get("parameters", []):
        if param.get("parameterId") == "blobData":
            ns = param.get("namespace", "")
            if ns.startswith("e") and "::" in ns:
                blob_eid = ns.split("::")[0][1:]   # strip leading 'e'
                print(f"  Blob EID resolved from Import feature: {blob_eid}")
                return target, blob_eid

    print(f"  Import feature found but no blobData namespace: {target.get('featureId')}")
    return target, None


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
        "formatName":          "STEP",
        "flattenAssemblies":   False,
        "yAxisIsUp":           False,
        "triggerAutoDownload": False,
        "storeInDocument":     False,
        # Note: linkDocumentWorkspaceId intentionally omitted — it triggers
        # external-reference resolution which significantly delays translations
        # on standalone Part Studios.
        "partIds": part_id,    # single part ID as string (not array)
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

    # Poll for completion — up to 5 minutes (60 × 5s)
    # Branch-based exports can take longer than main-workspace exports
    for attempt in range(60):
        time.sleep(5)
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
def create_version(did: str, wid: str, part_numbers: list[str],
                   label: str = "Check out",
                   description: str | None = None) -> str | None:
    """
    Create a named version in the document.

    label       — appended to the version name: 'BOT### - {label} reference'
    description — full description string; if None a default is generated.

    Returns the version ID or None on failure.
    """
    # Count existing versions to build the BOT### sequence number
    resp = _request("GET", f"/api/v6/documents/d/{did}/versions")
    if resp.status_code != 200:
        print(f"❌ Could not list versions: {resp.status_code}")
        return None

    existing = resp.json()
    # API may return a plain list or a paginated {"items": [...]} object
    if isinstance(existing, dict):
        existing = existing.get("items", [])
    next_num = len(existing) + 1

    parts_str    = ", ".join(part_numbers)
    version_name = f"BOT{next_num:03d} - {label} reference"
    desc         = description or f"CAD-BOT {label.lower()} reference. Part(s): {parts_str}"

    body = {
        "documentId":  did,
        "workspaceId": wid,
        "name":        version_name,
        "description": desc,
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
    # Only flag as shared if our specific blob is referenced by more than one Import feature.
    # A document with multiple imported parts legitimately has multiple Import features —
    # that alone doesn't mean the blob is shared.
    shared = refs > 1
    if shared:
        print(f"  ⚠️  Shared Import detected: {refs} Import feature(s) reference this blob")
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

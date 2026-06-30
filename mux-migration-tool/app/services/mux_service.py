import time
import requests
import logging
from app.config import MUX_TOKEN_ID, MUX_TOKEN_SECRET, DRM_CONFIGURATION_ID

BASE_URL = "https://api.mux.com/video/v1"
logger = logging.getLogger(__name__)
AUTH = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)

# ---------------------------------------------------------------------------
# Rate-limit-aware request wrapper
# ---------------------------------------------------------------------------
# Retries on 429 (rate limit) and 5xx (server errors) with exponential backoff.
# All Mux API calls in this module go through _mux_request().
# ---------------------------------------------------------------------------

def _mux_request(method: str, url: str, max_retries: int = 6, **kwargs) -> requests.Response:
    """
    Wrapper around requests that automatically retries on 429 / 5xx.

    429 handling:
      - Reads the Retry-After header (seconds) if present, otherwise doubles
        the wait starting at 10 s (10 → 20 → 40 → 60 cap).

    5xx handling:
      - Exponential backoff: 5 → 10 → 20 → 40 s (cap 60 s).

    Raises the last response as an Exception after max_retries exhausted.
    """
    kwargs.setdefault("auth", AUTH)
    wait = 10  # initial wait for 429
    srv_wait = 5  # initial wait for 5xx

    for attempt in range(1, max_retries + 1):
        r = requests.request(method, url, **kwargs)

        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", wait))
            logger.warning(
                f"[Mux] 429 rate-limited on {method} {url} "
                f"(attempt {attempt}/{max_retries}) — waiting {retry_after}s"
            )
            if attempt == max_retries:
                return r
            time.sleep(retry_after)
            wait = min(wait * 2, 60)  # cap at 60 s
            continue

        if r.status_code >= 500:
            logger.warning(
                f"[Mux] {r.status_code} server error on {method} {url} "
                f"(attempt {attempt}/{max_retries}) — waiting {srv_wait}s"
            )
            if attempt == max_retries:
                return r
            time.sleep(srv_wait)
            srv_wait = min(srv_wait * 2, 60)
            continue

        return r

    return r  # fallback (should not reach here)


# ---------------------------------------------------------------------------
# Asset creation
# ---------------------------------------------------------------------------

def upload_video(video_url: str, title: str = "Untitled", captions: list = None,
                 audio_tracks: list = None, folder_name: str = None) -> dict:
    """
    Creates a Mux asset from a remote URL.
    - DRM playback policy is set at creation time (not added afterwards).
    - Captions are submitted as inputs in the same API call.
    - Audio tracks can be included as inputs too (used when HLS URL is directly usable).
    Returns: {asset_id, playback_id, drm_playback_id}
    """
    inputs = [{"url": video_url}]

    for cap in (captions or []):
        lang = cap.get("language") or "en"
        inputs.append({
            "url": cap["url"],
            "type": "text",
            "text_type": "subtitles",
            "language_code": lang,
            "name": lang,
        })

    for track in (audio_tracks or []):
        lang = track.get("language") or "en"
        inputs.append({
            "url": track["url"],
            "type": "audio",
            "language_code": lang,
            "name": track.get("name") or lang,
        })

    safe_title = (title or "Untitled")[:512]

    if DRM_CONFIGURATION_ID:
        payload = {
            "input": inputs,
            "advanced_playback_policies": [
                {"policy": "drm", "drm_configuration_id": DRM_CONFIGURATION_ID}
            ],
            "video_quality": "premium",
            "meta": {"title": safe_title},
            "passthrough": (folder_name or "")[:255],
        }
    else:
        payload = {
            "input": inputs,
            "playback_policy": ["public"],
            "video_quality": "premium",
            "meta": {"title": safe_title},
            "passthrough": (folder_name or "")[:255],
        }

    r = _mux_request("POST", f"{BASE_URL}/assets", json=payload)
    if not r.ok:
        raise Exception(f"Mux create asset error ({r.status_code}): {r.text}")

    data = r.json()["data"]
    asset_id = data["id"]
    playback_ids = data.get("playback_ids", [])
    drm_id = next((p["id"] for p in playback_ids if p.get("policy") == "drm"), None)

    # For DRM assets, add BOTH:
    #   - signed playback ID  → used by app player (JWT-protected)
    #   - public playback ID  → used by Mux dashboard for preview only
    signed_id = None
    public_id = None
    if DRM_CONFIGURATION_ID and drm_id:
        # Signed ID (app player)
        rs = _mux_request("POST", f"{BASE_URL}/assets/{asset_id}/playback-ids", json={"policy": "signed"})
        if rs.ok:
            signed_id = rs.json()["data"]["id"]
            logger.info(f"[Mux] Signed playback ID added: {signed_id}")
        else:
            logger.warning(f"[Mux] Failed to add signed playback ID: {rs.status_code} {rs.text[:200]}")

        # Public ID (Mux dashboard preview)
        rp = _mux_request("POST", f"{BASE_URL}/assets/{asset_id}/playback-ids", json={"policy": "public"})
        if rp.ok:
            public_id = rp.json()["data"]["id"]
            logger.info(f"[Mux] Public playback ID added: {public_id}")
        else:
            logger.warning(f"[Mux] Failed to add public playback ID: {rp.status_code} {rp.text[:200]}")

    playback_id = public_id or (playback_ids[0]["id"] if playback_ids else None)

    logger.info(f"[Mux] Asset created: {asset_id} | DRM: {drm_id or 'none'} | Signed: {signed_id or 'none'} | Public: {public_id or 'none'}")
    return {"asset_id": asset_id, "playback_id": playback_id, "signed_playback_id": signed_id, "drm_playback_id": drm_id}


# ---------------------------------------------------------------------------
# Direct upload (for manual / local-file uploads)
# ---------------------------------------------------------------------------

def create_direct_upload(title: str) -> dict:
    """
    Creates a Mux Direct Upload slot.
    Returns: {upload_id, upload_url}
    """
    if DRM_CONFIGURATION_ID:
        asset_settings = {
            "advanced_playback_policies": [
                {"policy": "drm", "drm_configuration_id": DRM_CONFIGURATION_ID}
            ],
            "video_quality": "premium",
            "meta": {"title": title[:512]},
        }
    else:
        asset_settings = {
            "playback_policy": ["public"],
            "video_quality": "premium",
            "meta": {"title": title[:512]},
        }

    r = _mux_request("POST", f"{BASE_URL}/uploads",
                     json={"new_asset_settings": asset_settings, "cors_origin": "*"})
    if not r.ok:
        raise Exception(f"Mux direct upload error ({r.status_code}): {r.text}")
    data = r.json()["data"]
    return {"upload_id": data["id"], "upload_url": data["url"]}


def push_file_to_upload_url(upload_url: str, file_path: str) -> None:
    """Streams a local file to the Mux direct upload PUT URL."""
    import os
    file_size = os.path.getsize(file_path)
    with open(file_path, "rb") as f:
        r = requests.put(
            upload_url,
            data=f,
            headers={"Content-Type": "video/*", "Content-Length": str(file_size)},
            timeout=(30, 7200),
        )
    if not r.ok:
        raise Exception(f"File upload to Mux failed ({r.status_code}): {r.text[:300]}")


def poll_upload_for_asset_id(upload_id: str, timeout: int = 300) -> str:
    """Polls the upload record until asset_id is available. Returns asset_id."""
    elapsed = 0
    while elapsed < timeout:
        r = _mux_request("GET", f"{BASE_URL}/uploads/{upload_id}")
        if r.ok:
            asset_id = r.json()["data"].get("asset_id")
            if asset_id:
                return asset_id
        time.sleep(5)
        elapsed += 5
    raise Exception(f"Timed out waiting for asset_id from upload {upload_id}")


# ---------------------------------------------------------------------------
# Asset state
# ---------------------------------------------------------------------------

def get_asset(asset_id: str) -> dict:
    r = _mux_request("GET", f"{BASE_URL}/assets/{asset_id}")
    if not r.ok:
        raise Exception(f"Mux get asset error ({r.status_code}): {r.text}")
    return r.json()["data"]


def wait_for_asset_ready(asset_id: str, timeout: int = 600, interval: int = 10) -> dict:
    """Polls until status == 'ready'. Raises on error or timeout."""
    elapsed = 0
    while elapsed < timeout:
        asset = get_asset(asset_id)
        status = asset.get("status")
        logger.info(f"[Mux] Asset {asset_id} status: {status} ({elapsed}s)")
        if status == "ready":
            return asset
        if status == "errored":
            raise Exception(f"Mux asset {asset_id} errored during processing.")
        time.sleep(interval)
        elapsed += interval
    raise Exception(f"Mux asset {asset_id} not ready after {timeout}s.")


def list_all_assets() -> list:
    """Paginates through all Mux assets and returns them."""
    assets, page = [], 1
    while True:
        r = _mux_request("GET", f"{BASE_URL}/assets", params={"limit": 100, "page": page})
        if not r.ok:
            raise Exception(f"Mux list assets error ({r.status_code}): {r.text}")
        batch = r.json().get("data", [])
        assets.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return assets


# ---------------------------------------------------------------------------
# Asset mutation
# ---------------------------------------------------------------------------

def update_asset_title(asset_id: str, title: str) -> dict:
    """Updates the meta.title on a Mux asset (PATCH). Safe to call anytime."""
    r = _mux_request("PATCH", f"{BASE_URL}/assets/{asset_id}", json={"meta": {"title": title[:512]}})
    if not r.ok:
        raise Exception(f"Mux update asset error ({r.status_code}): {r.text}")
    logger.info(f"[Mux] Title updated on {asset_id}: '{title}'")
    return r.json()["data"]


def delete_asset(asset_id: str) -> bool:
    r = _mux_request("DELETE", f"{BASE_URL}/assets/{asset_id}")
    if not r.ok:
        raise Exception(f"Mux delete asset error ({r.status_code}): {r.text}")
    logger.info(f"[Mux] Asset deleted: {asset_id}")
    return True


def add_audio_track(asset_id: str, url: str, language: str, name: str) -> dict:
    """Attaches an alternate audio track to an existing ready asset."""
    r = _mux_request("POST", f"{BASE_URL}/assets/{asset_id}/tracks",
                     json={"url": url, "type": "audio", "language_code": language, "name": name})
    if not r.ok:
        raise Exception(f"Mux add audio track error ({r.status_code}): {r.text}")
    logger.info(f"[Mux] Audio track '{name}' ({language}) attached to {asset_id}")
    return r.json()["data"]


def add_text_track(asset_id: str, url: str, language: str, name: str) -> dict:
    """Attaches a subtitle/caption track to an existing ready asset."""
    r = _mux_request("POST", f"{BASE_URL}/assets/{asset_id}/tracks",
                     json={
                         "url": url,
                         "type": "text",
                         "text_type": "subtitles",
                         "language_code": language,
                         "name": name,
                     })
    if not r.ok:
        raise Exception(f"Mux add text track error ({r.status_code}): {r.text}")
    logger.info(f"[Mux] Text track '{name}' ({language}) attached to {asset_id}")
    return r.json()["data"]

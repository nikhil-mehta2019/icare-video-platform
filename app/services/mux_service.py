import requests
import logging
import json
import base64
import jwt
import time
from app.config import MUX_TOKEN_ID, MUX_TOKEN_SECRET, MUX_PRIVATE_KEY, MUX_SIGNING_KEY_ID, DRM_CONFIGURATION_ID

BASE_URL = "https://api.mux.com/video/v1"
logger = logging.getLogger(__name__)

def upload_video(video_url, title="Untitled", captions=None, audio_tracks=None, folder_name=None):
    logger.info(f"[Mux Service] Starting upload_video for title: '{title}'")
    
    inputs = [{"url": video_url}]
    logger.info(f"[Mux Service] Base video input added: {video_url}")
    
    if captions:
        logger.info(f"[Mux Service] Processing {len(captions)} caption tracks...")
        for cap in captions:
            lang = cap.get("language") or "en"
            name = lang  # use language code as name — always unique per asset
            inputs.append({
                "url": cap["url"],
                "type": "text",
                "text_type": "subtitles",
                "language_code": lang,
                "name": name
            })
            logger.info(f"[Mux Service] Added caption input: {name} ({lang})")
            
    if audio_tracks:
        logger.info(f"[Mux Service] Processing {len(audio_tracks)} alternate audio track(s)...")
        for track in audio_tracks:
            lang = track.get("language") or "en"
            name = track.get("name") or lang
            inputs.append({
                "url": track["url"],
                "type": "audio",
                "language_code": lang,
                "name": name
            })
            logger.info(f"[Mux Service] Added audio track input: {name} ({lang})")

    safe_title = title[:250] if title else "Untitled"

    # DRM requires `advanced_playback_policies` (not `playback_policy`) and video_quality "plus" or "premium".
    # Without DRM_CONFIGURATION_ID the asset is created as public (development / non-DRM env).
    if DRM_CONFIGURATION_ID:
        logger.info(f"[Mux Service] DRM enabled — using configuration {DRM_CONFIGURATION_ID}")
        payload = {
            "input": inputs,
            "advanced_playback_policies": [
                {"policy": "drm", "drm_configuration_id": DRM_CONFIGURATION_ID}
            ],
            "video_quality": "premium",
            "meta": {"title": safe_title},
            "passthrough": folder_name[:255] if folder_name else "",
        }
    else:
        logger.info("[Mux Service] DRM not configured — using public playback policy")
        payload = {
            "input": inputs,
            "playback_policy": ["public"],
            "video_quality": "premium",
            "meta": {"title": safe_title},
            "passthrough": folder_name[:255] if folder_name else "",
        }

    logger.info(f"[Mux Service] Dispatching POST request to Mux API...")
    response = requests.post(
        f"{BASE_URL}/assets",
        json=payload,
        auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET)
    )
    
    if not response.ok:
        error_details = response.text
        logger.error(f"[Mux Service] ❌ Mux API POST Error ({response.status_code}): {error_details}")
        raise Exception(f"Mux API Error ({response.status_code}): {error_details}")

    data = response.json()["data"]
    logger.info(f"[Mux Service] ✅ Mux asset created successfully. Asset ID: {data['id']}")

    playback_ids = data.get("playback_ids", [])
    drm_playback_id = next((p["id"] for p in playback_ids if p.get("policy") == "drm"), None)
    first_playback_id = playback_ids[0]["id"] if playback_ids else None

    return {
        "asset_id": data["id"],
        "playback_id": first_playback_id,
        "drm_playback_id": drm_playback_id,
    }

def get_asset(asset_id: str):
    logger.info(f"[Mux Service] Fetching asset details for ID: {asset_id}")
    response = requests.get(
        f"{BASE_URL}/assets/{asset_id}",
        auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET)
    )
    if not response.ok:
        logger.error(f"[Mux Service] ❌ Failed to fetch asset {asset_id}: {response.text}")
        raise Exception(f"Mux API Error: {response.text}")
        
    logger.info(f"[Mux Service] ✅ Successfully retrieved asset details for {asset_id}")
    return response.json()["data"]

def delete_asset(asset_id: str):
    logger.info(f"[Mux Service] Attempting to delete asset: {asset_id}")
    response = requests.delete(
        f"{BASE_URL}/assets/{asset_id}",
        auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET)
    )
    if not response.ok:
        logger.error(f"[Mux Service] ❌ Failed to delete asset {asset_id}: {response.text}")
        raise Exception(f"Mux API Error: {response.text}")
        
    logger.info(f"[Mux Service] ✅ Successfully deleted asset: {asset_id}")
    return True

def get_all_assets():
    logger.info("[Mux Service] Starting to fetch all assets from Mux...")
    assets = []
    page = 1
    while True:
        logger.info(f"[Mux Service] Fetching page {page} of Mux assets...")
        response = requests.get(
            f"{BASE_URL}/assets",
            params={"limit": 100, "page": page},
            auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET)
        )
        if not response.ok:
            logger.error(f"[Mux Service] ❌ Error fetching page {page}: {response.text}")
            break
            
        data = response.json().get("data", [])
        if not data:
            logger.info("[Mux Service] Reached the end of Mux asset pages.")
            break
            
        assets.extend(data)
        logger.info(f"[Mux Service] Fetched {len(data)} assets on page {page}. Total so far: {len(assets)}")
        page += 1
        
    return assets

def wait_for_asset_ready(asset_id: str, timeout_seconds: int = 600, poll_interval: int = 10):
    """Polls Mux every 10s until asset status is 'ready'. Raises on error or timeout."""
    elapsed = 0
    while elapsed < timeout_seconds:
        asset = get_asset(asset_id)
        status = asset.get("status")
        logger.info(f"[Mux Service] Asset {asset_id} status: {status} ({elapsed}s elapsed)")
        if status == "ready":
            return asset
        if status == "errored":
            raise Exception(f"Mux asset {asset_id} errored during processing.")
        time.sleep(poll_interval)
        elapsed += poll_interval
    raise Exception(f"Mux asset {asset_id} did not become ready within {timeout_seconds}s.")

def add_audio_track(asset_id: str, url: str, language: str, name: str):
    """Attaches a single alternate audio track to an existing ready Mux asset."""
    logger.info(f"[Mux Service] Attaching audio track '{name}' ({language}) to asset {asset_id}")
    response = requests.post(
        f"{BASE_URL}/assets/{asset_id}/tracks",
        json={
            "url": url,
            "type": "audio",
            "language_code": language,
            "name": name
        },
        auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET)
    )
    if not response.ok:
        raise Exception(f"Mux API Error ({response.status_code}): {response.text}")
    logger.info(f"[Mux Service] ✅ Audio track '{name}' attached successfully.")
    return response.json()["data"]

def add_public_playback_id(asset_id: str):
    """Adds a public playback ID to an existing Mux asset."""
    response = requests.post(
        f"{BASE_URL}/assets/{asset_id}/playback-ids",
        json={"policy": "public"},
        auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET)
    )
    if not response.ok:
        raise Exception(f"Mux API Error ({response.status_code}): {response.text}")
    return response.json()["data"]["id"]

def add_signed_playback_id(asset_id: str):
    """Adds a DRM playback ID if DRM is configured, falling back to signed on failure."""
    if DRM_CONFIGURATION_ID:
        drm_body = {"policy": "drm", "drm_configuration_id": DRM_CONFIGURATION_ID}
        response = requests.post(
            f"{BASE_URL}/assets/{asset_id}/playback-ids",
            json=drm_body,
            auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET)
        )
        if response.ok:
            return response.json()["data"]["id"], "drm"
        # DRM rejected (account not onboarded or invalid config) — fall through to signed
        logger.warning(f"[Mux Service] DRM playback ID failed for {asset_id} ({response.status_code}), falling back to signed: {response.text}")

    # Plain signed fallback
    response = requests.post(
        f"{BASE_URL}/assets/{asset_id}/playback-ids",
        json={"policy": "signed"},
        auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET)
    )
    if not response.ok:
        raise Exception(f"Mux API Error ({response.status_code}): {response.text}")
    return response.json()["data"]["id"], "signed"

def generate_drm_license_token(playback_id: str, expiration_hours: int = 6) -> str:
    """
    Generates a Mux DRM license token (aud='l').
    Required by the player alongside the stream URL when DRM is active.
    The player sends this to Mux's license server to obtain decryption keys.
    """
    decoded_private_key = base64.b64decode(MUX_PRIVATE_KEY)
    expiration_time = int(time.time()) + (expiration_hours * 3600)
    payload = {
        "sub": playback_id,
        "aud": "l",
        "exp": expiration_time,
        "kid": MUX_SIGNING_KEY_ID,
    }
    return jwt.encode(payload, decoded_private_key, algorithm="RS256")


def delete_playback_id(asset_id: str, playback_id: str):
    """Removes a specific playback ID from a Mux asset."""
    response = requests.delete(
        f"{BASE_URL}/assets/{asset_id}/playback-ids/{playback_id}",
        auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET)
    )
    if not response.ok:
        raise Exception(f"Mux API Error ({response.status_code}): {response.text}")
    logger.info(f"[Mux Service] ✅ Deleted playback ID {playback_id} from asset {asset_id}")


def generate_offline_license_token(playback_id: str, expiration_hours: int = 48) -> str:
    """
    Generates a persistent DRM license token for offline playback (aud='l', drm_offline=true).
    The mobile app presents this to Mux's license server once to obtain a persistent
    Widevine (Android) or FairPlay (iOS) license that allows offline decryption.
    Longer expiry (default 48h) gives the app time to complete the download and license fetch.
    """
    decoded_private_key = base64.b64decode(MUX_PRIVATE_KEY)
    expiration_time = int(time.time()) + (expiration_hours * 3600)
    payload = {
        "sub": playback_id,
        "aud": "l",
        "exp": expiration_time,
        "kid": MUX_SIGNING_KEY_ID,
        "drm_offline": True,  # signals Mux to issue a persistent (offline-capable) license
    }
    return jwt.encode(payload, decoded_private_key, algorithm="RS256")


def generate_download_token(signed_playback_id: str, expiration_hours: int = 1):
    """
    Generates a short-lived signed JWT for downloading the static MP4 rendition.
    aud='d' targets Mux static renditions (the downloadable MP4).
    Only our backend can issue this — nobody else can access the file.
    """
    decoded_private_key = base64.b64decode(MUX_PRIVATE_KEY)
    expiration_time = int(time.time()) + (expiration_hours * 3600)

    payload = {
        "sub": signed_playback_id,
        "aud": "d",           # "d" = static renditions / download (not streaming)
        "exp": expiration_time,
        "kid": MUX_SIGNING_KEY_ID
    }

    token = jwt.encode(payload, decoded_private_key, algorithm="RS256")
    return token

def generate_playback_token(playback_id: str, expiration_hours: int = 6):
    """Generates a secure, expiring JWT token for Mux playback."""
    decoded_private_key = base64.b64decode(MUX_PRIVATE_KEY)
    expiration_time = int(time.time()) + (expiration_hours * 3600)
    
    payload = {
        "sub": playback_id,
        "aud": "v",           
        "exp": expiration_time,
        "kid": MUX_SIGNING_KEY_ID
    }
    
    token = jwt.encode(payload, decoded_private_key, algorithm="RS256")
    return token
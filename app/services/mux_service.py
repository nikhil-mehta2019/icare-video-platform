import requests
import logging
import json
import base64
import jwt
import time
from app.config import MUX_TOKEN_ID, MUX_TOKEN_SECRET,MUX_PRIVATE_KEY

BASE_URL = "https://api.mux.com/video/v1"
logger = logging.getLogger(__name__)

def upload_video(video_url, title="Untitled", captions=None, audio_tracks=None):
    logger.info(f"[Mux Service] Starting upload_video for title: '{title}'")
    
    inputs = [{"url": video_url}]
    logger.info(f"[Mux Service] Base video input added: {video_url}")
    
    if captions:
        logger.info(f"[Mux Service] Processing {len(captions)} caption tracks...")
        for cap in captions:
            lang = cap.get("language") or "en"
            name = cap.get("name") or "English"
            inputs.append({
                "url": cap["url"],
                "type": "text",
                "text_type": "subtitles",
                "language_code": lang,
                "name": name
            })
            logger.info(f"[Mux Service] Added caption input: {name} ({lang})")
            
    if audio_tracks:
        logger.info(f"[Mux Service] Processing {len(audio_tracks)} audio tracks...")
        for aud in audio_tracks:
            lang = aud.get("language") or "en"
            name = aud.get("name") or "Alternate Audio"
            inputs.append({
                "url": aud["url"],
                "type": "audio",
                "language_code": lang,
                "name": name
            })
            logger.info(f"[Mux Service] Added audio input: {name} ({lang})")

    safe_title = title[:250] if title else "Untitled"
    payload = {
        "input": inputs,
        "playback_policy": ["public"],
        "meta": {
            "title": safe_title
        }
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

    return {
        "asset_id": data["id"],
        "playback_id": data["playback_ids"][0]["id"]
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


def generate_playback_token(playback_id: str, expiration_hours: int = 6):
    """Generates a secure, expiring JWT token for Mux playback."""
    # Decode the base64 private key provided by Mux
    decoded_private_key = base64.b64decode(MUX_PRIVATE_KEY)
    
    # Set expiration time
    expiration_time = int(time.time()) + (expiration_hours * 3600)
    
    payload = {
        "sub": playback_id,
        "aud": "v",           
        "exp": expiration_time,
        "kid": MUX_SIGNING_KEY_ID
    }
    
    # Sign it using RS256 encryption
    token = jwt.encode(payload, decoded_private_key, algorithm="RS256")
    return token
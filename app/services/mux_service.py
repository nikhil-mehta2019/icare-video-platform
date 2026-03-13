import requests
from app.config import MUX_TOKEN_ID, MUX_TOKEN_SECRET

BASE_URL = "https://api.mux.com/video/v1"

def upload_video(video_url):
    payload = {
        "input": video_url,
        "playback_policy": ["public"]
    }

    response = requests.post(
        f"{BASE_URL}/assets",
        json=payload,
        auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET)
    )
    
    response.raise_for_status()
    data = response.json()["data"]

    return {
        "asset_id": data["id"],
        "playback_id": data["playback_ids"][0]["id"]
    }

def get_asset(asset_id: str):
    response = requests.get(
        f"{BASE_URL}/assets/{asset_id}",
        auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET)
    )

    response.raise_for_status()
    return response.json()["data"]
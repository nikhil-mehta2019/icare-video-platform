import requests
import logging
from app.config import VIMEO_ACCESS_TOKEN

logger = logging.getLogger(__name__)

def get_vimeo_videos(limit=None):
    logger.info("[Vimeo Service] Starting to fetch videos from Vimeo API...")
    videos = []
    url = "https://api.vimeo.com/me/videos?per_page=50"

    headers = {
        "Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}"
    }

    page_count = 1
    while url:
        logger.info(f"[Vimeo Service] Requesting Vimeo Library Page {page_count}...")
        response = requests.get(url, headers=headers, timeout=(5,60))

        if response.status_code != 200:
            logger.error(f"[Vimeo Service] ❌ Vimeo API error: {response.text}")
            raise Exception(f"Vimeo API error: {response.text}")

        data = response.json()
        page_videos = data.get("data", [])

        videos.extend(page_videos)
        logger.info(f"[Vimeo Service] Found {len(page_videos)} videos on Page {page_count}. (Total so far: {len(videos)})")

        # STOP early if limit reached (only used if we explicitly pass a limit)
        if limit and len(videos) >= limit:
            logger.info(f"[Vimeo Service] Limit of {limit} reached. Stopping fetch.")
            return videos[:limit]

        next_page = data.get("paging", {}).get("next")

        if next_page:
            url = f"https://api.vimeo.com{next_page}"
            page_count += 1
        else:
            url = None

    logger.info(f"[Vimeo Service] ✅ Finished fetching all {len(videos)} videos from Vimeo.")
    return videos

def get_video_download_url(vimeo_id):
    url = f"https://api.vimeo.com/videos/{vimeo_id}"
    headers = {
        "Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}"
    }

    response = requests.get(url, headers=headers, timeout=(5,60))

    if response.status_code != 200:
        raise Exception(f"Vimeo API error: {response.text}")

    video_data = response.json()
    files = video_data.get("files", [])

    if not files:
        raise Exception("No downloadable video files found")

    # Sort files by height to get the highest quality
    files_sorted = sorted(files, key=lambda x: x.get("height", 0), reverse=True)

    return files_sorted[0]["link"]

def extract_folder_path(video_data):
    """Extracts and formats the folder hierarchy from Vimeo metadata."""
    folder_path = "Root"
    
    folders = video_data.get("folders", {}).get("data", [])
    if folders:
        folder_names = [f.get("name") for f in folders]
        folder_path = " / ".join(reversed(folder_names))
    elif video_data.get("parent_folder"):
        parent = video_data.get("parent_folder")
        if isinstance(parent, dict):
            folder_path = parent.get("name", "Root")
            
    return folder_path

def get_video_captions(vimeo_id):
    """Fetches all text tracks (captions/subtitles) for a specific Vimeo video."""
    url = f"https://api.vimeo.com/videos/{vimeo_id}/texttracks"
    headers = {
        "Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}"
    }

    try:
        response = requests.get(url, headers=headers, timeout=(5, 60))
        if response.status_code != 200:
            logger.warning(f"[Vimeo Service] Failed to fetch captions for Vimeo ID {vimeo_id}: HTTP {response.status_code}")
            return []
            
        data = response.json()
        tracks = data.get("data", [])
        
        captions = []
        for track in tracks:
            if track.get("link") and track.get("type") in ["captions", "subtitles"]:
                captions.append({
                    "url": track["link"],
                    "language": track.get("language"), 
                    "name": track.get("name")          
                })
        return captions
    except Exception as e:
        logger.error(f"[Vimeo Service] Error fetching captions for {vimeo_id}: {str(e)}")
        return []

def get_video_audio_tracks(vimeo_id):
    """Fetches alternate (dubbed) audio tracks by parsing Vimeo's HLS manifest."""
    import re
    from urllib.parse import urljoin

    headers = {"Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}"}

    try:
        # Step 1: Get the HLS manifest URL from the files list
        r = requests.get(
            f"https://api.vimeo.com/videos/{vimeo_id}?fields=files",
            headers=headers, timeout=(5, 60)
        )
        if r.status_code != 200:
            return []

        files = r.json().get("files", [])
        hls_file = next((f for f in files if f.get("rendition") == "adaptive"), None)
        if not hls_file or not hls_file.get("link"):
            return []

        # Step 2: Fetch manifest, following redirects to get the final base URL
        manifest_r = requests.get(hls_file["link"], allow_redirects=True, timeout=(5, 60))
        if manifest_r.status_code != 200:
            return []

        base_url = manifest_r.url
        manifest = manifest_r.text

        # Step 3: Parse EXT-X-MEDIA:TYPE=AUDIO lines, skip DEFAULT (already in video file)
        audio_tracks = []
        for line in manifest.splitlines():
            if not line.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
                continue
            if "DEFAULT=YES" in line:
                continue  # Original audio is already embedded in the video stream

            name = re.search(r'NAME="([^"]*)"', line)
            language = re.search(r'LANGUAGE="([^"]*)"', line)
            uri = re.search(r'URI="([^"]*)"', line)

            if not uri:
                continue

            absolute_uri = urljoin(base_url, uri.group(1))
            audio_tracks.append({
                "url": absolute_uri,
                "language": language.group(1) if language else "en",
                "name": name.group(1) if name else "Audio"
            })

        logger.info(f"[Vimeo Service] Found {len(audio_tracks)} alternate audio track(s) for {vimeo_id}")
        return audio_tracks

    except Exception as e:
        logger.warning(f"[Vimeo Service] Error fetching audio tracks for {vimeo_id}: {str(e)}")
        return []

# def get_vimeo_page(url=None):
#     """Fetches exactly one page of videos from Vimeo and returns the next page URL."""
#     if not url:
#         # Default start URL (50 videos per page)
#         url = "https://api.vimeo.com/me/videos?per_page=50"

#     headers = {
#         "Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}"
#     }

#     logger.info(f"[Vimeo Service] Requesting Vimeo API: {url.split('.com')[-1]}")
#     response = requests.get(url, headers=headers, timeout=(5,60))

#     if response.status_code != 200:
#         logger.error(f"[Vimeo Service] ❌ Vimeo API error: {response.text}")
#         raise Exception(f"Vimeo API error: {response.text}")

#     data = response.json()
#     page_videos = data.get("data", [])

#     # Get the URL for the next page (if it exists)
#     next_page = data.get("paging", {}).get("next")
#     next_url = f"https://api.vimeo.com{next_page}" if next_page else None

#     return page_videos, next_url

def get_vimeo_page(url=None, custom_start_url=None):
    """Fetches exactly one page of videos from Vimeo and returns the next page URL."""
    if not url:
        # Use custom start URL if provided, otherwise default to the whole library
        url = custom_start_url if custom_start_url else "https://api.vimeo.com/me/videos?per_page=50"

    headers = {
        "Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}"
    }

    logger.info(f"[Vimeo Service] Requesting Vimeo API: {url.split('.com')[-1]}")
    response = requests.get(url, headers=headers, timeout=(5,60))

    if response.status_code != 200:
        logger.error(f"[Vimeo Service] ❌ Vimeo API error: {response.text}")
        raise Exception(f"Vimeo API error: {response.text}")

    data = response.json()
    page_videos = data.get("data", [])

    # Get the URL for the next page (if it exists)
    next_page = data.get("paging", {}).get("next")
    next_url = f"https://api.vimeo.com{next_page}" if next_page else None

    return page_videos, next_url

def get_vimeo_folder_videos(folder_id: str):
    """Specifically fetches videos from a Vimeo project/folder ID."""
    url = f"https://api.vimeo.com/me/projects/{folder_id}/videos?per_page=100"
    headers = {"Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}"}
    response = requests.get(url, headers=headers, timeout=(5, 60))

    if response.status_code != 200:
        logger.error(f"Vimeo API Error: {response.text}")
        raise Exception(f"Failed to fetch folder: {response.status_code}")

    return response.json().get("data", [])
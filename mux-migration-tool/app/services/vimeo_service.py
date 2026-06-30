import time
import requests
import logging
from app.config import VIMEO_ACCESS_TOKEN

logger = logging.getLogger(__name__)
HEADERS = {"Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}"}


def get_vimeo_folder_videos(folder_id: str) -> list:
    """
    Recursively fetches all videos from a Vimeo folder and its sub-folders.
    Returns list of dicts: [{"video": {...}, "folder_name": str}, ...]
    """
    all_videos = []

    def _fetch_folder(fid: str, folder_name: str):
        url = f"https://api.vimeo.com/me/projects/{fid}/items?per_page=100"
        while url:
            logger.info(f"[Vimeo] Fetching folder '{folder_name}': {url.split('.com')[-1]}")
            for attempt in range(1, 5):
                r = requests.get(url, headers=HEADERS, timeout=(5, 90))
                if r.status_code == 200:
                    break
                wait = 2 ** attempt
                logger.warning(f"[Vimeo] HTTP {r.status_code} on folder '{folder_name}' (attempt {attempt}/4), retry in {wait}s")
                time.sleep(wait)
            else:
                raise Exception(f"Failed to fetch Vimeo folder {fid}: HTTP {r.status_code} — {r.text[:200]}")

            data = r.json()
            for item in data.get("data", []):
                if item.get("type") == "video":
                    all_videos.append({"video": item["video"], "folder_name": folder_name})
                elif item.get("type") == "folder":
                    sub_id = item["folder"]["uri"].split("/projects/")[-1]
                    sub_name = item["folder"].get("name", sub_id)
                    logger.info(f"[Vimeo] Found sub-folder '{sub_name}', recursing...")
                    _fetch_folder(sub_id, sub_name)

            next_page = data.get("paging", {}).get("next")
            url = f"https://api.vimeo.com{next_page}" if next_page else None

    _fetch_folder(folder_id, "Root")
    logger.info(f"[Vimeo] Folder {folder_id}: {len(all_videos)} total videos across all sub-folders.")
    return all_videos


def get_video_download_url(vimeo_id: str) -> str:
    """Returns the highest-quality direct download URL for a Vimeo video."""
    r = requests.get(f"https://api.vimeo.com/videos/{vimeo_id}", headers=HEADERS, timeout=(5, 60))
    if r.status_code != 200:
        raise Exception(f"Vimeo API error fetching video {vimeo_id}: {r.text}")
    files = r.json().get("files", [])
    if not files:
        raise Exception(f"No downloadable files found for Vimeo ID {vimeo_id}")
    return sorted(files, key=lambda x: x.get("height", 0), reverse=True)[0]["link"]


def get_video_captions(vimeo_id: str) -> list:
    """
    Fetches all text tracks for a Vimeo video.
    Deduplicates by language — prefers human captions over auto-generated.
    """
    r = requests.get(f"https://api.vimeo.com/videos/{vimeo_id}/texttracks", headers=HEADERS, timeout=(5, 60))
    if r.status_code != 200:
        logger.warning(f"[Vimeo] Could not fetch captions for {vimeo_id}: HTTP {r.status_code}")
        return []

    seen = {}
    for track in r.json().get("data", []):
        if not track.get("link") or track.get("type") not in ["captions", "subtitles"]:
            continue
        lang = track.get("language") or "unknown"
        is_autogen = "autogen" in lang.lower() or "autogen" in (track.get("name") or "").lower()
        if lang not in seen or (seen[lang]["autogen"] and not is_autogen):
            seen[lang] = {"url": track["link"], "language": lang, "name": track.get("name"), "autogen": is_autogen}

    captions = [{"url": v["url"], "language": v["language"], "name": v["name"]} for v in seen.values()]
    logger.info(f"[Vimeo] {len(captions)} caption track(s) for {vimeo_id}: {[c['language'] for c in captions]}")
    return captions


def get_video_audio_tracks(vimeo_id: str) -> list:
    """
    Discovers alternate (dubbed) audio tracks by parsing Vimeo's HLS manifest.
    Skips the default audio (already embedded in the video stream).
    """
    import re
    from urllib.parse import urljoin

    r = requests.get(
        f"https://api.vimeo.com/videos/{vimeo_id}?fields=files",
        headers=HEADERS, timeout=(5, 60)
    )
    if r.status_code != 200:
        return []

    hls_file = next((f for f in r.json().get("files", []) if f.get("rendition") == "adaptive"), None)
    if not hls_file or not hls_file.get("link"):
        return []

    manifest_r = requests.get(hls_file["link"], allow_redirects=True, timeout=(5, 60))
    if manifest_r.status_code != 200:
        return []

    base_url = manifest_r.url
    audio_tracks = []
    for line in manifest_r.text.splitlines():
        if not line.startswith("#EXT-X-MEDIA:TYPE=AUDIO") or "DEFAULT=YES" in line:
            continue
        name = re.search(r'NAME="([^"]*)"', line)
        language = re.search(r'LANGUAGE="([^"]*)"', line)
        uri = re.search(r'URI="([^"]*)"', line)
        if not uri:
            continue
        audio_tracks.append({
            "url": urljoin(base_url, uri.group(1)),
            "language": language.group(1) if language else "en",
            "name": name.group(1) if name else "Audio",
        })

    logger.info(f"[Vimeo] {len(audio_tracks)} alternate audio track(s) for {vimeo_id}")
    return audio_tracks

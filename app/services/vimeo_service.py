import requests
from app.config import VIMEO_ACCESS_TOKEN

def get_vimeo_videos():
    """Fetches all videos from Vimeo account using pagination."""
    videos = []
    url = "https://api.vimeo.com/me/videos"
    headers = {
        "Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}"
    }

    while url:
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            raise Exception(f"Vimeo API error: {response.text}")

        data = response.json()
        videos.extend(data.get("data", []))
        
        # Check for the next page in pagination
        paging = data.get("paging", {})
        next_page = paging.get("next")
        
        if next_page:
            url = f"https://api.vimeo.com{next_page}"
        else:
            url = None

    return videos

def get_video_download_url(vimeo_id):
    url = f"https://api.vimeo.com/videos/{vimeo_id}"
    headers = {
        "Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}"
    }

    response = requests.get(url, headers=headers)

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
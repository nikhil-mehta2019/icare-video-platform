import requests
from app.config import VIMEO_ACCESS_TOKEN


def get_vimeo_videos():

    url = "https://api.vimeo.com/me/videos"

    headers = {
        "Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}"
    }

    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        raise Exception(f"Vimeo API error: {response.text}")

    data = response.json()

    return data.get("data", [])


def get_video_download_url(video_uri):

    video_id = video_uri.split("/")[-1]

    url = f"https://api.vimeo.com/videos/{video_id}"

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

    files_sorted = sorted(files, key=lambda x: x.get("height", 0), reverse=True)

    return files_sorted[0]["link"]
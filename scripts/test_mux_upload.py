import requests
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN_ID = os.getenv("MUX_TOKEN_ID")
TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")

url = "https://api.mux.com/video/v1/assets"

payload = {
    "input": "https://storage.googleapis.com/muxdemofiles/mux-video-intro.mp4",
    "playback_policy": ["public"]
}

response = requests.post(
    url,
    json=payload,
    auth=(TOKEN_ID, TOKEN_SECRET)
)

print(response.json())
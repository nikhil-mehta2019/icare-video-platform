import requests
import os
from dotenv import load_dotenv

load_dotenv()

MUX_TOKEN_ID = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")

url = "https://api.mux.com/video/v1/assets"

response = requests.get(
    url,
    auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET)
)

print("Status Code:", response.status_code)
print("Response:", response.text)
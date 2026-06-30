"""Quick diagnostic — tests various Mux API endpoints to find what works."""
import sys, os
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()
import requests
from app.database.session import SessionLocal
from app.database.models import Video
from datetime import datetime

AUTH     = (os.getenv("MUX_TOKEN_ID"), os.getenv("MUX_TOKEN_SECRET"))
MUX_BASE = "https://api.mux.com/video/v1"
CUTOFF   = datetime(2026, 6, 1)

print(f"MUX_TOKEN_ID loaded: {'yes' if AUTH[0] else 'NO - MISSING'}")
print(f"MUX_TOKEN_SECRET loaded: {'yes' if AUTH[1] else 'NO - MISSING'}")
print(f"Token ID prefix: {(AUTH[0] or '')[:8]}...")

known_id = "KIsJ35us00Be29uv9mO02wDotjOfACgWT1ZFapXmRyKUw"

r = requests.get(f"{MUX_BASE}/assets/{known_id}", auth=AUTH, timeout=15)
tracks = r.json().get("data", {}).get("tracks", [])
print(f"\nTracks via GET /assets/{known_id} ({len(tracks)} found):")
for t in tracks:
    print(f"  type={t.get('type')} lang={t.get('language_code')} name={t.get('name')} status={t.get('status')} id={t.get('id')}")

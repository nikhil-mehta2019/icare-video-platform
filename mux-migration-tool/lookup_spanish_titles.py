"""
lookup_spanish_titles.py
Finds DB IDs for the 7 Spanish titles that didn't auto-match.
Run this, then paste output back to get manual overrides added.
"""
import sys, re
sys.path.insert(0, __import__('os').path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()
from app.database.session import SessionLocal
from app.database.models import Video
from datetime import datetime

CUTOFF = datetime(2026, 6, 1)

SEARCH = [
    "age related changes",
    "avoiding potential conflicts",
    "creating a structured schedule",
    "first day of duty",
    "planning mealtimes",
    "prepare for the day",
    "urinogenital",
]

def normalize(s):
    s = re.sub(r"[''_]", "", s.lower())
    return re.sub(r'\s+', ' ', s).strip()

with SessionLocal() as db:
    videos = db.query(Video).filter(Video.created_at > CUTOFF).all()

# Only show videos whose title contains "new" (case-insensitive)
for keyword in SEARCH:
    nkw = normalize(keyword)
    matches = [v for v in videos
               if nkw in normalize(v.vimeo_title or "")
               and "new" in (v.vimeo_title or "").lower()]
    print(f"\n[{keyword}]")
    if matches:
        for v in matches:
            print(f"  id={v.id}  '{v.vimeo_title}'")
    else:
        print("  -- no 'New' variant found --")

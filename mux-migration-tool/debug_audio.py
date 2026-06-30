import sys, os, requests, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.config import VIMEO_ACCESS_TOKEN

HEADERS = {"Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}"}
vimeo_id = sys.argv[1] if len(sys.argv) > 1 else "1171854126"

print(f"\n=== Checking Vimeo {vimeo_id} ===\n")

# Step 1: Get files
r = requests.get(f"https://api.vimeo.com/videos/{vimeo_id}?fields=files", headers=HEADERS)
files = r.json().get("files", [])
print(f"Files returned: {len(files)}")
for f in files:
    print(f"  rendition={f.get('rendition')} quality={f.get('quality')} type={f.get('type')} link={str(f.get('link',''))[:80]}")

# Step 2: Get adaptive/HLS file
hls_file = next((f for f in files if f.get("rendition") == "adaptive"), None)
if not hls_file:
    print("\nNo adaptive/HLS file found!")
    sys.exit(1)

print(f"\nHLS URL: {hls_file['link'][:100]}")

# Step 3: Fetch manifest
mr = requests.get(hls_file["link"], allow_redirects=True)
print(f"\nManifest status: {mr.status_code}")
print(f"Final URL: {mr.url[:100]}")
print(f"\n--- Manifest (first 3000 chars) ---")
print(mr.text[:3000])

# Step 4: Show all EXT-X-MEDIA lines
print("\n--- All #EXT-X-MEDIA lines ---")
for line in mr.text.splitlines():
    if line.startswith("#EXT-X-MEDIA"):
        print(line)

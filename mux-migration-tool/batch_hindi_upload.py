"""
batch_hindi_upload.py — Two-phase batch uploader for Hindi videos to Mux.

Phase 1  : Upload all MP4 videos → save asset_id + title to state file.
Phase 2  : For each ready asset, attach SRT subtitles and Hindi VO audio track.

Usage:
    python batch_hindi_upload.py --preview         # Show matched files, no uploads
    python batch_hindi_upload.py --phase1          # Upload videos only
    python batch_hindi_upload.py --phase2          # Attach SRT + audio to ready assets
    python batch_hindi_upload.py --all             # Run both phases sequentially
    python batch_hindi_upload.py --report          # Export Excel summary
    python batch_hindi_upload.py --phase1 --limit 5   # Upload first 5 only (for testing)

Requirements:
    pip install requests pandas openpyxl python-dotenv
    FastAPI server (run.py) must be running for Phase 2 (serves temp files to Mux).
"""

import os, sys, re, json, time, shutil, argparse, logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — edit paths if needed
# ─────────────────────────────────────────────────────────────────────────────
VIDEO_DIR = r"D:\new uploads\New folder\03-06-2026"
SRT_DIR   = r"D:\new uploads\New folder\SRT Hindi (160)"
VO_DIR    = r"D:\new uploads\New folder\VO Hindi Final Output"

# Temp folder for SRT/audio served by the FastAPI app (/temp/<file>)
BASE_DIR  = Path(__file__).parent
TEMP_DIR  = BASE_DIR / "temp"
LOGS_DIR  = BASE_DIR / "logs"
STATE_FILE = LOGS_DIR / "hindi_upload_state.json"

# Mux credentials (loaded from .env)
MUX_TOKEN_ID      = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET  = os.getenv("MUX_TOKEN_SECRET")
DRM_CONFIG_ID     = os.getenv("DRM_CONFIGURATION_ID")
SERVER_BASE_URL   = os.getenv("SERVER_BASE_URL", "http://localhost:8000").rstrip("/")

MUX_BASE = "https://api.mux.com/video/v1"
AUTH     = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)

# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# FILE MATCHING
# ─────────────────────────────────────────────────────────────────────────────

def _norm(name: str) -> str:
    """
    Strips known suffixes and normalises for matching:
      - Collapses whitespace
      - Strips version tags like V4, V3
      - Normalises & → and (handles 'Communication & ...' vs 'Communication and ...')
      - Strips suffixes: Hindi SRT, VO Hindi, Hindi VO, Hindi, VO
    """
    name = os.path.splitext(name)[0]
    name = re.sub(r'\s+', ' ', name).strip()
    # Strip version numbers at end (e.g. " V4", " V2")
    name = re.sub(r'\s+V\d+$', '', name).strip()
    # Strip known suffixes — longest first
    for suffix in [" Hindi SRT", " SRT Hindi", " VO  Hindi", " VO Hindi",
                   " Hindi VO", " Hindi", " VO"]:
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
            break
    # Normalise & → and  (handles "Communication & Listening" vs "Communication and Listening")
    name = name.replace(" & ", " and ")
    return name.strip(" -_")


def _fuzzy_match(key: str, candidates: dict, threshold: float = 0.82) -> str | None:
    """
    Fallback: finds the best fuzzy match in candidates for key.
    Returns the best matching candidate key, or None if below threshold.
    """
    from difflib import SequenceMatcher
    best_key, best_score = None, 0.0
    for candidate in candidates:
        score = SequenceMatcher(None, key, candidate).ratio()
        if score > best_score:
            best_score, best_key = score, candidate
    return best_key if best_score >= threshold else None


def build_file_index(directory: str, extensions: tuple) -> dict:
    """Returns {normalised_name: full_path} for all matching files in directory."""
    index = {}
    for fname in os.listdir(directory):
        if fname.lower().endswith(extensions):
            key = _norm(fname).lower()
            index[key] = os.path.join(directory, fname)
    return index


def match_files() -> list[dict]:
    """
    Matches videos → SRT → VO by normalised base name.
    Falls back to fuzzy matching for typo/& vs and mismatches.
    Returns list of dicts with keys: base, title, video, srt, vo.
    """
    videos = build_file_index(VIDEO_DIR, (".mp4", ".mov", ".mkv"))
    srts   = build_file_index(SRT_DIR, (".srt", ".vtt"))
    vos    = build_file_index(VO_DIR, (".mp3", ".m4a", ".aac", ".wav"))

    results = []
    for key, video_path in sorted(videos.items()):
        title = os.path.splitext(os.path.basename(video_path))[0]

        # Exact match first, then fuzzy fallback
        srt_path = srts.get(key) or (srts.get(fk) if (fk := _fuzzy_match(key, srts)) else None)
        vo_path  = vos.get(key)  or (vos.get(fk)  if (fk := _fuzzy_match(key, vos))  else None)

        results.append({
            "base":  key,
            "title": title,
            "video": video_path,
            "srt":   srt_path,
            "vo":    vo_path,
        })
    return results


# ─────────────────────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    LOGS_DIR.mkdir(exist_ok=True)
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    LOGS_DIR.mkdir(exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# MUX API HELPERS
# ─────────────────────────────────────────────────────────────────────────────

import requests

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

sys.path.insert(0, str(BASE_DIR))
from app.database.session import SessionLocal
from app.database.models import Video


def db_save_video(entry: dict, asset_id: str):
    """Creates a Video record in DB when upload starts (status=processing)."""
    db_id = f"manual_hindi_{entry['base']}"
    with SessionLocal() as db:
        existing = db.query(Video).filter(Video.vimeo_id == db_id).first()
        if existing:
            log.info(f"  DB record already exists: {db_id}")
            entry["db_id"] = db_id
            return
        video = Video(
            vimeo_id=db_id,
            vimeo_title=entry["title"],
            display_title=entry["title"],
            vimeo_url=f"mux:{asset_id}",   # SQL Server requires NOT NULL; store asset ref
            vimeo_folder_path="Hindi Manual Upload",
            source="manual",
            mux_asset_id=asset_id,
            status="processing",
        )
        db.add(video)
        db.commit()
        entry["db_id"] = db_id
        log.info(f"  ✅ DB record saved: {db_id}")


def db_update_ready(entry: dict, playback_id: str = None, drm_id: str = None,
                    srt_lang: str = None, audio_lang: str = None):
    """Updates Video record to ready after Phase 2 completes."""
    db_id = entry.get("db_id") or f"manual_hindi_{entry['base']}"
    with SessionLocal() as db:
        v = db.query(Video).filter(Video.vimeo_id == db_id).first()
        if not v:
            log.warning(f"  DB record not found for update: {db_id}")
            return
        v.status = "ready"
        if playback_id:
            v.mux_playback_id = playback_id
            v.mux_stream_url  = f"https://stream.mux.com/{playback_id}.m3u8"
        if drm_id:
            v.mux_drm_playback_id = drm_id
        if srt_lang:
            v.captions_count    = 1
            v.captions_languages = srt_lang
        if audio_lang:
            v.audio_tracks_count = 2   # default + hindi
            v.audio_languages    = audio_lang
        db.commit()
        log.info(f"  ✅ DB updated to ready: {db_id}")


def mux_create_upload(title: str) -> dict:
    """Creates a Mux Direct Upload slot. Returns {upload_id, upload_url}."""
    if DRM_CONFIG_ID:
        asset_settings = {
            "advanced_playback_policies": [{"policy": "drm", "drm_configuration_id": DRM_CONFIG_ID}],
            "video_quality": "premium",
            "meta": {"title": title[:512]},
        }
    else:
        asset_settings = {
            "playback_policy": ["public"],
            "video_quality": "premium",
            "meta": {"title": title[:512]},
        }
    r = requests.post(f"{MUX_BASE}/uploads",
                      json={"new_asset_settings": asset_settings, "cors_origin": "*"},
                      auth=AUTH, timeout=30)
    r.raise_for_status()
    d = r.json()["data"]
    return {"upload_id": d["id"], "upload_url": d["url"]}


def mux_push_file(upload_url: str, file_path: str):
    """Streams a local file to Mux upload URL."""
    size = os.path.getsize(file_path)
    log.info(f"  Uploading {size / 1_000_000:.1f} MB → Mux ...")
    with open(file_path, "rb") as f:
        r = requests.put(upload_url, data=f,
                         headers={"Content-Type": "video/*", "Content-Length": str(size)},
                         timeout=(30, 7200))
    r.raise_for_status()


def mux_poll_asset_id(upload_id: str, timeout: int = 300) -> str:
    """Polls until asset_id appears on the upload record."""
    for _ in range(timeout // 5):
        r = requests.get(f"{MUX_BASE}/uploads/{upload_id}", auth=AUTH, timeout=15)
        if r.ok:
            asset_id = r.json()["data"].get("asset_id")
            if asset_id:
                return asset_id
        time.sleep(5)
    raise TimeoutError(f"asset_id not available after {timeout}s for upload {upload_id}")


def mux_asset_status(asset_id: str) -> tuple[str, dict]:
    """Returns (status, asset_data). status: 'preparing'|'ready'|'errored'."""
    r = requests.get(f"{MUX_BASE}/assets/{asset_id}", auth=AUTH, timeout=15)
    r.raise_for_status()
    data = r.json()["data"]
    return data.get("status", "preparing"), data


def mux_wait_ready(asset_id: str, timeout: int = 1200) -> dict:
    """Polls until asset is ready. Max wait 20 min."""
    log.info(f"  Waiting for asset {asset_id} to be ready ...")
    for elapsed in range(0, timeout, 15):
        status, data = mux_asset_status(asset_id)
        if status == "ready":
            log.info(f"  ✅ Asset ready ({elapsed}s)")
            return data
        if status == "errored":
            raise RuntimeError(f"Mux asset {asset_id} errored.")
        time.sleep(15)
    raise TimeoutError(f"Asset {asset_id} not ready after {timeout}s")


def mux_add_text_track(asset_id: str, url: str, language: str, name: str):
    r = requests.post(f"{MUX_BASE}/assets/{asset_id}/tracks",
                      json={"url": url, "type": "text", "text_type": "subtitles",
                            "language_code": language, "name": name},
                      auth=AUTH, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Text track attach failed ({r.status_code}): {r.text[:200]}")


def mux_add_audio_track(asset_id: str, url: str, language: str, name: str):
    r = requests.post(f"{MUX_BASE}/assets/{asset_id}/tracks",
                      json={"url": url, "type": "audio", "language_code": language, "name": name},
                      auth=AUTH, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Audio track attach failed ({r.status_code}): {r.text[:200]}")


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 — Upload videos
# ─────────────────────────────────────────────────────────────────────────────

def phase1(matches: list[dict], limit: int = None, state: dict = None) -> dict:
    state = state or load_state()

    # Ensure every existing entry has its "base" key populated (fixes state from old bug)
    for m in matches:
        if m["base"] in state:
            state[m["base"]].setdefault("base", m["base"])

    # Recover entries that uploaded to Mux successfully but failed DB save
    to_recover = [m for m in matches
                  if state.get(m["base"], {}).get("asset_id")
                  and state.get(m["base"], {}).get("upload_status") == "errored"]
    if to_recover:
        log.info(f"\n  Recovering DB save for {len(to_recover)} previously-uploaded assets ...")
        for m in to_recover:
            base  = m["base"]
            entry = state[base]
            entry["base"] = base
            log.info(f"  Recovering: {entry['title']} | {entry['asset_id']}")
            try:
                db_save_video(entry, entry["asset_id"])
                entry["upload_status"] = "processing"
                entry.pop("error", None)
                save_state(state)
            except Exception as e:
                log.error(f"  ❌ DB recover failed: {e}")

    # Only upload entries that have no asset_id yet (never reached Mux)
    to_upload = [m for m in matches
                 if not state.get(m["base"], {}).get("asset_id")
                 and state.get(m["base"], {}).get("upload_status") not in ("uploading", "processing", "ready")]
    if limit:
        to_upload = to_upload[:limit]

    log.info(f"\n{'═'*60}")
    log.info(f" PHASE 1 — Uploading {len(to_upload)} videos (of {len(matches)} total)")
    log.info(f"{'═'*60}")

    for i, m in enumerate(to_upload, 1):
        base  = m["base"]
        title = m["title"]
        log.info(f"\n[{i}/{len(to_upload)}] {title}")

        entry = state.setdefault(base, {
            "base": base,                        # always store base key in entry
            "title": title, "video": m["video"],
            "srt": m["srt"], "vo": m["vo"],
            "upload_id": None, "asset_id": None,
            "playback_id": None, "drm_playback_id": None,
            "upload_status": "pending",
            "srt_attached": False, "audio_attached": False,
        })
        entry["base"] = base  # ensure even setdefault hits have it

        try:
            # Create upload slot
            up = mux_create_upload(title)
            entry["upload_id"] = up["upload_id"]
            entry["upload_status"] = "uploading"
            save_state(state)

            # Stream file to Mux
            mux_push_file(up["upload_url"], m["video"])
            log.info(f"  File pushed. Polling for asset_id ...")

            # Get asset_id
            asset_id = mux_poll_asset_id(up["upload_id"])
            entry["asset_id"] = asset_id
            entry["upload_status"] = "processing"
            log.info(f"  Asset ID: {asset_id}")
            save_state(state)

            # Save to DB
            db_save_video(entry, asset_id)
            save_state(state)

        except Exception as e:
            log.error(f"  ❌ Upload failed: {e}")
            entry["upload_status"] = "errored"
            entry["error"] = str(e)
            save_state(state)

    log.info(f"\n✅ Phase 1 complete. State saved to {STATE_FILE}")
    return state


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 — Attach SRT + audio tracks
# ─────────────────────────────────────────────────────────────────────────────

def _serve_file(src_path: str, filename: str) -> str:
    """POSTs file to the FastAPI server's /upload/temp-file endpoint.
    The server saves it to its /temp folder and returns the public URL for Mux to fetch."""
    size_mb = os.path.getsize(src_path) / 1_000_000
    log.info(f"  Uploading {filename} ({size_mb:.1f} MB) to server temp ...")
    upload_url = f"{SERVER_BASE_URL}/upload/temp-file"
    with open(src_path, "rb") as f:
        r = requests.post(upload_url, files={"file": (filename, f)}, timeout=300)
    if not r.ok:
        raise RuntimeError(f"Server temp upload failed ({r.status_code}): {r.text[:200]}")
    url = r.json()["url"]
    log.info(f"  Hosted at: {url}")
    return url


def _cleanup_temp(filename: str):
    """Asks the server to delete a temp file after Mux has fetched it."""
    try:
        requests.delete(f"{SERVER_BASE_URL}/upload/temp-file/{filename}", timeout=10)
    except Exception:
        pass   # best-effort cleanup


def phase2(state: dict = None) -> dict:
    state = state or load_state()

    pending = {k: v for k, v in state.items()
               if v.get("asset_id") and
               (not v.get("srt_attached") or not v.get("audio_attached"))}

    log.info(f"\n{'═'*60}")
    log.info(f" PHASE 2 — Attaching tracks to {len(pending)} assets")
    log.info(f"{'═'*60}")

    for i, (base, entry) in enumerate(pending.items(), 1):
        asset_id = entry["asset_id"]
        title    = entry["title"]
        log.info(f"\n[{i}/{len(pending)}] {title} | {asset_id}")

        # ── Confirm asset ready before ANY track attachment ───────────────────
        playback_id = None
        drm_id      = None
        try:
            status, asset_data = mux_asset_status(asset_id)
            if status == "errored":
                log.error(f"  ❌ Asset errored on Mux — skipping all track attachment.")
                entry["upload_status"] = "errored"
                save_state(state)
                continue
            if status != "ready":
                log.info(f"  Asset status: {status} — waiting for ready before attaching tracks ...")
                asset_data = mux_wait_ready(asset_id)   # polls every 15s, max 20 min; raises on error/timeout
            log.info(f"  ✅ Asset confirmed ready — proceeding with SRT + audio attachment.")
            entry["upload_status"] = "ready"
            # Extract playback IDs from asset data
            for p in asset_data.get("playback_ids", []):
                if p.get("policy") == "public":
                    playback_id = p["id"]
                elif p.get("policy") == "drm":
                    drm_id = p["id"]
            entry["playback_id"]     = playback_id
            entry["drm_playback_id"] = drm_id
        except Exception as e:
            log.error(f"  ❌ Could not confirm ready: {e}")
            save_state(state)
            continue

        # ── Attach SRT ────────────────────────────────────────────────────
        if not entry.get("srt_attached"):
            srt_src = entry.get("srt")
            if srt_src and os.path.exists(srt_src):
                srt_filename = f"{base}_hi.srt"
                try:
                    url = _serve_file(srt_src, srt_filename)
                    log.info(f"  Attaching SRT: {url}")
                    mux_add_text_track(asset_id, url, "hi", "Hindi")
                    log.info(f"  ✅ SRT attached.")
                    entry["srt_attached"] = True
                    save_state(state)
                    time.sleep(60)       # give Mux time to fetch before deleting
                    _cleanup_temp(srt_filename)
                except Exception as e:
                    log.error(f"  ❌ SRT attach failed: {e}")
            else:
                log.warning(f"  ⚠️  No SRT file found for '{base}'")
                entry["srt_attached"] = False  # keep false, can retry

        # ── Attach VO audio ────────────────────────────────────────────────
        if not entry.get("audio_attached"):
            vo_src = entry.get("vo")
            if vo_src and os.path.exists(vo_src):
                vo_ext = Path(vo_src).suffix
                vo_filename = f"{base}_hi{vo_ext}"
                try:
                    url = _serve_file(vo_src, vo_filename)
                    log.info(f"  Attaching audio: {url}")
                    mux_add_audio_track(asset_id, url, "hi", "Hindi")
                    log.info(f"  ✅ Audio attached.")
                    entry["audio_attached"] = True
                    save_state(state)
                    time.sleep(60)
                    _cleanup_temp(vo_filename)
                except Exception as e:
                    log.error(f"  ❌ Audio attach failed: {e}")
            else:
                log.warning(f"  ⚠️  No VO file found for '{base}'")

        # Only update DB to ready if both tracks successfully attached
        if entry.get("srt_attached") and entry.get("audio_attached"):
            db_update_ready(
                entry,
                playback_id = playback_id,
                drm_id      = drm_id,
                srt_lang    = "hi",
                audio_lang  = "hi",
            )
        else:
            log.warning(f"  ⚠️  Skipping DB ready update — SRT={entry.get('srt_attached')} Audio={entry.get('audio_attached')}")
        save_state(state)

    log.info(f"\n✅ Phase 2 complete.")
    return state


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────

def generate_report(state: dict):
    try:
        import pandas as pd
    except ImportError:
        log.error("pandas not installed. Run: pip install pandas openpyxl")
        return

    rows = []
    for base, e in sorted(state.items(), key=lambda x: x[1].get("title", "")):
        rows.append({
            "Title":           e.get("title", base),
            "Mux Asset ID":    e.get("asset_id", ""),
            "Mux Playback ID": e.get("playback_id", ""),
            "DRM Playback ID": e.get("drm_playback_id", ""),
            "Upload Status":   e.get("upload_status", ""),
            "SRT Attached":    "✅" if e.get("srt_attached") else "❌",
            "Audio Attached":  "✅" if e.get("audio_attached") else "❌",
            "SRT File":        os.path.basename(e.get("srt") or ""),
            "VO File":         os.path.basename(e.get("vo") or ""),
            "Error":           e.get("error", ""),
        })

    df = pd.DataFrame(rows)
    LOGS_DIR.mkdir(exist_ok=True)
    out_path = LOGS_DIR / "hindi_upload_report.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Hindi Uploads")
        ws = writer.sheets["Hindi Uploads"]
        for col in ws.columns:
            width = max((len(str(c.value)) for c in col if c.value), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(width + 2, 60)

    log.info(f"\n📊 Report saved to: {out_path}")
    print(f"\nSummary:")
    print(df["Upload Status"].value_counts().to_string())
    print(f"\nSRT attached  : {df['SRT Attached'].eq('✅').sum()} / {len(df)}")
    print(f"Audio attached: {df['Audio Attached'].eq('✅').sum()} / {len(df)}")


# ─────────────────────────────────────────────────────────────────────────────
# PREVIEW
# ─────────────────────────────────────────────────────────────────────────────

def preview(matches: list[dict]):
    no_srt = [m for m in matches if not m["srt"]]
    no_vo  = [m for m in matches if not m["vo"]]

    print(f"\n{'─'*80}")
    print(f" PREVIEW — {len(matches)} videos matched")
    print(f"{'─'*80}")
    print(f"  ✅ Have SRT   : {len(matches) - len(no_srt)}")
    print(f"  ✅ Have VO    : {len(matches) - len(no_vo)}")
    print(f"  ⚠️  Missing SRT: {len(no_srt)}")
    print(f"  ⚠️  Missing VO : {len(no_vo)}")

    if no_srt:
        print(f"\n  Videos with no matching SRT:")
        for m in no_srt:
            print(f"    - {m['title']}")

    if no_vo:
        print(f"\n  Videos with no matching VO:")
        for m in no_vo:
            print(f"    - {m['title']}")

    print(f"\n  Sample matches (first 5):")
    for m in matches[:5]:
        print(f"  VIDEO : {os.path.basename(m['video'])}")
        print(f"    SRT : {os.path.basename(m['srt']) if m['srt'] else '❌ NOT FOUND'}")
        print(f"    VO  : {os.path.basename(m['vo'])  if m['vo']  else '❌ NOT FOUND'}")
        print()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Batch Hindi video uploader for Mux")
    parser.add_argument("--preview", action="store_true", help="Show matched files, no uploads")
    parser.add_argument("--phase1",  action="store_true", help="Upload videos only")
    parser.add_argument("--phase2",  action="store_true", help="Attach SRT + audio to ready assets")
    parser.add_argument("--all",     action="store_true", help="Run both phases")
    parser.add_argument("--report",  action="store_true", help="Generate Excel report from saved state")
    parser.add_argument("--limit",   type=int, default=None, help="Limit number of uploads (Phase 1)")
    args = parser.parse_args()

    if not any([args.preview, args.phase1, args.phase2, args.all, args.report]):
        parser.print_help()
        sys.exit(0)

    if not MUX_TOKEN_ID or not MUX_TOKEN_SECRET:
        log.error("MUX_TOKEN_ID / MUX_TOKEN_SECRET not set. Copy .env.example → .env and fill in your keys.")
        sys.exit(1)

    matches = match_files()

    if args.preview:
        preview(matches)
        return

    if args.report:
        generate_report(load_state())
        return

    state = load_state()

    if args.phase1 or args.all:
        state = phase1(matches, limit=args.limit, state=state)

    if args.phase2 or args.all:
        # Sync SRT/VO paths from current matches into state (in case state was loaded
        # from a previous run where paths weren't stored)
        for m in matches:
            if m["base"] in state:
                state[m["base"]]["srt"] = state[m["base"]].get("srt") or m["srt"]
                state[m["base"]]["vo"]  = state[m["base"]].get("vo")  or m["vo"]
        save_state(state)
        state = phase2(state)

    generate_report(state)


if __name__ == "__main__":
    main()

import asyncio
import logging
from app.services.mux_service import add_audio_track, _mux_request, BASE_URL
from app.services.vimeo_service import get_video_audio_tracks
from app.database.session import SessionLocal
from sqlalchemy import text

logger = logging.getLogger(__name__)


def _get_mux_audio_tracks(mux_asset_id: str) -> dict:
    """
    Fetches current tracks on a Mux asset.
    Tracks are embedded in the asset object (GET /assets/{id}), not a separate endpoint.
    Returns {"names": set_of_track_names, "languages": list_of_language_codes, "total": int}
    where total = all audio tracks (default + alternates).
    """
    r = _mux_request("GET", f"{BASE_URL}/assets/{mux_asset_id}")
    if not r.ok:
        logger.warning(f"[Audio] Could not fetch Mux asset for {mux_asset_id}: {r.status_code}")
        return {"names": set(), "languages": [], "total": 0}
    tracks = r.json().get("data", {}).get("tracks", [])
    audio = [t for t in tracks if t.get("type") == "audio"]
    alt = [t for t in audio if not t.get("primary", False)]
    names = {t.get("name", "") for t in audio}
    langs = [t.get("language_code", "") for t in alt]
    return {"names": names, "languages": langs, "total": len(audio)}


async def attach_audio_tracks_background(
    mux_asset_id: str,
    vimeo_id: str,
    vimeo_url: str,
    only_language: str | None = None,
) -> list[str]:
    """
    Discovers alternate audio tracks via Vimeo API (HLS manifest parsing)
    and attaches them directly to the Mux asset by passing the HLS playlist
    URL straight to Mux — no yt-dlp, no local downloads, no temp files.

    Skips tracks already present on the Mux asset (by name) to avoid 400 errors
    on re-runs. DB count is always synced from the real Mux track list after
    attachment, so partial previous runs don't cause under-counts.

    Returns list of newly attached language codes in this call.
    """
    logger.info(f"[Audio] Starting for Vimeo {vimeo_id} → Mux {mux_asset_id}")

    # Discover alternate audio tracks via Vimeo API
    tracks = await asyncio.to_thread(get_video_audio_tracks, vimeo_id)
    if not tracks:
        logger.info(f"[Audio] No alternate audio tracks found for {vimeo_id}.")
        return []

    if only_language:
        tracks = [t for t in tracks if t["language"] == only_language]
        if not tracks:
            logger.warning(f"[Audio] Language '{only_language}' not found for {vimeo_id}.")
            return []

    # Fetch tracks already on this Mux asset so we can skip duplicates
    existing = await asyncio.to_thread(_get_mux_audio_tracks, mux_asset_id)
    existing_names = existing["names"]
    if existing_names:
        logger.info(f"[Audio] Already on Mux: {existing_names}")

    logger.info(f"[Audio] Found {len(tracks)} alternate track(s) on Vimeo: {[t['language'] for t in tracks]}")

    attached = []
    skipped = []
    for track in tracks:
        language = track["language"]
        name = track["name"]
        hls_url = track["url"]

        if name in existing_names:
            logger.info(f"[Audio] ⏭ '{name}' ({language}) already on Mux — skipping")
            skipped.append(language)
            continue

        try:
            await asyncio.to_thread(add_audio_track, mux_asset_id, hls_url, language, name)
            logger.info(f"[Audio] ✅ '{name}' ({language}) attached to {mux_asset_id}")
            attached.append(language)
            existing_names.add(name)  # prevent duplicate if Vimeo returns same name twice
        except Exception as e:
            logger.error(f"[Audio] ❌ Failed for '{name}' ({language}): {e}")
        # Brief pause between track attachments to avoid burst 429s on the same asset
        await asyncio.sleep(0.3)

    # Sync DB count from the real Mux track list (not just what we attached this run)
    # This ensures partial previous runs don't leave the DB under-counted.
    real = await asyncio.to_thread(_get_mux_audio_tracks, mux_asset_id)
    real_alt_langs = real["languages"]
    real_total = real["total"]  # includes the default track

    if real_total > 0:
        try:
            with SessionLocal() as db:
                db.execute(
                    text(
                        "UPDATE videos SET audio_tracks_count=:c, audio_languages=:l "
                        "WHERE mux_asset_id=:aid"
                    ),
                    {
                        "c": real_total,
                        "l": ", ".join(real_alt_langs),
                        "aid": mux_asset_id,
                    },
                )
                db.commit()
            logger.info(
                f"[Audio] DB synced: {real_total} total track(s) "
                f"({len(attached)} new, {len(skipped)} already existed) for {mux_asset_id}"
            )
        except Exception as e:
            logger.warning(f"[Audio] DB update failed for {mux_asset_id}: {e}")

    return attached


# Alias for backward compatibility
def _discover_audio_languages(vimeo_url: str) -> list[dict]:
    import re
    m = re.search(r'vimeo\.com/(\d+)', vimeo_url)
    if not m:
        return []
    return get_video_audio_tracks(m.group(1))

"""Excel report (Videos / Audio Tracks / Caption Tracks / Errors / Summary).

The same writer serves both the dry run (records built from the discovery plan) and the
real run (records read from the state store); both use the same record shapes.
"""
import os
import re
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from migration import state as st
from migration.config import EXPECTED_LANGUAGES, language_name

VIDEO_COLS = ["Migration Status", "Vimeo Video ID", "Vimeo URL", "Vimeo Title", "Duration", "Vimeo Created At",
              "Vimeo Modified At", "Mux Asset ID", "Mux Asset Status", "Public Playback ID", "Signed Playback ID",
              "DRM Playback ID", "Audio Track Count", "Caption Track Count", "Migration Started At",
              "Migration Completed At", "Error", "Notes"]
AUDIO_COLS = ["Vimeo Video ID", "Vimeo Title", "Mux Asset ID", "Language", "Track Name", "Source", "Mux Track ID",
              "Status", "Created At", "Updated At", "Error"]
CAPTION_COLS = ["Vimeo Video ID", "Vimeo Title", "Mux Asset ID", "Language", "Source Format", "Mux Track ID", "Status",
                "Created At", "Updated At", "Error"]
ERROR_COLS = ["Vimeo Video ID", "Title", "Stage", "Track Type", "Language", "Error", "Timestamp", "Retry Status"]


_ILLEGAL = re.compile("[" + "".join(chr(c) for c in (*range(0, 9), 11, 12, *range(14, 32))) + "]")


def _ts(v):
    if isinstance(v, datetime):
        return v.replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return v or ""


def report_status(v: dict) -> str:
    if v["status"] == "COMPLETED" or v["outcome"] == st.COMPLETED:
        return "COMPLETED"
    if v["outcome"] == st.FAILED:
        return f"FAILED ({v['status']})"
    if v["outcome"] == st.INCOMPLETE:
        return "INCOMPLETE (asset verified, tracks missing/failed)"
    return v["status"]


def records_from_plans(plans, existing=None):
    """Dry-run records: nothing has been created, so Mux fields are empty and statuses read PLANNED."""
    existing = existing or {}
    videos, tracks, errors = [], [], []
    for p in plans:
        i = p.info
        rec = existing.get(i.vimeo_id)
        videos.append({
            "vimeo_id": i.vimeo_id, "title": i.title, "vimeo_url": i.url, "duration": i.duration,
            "vimeo_created_at": i.created_time, "vimeo_modified_at": i.modified_time,
            "status": rec["status"] if rec else "PLANNED", "outcome": rec["outcome"] if rec else st.IN_PROGRESS,
            "mux_asset_id": rec["mux_asset_id"] if rec else None, "mux_asset_status": None,
            "mux_public_playback_id": None, "mux_drm_playback_id": None, "mux_signed_playback_id": None,
            "started_at": None, "completed_at": None, "error": None,
            "notes": "; ".join(p.notes) if p.notes else None, "error_stage": None,
        })
        for ttype, found in (("audio", p.audio.tracks), ("caption", p.captions)):
            for lang in EXPECTED_LANGUAGES:
                src = found.get(lang)
                row = {"source_video_id": i.vimeo_id, "mux_asset_id": None, "track_type": ttype, "language": lang,
                       "language_code": lang, "track_name": language_name(lang), "mux_track_id": None,
                       "created_at": None, "updated_at": None}
                if src is None:
                    row.update(status=st.T_MISSING, error=f"no {language_name(lang)} {ttype} track found on Vimeo",
                               source=None, source_info=None, source_format=None)
                elif ttype == "audio":
                    row.update(status="PLANNED", error=None, source=f"vimeo-hls-audio:{src.rendition_key}",
                               source_info=f"NAME={src.vimeo_name};CHANNELS={src.channels}", source_format="m4a/mp4 (HLS audio)")
                else:
                    row.update(status="PLANNED", error=None, source=f"vimeo-texttrack:{src.vimeo_track_id}",
                               source_info=f"type={src.vimeo_type}", source_format=src.source_format)
                tracks.append(row)
        for pr in p.problems:
            errors.append({"vimeo_id": i.vimeo_id, "title": i.title, "stage": pr["stage"], "track_type": pr["track_type"],
                           "language": pr["language"], "error": pr["message"], "created_at": None,
                           "retry_status": "DISCOVERY FINDING (dry run)"})
    return videos, tracks, errors


def records_from_store(store):
    return store.list_videos(), store.list_tracks(), store.list_errors()


def summarize(videos, tracks, mode):
    n = len(videos)
    audio = [t for t in tracks if t["track_type"] == "audio"]
    caps = [t for t in tracks if t["track_type"] == "caption"]
    bad = (st.T_FAILED, st.T_MISSING)
    s = [("Mode", mode),
         ("Vimeo videos discovered", n),
         ("Videos migrated (Mux asset created)", sum(1 for v in videos if v["mux_asset_id"])),
         ("Videos completed", sum(1 for v in videos if v["status"] == "COMPLETED")),
         ("Videos incomplete (asset verified, tracks missing/failed)", sum(1 for v in videos if v["outcome"] == st.INCOMPLETE)),
         ("Videos failed", sum(1 for v in videos if v["outcome"] == st.FAILED)),
         ("Videos pending", sum(1 for v in videos if not v["mux_asset_id"] and v["outcome"] != st.FAILED)),
         ("Expected audio tracks", n * len(EXPECTED_LANGUAGES)),
         ("Audio tracks found on Vimeo", sum(1 for t in audio if t["status"] != st.T_MISSING)),
         ("Successfully migrated audio tracks", sum(1 for t in audio if t["status"] == st.T_VERIFIED)),
         ("Failed audio tracks (failed or missing on Vimeo)", sum(1 for t in audio if t["status"] in bad)),
         ("Expected caption tracks", n * len(EXPECTED_LANGUAGES)),
         ("Caption tracks found on Vimeo", sum(1 for t in caps if t["status"] != st.T_MISSING)),
         ("Successfully migrated caption tracks", sum(1 for t in caps if t["status"] == st.T_VERIFIED)),
         ("Failed caption tracks (failed or missing on Vimeo)", sum(1 for t in caps if t["status"] in bad)),
         ("Videos with DRM", sum(1 for v in videos if v["mux_drm_playback_id"])),
         ("Videos with Public Playback ID", sum(1 for v in videos if v["mux_public_playback_id"])),
         ("Videos with Signed Playback ID", sum(1 for v in videos if v["mux_signed_playback_id"])),
         ("Videos with DRM Playback ID", sum(1 for v in videos if v["mux_drm_playback_id"])),
         ("Videos with all three playback IDs", sum(1 for v in videos if v["mux_public_playback_id"]
                                                    and v["mux_signed_playback_id"] and v["mux_drm_playback_id"]))]
    return s


def write_workbook(path, videos, tracks, errors, mode, extra_summary=()):
    titles = {v["vimeo_id"]: v["title"] for v in videos}
    counts = {}
    for t in tracks:
        if t["status"] not in (st.T_MISSING,):
            counts[(t["source_video_id"], t["track_type"])] = counts.get((t["source_video_id"], t["track_type"]), 0) + 1

    wb = Workbook()
    ws = wb.active
    ws.title = "Videos"
    ws.append(VIDEO_COLS)
    for v in videos:
        ws.append([report_status(v), v["vimeo_id"], v["vimeo_url"], v["title"], v["duration"], v["vimeo_created_at"],
                   v["vimeo_modified_at"], v["mux_asset_id"], v["mux_asset_status"], v["mux_public_playback_id"],
                   v["mux_signed_playback_id"], v["mux_drm_playback_id"],
                   counts.get((v["vimeo_id"], "audio"), 0), counts.get((v["vimeo_id"], "caption"), 0),
                   _ts(v["started_at"]), _ts(v["completed_at"]), v["error"], v["notes"]])

    ws = wb.create_sheet("Audio Tracks")
    ws.append(AUDIO_COLS)
    for t in (x for x in tracks if x["track_type"] == "audio"):
        ws.append([t["source_video_id"], titles.get(t["source_video_id"]), t["mux_asset_id"], t["language"],
                   t["track_name"], t["source"], t["mux_track_id"], t["status"], _ts(t["created_at"]),
                   _ts(t["updated_at"]), t["error"]])

    ws = wb.create_sheet("Caption Tracks")
    ws.append(CAPTION_COLS)
    for t in (x for x in tracks if x["track_type"] == "caption"):
        ws.append([t["source_video_id"], titles.get(t["source_video_id"]), t["mux_asset_id"], t["language"],
                   t["source_format"], t["mux_track_id"], t["status"], _ts(t["created_at"]), _ts(t["updated_at"]),
                   t["error"]])

    ws = wb.create_sheet("Errors")
    ws.append(ERROR_COLS)
    for e in errors:
        ws.append([e["vimeo_id"], e["title"], e["stage"], e["track_type"], e["language"], e["error"],
                   _ts(e["created_at"]), e["retry_status"]])

    ws = wb.create_sheet("Summary")
    ws.append(["Metric", "Value"])
    for k, val in summarize(videos, tracks, mode):
        ws.append([k, val])
    for k, val in extra_summary:
        ws.append([k, val])

    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for c in row:
                if isinstance(c.value, str):
                    c.value = _ILLEGAL.sub("", c.value)
        for c in sheet[1]:
            c.font = Font(bold=True)
        sheet.freeze_panes = "A2"
        for idx, col in enumerate(sheet.columns, 1):
            width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            sheet.column_dimensions[get_column_letter(idx)].width = min(width + 2, 60)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    wb.save(path)
    return path


LATEST_NAME = "folder1_migration_latest.xlsx"


class Checkpointer:
    """Rewrites the cumulative workbook from the state DB. The file is built next to the target and
    swapped in with os.replace, so a reader never sees a half-written workbook. If the file is locked
    (e.g. open in Excel) the checkpoint is skipped with a warning; the next one retries."""

    def __init__(self, store, path):
        import logging
        import threading
        self.store, self.path = store, path
        self._lock = threading.Lock()
        self._log = logging.getLogger("migration.folder1")

    def __call__(self, reason="checkpoint"):
        with self._lock:
            tmp = self.path[:-5] + ".writing.xlsx"
            try:
                videos, tracks, errors = records_from_store(self.store)
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                write_workbook(tmp, videos, tracks, errors, "MIGRATION STATE (cumulative)",
                               [("Last updated", stamp), ("Last checkpoint reason", reason)])
                os.replace(tmp, self.path)
                return self.path
            except OSError as exc:
                self._log.warning("could not update %s (%s); will retry at the next checkpoint", self.path, exc)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return None

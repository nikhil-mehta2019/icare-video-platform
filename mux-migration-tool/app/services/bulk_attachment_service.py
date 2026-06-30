import os
import re
from pathlib import Path
from typing import Dict, Optional, List

import pandas as pd
import requests

from app.services.mux_service import add_audio_track, add_text_track
from app.config import SERVER_BASE_URL


def normalize_name(name: str) -> str:
    base = Path(name).stem
    base = re.sub(r"\s+", " ", base).strip()
    base = base.replace("&", "and")
    for suffix in [" Hindi SRT", " SRT Hindi", " SRT", " VO Hindi", " Hindi VO", " Hindi", " VO"]:
        if base.endswith(suffix):
            base = base[: -len(suffix)].strip()
            break
    base = base.replace(" for the Elder", "")
    base = base.replace(" with the Family", "")
    base = base.replace(" with the Elder", "")
    base = base.replace(" of the Elder", "")
    base = base.replace(" the Elder", "")
    base = base.replace(" the Family", "")
    base = base.replace(" and ", " ")
    base = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    return base or "unknown"


def _score_match(video_key: str, candidate_key: str) -> int:
    if video_key == candidate_key:
        return 100
    if candidate_key.startswith(video_key) or video_key.startswith(candidate_key):
        return 80
    if video_key in candidate_key or candidate_key in video_key:
        return 60
    return 0


def find_matching_files(video_name: str, srt_dir: Path, audio_dir: Path) -> Dict[str, Optional[Path]]:
    key = normalize_name(video_name)
    candidates = {normalize_name(p.name): p for p in srt_dir.glob("*") if p.is_file()} if srt_dir.exists() else {}
    audio_candidates = {normalize_name(p.name): p for p in audio_dir.glob("*") if p.is_file()} if audio_dir.exists() else {}

    srt_path = candidates.get(key)
    audio_path = audio_candidates.get(key)

    if not srt_path:
        best_score = -1
        best_match = None
        for candidate_name, candidate_path in candidates.items():
            score = _score_match(key, candidate_name)
            if score > best_score:
                best_score = score
                best_match = candidate_path
        srt_path = best_match if best_score >= 60 else None

    if not audio_path:
        best_score = -1
        best_match = None
        for candidate_name, candidate_path in audio_candidates.items():
            score = _score_match(key, candidate_name)
            if score > best_score:
                best_score = score
                best_match = candidate_path
        audio_path = best_match if best_score >= 60 else None

    return {"srt": srt_path, "audio": audio_path}


def read_excel_rows(file_path: str) -> List[Dict[str, object]]:
    df = pd.read_excel(file_path, engine="openpyxl")
    return df.to_dict(orient="records")


def _upload_temp_file(file_path: str, filename: str) -> str:
    with open(file_path, "rb") as f:
        response = requests.post(
            f"{SERVER_BASE_URL.rstrip('/')}/upload/temp-file",
            files={"file": (filename, f)},
            timeout=300,
        )
    if not response.ok:
        raise RuntimeError(f"Temp upload failed ({response.status_code}): {response.text[:300]}")
    return response.json()["url"]


def attach_tracks_to_asset(asset_id: str, srt_path: Optional[str], audio_path: Optional[str],
                           srt_language: str = "hi", audio_language: str = "hi",
                           audio_name: str = "Hindi") -> Dict[str, bool]:
    results = {"srt": False, "audio": False}

    if srt_path and os.path.exists(srt_path):
        srt_filename = os.path.basename(srt_path)
        srt_url = _upload_temp_file(srt_path, srt_filename)
        add_text_track(asset_id, srt_url, srt_language, srt_language)
        results["srt"] = True

    if audio_path and os.path.exists(audio_path):
        audio_filename = os.path.basename(audio_path)
        audio_url = _upload_temp_file(audio_path, audio_filename)
        add_audio_track(asset_id, audio_url, audio_language, audio_name)
        results["audio"] = True

    return results

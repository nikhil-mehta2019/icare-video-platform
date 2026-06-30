"""
check_swahili_srt.py
--------------------
Checks which Swahili SRT + VO files have a matching video in the DB.

Usage:
    python check_swahili_srt.py
"""

import os, sys, re
sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path
from app.database.session import SessionLocal
from app.database.models import Video
from datetime import datetime

SRT_DIR = r"D:\new uploads\Swahili\Final Swahili Output Rendered-20260613T072713Z-3-001\Final Swahili Output Rendered\English SRT to Swahili 79 (Male)"
VO_DIR  = r"D:\new uploads\Swahili\Final Swahili Output Rendered-20260613T072713Z-3-001\Final Swahili Output Rendered\VOM Swahili 79 (79) Male)"

CUTOFF = datetime(2026, 6, 1)


def base_title(filename: str) -> str:
    """'Administering Medicines Swahili.srt' → 'Administering Medicines'"""
    name = Path(filename).stem          # remove .srt
    # Strip any trailing Swahili marker variants
    for suffix in [" Swahili SRT", " Swahili"]:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.strip()


def base_title_vo(filename: str) -> str:
    """Strip VOM/Swahili markers from VO filenames.
    Handles: 'Title VOM Swahili.mp3', 'Title Swahili VOM.mp3', double spaces, trailing spaces."""
    name = Path(filename).stem.strip()
    name = re.sub(r'\s+VOM\s+Swahili\s*$', '', name, flags=re.IGNORECASE).strip()
    name = re.sub(r'\s+Swahili\s+VOM\s*$', '', name, flags=re.IGNORECASE).strip()
    return name

def normalize(s: str) -> str:
    """Lowercase + replace underscores with apostrophes for fuzzy compare."""
    return s.lower().replace("_", "'").strip()


def main():
    srt_files = sorted(f for f in os.listdir(SRT_DIR) if f.endswith(".srt"))
    print(f"Total SRT files in folder: {len(srt_files)}\n")

    matched   = []
    not_found = []
    multi     = []

    with SessionLocal() as db:
        videos = (
            db.query(Video)
            .filter(Video.created_at > CUTOFF)
            .all()
        )
        # Build a lookup: vimeo_title (lower) → list of Video records
        title_map: dict[str, list] = {}
        for v in videos:
            key = (v.vimeo_title or "").strip().lower()
            title_map.setdefault(key, []).append(v)

    # Also build a normalized map (underscore → apostrophe) for fuzzy lookup
    norm_map: dict[str, list] = {}
    for key, vlist in title_map.items():
        norm_map[normalize(key)] = vlist

    for f in srt_files:
        title = base_title(f)
        key   = title.lower()
        hits  = title_map.get(key, [])

        # Try normalized match (underscore → apostrophe) if exact fails
        if not hits:
            hits = norm_map.get(normalize(title), [])

        if len(hits) == 1:
            v = hits[0]
            matched.append((f, title, v))
        elif len(hits) > 1:
            multi.append((f, title, hits))
        else:
            not_found.append((f, title))

    # ── Results ────────────────────────────────────────────────────────────────
    print(f"{'─'*80}")
    print(f"✅ MATCHED ({len(matched)})")
    print(f"{'─'*80}")
    for f, title, v in matched:
        print(f"  {title}")
        print(f"    DB id={v.id} | mux_asset={v.mux_asset_id} | status={v.status}")

    print(f"\n{'─'*80}")
    print(f"⚠️  MULTIPLE MATCHES ({len(multi)})  — need manual selection")
    print(f"{'─'*80}")
    for f, title, hits in multi:
        print(f"  {title}")
        for v in hits:
            print(f"    DB id={v.id} | vimeo_id={v.vimeo_id} | status={v.status}")

    print(f"\n{'─'*80}")
    print(f"❌ NOT FOUND IN DB ({len(not_found)}) — closest DB titles shown for manual mapping")
    print(f"{'─'*80}")
    from difflib import get_close_matches  # noqa: already imported below if needed
    all_db_titles = list(title_map.keys())
    for f, title in not_found:
        print(f"\n  SRT : {title}")
        print(f"  File: {f}")
        suggestions = get_close_matches(title.lower(), all_db_titles, n=3, cutoff=0.4)
        if suggestions:
            for s in suggestions:
                v = title_map[s][0]
                print(f"  → DB: \"{title_map[s][0].vimeo_title}\"  |  id={v.id}  |  mux_asset={v.mux_asset_id}  |  status={v.status}")
        else:
            print(f"  → No close match found")

    print(f"\n{'═'*80}")
    print(f"  SRT — Matched: {len(matched)} / {len(srt_files)}  |  Multi: {len(multi)}  |  Missing: {len(not_found)}")
    print(f"{'═'*80}")

    # ── VO check ───────────────────────────────────────────────────────────────
    print(f"\n\n{'═'*80}")
    print(f" VO FILES CHECK")
    print(f"{'═'*80}")

    vo_files = sorted(f for f in os.listdir(VO_DIR) if f.lower().endswith(".mp3"))
    print(f"Total VO files in folder: {len(vo_files)}\n")

    vo_matched   = []
    vo_not_found = []
    vo_multi     = []

    for f in vo_files:
        title = base_title_vo(f)
        key   = title.lower()
        hits  = title_map.get(key, [])
        if not hits:
            hits = norm_map.get(normalize(title), [])
        if len(hits) == 1:
            vo_matched.append((f, title, hits[0]))
        elif len(hits) > 1:
            vo_multi.append((f, title, hits))
        else:
            vo_not_found.append((f, title))

    print(f"{'─'*80}")
    print(f"✅ MATCHED ({len(vo_matched)})")
    print(f"{'─'*80}")
    for f, title, v in vo_matched:
        print(f"  {title}")
        print(f"    DB id={v.id} | mux_asset={v.mux_asset_id} | status={v.status}")

    if vo_multi:
        print(f"\n{'─'*80}")
        print(f"⚠️  MULTIPLE MATCHES ({len(vo_multi)})")
        print(f"{'─'*80}")
        for f, title, hits in vo_multi:
            print(f"  {title}")
            for v in hits:
                print(f"    DB id={v.id} | vimeo_title={v.vimeo_title} | status={v.status}")

    print(f"\n{'─'*80}")
    print(f"❌ NOT FOUND ({len(vo_not_found)}) — closest DB titles shown")
    print(f"{'─'*80}")
    for f, title in vo_not_found:
        print(f"\n  VO  : {title}")
        print(f"  File: {f}")
        suggestions = get_close_matches(title.lower(), list(title_map.keys()), n=3, cutoff=0.4)
        for s in suggestions:
            v = title_map[s][0]
            print(f"  → DB: \"{v.vimeo_title}\"  |  id={v.id}  |  status={v.status}")
        if not suggestions:
            print(f"  → No close match found")

    print(f"\n{'═'*80}")
    print(f"  VO  — Matched: {len(vo_matched)} / {len(vo_files)}  |  Multi: {len(vo_multi)}  |  Missing: {len(vo_not_found)}")
    print(f"{'═'*80}")


if __name__ == "__main__":
    main()

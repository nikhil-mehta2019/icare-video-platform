"""
check_swahili_81f.py
--------------------
Checks Swahili 81 Female SRT + VO files against DB.
Usage:
    python check_swahili_81f.py
"""

import os, sys, re
from pathlib import Path
from difflib import get_close_matches
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from app.database.session import SessionLocal
from app.database.models import Video

SRT_DIR = r"D:\new uploads\Swahili\Final Swahili Output Rendered-20260613T072713Z-3-001\Final Swahili Output Rendered\English SRT to Swahili 81 (81 Female)"
VO_DIR  = r"D:\new uploads\Swahili\Final Swahili Output Rendered-20260613T072713Z-3-001\Final Swahili Output Rendered\VOM Swahili 81 (81) Female)"


def base_title_srt(filename: str) -> str:
    name = Path(filename).stem
    for suffix in [" Swahili SRT", " Swahili"]:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name.strip()


def base_title_vo(filename: str) -> str:
    name = Path(filename).stem.strip()
    name = re.sub(r'\s+VOM\s+Swahili\s*$', '', name, flags=re.IGNORECASE).strip()
    name = re.sub(r'\s+Swahili\s+VOM\s*$', '', name, flags=re.IGNORECASE).strip()
    return name


def normalize(s: str) -> str:
    return s.lower().replace("_", "'").strip()


def main():
    srt_files = sorted(f for f in os.listdir(SRT_DIR) if f.endswith(".srt"))
    vo_files  = sorted(f for f in os.listdir(VO_DIR)  if f.lower().endswith(".mp3"))
    print(f"SRT files: {len(srt_files)} | VO files: {len(vo_files)}\n")

    # Same cutoff as 79M batch
    CUTOFF = datetime(2026, 6, 1)
    with SessionLocal() as db:
        videos = db.query(Video).filter(Video.created_at > CUTOFF).all()
        title_map: dict[str, list] = {}
        for v in videos:
            key = (v.vimeo_title or "").strip().lower()
            title_map.setdefault(key, []).append(v)
        norm_map: dict[str, list] = {normalize(k): vl for k, vl in title_map.items()}

    def resolve(title: str) -> list:
        hits = title_map.get(title.lower(), [])
        if not hits:
            hits = norm_map.get(normalize(title), [])
        return hits

    all_titles = list(title_map.keys())

    # ── SRT ──────────────────────────────────────────────────────────────────
    srt_matched, srt_not_found, srt_multi = [], [], []
    for f in srt_files:
        title = base_title_srt(f)
        hits  = resolve(title)
        if len(hits) == 1:
            srt_matched.append((f, title, hits[0]))
        elif len(hits) > 1:
            srt_multi.append((f, title, hits))
        else:
            srt_not_found.append((f, title))

    print(f"{'─'*80}")
    print(f"✅ SRT MATCHED ({len(srt_matched)})")
    print(f"{'─'*80}")
    for f, title, v in srt_matched:
        print(f"  {title}")
        print(f"    id={v.id} | mux={v.mux_asset_id} | status={v.status} | created={str(v.created_at)[:10]}")

    if srt_multi:
        print(f"\n⚠️  SRT MULTIPLE MATCHES ({len(srt_multi)})")
        for f, title, hits in srt_multi:
            print(f"  {title}")
            for v in hits:
                print(f"    id={v.id} | mux={v.mux_asset_id} | status={v.status} | created={str(v.created_at)[:10]}")

    print(f"\n{'─'*80}")
    print(f"❌ SRT NOT FOUND ({len(srt_not_found)})")
    print(f"{'─'*80}")
    for f, title in srt_not_found:
        print(f"\n  SRT: {title}")
        print(f"  File: {f}")
        sug = get_close_matches(title.lower(), all_titles, n=3, cutoff=0.4)
        for s in sug:
            v = title_map[s][0]
            print(f"  → \"{title_map[s][0].vimeo_title}\" | id={v.id} | status={v.status} | created={str(v.created_at)[:10]}")
        if not sug:
            print(f"  → No close match in DB")

    print(f"\n{'═'*80}")
    print(f"  SRT: {len(srt_matched)} matched | {len(srt_multi)} multi | {len(srt_not_found)} NOT FOUND")
    print(f"{'═'*80}")

    # ── VO ───────────────────────────────────────────────────────────────────
    vo_matched, vo_not_found, vo_multi = [], [], []
    for f in vo_files:
        title = base_title_vo(f)
        hits  = resolve(title)
        if len(hits) == 1:
            vo_matched.append((f, title, hits[0]))
        elif len(hits) > 1:
            vo_multi.append((f, title, hits))
        else:
            vo_not_found.append((f, title))

    print(f"\n\n{'─'*80}")
    print(f"✅ VO MATCHED ({len(vo_matched)})")
    print(f"{'─'*80}")
    for f, title, v in vo_matched:
        print(f"  {title}")
        print(f"    id={v.id} | mux={v.mux_asset_id} | status={v.status} | created={str(v.created_at)[:10]}")

    if vo_multi:
        print(f"\n⚠️  VO MULTIPLE MATCHES ({len(vo_multi)})")
        for f, title, hits in vo_multi:
            print(f"  {title}")
            for v in hits:
                print(f"    id={v.id} | status={v.status} | created={str(v.created_at)[:10]}")

    print(f"\n{'─'*80}")
    print(f"❌ VO NOT FOUND ({len(vo_not_found)})")
    print(f"{'─'*80}")
    for f, title in vo_not_found:
        print(f"\n  VO: {title}")
        print(f"  File: {f}")
        sug = get_close_matches(title.lower(), all_titles, n=3, cutoff=0.4)
        for s in sug:
            v = title_map[s][0]
            print(f"  → \"{title_map[s][0].vimeo_title}\" | id={v.id} | status={v.status} | created={str(v.created_at)[:10]}")
        if not sug:
            print(f"  → No close match in DB")

    print(f"\n{'═'*80}")
    print(f"  VO:  {len(vo_matched)} matched | {len(vo_multi)} multi | {len(vo_not_found)} NOT FOUND")
    print(f"{'═'*80}")

    # ── SRT files with no VO counterpart ─────────────────────────────────────
    srt_titles = {base_title_srt(f).lower() for f in srt_files}
    vo_titles  = {base_title_vo(f).lower()  for f in vo_files}
    srt_only = srt_titles - vo_titles
    if srt_only:
        print(f"\n\n{'─'*80}")
        print(f"⚠️  SRT WITH NO VO ({len(srt_only)})")
        print(f"{'─'*80}")
        for t in sorted(srt_only):
            print(f"  {t}")


if __name__ == "__main__":
    main()

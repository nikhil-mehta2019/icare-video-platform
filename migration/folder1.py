"""Vimeo Folder 1 (25228807) -> NEW Mux assets.

    python -m migration.folder1 --probe-video <VIMEO_ID>   # read-only audio/caption discovery for ONE video
    python -m migration.folder1 --dry-run                  # read-only plan + Excel report for the whole folder
    python -m migration.folder1 --report                   # Excel report from the saved migration state
    python -m migration.folder1 --execute --confirm-folder 25228807   # the real migration (creates Mux assets)

Only Folder 1 is supported; the folder ID is a constant, not an argument. This tool contains no
delete/update of Mux assets, no Vimeo writes and no Base44 access.
"""
import argparse
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from migration import state as st
from migration.config import (DEFAULT_OUTPUT_DIR, DEFAULT_STATE_DB_URL, STOP_FILE, EXPECTED_LANGUAGES, FOLDER_ID, LOG_DIR,
                              base_lang, language_name, redact)
from migration.report import (LATEST_NAME, Checkpointer, records_from_plans, records_from_store, summarize,
                              write_workbook)
from migration.vimeo_source import VimeoApi, VimeoError, build_video_plan, dedupe_videos, video_id_from_uri

logger = logging.getLogger("migration.folder1")


class _RedactFilter(logging.Filter):
    def filter(self, record):
        record.msg, record.args = redact(record.getMessage()), ()
        return True


def setup_logging(name):
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.FileHandler(os.path.join(LOG_DIR, f"folder1_{name}.log"), encoding="utf-8"),
              logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        h.addFilter(_RedactFilter())
        root.addHandler(h)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_services(args):
    from app import config as appcfg
    from migration.audio_fetch import LocalTunnelHost, ServerUploadHost, YtDlpAudioFetcher
    from migration.mux_client import MuxClient
    mux = MuxClient(appcfg.MUX_TOKEN_ID, appcfg.MUX_TOKEN_SECRET, appcfg.DRM_CONFIGURATION_ID)
    if getattr(args, "audio_host", "server") == "local":
        host = LocalTunnelHost()
    else:
        host = ServerUploadHost(getattr(args, "server_url", None) or os.environ.get("MIGRATION_AUDIO_SERVER_URL")
                                or appcfg.SERVER_BASE_URL)
    return VimeoApi(), mux, YtDlpAudioFetcher(), host


def preflight(vimeo, mux, fetcher, host, state_url):
    """[(label, ok|None, detail, blocking_for_execute)]"""
    checks = []
    try:
        vimeo.get_json("/me", {"fields": "uri"})
        checks.append(("Vimeo authentication", True, "token accepted by api.vimeo.com", True))
    except VimeoError as exc:
        checks.append(("Vimeo authentication", False, str(exc), True))
    ok, detail = mux.check_credentials()
    checks.append(("Mux credentials", ok, detail, True))
    ok, detail = mux.check_drm_configuration()
    checks.append(("DRM configuration (required)", ok, detail, True))
    ver = fetcher.check_installed()
    checks.append(("yt-dlp installed", bool(ver), f"version {ver}" if ver else "yt_dlp not importable", True))
    ok, detail = host.check()
    checks.append(("Audio hosting reachable", ok, detail, True))
    checks.append(("State database", True, redact(state_url), False))
    return checks


def print_checks(checks):
    print("\n== Configuration validation ==")
    for label, ok, detail, _ in checks:
        mark = {True: "OK  ", False: "FAIL", None: "WARN"}[ok]
        print(f"  [{mark}] {label}: {detail}")


# ------------------------------------------------------------------------ probe


def cmd_probe(args):
    vimeo, mux, fetcher, host = build_services(args)
    vid = args.probe_video
    video = vimeo.get_video(vid)
    plan = build_video_plan(vimeo, video)
    print(f"\nVimeo Video: {vid}  ({plan.info.title})")
    print(f"Current version / notes: {'; '.join(plan.notes) if plan.notes else 'single version'}")
    fmts, fmt_err = None, None
    try:
        fmts = fetcher.formats(vid)
    except Exception as exc:
        fmt_err = redact(exc)
    from migration.audio_fetch import match_format_id
    print("\n-- Dubbed audio (read-only; nothing downloaded or uploaded) --")
    for lang in EXPECTED_LANGUAGES:
        t = plan.audio.tracks.get(lang)
        if not t:
            print(f"{language_name(lang)}: NOT FOUND")
            continue
        yt = "yt-dlp format not checked" if fmts is None else (
            f"yt-dlp format {match_format_id(fmts, t.rendition_key)}" if match_format_id(fmts, t.rendition_key)
            else "NO yt-dlp format match")
        print(f"{language_name(lang)}: FOUND  (HLS audio rendition {t.rendition_key}, {t.channels}ch, "
              f"Vimeo name '{t.vimeo_name}', dubbed; {yt})")
    for t in plan.audio.original:
        print(f"Embedded/original audio (preserved, not re-attached): {t.vimeo_name} [{t.language}] "
              f"rendition {t.rendition_key}{' DEFAULT' if t.default else ''}")
    if fmt_err:
        print(f"yt-dlp check error: {fmt_err}")
    print("\n-- Captions --")
    for lang in EXPECTED_LANGUAGES:
        c = plan.captions.get(lang)
        print(f"{language_name(lang)}: " + (f"FOUND  (Vimeo {c.vimeo_type} track {c.vimeo_track_id}; Mux source format "
                                          f"{c.source_format}; file '{c.source_name}')" if c else "NOT FOUND"))
    ok = not plan.missing_audio and not plan.missing_captions and not plan.audio.error
    dl = plan.download
    print(f"\nVideo download: {dl['rendition']} {dl['width']}x{dl['height']} via {dl['source']}" if dl else
          "\nVideo download: NONE AVAILABLE")
    print(f"RESULT: {'all six audio and caption languages discovered' if ok else 'INCOMPLETE - see NOT FOUND above'}")
    return 0 if ok else 2


# ---------------------------------------------------------------------- dry run


def discover_all(vimeo, items, sniff, workers=4):
    plans, lock, done = [], threading.Lock(), [0]

    def one(item):
        try:
            p = build_video_plan(vimeo, item["video"], item.get("folder_name", "Root"), sniff=sniff)
        except Exception as exc:   # one failing video must not hide the rest of the plan
            from migration.vimeo_source import AudioDiscovery, VideoPlan, parse_video
            info = parse_video(item["video"], item.get("folder_name", "Root"))
            p = VideoPlan(info=info, download=None, audio=AudioDiscovery(error=str(exc)))
            p.problems.append({"stage": "DISCOVERY", "track_type": None, "language": None,
                               "message": f"discovery failed: {redact(exc)}"})
        with lock:
            plans.append(p)
            done[0] += 1
            if done[0] % 10 == 0:
                print(f"  discovered {done[0]}/{len(items)} videos...")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, items))
    return sorted(plans, key=lambda p: (p.info.title, p.info.vimeo_id))


def cmd_dry_run(args):
    vimeo, mux, fetcher, host = build_services(args)
    checks = preflight(vimeo, mux, fetcher, host, args.state_db)
    print_checks(checks)

    print(f"\n== Enumerating Vimeo Folder {FOLDER_ID} ==")
    items, dups = dedupe_videos(vimeo.enumerate_folder(FOLDER_ID))
    print(f"  {len(items)} unique videos (duplicate Vimeo IDs dropped: {dups or 'none'})")
    print("== Discovering audio / captions / versions (read-only) ==")
    plans = discover_all(vimeo, items, sniff=not args.no_sniff)

    existing = {}
    store = st.StateStore.open_existing(args.state_db)
    if store:
        existing = {v["vimeo_id"]: v for v in store.list_videos()}
    videos, tracks, errors = records_from_plans(plans, existing)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(args.output, f"folder1_dry_run_{stamp}.xlsx")
    extra = [(f"Check: {l}", f"{'OK' if ok else ('WARN' if ok is None else 'FAIL')} - {d}") for l, ok, d, _ in checks]
    extra.append(("Duplicate Vimeo IDs in folder", ", ".join(dups) or "none"))
    write_workbook(path, videos, tracks, errors, "DRY RUN (nothing created)", extra)

    print_dry_run_summary(plans, videos, tracks, errors, checks, existing, path)
    return 0


def print_dry_run_summary(plans, videos, tracks, errors, checks, existing, path):
    audio_found = sum(1 for t in tracks if t["track_type"] == "audio" and t["status"] != st.T_MISSING)
    cap_found = sum(1 for t in tracks if t["track_type"] == "caption" and t["status"] != st.T_MISSING)
    srt = sum(1 for t in tracks if t["track_type"] == "caption" and t["source_format"] == "srt")
    vtt = sum(1 for t in tracks if t["track_type"] == "caption" and t["source_format"] == "vtt")
    n = len(plans)
    exp = n * len(EXPECTED_LANGUAGES)
    full = sum(1 for p in plans if not p.missing_audio and not p.missing_captions and not p.audio.error)
    print("\n================ DRY-RUN SUMMARY ================")
    print(f"Vimeo videos discovered:           {n}")
    print(f"Videos with all 6 audio + 6 captions: {full}")
    print(f"Audio tracks found:                {audio_found} (expected {exp})")
    print(f"Caption tracks found:              {cap_found} (expected {exp}); Mux source: {srt} SRT, {vtt} VTT")
    print(f"Videos without a downloadable file:{sum(1 for p in plans if p.download is None):>3}")
    print(f"Videos already in migration state: {len(existing)}")
    for lang in EXPECTED_LANGUAGES:
        a = sum(1 for p in plans if lang in p.audio.tracks)
        c = sum(1 for p in plans if lang in p.captions)
        print(f"  {language_name(lang):<11} audio {a:>3}/{n}   captions {c:>3}/{n}")
    notes = [(p.info.vimeo_id, x) for p in plans for x in p.notes if "version" in x.lower()]
    for vid, x in notes:
        print(f"Version note [{vid}]: {x}")
    if errors:
        print(f"\nDiscovery findings ({len(errors)}):")
        for e in errors[:25]:
            print(f"  {e['vimeo_id']} {e['stage']} {e['language'] or ''}: {e['error']}")
        if len(errors) > 25:
            print(f"  ... {len(errors) - 25} more in the Errors sheet")
    blocking = [l for l, ok, _, b in checks if b and ok is not True]
    print(f"\nReady for --execute: {'NO - fix: ' + ', '.join(blocking) if blocking else 'configuration checks pass'}")
    print(f"Report: {path}")


# -------------------------------------------------------------------- execute


class RunLock:
    """Refuses to start a second --execute against the same state file."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode())
        except FileExistsError:
            raise SystemExit(f"Another migration run holds {self.path}. If no run is active, delete that file and retry.")
        return self

    def __exit__(self, *exc):
        os.close(self.fd)
        os.remove(self.path)


def cmd_execute(args):
    if args.confirm_folder != FOLDER_ID:
        print(f"Refusing to run: pass --confirm-folder {FOLDER_ID} to confirm you intend to migrate Folder 1.")
        return 1
    if os.path.exists(STOP_FILE):
        print(f"Refusing to run: {STOP_FILE} exists (a previous run or watchdog asked for a stop). Review, then delete it.")
        return 1
    vimeo, mux, fetcher, host = build_services(args)
    checks = preflight(vimeo, mux, fetcher, host, args.state_db)
    print_checks(checks)
    blocking = [l for l, ok, _, b in checks if b and ok is not True]
    if blocking:
        print(f"\nAborting before any change: {', '.join(blocking)}")
        return 1

    items, dups = dedupe_videos(vimeo.enumerate_folder(FOLDER_ID))
    print(f"\n{len(items)} unique videos in Folder {FOLDER_ID} (duplicates dropped: {dups or 'none'})")
    store = st.StateStore(args.state_db)
    checkpoint = Checkpointer(store, os.path.join(args.output, LATEST_NAME))
    seed_state(vimeo, store, items)
    checkpoint("seeded all discovered videos")
    from migration.runner import Migrator
    migrator = Migrator(store, mux, vimeo, fetcher, host, workers=args.workers, checkpoint=checkpoint)
    migrator.stop_file = STOP_FILE
    if args.recover_errored:
        if not args.only:
            print("Refusing to run: --recover-errored requires explicit --only <VIMEO_ID> (repeatable).")
            return 1
        migrator.recover_errored = True
    lock_path = os.path.join(DEFAULT_OUTPUT_DIR, "folder1.lock")
    os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)
    t0 = time.monotonic()
    with RunLock(lock_path):
        results = migrator.run(items, only=set(args.only) if args.only else None, limit=args.limit,
                               max_consecutive_failures=args.stop_after_failures,
                               exclude=set(args.exclude) if args.exclude else None)
    counts = {}
    for r in results.values():
        counts[r] = counts.get(r, 0) + 1
    print(f"\nRun finished in {time.monotonic() - t0:.0f}s: {counts}")
    checkpoint("run finished")
    return _write_state_report(store, args)


def seed_state(vimeo, store, items, sniff=True):
    """Read-only against Vimeo; writes ONLY the local state DB. Records every folder video plus its 12
    expected track rows (PENDING / MISSING_ON_VIMEO) so the workbook always lists the whole folder.
    Existing progress, Mux IDs and verified tracks are never reset."""
    print(f"Recording all {len(items)} videos and their tracks in the state database (read-only Vimeo discovery)...")
    plans = discover_all(vimeo, items, sniff=sniff)
    skipped = []
    for p in plans:
        if p.audio.error and not p.audio.tracks and any(x["message"].startswith("discovery failed") for x in p.problems):
            skipped.append(p.info.vimeo_id)   # transient discovery failure: do not mislabel its tracks as missing
            continue
        store.upsert_discovered(p, FOLDER_ID)
    if skipped:
        print(f"  WARNING: discovery failed for {len(skipped)} video(s), not recorded yet: {skipped}")
    print(f"  state database now holds {len(store.list_videos())} videos and {len(store.list_tracks())} track rows")


def cmd_ensure_playback_ids(args):
    """Adds ONLY the missing public/signed/drm playback IDs to the explicitly named, already-migrated
    videos. Never creates assets, never deletes or replaces playback IDs, never overwrites stored IDs."""
    if not args.only:
        print("Refusing to run: --ensure-playback-ids requires explicit --only <VIMEO_ID> (repeatable).")
        return 1
    from app import config as appcfg
    from migration.mux_client import POLICIES, check_signing_key, playback_fields, verify_signed_playback
    _, mux, _, _ = build_services(args)
    ok, detail = check_signing_key(mux, appcfg.MUX_SIGNING_KEY_ID, appcfg.MUX_PRIVATE_KEY)
    print(f"Signing key check: {'OK' if ok else 'FAIL'} - {detail}")
    if not ok:
        print("Aborting before any change.")
        return 1
    drm_ok, drm_detail = mux.check_drm_configuration()
    print(f"DRM configuration: {'OK' if drm_ok else 'FAIL'} - {drm_detail}")
    if drm_ok is not True:
        print("Aborting before any change.")
        return 1
    store = st.StateStore(args.state_db)
    rc = 0
    for vid in args.only:
        rec = store.get_video(vid)
        if not rec or not rec["mux_asset_id"]:
            print(f"[{vid}] no Mux asset recorded in the state DB - skipped")
            rc = 1
            continue
        before = {p["policy"]: p["id"] for p in mux.get_asset(rec["mux_asset_id"]).get("playback_ids", [])}
        asset, created = mux.ensure_playback_ids(rec["mux_asset_id"])
        after = {p["policy"]: p["id"] for p in asset.get("playback_ids", [])}
        changed, conflicts = store.set_video_fields(vid, **playback_fields(asset))
        print(f"[{vid}] asset {rec['mux_asset_id']}")
        for pol in POLICIES:
            tag = "created" if pol in created else ("kept" if pol in before else "MISSING")
            print(f"    {pol:<7} {after.get(pol)}  ({tag})")
        if conflicts:
            print(f"    WARNING: stored IDs differ from Mux and were NOT overwritten: {conflicts}")
            rc = 1
        if set(after) < set(POLICIES):
            rc = 1
        if after.get("signed"):
            sok, sdetail = verify_signed_playback(after["signed"], appcfg.MUX_SIGNING_KEY_ID, appcfg.MUX_PRIVATE_KEY)
            print(f"    signed playback proof: {'OK' if sok else 'FAIL'} ({sdetail})")
            rc = rc or (0 if sok else 1)
    Checkpointer(store, os.path.join(args.output, LATEST_NAME))("ensure-playback-ids")
    return rc


def cmd_sync_report(args):
    """No Mux calls, no media: seed the state DB from Vimeo and refresh the cumulative workbook."""
    vimeo = VimeoApi()
    items, _ = dedupe_videos(vimeo.enumerate_folder(FOLDER_ID))
    store = st.StateStore(args.state_db)
    seed_state(vimeo, store, items, sniff=not args.no_sniff)
    path = Checkpointer(store, os.path.join(args.output, LATEST_NAME))("sync-report")
    print(f"Workbook: {path}")
    return 0 if path else 1


def _write_state_report(store, args):
    videos, tracks, errors = records_from_store(store)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(args.output, f"folder1_migration_{stamp}.xlsx")
    write_workbook(path, videos, tracks, errors, "MIGRATION STATE")
    Checkpointer(store, os.path.join(args.output, LATEST_NAME))("report")
    for k, v in summarize(videos, tracks, "MIGRATION STATE"):
        print(f"  {k}: {v}")
    print(f"Report: {path}")
    return 0


def cmd_report(args):
    store = st.StateStore.open_existing(args.state_db)
    if not store:
        print("No migration state found yet.")
        return 1
    return _write_state_report(store, args)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m migration.folder1", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="read-only plan + report; creates nothing")
    mode.add_argument("--execute", action="store_true", help="really migrate (creates Mux assets)")
    mode.add_argument("--probe-video", metavar="VIMEO_ID", help="read-only audio/caption discovery for one video")
    mode.add_argument("--report", action="store_true", help="Excel report from saved migration state")
    mode.add_argument("--ensure-playback-ids", action="store_true",
                      help="add only the missing public/signed/drm playback IDs to the --only videos' existing assets")
    mode.add_argument("--sync-report", action="store_true",
                      help="record all Folder 1 videos/tracks in the state DB (read-only Vimeo, no Mux) and refresh the cumulative workbook")
    ap.add_argument("--confirm-folder", help=f"must equal {FOLDER_ID} with --execute")
    ap.add_argument("--only", action="append", metavar="VIMEO_ID", help="(--execute) restrict to these Vimeo IDs")
    ap.add_argument("--exclude", action="append", metavar="VIMEO_ID",
                    help="(--execute) never touch these Vimeo IDs (repeatable), e.g. videos parked for a recovery phase")
    ap.add_argument("--recover-errored", action="store_true",
                    help="(--execute, needs --only) when Mux holds an ERRORED track, POST a fresh one instead of giving up; never deletes")
    ap.add_argument("--limit", type=int, help="(--execute) only the first N videos (canary run)")
    ap.add_argument("--stop-after-failures", type=int, default=1,
                    help="(--execute) stop starting new videos after this many consecutive failed/incomplete videos (default 1)")
    ap.add_argument("--workers", type=int, default=1, help="(--execute) videos migrated in parallel (default 1)")
    ap.add_argument("--audio-host", choices=("server", "local"), default="server",
                    help="where Mux pulls dubbed audio from: the platform server upload endpoint (default) or a local file server published through an ngrok tunnel")
    ap.add_argument("--server-url", help="(--audio-host server) public base URL of the platform server, by HOSTNAME, e.g. https://videoplayer.icare.life")
    ap.add_argument("--no-sniff", action="store_true", help="(--dry-run) skip the SRT/VTT format check of captions")
    ap.add_argument("--state-db", default=os.environ.get("MIGRATION_DATABASE_URL", DEFAULT_STATE_DB_URL))
    ap.add_argument("--output", default=DEFAULT_OUTPUT_DIR)
    args = ap.parse_args(argv)
    setup_logging("dry_run" if args.dry_run else "probe" if args.probe_video else "execute" if args.execute else "report")
    if args.dry_run:
        return cmd_dry_run(args)
    if args.probe_video:
        return cmd_probe(args)
    if args.ensure_playback_ids:
        return cmd_ensure_playback_ids(args)
    if args.sync_report:
        return cmd_sync_report(args)
    if args.report:
        return cmd_report(args)
    return cmd_execute(args)


if __name__ == "__main__":
    sys.exit(main())

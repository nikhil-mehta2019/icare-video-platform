"""Resumable Folder 1 execution: one new Mux asset per Vimeo Video ID, then dubbed audio and
captions attached to that asset, each step persisted and verified against Mux.

Resume rules (nothing is created twice):
  * asset: DB row has mux_asset_id -> reuse. Else look the Vimeo ID up in Mux by passthrough
    (covers a crash between "asset created" and "DB saved"); create only if neither knows it.
  * tracks: VERIFIED -> skip. Else look for a Mux track with the same type+language+name
    (covers a crash between "track POSTed" and "DB saved") and adopt it; POST only if absent.
  * a track that Mux reports 'errored' is NOT re-POSTed (no deletes in this tool): it is left
    FAILED with a message for manual handling.
"""
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from migration import state as st
from migration.config import FOLDER_ID, EXPECTED_LANGUAGES, language_name
from migration.mux_client import (AssetIndex, MuxError, audio_track_payload, find_track, playback_fields,
                                  text_track_payload, verify_asset)
from migration.vimeo_source import build_video_plan, video_id_from_uri

logger = logging.getLogger("migration.folder1")


class StageFailure(Exception):
    def __init__(self, stage, message, track_type=None, language=None):
        super().__init__(message)
        self.stage, self.track_type, self.language = stage, track_type, language


class Migrator:
    def __init__(self, store, mux, vimeo, fetcher, host, workers=1, track_timeout=900, asset_timeout=3600,
                 folder_id=FOLDER_ID, checkpoint=None):
        self.recover_errored = False   # explicit opt-in: POST a fresh track when Mux holds an ERRORED one (never deletes)
        self.stop_file = None          # if this file appears, no NEW video is started (in-flight ones finish)
        self.checkpoint = checkpoint   # callable(reason) -> refreshes the cumulative workbook; must never raise
        self.store, self.mux, self.vimeo, self.fetcher, self.host = store, mux, vimeo, fetcher, host
        self.workers, self.track_timeout, self.asset_timeout, self.folder_id = workers, track_timeout, asset_timeout, folder_id
        self.index = AssetIndex(mux)
        self._index_lock = threading.Lock()

    # ------------------------------------------------------------------ driver
    def run(self, items, only=None, limit=None, max_consecutive_failures=3, exclude=None):
        """items: [{"video": {...}, "folder_name": str}] from the Vimeo folder listing."""
        todo = [i for i in items if not only or video_id_from_uri(i["video"]["uri"]) in only]
        todo = [i for i in todo if video_id_from_uri(i["video"]["uri"]) not in (exclude or ())]   # never touched at all
        if limit:
            todo = todo[:limit]
        results, streak = {}, [0]
        stop = threading.Event()

        def one(item):
            vid = video_id_from_uri(item["video"]["uri"])
            if stop.is_set() or (self.stop_file and os.path.exists(self.stop_file)):
                results[vid] = "SKIPPED_CIRCUIT_BREAKER"
                return
            outcome = self.migrate_one(item)
            results[vid] = outcome
            streak[0] = streak[0] + 1 if outcome in (st.FAILED, st.INCOMPLETE) else 0
            if max_consecutive_failures and streak[0] >= max_consecutive_failures:
                logger.error("%d consecutive failures - stopping the run (rerun --execute resumes).", streak[0])
                stop.set()

        if self.workers > 1:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                list(pool.map(one, todo))
        else:
            for it in todo:
                one(it)
        return results

    # ----------------------------------------------------------------- one video
    def _cp(self, reason):
        if self.checkpoint:
            try:
                self.checkpoint(reason)
            except Exception:   # a report problem must never affect the migration
                logger.exception("workbook checkpoint failed (%s); migration continues", reason)

    def migrate_one(self, item) -> str:
        outcome = self._migrate_one(item)
        self._cp(f"{video_id_from_uri(item['video']['uri'])} {outcome}")
        return outcome

    def _migrate_one(self, item) -> str:
        video = item["video"]
        vid = video_id_from_uri(video["uri"])
        existing = self.store.get_video(vid)
        if existing and existing["status"] == "COMPLETED":
            logger.info("[%s] already COMPLETED - skipping", vid)
            return st.COMPLETED
        try:
            fresh = self.vimeo.get_video(vid)   # fresh signed links; list-time links can expire
            plan = build_video_plan(self.vimeo, fresh, item.get("folder_name", "Root"))
            rec = self.store.upsert_discovered(plan, self.folder_id)
            self.store.begin_attempt(vid)
            logger.info("[%s] %s | status=%s audio=%d/%d captions=%d/%d", vid, plan.info.title, rec["status"],
                        len(plan.audio.tracks), len(EXPECTED_LANGUAGES), len(plan.captions), len(EXPECTED_LANGUAGES))
            self._ensure_asset(vid, plan)
            self._ensure_policies(vid)
            self._ensure_ready(vid)
            self._stage_audio(vid, plan)
            self._stage_captions(vid, plan)
            return self._finalize(vid)
        except StageFailure as exc:
            logger.error("[%s] FAILED at %s: %s", vid, exc.stage, exc)
            self.store.fail(vid, exc.stage, str(exc), exc.track_type, exc.language)
            return st.FAILED
        except Exception as exc:   # per-video isolation: one bad video never stops the batch
            logger.exception("[%s] unexpected error", vid)
            if self.store.get_video(vid):
                self.store.fail(vid, "UNEXPECTED", f"{type(exc).__name__}: {exc}")
            return st.FAILED

    # -------------------------------------------------------------- asset stages
    def _ensure_asset(self, vid, plan):
        rec = self.store.get_video(vid)
        if rec["mux_asset_id"]:
            return
        if plan.download is None:
            raise StageFailure("CREATING_ASSET", "no downloadable video file on Vimeo")
        pt = rec["passthrough"]
        self.store.advance(vid, "CREATING_ASSET")
        with self._index_lock:
            found = self.index.find(pt)
        if len(found) > 1:
            raise StageFailure("CREATING_ASSET", f"{len(found)} Mux assets already carry passthrough {pt}; resolve manually")
        if found:
            asset = found[0]
            logger.warning("[%s] adopting existing Mux asset %s found by passthrough (crash recovery)", vid, asset["id"])
        else:
            try:
                asset = self.mux.create_asset(plan.download["link"], plan.info.title, pt)
            except MuxError as exc:
                raise StageFailure("CREATING_ASSET", str(exc))
            with self._index_lock:
                self.index.add(asset)
            logger.info("[%s] created Mux asset %s", vid, asset["id"])
        self.store.advance(vid, "ASSET_CREATED", mux_asset_id=asset["id"], mux_asset_status=asset.get("status"),
                           **playback_fields(asset))
        self.store.set_track_asset(vid, asset["id"])

    def _ensure_ready(self, vid):
        rec = self.store.get_video(vid)
        if st.state_index(rec["status"]) >= st.state_index("ASSET_READY"):
            return
        try:
            asset = self.mux.wait_for_asset(rec["mux_asset_id"], timeout=self.asset_timeout)
        except MuxError as exc:
            raise StageFailure("ASSET_READY", str(exc))
        fields = playback_fields(asset)
        missing = [pol for pol, key in (("public", "mux_public_playback_id"), ("signed", "mux_signed_playback_id"),
                                        ("drm", "mux_drm_playback_id")) if not fields[key]]
        if missing:
            raise StageFailure("ASSET_READY", f"asset is ready but is missing playback ID(s): {', '.join(missing)}")
        self.store.advance(vid, "ASSET_READY", mux_asset_status="ready", **fields)

    def _ensure_policies(self, vid):
        """Resumed assets created before all three policies were requested: add ONLY the missing ones."""
        rec = self.store.get_video(vid)
        asset = self.mux.get_asset(rec["mux_asset_id"])
        have = {p.get("policy") for p in asset.get("playback_ids") or []}
        if {"public", "signed", "drm"} <= have:
            return
        try:
            asset, created = self.mux.ensure_playback_ids(rec["mux_asset_id"])
        except MuxError as exc:
            raise StageFailure("PLAYBACK_IDS", str(exc))
        logger.warning("[%s] added missing playback ID policies %s to existing asset %s", vid, created, rec["mux_asset_id"])
        self.store.set_video_fields(vid, **playback_fields(asset))

    # -------------------------------------------------------------- track stages
    def _pending(self, vid, ttype):
        return [t for t in self.store.list_tracks(vid, ttype) if t["status"] not in (st.T_VERIFIED, st.T_MISSING)]

    def _rewind_if_needed(self, vid, target):
        rec = self.store.get_video(vid)
        if st.state_index(rec["status"]) > st.state_index(target):
            self.store.rewind(vid, target)

    def _stage_audio(self, vid, plan):
        pending = self._pending(vid, "audio")
        rec = self.store.get_video(vid)
        if not pending and st.state_index(rec["status"]) >= st.state_index("AUDIO_VERIFIED"):
            return
        if pending:
            self._rewind_if_needed(vid, "ASSET_READY")
            self.store.advance(vid, "AUDIO_ATTACHING")
        failures = []
        for row in pending:
            tinfo = plan.audio.tracks.get(row["language"])
            err = self._attach_audio(vid, rec["mux_asset_id"], row, tinfo)
            if err:
                failures.append((row["language"], err))
                break   # fail fast: do not continue blindly; the rest stay PENDING for the next run
        if failures:
            lang, err = failures[0]
            raise StageFailure("AUDIO_ATTACHING", f"audio track {lang} failed (remaining tracks left pending): {err}", "audio", lang)
        self._verify_stage(vid, "audio", "AUDIO_VERIFIED")

    def _stage_captions(self, vid, plan):
        pending = self._pending(vid, "caption")
        rec = self.store.get_video(vid)
        if not pending and st.state_index(rec["status"]) >= st.state_index("CAPTIONS_VERIFIED"):
            return
        if pending:
            self._rewind_if_needed(vid, "AUDIO_VERIFIED")
        failures = []
        for row in pending:
            cinfo = plan.captions.get(row["language"])
            err = self._attach_caption(vid, rec["mux_asset_id"], row, cinfo)
            if err:
                failures.append((row["language"], err))
                break
        if failures:
            lang, err = failures[0]
            raise StageFailure("CAPTIONS_ATTACHED", f"caption track {lang} failed (remaining tracks left pending): {err}",
                               "caption", lang)
        self.store.advance(vid, "CAPTIONS_ATTACHED")
        self._verify_stage(vid, "caption", "CAPTIONS_VERIFIED")

    def _attach_audio(self, vid, asset_id, row, tinfo):
        lang, name = row["language"], row["track_name"]
        if tinfo is None:
            self.store.update_track(vid, "audio", lang, status=st.T_FAILED, error="audio rendition no longer listed by Vimeo")
            self.store.record_error(vid, "AUDIO_ATTACHING", "audio rendition no longer listed by Vimeo", "audio", lang,
                                    "RETRYABLE")
            return "rendition missing on Vimeo"

        def post(url):
            return self.mux.add_track(asset_id, audio_track_payload(url, row["language_code"], name))

        def prepare():
            self.store.update_track(vid, "audio", lang, status=st.T_DOWNLOADING, error=None)
            path = self.fetcher.download(vid, tinfo)
            url, hosted = self.host.publish(path)
            return url, (path, hosted)

        def cleanup(handle):
            path, hosted = handle
            self.host.release(hosted)
            try:
                os.remove(path)
            except OSError:
                pass

        return self._attach(vid, asset_id, "audio", "audio", row, prepare, post, cleanup)

    def _attach_caption(self, vid, asset_id, row, cinfo):
        lang, name = row["language"], row["track_name"]
        if cinfo is None or not cinfo.source_url:
            self.store.update_track(vid, "caption", lang, status=st.T_FAILED, error="caption no longer available on Vimeo")
            self.store.record_error(vid, "CAPTIONS_ATTACHED", "caption no longer available on Vimeo", "caption", lang,
                                    "RETRYABLE")
            return "caption missing on Vimeo"
        self.store.update_track(vid, "caption", lang, source_format=cinfo.source_format)

        def prepare():
            return cinfo.source_url, None

        def post(url):
            return self.mux.add_track(asset_id, text_track_payload(url, row["language_code"], name))

        return self._attach(vid, asset_id, "caption", "text", row, prepare, post, lambda h: None)

    def _attach(self, vid, asset_id, ttype, mux_type, row, prepare, post, cleanup):
        """Shared adopt-or-post-then-verify flow. Returns None on success, else an error string."""
        lang, name, stage = row["language"], row["track_name"], ("AUDIO_ATTACHING" if ttype == "audio" else "CAPTIONS_ATTACHED")
        handle = None
        try:
            asset = self.mux.get_asset(asset_id)
            track = find_track(asset, mux_type, row["language_code"], name)
            if track is not None and track.get("status") == "errored" and self.recover_errored:
                logger.warning("[%s] %s %s: Mux holds an errored track (%s); attempting a fresh track (nothing is deleted)",
                               vid, ttype, lang, track["id"])
                track = None
            if track is None:
                url, handle = prepare()
                track = post(url)
                logger.info("[%s] %s %s attached (track %s)", vid, ttype, lang, track["id"])
            else:
                logger.info("[%s] %s %s already on Mux (track %s) - adopting", vid, ttype, lang, track["id"])
            self.store.update_track(vid, ttype, lang, mux_track_id=track["id"], mux_asset_id=asset_id,
                                    status=st.T_ATTACHED, error=None)
            final = track if track.get("status") == "ready" else self.mux.wait_for_track(asset_id, track["id"], self.track_timeout)
            if final.get("status") == "ready":
                self.store.update_track(vid, ttype, lang, status=st.T_VERIFIED, error=None)
                return None
            msg = f"Mux track is '{final.get('status')}'" + (f": {final.get('error')}" if final.get("error") else "")
            self.store.update_track(vid, ttype, lang, status=st.T_FAILED, error=msg)
            self.store.record_error(vid, stage, msg, ttype, lang, "NOT AUTO-RETRIED if Mux errored the track (no deletes)")
            return msg
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            self.store.update_track(vid, ttype, lang, status=st.T_FAILED, error=msg)
            self.store.record_error(vid, stage, msg, ttype, lang, "RETRYABLE")
            return msg
        finally:
            if handle is not None:
                cleanup(handle)

    def _verify_stage(self, vid, ttype, new_state):
        rec = self.store.get_video(vid)
        rows = [t for t in self.store.list_tracks(vid, ttype) if t["status"] == st.T_VERIFIED]
        asset = self.mux.get_asset(rec["mux_asset_id"])
        expected = [(t["language_code"], t["track_name"]) for t in rows]
        problems = verify_asset(asset, expected if ttype == "audio" else [], expected if ttype == "caption" else [],
                                required_policies=())
        problems = [p for p in problems if not p.startswith("asset status")]   # asset readiness is checked in _finalize
        if problems:
            raise StageFailure(new_state, "; ".join(problems), ttype)
        self.store.advance(vid, new_state)
        self._cp(f"{vid} {new_state}")

    # ----------------------------------------------------------------- finalize
    def _finalize(self, vid):
        rec = self.store.get_video(vid)
        tracks = self.store.list_tracks(vid)
        verified = [t for t in tracks if t["status"] == st.T_VERIFIED]
        asset = self.mux.get_asset(rec["mux_asset_id"])
        problems = verify_asset(
            asset,
            [(t["language_code"], t["track_name"]) for t in verified if t["track_type"] == "audio"],
            [(t["language_code"], t["track_name"]) for t in verified if t["track_type"] == "caption"])
        if problems:
            raise StageFailure("DRM_VERIFIED", "; ".join(problems))
        self.store.advance(vid, "DRM_VERIFIED", mux_asset_status=asset.get("status"))
        self.store.set_video_fields(vid, **playback_fields(asset))   # fills blanks only; never overwrites stored IDs
        gaps = [t for t in tracks if t["status"] != st.T_VERIFIED]
        if gaps:
            desc = ", ".join(f"{t['track_type']}:{t['language']}({t['status']})" for t in gaps)
            self.store.finish(vid, st.INCOMPLETE, f"asset verified but tracks missing/failed: {desc}", "TRACK_GAPS")
            logger.warning("[%s] INCOMPLETE: %s", vid, desc)
            return st.INCOMPLETE
        self.store.advance(vid, "COMPLETED")
        logger.info("[%s] COMPLETED (asset %s)", vid, rec["mux_asset_id"])
        return st.COMPLETED

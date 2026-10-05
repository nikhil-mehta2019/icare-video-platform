"""Persistent migration state (video level + track level), keyed by Vimeo Video ID.

Uses its own declarative Base and database (default: a local SQLite file), so the
production app's create_all never creates these tables and the production `videos`
table is never read or written by this migration.
"""
import json
import os
import threading
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from migration.config import EXPECTED_LANGUAGES, language_name, passthrough_for, redact

Base = declarative_base()

STATES = ["DISCOVERED", "CREATING_ASSET", "ASSET_CREATED", "ASSET_READY", "AUDIO_ATTACHING", "AUDIO_VERIFIED",
          "CAPTIONS_ATTACHED", "CAPTIONS_VERIFIED", "DRM_VERIFIED", "COMPLETED"]
IN_PROGRESS, FAILED, INCOMPLETE, COMPLETED = "IN_PROGRESS", "FAILED", "INCOMPLETE", "COMPLETED"

T_PENDING, T_DOWNLOADING, T_ATTACHED, T_VERIFIED, T_FAILED, T_MISSING = (
    "PENDING", "DOWNLOADING", "ATTACHED", "VERIFIED", "FAILED", "MISSING_ON_VIMEO")


class InvalidTransition(Exception):
    pass


def state_index(state: str) -> int:
    return STATES.index(state)


def utcnow() -> datetime:
    return datetime.utcnow()


class F1Video(Base):
    __tablename__ = "f1_videos"
    id = Column(Integer, primary_key=True, autoincrement=True)
    vimeo_id = Column(String(50), unique=True, index=True, nullable=False)
    folder_id = Column(String(50))
    title = Column(String(500))
    vimeo_url = Column(String(2000))
    duration = Column(Integer)
    vimeo_created_at = Column(String(40))
    vimeo_modified_at = Column(String(40))
    status = Column(String(30), default="DISCOVERED", nullable=False)
    outcome = Column(String(20), default=IN_PROGRESS, nullable=False)
    passthrough = Column(String(255))
    mux_asset_id = Column(String(100))
    mux_asset_status = Column(String(30))
    mux_playback_id = Column(String(100))       # legacy (first playback ID); superseded by the three columns below
    mux_playback_policy = Column(String(20))    # legacy
    mux_public_playback_id = Column(String(100))
    mux_drm_playback_id = Column(String(100))
    mux_signed_playback_id = Column(String(100))
    download_info = Column(String(200))
    attempts = Column(Integer, default=0)
    error = Column(Text)
    error_stage = Column(String(40))
    notes = Column(Text)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class F1Track(Base):
    __tablename__ = "f1_tracks"
    __table_args__ = (UniqueConstraint("source_video_id", "track_type", "language", name="uq_f1_track"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    source_video_id = Column(String(50), index=True, nullable=False)
    mux_asset_id = Column(String(100))
    track_type = Column(String(10), nullable=False)      # 'audio' | 'caption'
    language = Column(String(20), nullable=False)        # base language (en, fr, ...)
    language_code = Column(String(20))                   # code sent to Mux
    track_name = Column(String(100))
    source = Column(String(300))                         # e.g. vimeo-hls-audio:<rendition> / vimeo-texttrack:<id>
    source_info = Column(String(300))                    # Vimeo audio/caption details
    source_format = Column(String(20))
    mux_track_id = Column(String(100))
    status = Column(String(20), default=T_PENDING, nullable=False)
    error = Column(Text)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class F1Error(Base):
    __tablename__ = "f1_errors"
    id = Column(Integer, primary_key=True, autoincrement=True)
    vimeo_id = Column(String(50), index=True)
    title = Column(String(500))
    stage = Column(String(40))
    track_type = Column(String(10))
    language = Column(String(20))
    error = Column(Text)
    retry_status = Column(String(60))
    created_at = Column(DateTime, default=utcnow)


def _row(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


class StateStore:
    def __init__(self, url: str, create: bool = True):
        self.url = url
        kw = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, **kw)
        if create:
            Base.metadata.create_all(self.engine)
        self._upgrade_schema()
        self.Session = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self._lock = threading.Lock()   # SQLite serialises writers; keep read-modify-write atomic

    def _upgrade_schema(self):
        """Additive only: older state files lack mux_public_playback_id."""
        from sqlalchemy import inspect, text
        insp = inspect(self.engine)
        if "f1_videos" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("f1_videos")}
            if "mux_public_playback_id" not in cols:
                with self.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE f1_videos ADD COLUMN mux_public_playback_id VARCHAR(100)"))

    @classmethod
    def open_existing(cls, url: str):
        """Read-only helper for dry runs: returns None (and creates nothing) if there is no state yet."""
        if url.startswith("sqlite:///"):
            path = url[len("sqlite:///"):]
            if not os.path.exists(path):
                return None
        store = cls(url, create=False)
        try:
            store.list_videos()
        except Exception:
            return None
        return store

    # ---------------------------------------------------------------- reads
    def get_video(self, vimeo_id):
        with self.Session() as s:
            v = s.query(F1Video).filter_by(vimeo_id=vimeo_id).first()
            return _row(v) if v else None

    def list_videos(self):
        with self.Session() as s:
            return [_row(v) for v in s.query(F1Video).order_by(F1Video.title, F1Video.vimeo_id).all()]

    def list_tracks(self, vimeo_id=None, track_type=None):
        with self.Session() as s:
            q = s.query(F1Track)
            if vimeo_id:
                q = q.filter_by(source_video_id=vimeo_id)
            if track_type:
                q = q.filter_by(track_type=track_type)
            return [_row(t) for t in q.order_by(F1Track.source_video_id, F1Track.track_type, F1Track.language).all()]

    def list_errors(self):
        with self.Session() as s:
            return [_row(e) for e in s.query(F1Error).order_by(F1Error.id).all()]

    # --------------------------------------------------------------- writes
    def upsert_discovered(self, plan, folder_id: str) -> dict:
        """Create the video + expected track rows, or refresh descriptive fields of existing ones.
        Never resets status, Mux IDs or verified tracks."""
        info = plan.info
        notes = "; ".join(plan.notes) if plan.notes else None
        dl = plan.download
        dl_text = f"{dl['source']}:{dl.get('rendition')} {dl.get('width')}x{dl.get('height')}" if dl else None
        with self._lock, self.Session() as s:
            v = s.query(F1Video).filter_by(vimeo_id=info.vimeo_id).first()
            if v is None:
                v = F1Video(vimeo_id=info.vimeo_id, folder_id=folder_id, status="DISCOVERED", outcome=IN_PROGRESS,
                            passthrough=passthrough_for(info.vimeo_id))
                s.add(v)
            v.title, v.vimeo_url, v.duration = info.title, info.url, info.duration
            v.vimeo_created_at, v.vimeo_modified_at = info.created_time, info.modified_time
            v.notes, v.download_info = notes, dl_text
            self._sync_tracks(s, v, plan)
            s.commit()
            return _row(v)

    @staticmethod
    def _sync_tracks(s, v, plan):
        existing = {(t.track_type, t.language): t for t in
                    s.query(F1Track).filter_by(source_video_id=v.vimeo_id).all()}
        for ttype, found in (("audio", plan.audio.tracks), ("caption", plan.captions)):
            for lang in EXPECTED_LANGUAGES:
                row = existing.get((ttype, lang))
                src = found.get(lang)
                if row is None:
                    row = F1Track(source_video_id=v.vimeo_id, track_type=ttype, language=lang)
                    s.add(row)
                if row.status == T_VERIFIED:
                    continue
                if src is None:
                    if row.status in (None, T_PENDING, T_MISSING, T_FAILED) and not row.mux_track_id:
                        row.status = T_MISSING
                        row.error = f"no {language_name(lang)} {ttype} track found on Vimeo"
                    continue
                if row.status in (None, T_MISSING):
                    row.status, row.error = T_PENDING, None
                row.language_code = src.language
                row.track_name = src.name
                if ttype == "audio":
                    row.source = f"vimeo-hls-audio:{src.rendition_key}"
                    row.source_info = (f"NAME={src.vimeo_name};LANGUAGE={src.language};CHANNELS={src.channels};"
                                       f"dubbed={src.dubbed}")
                    row.source_format = "m4a/mp4 (HLS audio rendition via yt-dlp)"
                else:
                    row.source = f"vimeo-texttrack:{src.vimeo_track_id}"
                    row.source_info = f"type={src.vimeo_type};file={src.source_name}"
                    row.source_format = src.source_format

    def begin_attempt(self, vimeo_id):
        with self._lock, self.Session() as s:
            v = s.query(F1Video).filter_by(vimeo_id=vimeo_id).one()
            v.attempts = (v.attempts or 0) + 1
            if v.outcome in (FAILED, INCOMPLETE):
                v.outcome = IN_PROGRESS
            s.commit()

    def advance(self, vimeo_id, new_state, **fields) -> dict:
        if new_state not in STATES:
            raise InvalidTransition(f"unknown state {new_state}")
        with self._lock, self.Session() as s:
            v = s.query(F1Video).filter_by(vimeo_id=vimeo_id).one()
            if state_index(new_state) < state_index(v.status):
                raise InvalidTransition(f"{vimeo_id}: cannot move backwards {v.status} -> {new_state}")
            v.status = new_state
            v.outcome = IN_PROGRESS if new_state != "COMPLETED" else COMPLETED
            v.error = v.error_stage = None
            if new_state == "CREATING_ASSET" and v.started_at is None:
                v.started_at = utcnow()
            if new_state == "COMPLETED":
                v.completed_at = utcnow()
            for k, val in fields.items():
                setattr(v, k, val)
            s.commit()
            return _row(v)

    def set_video_fields(self, vimeo_id, **fields):
        """Fill playback-ID fields without touching status/progress. A non-empty stored value is NEVER
        overwritten; a differing value from Mux is returned as a conflict instead. -> (changed, conflicts)"""
        changed, conflicts = {}, {}
        with self._lock, self.Session() as s:
            v = s.query(F1Video).filter_by(vimeo_id=vimeo_id).one()
            for k, val in fields.items():
                cur = getattr(v, k)
                if cur and val and cur != val:
                    conflicts[k] = (cur, val)
                elif val and not cur:
                    setattr(v, k, val)
                    changed[k] = val
            s.commit()
        return changed, conflicts

    def rewind(self, vimeo_id, to_state):
        """Explicit re-entry for a video whose asset exists but has tracks still to do."""
        with self._lock, self.Session() as s:
            v = s.query(F1Video).filter_by(vimeo_id=vimeo_id).one()
            if not v.mux_asset_id or state_index(to_state) < state_index("ASSET_READY"):
                raise InvalidTransition("rewind is only allowed back to ASSET_READY or later, on an existing asset")
            v.status, v.outcome, v.completed_at = to_state, IN_PROGRESS, None
            s.commit()

    def finish(self, vimeo_id, outcome, error=None, stage=None):
        with self._lock, self.Session() as s:
            v = s.query(F1Video).filter_by(vimeo_id=vimeo_id).one()
            v.outcome = outcome
            v.error = redact(error)[:4000] if error else None
            v.error_stage = stage
            s.commit()

    def fail(self, vimeo_id, stage, message, track_type=None, language=None):
        msg = redact(message)
        self.finish(vimeo_id, FAILED, msg, stage)
        self.record_error(vimeo_id, stage, msg, track_type, language, "RETRYABLE: rerun --execute resumes here")

    def record_error(self, vimeo_id, stage, message, track_type=None, language=None, retry_status=None):
        with self._lock, self.Session() as s:
            v = s.query(F1Video).filter_by(vimeo_id=vimeo_id).first()
            s.add(F1Error(vimeo_id=vimeo_id, title=v.title if v else None, stage=stage, track_type=track_type,
                          language=language, error=redact(message)[:4000], retry_status=retry_status))
            s.commit()

    def update_track(self, vimeo_id, track_type, language, **fields) -> dict:
        if "error" in fields and fields["error"]:
            fields["error"] = redact(fields["error"])[:4000]
        with self._lock, self.Session() as s:
            t = s.query(F1Track).filter_by(source_video_id=vimeo_id, track_type=track_type, language=language).one()
            for k, val in fields.items():
                setattr(t, k, val)
            s.commit()
            return _row(t)

    def set_track_asset(self, vimeo_id, mux_asset_id):
        with self._lock, self.Session() as s:
            for t in s.query(F1Track).filter_by(source_video_id=vimeo_id).all():
                t.mux_asset_id = mux_asset_id
            s.commit()

    def dump_json(self) -> str:
        return json.dumps({"videos": self.list_videos(), "tracks": self.list_tracks()}, default=str, indent=1)

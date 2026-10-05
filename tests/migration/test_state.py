import pytest

from migration import state as st
from migration.vimeo_source import build_video_plan
from tests.migration.fakes import FakeVimeo


@pytest.fixture
def store():
    return st.StateStore("sqlite:///:memory:")


def plan(**kw):
    api = FakeVimeo(**kw)
    return build_video_plan(api, api.videos["111"])


def test_upsert_creates_video_and_twelve_track_rows(store):
    rec = store.upsert_discovered(plan(), "25228807")
    assert rec["status"] == "DISCOVERED" and rec["passthrough"] == "vimeo:111"
    tracks = store.list_tracks("111")
    assert len(tracks) == 12 and {t["status"] for t in tracks} == {st.T_PENDING}


def test_missing_tracks_are_recorded_not_skipped(store):
    store.upsert_discovered(plan(audio_langs=("en", "fr"), caption_langs=("en",)), "f")
    missing = [t for t in store.list_tracks("111") if t["status"] == st.T_MISSING]
    assert len(missing) == 4 + 5 and all("found on Vimeo" in t["error"] for t in missing)


def test_forward_only_transitions(store):
    store.upsert_discovered(plan(), "f")
    store.advance("111", "CREATING_ASSET")
    store.advance("111", "ASSET_CREATED", mux_asset_id="a1")
    with pytest.raises(st.InvalidTransition):
        store.advance("111", "DISCOVERED")
    assert store.get_video("111")["started_at"] is not None


def test_full_state_sequence_reaches_completed(store):
    store.upsert_discovered(plan(), "f")
    for s in st.STATES[1:]:
        store.advance("111", s, **({"mux_asset_id": "a1"} if s == "ASSET_CREATED" else {}))
    v = store.get_video("111")
    assert v["status"] == "COMPLETED" and v["outcome"] == st.COMPLETED and v["completed_at"]


def test_upsert_never_resets_progress_or_verified_tracks(store):
    store.upsert_discovered(plan(), "f")
    store.advance("111", "CREATING_ASSET")
    store.advance("111", "ASSET_CREATED", mux_asset_id="a1")
    store.update_track("111", "audio", "fr", status=st.T_VERIFIED, mux_track_id="t1")
    rec = store.upsert_discovered(plan(audio_langs=("en",)), "f")   # fr vanished from Vimeo
    assert rec["status"] == "ASSET_CREATED" and rec["mux_asset_id"] == "a1"
    fr = [t for t in store.list_tracks("111", "audio") if t["language"] == "fr"][0]
    assert fr["status"] == st.T_VERIFIED and fr["mux_track_id"] == "t1"


def test_missing_track_becomes_pending_when_it_reappears(store):
    store.upsert_discovered(plan(audio_langs=("en",)), "f")
    store.upsert_discovered(plan(), "f")
    assert all(t["status"] == st.T_PENDING for t in store.list_tracks("111", "audio"))


def test_fail_records_error_row_and_redacts_urls(store):
    store.upsert_discovered(plan(), "f")
    store.fail("111", "AUDIO_ATTACHING", "bad https://x.example/a?sig=SECRET", "audio", "fr")
    v = store.get_video("111")
    assert v["outcome"] == st.FAILED and "SECRET" not in v["error"]
    assert store.list_errors()[0]["stage"] == "AUDIO_ATTACHING"


def test_rewind_requires_existing_asset(store):
    store.upsert_discovered(plan(), "f")
    with pytest.raises(st.InvalidTransition):
        store.rewind("111", "ASSET_READY")


def test_old_state_file_is_upgraded_without_losing_data(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    s = st.StateStore(f"sqlite:///{path.as_posix()}")
    s.upsert_discovered(plan(), "f")
    s.advance("111", "CREATING_ASSET")
    s.advance("111", "ASSET_CREATED", mux_asset_id="a1", mux_drm_playback_id="drm1")
    s.engine.dispose()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE f1_videos DROP COLUMN mux_public_playback_id")
    con.commit()
    con.close()
    s2 = st.StateStore.open_existing(f"sqlite:///{path.as_posix()}")
    v = s2.get_video("111")
    assert v["mux_drm_playback_id"] == "drm1" and v["mux_public_playback_id"] is None and v["status"] == "ASSET_CREATED"


def test_set_video_fields_fills_blanks_but_never_overwrites(store):
    store.upsert_discovered(plan(), "f")
    store.advance("111", "CREATING_ASSET")
    store.advance("111", "ASSET_CREATED", mux_asset_id="a1", mux_drm_playback_id="drm1")
    changed, conflicts = store.set_video_fields("111", mux_drm_playback_id="OTHER", mux_public_playback_id="pub1")
    v = store.get_video("111")
    assert v["mux_drm_playback_id"] == "drm1" and v["mux_public_playback_id"] == "pub1"
    assert changed == {"mux_public_playback_id": "pub1"} and conflicts == {"mux_drm_playback_id": ("drm1", "OTHER")}
    assert v["status"] == "ASSET_CREATED"

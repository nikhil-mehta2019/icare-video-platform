import pytest

from migration import state as st
from migration.mux_client import MuxClient
from migration.runner import Migrator
from tests.migration.fakes import FakeFetcher, FakeHost, FakeMux, FakeVimeo


@pytest.fixture
def env(tmp_path):
    class E:
        store = st.StateStore("sqlite:///:memory:")
        mux = FakeMux()
        vimeo = FakeVimeo()
        fetcher = FakeFetcher(tmp_path)
        host = FakeHost()

    return E()


def migrator(e, mux=None):
    return Migrator(e.store, mux or e.mux, e.vimeo, e.fetcher, e.host)


def test_happy_path_creates_one_asset_with_six_audio_and_six_caption_tracks(env):
    res = migrator(env).run(env.vimeo.items())
    assert res == {"111": st.COMPLETED}
    assert env.mux.create_calls == 1
    assert sorted(l for _, t, l in env.mux.track_posts if t == "audio") == ["en", "es", "fr", "it", "pt", "ro"]
    assert sorted(l for _, t, l in env.mux.track_posts if t == "text") == ["en", "es", "fr", "it", "pt", "ro"]
    v = env.store.get_video("111")
    assert v["status"] == "COMPLETED"
    ids = (v["mux_public_playback_id"], v["mux_signed_playback_id"], v["mux_drm_playback_id"])
    assert all(ids) and len(set(ids)) == 3          # three distinct playback IDs, one per policy
    assert env.mux.pb_posts == []                   # created with all policies at asset creation; nothing added later
    assert all(t["status"] == st.T_VERIFIED and t["mux_track_id"] for t in env.store.list_tracks("111"))
    # audio files are hosted only until Mux has the track, then released and removed locally
    assert sorted(env.host.published) == sorted(env.host.released) and len(env.host.published) == 6


def test_rerun_after_completion_creates_nothing(env):
    migrator(env).run(env.vimeo.items())
    posts = list(env.mux.track_posts)
    res = migrator(env).run(env.vimeo.items())
    assert res == {"111": st.COMPLETED} and env.mux.create_calls == 1 and env.mux.track_posts == posts


def test_crash_between_asset_create_and_db_save_adopts_asset_by_passthrough(env):
    # state: DB says CREATING_ASSET but never saved the asset id; Mux already has the asset
    plan_api = FakeVimeo()
    from migration.vimeo_source import build_video_plan
    env.store.upsert_discovered(build_video_plan(plan_api, plan_api.videos["111"]), "f")
    env.store.advance("111", "CREATING_ASSET")
    orphan = env.mux.create_asset("u", "Video", "vimeo:111")
    env.mux.create_calls = 0
    res = migrator(env).run(env.vimeo.items())
    assert res == {"111": st.COMPLETED}
    assert env.mux.create_calls == 0
    assert env.store.get_video("111")["mux_asset_id"] == orphan["id"]


def test_two_mux_assets_with_same_passthrough_fail_instead_of_guessing(env):
    env.mux.create_asset("u", "a", "vimeo:111")
    env.mux.create_asset("u", "b", "vimeo:111")
    env.mux.create_calls = 0
    res = migrator(env).run(env.vimeo.items())
    assert res == {"111": st.FAILED} and env.mux.create_calls == 0


def test_existing_mux_track_is_adopted_not_duplicated(env):
    m = migrator(env)
    env.mux.fail_track = {"ro"}
    assert m.run(env.vimeo.items()) == {"111": st.FAILED}
    first_posts = len(env.mux.track_posts)
    # crash window: the Mux track exists but the DB never recorded it
    asset = next(iter(env.mux.assets.values()))
    env.store.update_track("111", "audio", "fr", status=st.T_PENDING, mux_track_id=None)
    env.mux.fail_track = set()
    assert m.run(env.vimeo.items()) == {"111": st.COMPLETED}
    posts = env.mux.track_posts[first_posts:]
    assert ("audio", "fr") not in [(t, l) for _, t, l in posts]            # adopted, not re-posted
    assert [l for _, t, l in posts if t == "audio"] == ["ro"]              # only the failed track retried
    assert env.mux.create_calls == 1
    fr = [t for t in env.store.list_tracks("111", "audio") if t["language"] == "fr"][0]
    assert fr["status"] == st.T_VERIFIED and fr["mux_track_id"]


def test_failed_audio_track_blocks_completion_and_is_retried_alone(env):
    env.mux.fail_track = {"it"}
    m = migrator(env)
    assert m.run(env.vimeo.items()) == {"111": st.FAILED}
    v = env.store.get_video("111")
    assert v["outcome"] == st.FAILED and v["error_stage"] == "AUDIO_ATTACHING" and v["status"] == "AUDIO_ATTACHING"
    assert not any(t == "text" for _, t, _ in env.mux.track_posts)       # captions wait until audio verified
    env.mux.fail_track = set()
    assert m.run(env.vimeo.items()) == {"111": st.COMPLETED}
    assert env.mux.create_calls == 1
    assert [l for _, t, l in env.mux.track_posts if t == "audio"].count("fr") == 1


def test_track_errored_by_mux_is_not_reposted(env):
    env.mux.error_track = {"es"}
    m = migrator(env)
    assert m.run(env.vimeo.items()) == {"111": st.FAILED}
    posts = len(env.mux.track_posts)
    assert m.run(env.vimeo.items()) == {"111": st.FAILED}
    assert len(env.mux.track_posts) == posts
    assert "errored" in env.store.get_video("111")["error"]


def test_missing_language_on_vimeo_makes_video_incomplete_never_completed(env):
    env.vimeo.audio_langs = ("en", "fr", "it", "pt", "es")
    assert migrator(env).run(env.vimeo.items()) == {"111": st.INCOMPLETE}
    v = env.store.get_video("111")
    assert v["status"] == "DRM_VERIFIED" and "audio:ro" in v["error"]


def test_incomplete_video_finishes_when_track_appears_later(env):
    env.vimeo.audio_langs = ("en", "fr", "it", "pt", "es")
    m = migrator(env)
    m.run(env.vimeo.items())
    env.vimeo.audio_langs = ("en", "fr", "it", "pt", "es", "ro")
    assert m.run(env.vimeo.items()) == {"111": st.COMPLETED}
    assert env.mux.create_calls == 1


def test_asset_without_drm_playback_id_fails(env):
    env.mux.drm = False
    assert migrator(env).run(env.vimeo.items()) == {"111": st.FAILED}
    assert "drm" in env.store.get_video("111")["error"].lower()
    assert env.mux.track_posts == []


def test_missing_drm_configuration_fails_without_creating_anything(env):
    class Sess:
        calls = 0

        def request(self, *a, **k):
            Sess.calls += 1

    mux = MuxClient("i", "s", "", session=Sess())
    mux.list_assets = lambda: []
    assert migrator(env, mux).run(env.vimeo.items()) == {"111": st.FAILED}
    assert Sess.calls == 0
    assert env.store.get_video("111")["mux_asset_id"] is None


def test_circuit_breaker_stops_after_consecutive_failures(env):
    env.vimeo.videos = {str(i): __import__("tests.migration.fakes", fromlist=["x"]).video_json(str(i)) for i in range(1, 6)}
    env.mux.drm = False
    res = migrator(env).run(env.vimeo.items(), max_consecutive_failures=2)
    assert list(res.values()).count(st.FAILED) == 2 and "SKIPPED_CIRCUIT_BREAKER" in res.values()
    assert env.mux.create_calls == 2


def test_three_workers_share_state_without_duplicates(tmp_path):
    from tests.migration.fakes import video_json
    store = st.StateStore(f"sqlite:///{(tmp_path / 's.db').as_posix()}")   # file DB: :memory: is per-thread
    vimeo = FakeVimeo(videos={str(i): video_json(str(i)) for i in range(1, 7)})
    mux, host = FakeMux(), FakeHost()
    res = Migrator(store, mux, vimeo, FakeFetcher(tmp_path), host, workers=3).run(vimeo.items())
    assert set(res.values()) == {st.COMPLETED} and len(res) == 6
    assert mux.create_calls == 6 and len({a["passthrough"] for a in mux.assets.values()}) == 6
    assert len(mux.track_posts) == 6 * 12 and len(set(mux.track_posts)) == 6 * 12   # no duplicate track on any asset
    assert len(host.published) == 36 and len(set(host.published)) == 36 and sorted(host.published) == sorted(host.released)


def test_resumed_asset_missing_policies_gets_only_the_missing_ones(env):
    from migration.vimeo_source import build_video_plan
    plan = build_video_plan(env.vimeo, env.vimeo.videos["111"])
    env.store.upsert_discovered(plan, "f")
    env.store.advance("111", "CREATING_ASSET")
    a = env.mux.create_asset("u", "Video", "vimeo:111")
    env.mux.assets[a["id"]]["playback_ids"] = [p for p in a["playback_ids"] if p["policy"] == "drm"]   # old drm-only asset
    drm_id = env.mux.assets[a["id"]]["playback_ids"][0]["id"]
    env.store.advance("111", "ASSET_CREATED", mux_asset_id=a["id"], mux_drm_playback_id=drm_id)
    env.mux.create_calls = 0
    assert migrator(env).run(env.vimeo.items()) == {"111": st.COMPLETED}
    assert sorted(p for _, p in env.mux.pb_posts) == ["public", "signed"] and env.mux.create_calls == 0
    v = env.store.get_video("111")
    assert v["mux_drm_playback_id"] == drm_id and v["mux_public_playback_id"] and v["mux_signed_playback_id"]
    assert migrator(env).run(env.vimeo.items()) == {"111": st.COMPLETED} and len(env.mux.pb_posts) == 2


def test_excluded_videos_are_never_touched_or_counted(env):
    from tests.migration.fakes import video_json
    env.vimeo.videos = {str(i): video_json(str(i)) for i in range(1, 4)}
    res = migrator(env).run(env.vimeo.items(), exclude={"2"})
    assert set(res) == {"1", "3"} and env.mux.create_calls == 2
    assert env.store.get_video("2") is None


def test_errored_track_is_only_replaced_when_recovery_is_explicitly_enabled(env):
    env.mux.error_track = {"es"}
    m = migrator(env)
    assert m.run(env.vimeo.items()) == {"111": st.FAILED}
    posts = len(env.mux.track_posts)
    assert m.run(env.vimeo.items()) == {"111": st.FAILED} and len(env.mux.track_posts) == posts    # default: never re-posted
    env.mux.error_track = set()
    m.recover_errored = True
    assert m.run(env.vimeo.items()) == {"111": st.COMPLETED}
    asset = next(iter(env.mux.assets.values()))
    es = [t for t in asset["tracks"] if t["type"] == "audio" and t.get("language_code") == "es"]
    assert sorted(t["status"] for t in es) == ["errored", "ready"]       # the errored one is left in place, not deleted
    assert env.store.get_video("111")["outcome"] == st.COMPLETED and env.mux.create_calls == 1
    assert m.run(env.vimeo.items()) == {"111": st.COMPLETED} and len(env.mux.track_posts) == posts + 11    # es retry + fr,it,pt,ro + 6 captions; nothing repeated


def test_mux_refusal_of_a_replacement_stops_that_video_with_the_exact_error(env):
    env.mux.error_track = {"es"}
    m = migrator(env)
    m.run(env.vimeo.items())
    env.mux.fail_track = {"es"}                     # Mux refuses the duplicate language+name
    m.recover_errored = True
    assert m.run(env.vimeo.items()) == {"111": st.FAILED}
    assert "boom" in env.store.get_video("111")["error"]

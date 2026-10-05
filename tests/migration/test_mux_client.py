import pytest

from migration.mux_client import (DrmConfigError, MuxClient, MuxError, build_asset_payload, find_track, playback_fields,
                                  verify_asset)


class R:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._d, self.headers, self.text = status, data or {}, headers or {}, str(data)

    def json(self):
        return self._d


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


def test_payload_requires_drm_and_uses_drm_policy_only():
    with pytest.raises(DrmConfigError):
        build_asset_payload("u", "t", "vimeo:1", "")
    with pytest.raises(DrmConfigError):
        build_asset_payload("u", "t", "vimeo:1", "   ")
    p = build_asset_payload("https://v", "Title", "vimeo:1", "drm1")
    assert p["advanced_playback_policies"] == [{"policy": "public"}, {"policy": "signed"},
                                               {"policy": "drm", "drm_configuration_id": "drm1"}]
    assert "playback_policy" not in p and p["passthrough"] == "vimeo:1"


def test_create_asset_without_drm_never_calls_mux():
    s = Session([])
    c = MuxClient("i", "s", "", session=s)
    with pytest.raises(DrmConfigError):
        c.create_asset("u", "t", "vimeo:1")
    assert s.calls == []


def test_drm_configuration_validation():
    ok = MuxClient("i", "s", "d1", session=Session([R(200, {"data": [{"id": "d0"}, {"id": "d1"}]})]))
    assert ok.check_drm_configuration()[0] is True
    bad = MuxClient("i", "s", "zzz", session=Session([R(200, {"data": [{"id": "d1"}]})]))
    assert bad.check_drm_configuration()[0] is False
    unk = MuxClient("i", "s", "d1", session=Session([R(403)] * 6), sleep=lambda s: None)
    assert unk.check_drm_configuration()[0] is None
    assert MuxClient("i", "s", "", session=Session([])).check_drm_configuration()[0] is False


def test_post_is_not_retried_on_5xx_but_is_on_429():
    s = Session([R(500, {})])
    with pytest.raises(MuxError):
        MuxClient("i", "s", "d", session=s, sleep=lambda x: None).create_asset("u", "t", "p")
    assert len(s.calls) == 1
    s2 = Session([R(429), R(201, {"data": {"id": "a1"}})])
    assert MuxClient("i", "s", "d", session=s2, sleep=lambda x: None).create_asset("u", "t", "p")["id"] == "a1"


def _asset(**kw):
    a = {"status": "ready", "playback_ids": [{"id": "p", "policy": "drm"}, {"id": "q", "policy": "public"},
                                             {"id": "r", "policy": "signed"}], "tracks": [
        {"type": "audio", "primary": True, "language_code": "en", "name": "English", "status": "ready"},
        {"type": "audio", "language_code": "fr", "name": "French", "status": "ready"},
        {"type": "text", "language_code": "fr", "name": "French", "status": "ready"}]}
    a.update(kw)
    return a


def test_verify_asset_ok_and_problems():
    assert verify_asset(_asset(), [("fr", "French")], [("fr", "French")]) == []
    probs = verify_asset(_asset(status="preparing", playback_ids=[{"id": "p", "policy": "public"}]),
                         [("es", "Spanish")], [("it", "Italian")])
    text = " | ".join(probs)
    assert "not 'ready'" in text and "no drm playback ID" in text and "no signed playback ID" in text and "audio track es" in text and "caption track it" in text


def test_primary_embedded_audio_never_matches_our_track():
    assert find_track(_asset(), "audio", "en", "English") is None


def test_playback_fields_keep_the_three_policies_separate():
    f = playback_fields({"playback_ids": [{"id": "d", "policy": "drm"}]})
    assert f == {"mux_public_playback_id": None, "mux_signed_playback_id": None, "mux_drm_playback_id": "d"}
    f = playback_fields({"playback_ids": [{"id": "d", "policy": "drm"}, {"id": "s", "policy": "signed"},
                                          {"id": "p", "policy": "public"}]})
    assert (f["mux_public_playback_id"], f["mux_signed_playback_id"], f["mux_drm_playback_id"]) == ("p", "s", "d")


def test_ensure_playback_ids_creates_only_missing_policies_and_never_duplicates():
    asset = {"id": "a1", "playback_ids": [{"id": "d", "policy": "drm"}]}
    s = Session([R(200, {"data": asset}), R(201, {"data": {}}), R(201, {"data": {}}),
                 R(200, {"data": {"id": "a1", "playback_ids": [{"policy": p} for p in ("drm", "public", "signed")]}})])
    c = MuxClient("i", "s", "drm1", session=s)
    _, created = c.ensure_playback_ids("a1")
    assert created == ["public", "signed"]
    posts = [(m, u.split("/assets/")[1], kw["json"]) for m, u, kw in s.calls if m == "POST"]
    assert posts == [("POST", "a1/playback-ids", {"policy": "public"}), ("POST", "a1/playback-ids", {"policy": "signed"})]
    full = {"id": "a1", "playback_ids": [{"id": "x", "policy": p} for p in ("drm", "public", "signed")]}
    s2 = Session([R(200, {"data": full})])
    assert MuxClient("i", "s", "drm1", session=s2).ensure_playback_ids("a1")[1] == []
    assert [m for m, *_ in s2.calls] == ["GET"]


def test_signing_key_check_and_signed_playback_proof_do_not_leak_the_key():
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from migration.mux_client import check_signing_key, verify_signed_playback

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = base64.b64encode(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption())).decode()
    ok, detail = check_signing_key(MuxClient("i", "s", "d", session=Session([R(200, {"data": {"id": "k"}})])), "kid1", priv)
    assert ok and priv not in detail
    assert check_signing_key(MuxClient("i", "s", "d", session=Session([R(404)])), "kid1", priv)[0] is False
    assert check_signing_key(MuxClient("i", "s", "d", session=Session([R(200)])), "kid1", "bm90LWEta2V5")[0] is False
    assert check_signing_key(MuxClient("i", "s", "d", session=Session([])), "", "")[0] is False

    class Web:
        def __init__(self, codes):
            self.codes, self.calls = list(codes), []

        def get(self, url, **kw):
            self.calls.append(kw)
            return R(self.codes.pop(0))

    w = Web([403, 200])
    ok, detail = verify_signed_playback("pb1", "kid1", priv, session=w)
    assert ok and "token" in w.calls[1]["params"] and priv not in detail and w.calls[1]["params"]["token"] not in detail
    assert verify_signed_playback("pb1", "kid1", priv, session=Web([200, 200]))[0] is False   # not actually signed
    assert verify_signed_playback("pb1", "kid1", priv, session=Web([403, 403]))[0] is False   # key does not match

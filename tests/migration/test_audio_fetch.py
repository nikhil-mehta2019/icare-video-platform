import json
import subprocess
from types import SimpleNamespace

import pytest

from migration.audio_fetch import AudioFetchError, YtDlpAudioFetcher, classify_failure, match_format_id

DNS = ("ERROR: [download] Got error: HTTPSConnection(host='vod-adaptive-ak.vimeocdn.com', port=443): Failed to resolve "
       "'vod-adaptive-ak.vimeocdn.com' ([Errno 11001] getaddrinfo failed). Giving up after 10 retries")


def proc(code=0, err="", out=""):
    return SimpleNamespace(returncode=code, stderr=err, stdout=out)


class Runner:
    def __init__(self, results):
        self.results, self.calls = list(results), 0

    def __call__(self, cmd, **kw):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


TRACK = SimpleNamespace(language="it", rendition_key="74963bcf")
FORMATS = json.dumps({"formats": [{"format_id": "hls-it", "vcodec": "none",
                                   "url": "https://x/v2/playlist/av/74963bcf/avf/a/media.m3u8"}]})


def make(tmp_path, results):
    sleeps, runner = [], Runner(results)
    return YtDlpAudioFetcher(workdir=str(tmp_path), run=runner, sleep=sleeps.append), runner, sleeps


@pytest.mark.parametrize("text,reason", [
    (DNS, "DNS resolution failure"), ("Connection reset by peer", "connection reset"), ("Read timed out", "connection timeout"),
    ("HTTP Error 429: Too Many Requests", "HTTP 429"), ("HTTP Error 503: Service Unavailable", "HTTP 5xx"),
    ("HTTP Error 502: Bad Gateway", "HTTP 5xx"),
    ("HTTP Error 401: Unauthorized", None), ("HTTP Error 403: Forbidden", None), ("HTTP Error 404: Not Found", None),
    ("ERROR: Unsupported URL", None), ("ERROR: Requested format is not available", None), ("some unknown failure", None),
])
def test_failure_classification(text, reason):
    assert classify_failure(text) == reason


def test_dns_failures_are_retried_with_5_15_30_waits_then_succeed(tmp_path, caplog):
    (tmp_path / "f1_1_it_74963bcf.mp4").write_bytes(b"audio")
    f, runner, sleeps = make(tmp_path, [proc(0, out=FORMATS), proc(1, DNS), proc(1, DNS), proc(1, DNS), proc(0)])
    with caplog.at_level("WARNING"):
        path = f.download("1", TRACK)
    assert path.endswith("f1_1_it_74963bcf.mp4") and sleeps == [5, 15, 30]
    assert "Attempt: 1/4 failed" in caplog.text and "Reason: DNS resolution failure" in caplog.text
    assert "Retrying in 15 seconds" in caplog.text and "Track: Italian" in caplog.text and "Video: 1" in caplog.text


def test_gives_up_after_four_attempts_and_never_a_fifth(tmp_path):
    f, runner, sleeps = make(tmp_path, [proc(0, out=FORMATS)] + [proc(1, DNS)] * 4)
    with pytest.raises(AudioFetchError, match="gave up after 4 attempts"):
        f.download("1", TRACK)
    assert runner.calls == 1 + 4 and sleeps == [5, 15, 30]


@pytest.mark.parametrize("err", ["HTTP Error 403: Forbidden", "HTTP Error 404: Not Found", "ERROR: Unsupported URL"])
def test_permanent_failure_is_not_retried(tmp_path, err):
    f, runner, sleeps = make(tmp_path, [proc(0, out=FORMATS), proc(1, err)])
    with pytest.raises(AudioFetchError, match="not retried"):
        f.download("1", TRACK)
    assert runner.calls == 2 and sleeps == []


def test_subprocess_timeout_counts_as_transient(tmp_path):
    (tmp_path / "f1_1_it_74963bcf.mp4").write_bytes(b"a")
    f, runner, sleeps = make(tmp_path, [proc(0, out=FORMATS), subprocess.TimeoutExpired("yt-dlp", 900), proc(0)])
    assert f.download("1", TRACK) and sleeps == [5]


def test_format_listing_is_retried_too(tmp_path):
    f, runner, sleeps = make(tmp_path, [proc(1, DNS), proc(0, out=FORMATS)])
    assert match_format_id(f.formats("1"), "74963bcf") == "hls-it" and sleeps == [5]


def test_server_host_uses_its_own_base_url_and_m4a_name(tmp_path):
    from migration.audio_fetch import ServerUploadHost
    src = tmp_path / "f1_1_en_22571df7.mp4"
    src.write_bytes(b"x")
    posted = []

    class S:
        def post(self, url, **kw):
            posted.append(url)
            return SimpleNamespace(status_code=200, text="", json=lambda: {"url": "http://35.154.164.178:8000/temp-audio/x"})

    url, name = ServerUploadHost("https://videoplayer.icare.life/", session=S()).publish(str(src))
    assert name == "f1_1_en_22571df7.m4a"
    assert posted == ["https://videoplayer.icare.life/upload/temp-file/f1_1_en_22571df7.m4a"]
    assert url == "https://videoplayer.icare.life/temp-audio/f1_1_en_22571df7.m4a" and "35.154" not in url

"""In-memory stand-ins for Vimeo, Mux, yt-dlp and audio hosting (no network)."""
import itertools

from migration.mux_client import MuxError

LANGS = {"en": "English", "fr": "français", "it": "italiano", "pt": "português", "ro": "română", "es": "español"}
KEYS = {"en": "22571df7", "fr": "b97e30bf", "it": "74963bcf", "pt": "fa0e562c", "ro": "bdf29444", "es": "66408762"}


def manifest(langs=tuple(LANGS), original=True):
    lines = ["#EXTM3U", '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="x",LANGUAGE="fr",URI="sub.m3u8"']
    if original:
        lines.append('#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-high",NAME="English",DEFAULT=YES,AUTOSELECT=YES,'
                     'LANGUAGE="en",CHANNELS="2",URI="../v2/playlist/av/434fc542/avf/648defbd/media.m3u8"')
    for l in langs:
        lines.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-high",NAME="{LANGS[l]}",AUTOSELECT=YES,'
                     f'CHARACTERISTICS="public.translation.dubbed",LANGUAGE="{l}",CHANNELS="1",'
                     f'URI="../v2/playlist/av/{KEYS[l]}/avf/aaaa/media.m3u8"')
    lines.append('#EXT-X-STREAM-INF:BANDWIDTH=1,AUDIO="audio-high"\nvideo.m3u8')
    return "\n".join(lines)


def video_json(vid="111", name="Video", versions=1):
    return {
        "uri": f"/videos/{vid}", "name": name, "link": f"https://vimeo.com/{vid}", "duration": 300,
        "created_time": "2025-01-01T00:00:00+00:00", "modified_time": "2025-02-01T00:00:00+00:00",
        "status": "available", "is_playable": True, "transcode": {"status": "complete"},
        "metadata": {"connections": {"versions": {"total": versions, "current_uri": f"/videos/{vid}/versions/2"}}},
        "files": [
            {"rendition": "1080p", "quality": "hd", "type": "video/mp4", "width": 1920, "height": 1080, "size": 5, "link": "https://v/1080?sig=1"},
            {"rendition": "720p", "quality": "hd", "type": "video/mp4", "width": 1280, "height": 720, "size": 3, "link": "https://v/720?sig=1"},
            {"rendition": "adaptive", "quality": "hls", "type": "video/mp4", "link": "https://v/master.m3u8?sig=1"},
        ],
        "download": [],
    }


def texttracks(langs=tuple(LANGS)):
    return [{"uri": f"/videos/1/texttracks/{900 + i}", "id": 900 + i, "type": "captions", "language": l, "active": True,
             "link": f"https://captions/{l}.vtt?sig=1", "source_link": f"https://captions/src/{l}?sig=1",
             "name": f"Title {l}.srt"} for i, l in enumerate(langs)]


class FakeVimeo:
    def __init__(self, videos=None, audio_langs=tuple(LANGS), caption_langs=tuple(LANGS), srt=True):
        self.videos = videos or {"111": video_json("111")}
        self.audio_langs, self.caption_langs, self.srt = audio_langs, caption_langs, srt
        self.get_video_calls = 0

    def get_video(self, vid):
        self.get_video_calls += 1
        return self.videos[vid]

    def get_manifest(self, link):
        return manifest(self.audio_langs), "https://cdn.example/x/y/master.m3u8"

    def get_texttracks(self, vid):
        return texttracks(self.caption_langs)

    def get_versions(self, vid):
        return [{"uri": f"/videos/{vid}/versions/1", "upload_date": "2025-01-01"},
                {"uri": f"/videos/{vid}/versions/2", "upload_date": "2025-02-01"}]

    def sniff_caption(self, url):
        return "srt" if self.srt else None

    def items(self):
        return [{"video": v, "folder_name": "Root"} for v in self.videos.values()]


class FakeMux:
    def __init__(self, drm=True):
        self.assets, self.drm = {}, drm
        self.create_calls, self.track_posts, self.pb_posts = 0, [], []
        self.fail_track = set()       # languages whose POST raises
        self.error_track = set()      # languages Mux reports as errored
        self._ids = itertools.count(1)

    def _asset(self, pt, title):
        aid = f"asset{next(self._ids)}"
        a = {"id": aid, "status": "ready", "passthrough": pt, "meta": {"title": title},
             "playback_ids": [{"id": f"pub{aid}", "policy": "public"}, {"id": f"sig{aid}", "policy": "signed"}]
             + ([{"id": f"drm{aid}", "policy": "drm"}] if self.drm else []),
             "tracks": [{"id": f"t{aid}v", "type": "video", "status": "ready"},
                        {"id": f"t{aid}a", "type": "audio", "primary": True, "status": "ready"}]}
        self.assets[aid] = a
        return a

    def create_asset(self, url, title, pt):
        self.create_calls += 1
        return dict(self._asset(pt, title))

    def ensure_playback_ids(self, aid, wanted=("public", "signed", "drm")):
        a = self.assets[aid]
        have = {p["policy"] for p in a["playback_ids"]}
        created = [w for w in wanted if w not in have]
        if "drm" in created and not self.drm:
            raise MuxError("DRM playback ID cannot be added")
        for w in created:
            self.pb_posts.append((aid, w))
            a["playback_ids"].append({"id": f"{w}{aid}", "policy": w})
        return a, created

    def get_asset(self, aid):
        return self.assets[aid]

    def list_assets(self):
        return list(self.assets.values())

    def wait_for_asset(self, aid, timeout=0):
        return self.assets[aid]

    def add_track(self, aid, payload):
        lang = payload["language_code"]
        if lang in self.fail_track and payload["type"] == "audio":
            raise MuxError("boom")
        self.track_posts.append((aid, payload["type"], lang))
        t = {"id": f"trk{next(self._ids)}", "type": payload["type"], "language_code": lang, "name": payload["name"],
             "status": "errored" if lang in self.error_track and payload["type"] == "audio" else "ready"}
        self.assets[aid]["tracks"].append(t)
        return t

    def wait_for_track(self, aid, tid, timeout=0):
        return next(t for t in self.assets[aid]["tracks"] if t["id"] == tid)


class FakeFetcher:
    def __init__(self, tmp_path):
        self.tmp, self.downloads = tmp_path, []

    def download(self, vid, track):
        p = self.tmp / f"{vid}_{track.language}.m4a"
        p.write_bytes(b"x")
        self.downloads.append(track.language)
        return str(p)


class FakeHost:
    def __init__(self):
        self.published, self.released = [], []

    def publish(self, path):
        name = path.replace("\\", "/").split("/")[-1]
        self.published.append(name)
        return f"https://host/{name}", name

    def release(self, name):
        self.released.append(name)

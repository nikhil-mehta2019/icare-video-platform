"""Mux calls for the Folder 1 migration. Create/read/add-track only: there is deliberately
no delete or update of assets, tracks or playback IDs in this module.

Retry policy: GETs retry on 429/5xx; POSTs retry only on 429 (request not processed).
A POST that times out or 5xxs is NOT blindly repeated - the caller recovers by
looking the asset/track up (passthrough / language+name) instead of risking a duplicate.
"""
import logging
import time

import requests

from migration.config import VIDEO_QUALITY, base_lang

logger = logging.getLogger(__name__)

BASE_URL = "https://api.mux.com/video/v1"


class MuxError(Exception):
    pass


class DrmConfigError(MuxError):
    pass


# --------------------------------------------------------------- payload builders


def build_asset_payload(video_url: str, title: str, passthrough: str, drm_configuration_id: str) -> dict:
    """DRM is mandatory: refuse to build a non-DRM payload."""
    if not drm_configuration_id or not str(drm_configuration_id).strip():
        raise DrmConfigError("DRM_CONFIGURATION_ID is not set; refusing to create a non-DRM asset.")
    return {
        "input": [{"url": video_url}],
        # every asset gets all three policies; Mux issues a separate playback ID per policy
        "advanced_playback_policies": [{"policy": "public"}, {"policy": "signed"},
                                       {"policy": "drm", "drm_configuration_id": drm_configuration_id}],
        "video_quality": VIDEO_QUALITY,
        "meta": {"title": (title or "Untitled")[:250]},
        "passthrough": passthrough[:255],
    }


def audio_track_payload(url: str, language: str, name: str) -> dict:
    return {"url": url, "type": "audio", "language_code": language, "name": name}


def text_track_payload(url: str, language: str, name: str) -> dict:
    return {"url": url, "type": "text", "text_type": "subtitles", "language_code": language, "name": name}


# -------------------------------------------------------------------- verification


def find_track(asset: dict, track_type: str, language: str, name: str) -> dict | None:
    matches = []
    for t in asset.get("tracks") or []:
        if t.get("type") != track_type:
            continue
        if track_type == "audio" and t.get("primary"):
            continue   # the embedded original audio is never one of ours
        if (t.get("language_code") or "").lower() == language.lower() and (t.get("name") or "") == name:
            matches.append(t)
    # a healthy track wins over an errored leftover with the same language+name
    return next((t for t in matches if t.get("status") != "errored"), matches[0] if matches else None)


POLICIES = ("public", "signed", "drm")


def verify_asset(asset: dict, audio: list, captions: list, required_policies=POLICIES) -> list[str]:
    """audio/captions: [(language, name)] expected on the asset. Returns a list of problems (empty = verified)."""
    problems = []
    if asset.get("status") != "ready":
        problems.append(f"asset status is '{asset.get('status')}', not 'ready'")
    pids = asset.get("playback_ids") or []
    if not pids:
        problems.append("asset has no playback ID")
    for pol in required_policies:
        if not any(p.get("policy") == pol for p in pids):
            problems.append(f"asset has no {pol} playback ID")
    for kind, expected in (("audio", audio), ("text", captions)):
        for lang, name in expected:
            t = find_track(asset, kind, lang, name)
            label = "caption" if kind == "text" else "audio"
            if not t:
                problems.append(f"{label} track {lang}/{name} not found on Mux asset")
            elif t.get("status") != "ready":
                problems.append(f"{label} track {lang}/{name} is '{t.get('status')}'")
    return problems


def playback_fields(asset: dict) -> dict:
    """One distinct field per policy. Never derived from another policy's ID."""
    by_policy = {}
    for p in asset.get("playback_ids") or []:
        by_policy.setdefault(p.get("policy"), p.get("id"))
    return {
        "mux_public_playback_id": by_policy.get("public"),
        "mux_signed_playback_id": by_policy.get("signed"),
        "mux_drm_playback_id": by_policy.get("drm"),
    }


def check_signing_key(mux, key_id, private_key_b64):
    """(ok, detail). Checks the configured signing key exists on the Mux account and that the private key
    parses and can sign an RS256 token locally. (Mux does not expose the public key, so a match is proven
    functionally by verify_signed_playback.) Nothing secret is returned or logged."""
    import base64
    import time as _t
    import jwt
    if not key_id or not private_key_b64:
        return False, "MUX_SIGNING_KEY_ID / MUX_PRIVATE_KEY not configured"
    try:
        r = mux.session.request("GET", f"https://api.mux.com/system/v1/signing-keys/{key_id}", auth=mux.auth, timeout=30)
    except Exception as exc:
        return False, f"could not query signing key ({type(exc).__name__})"
    if r.status_code != 200:
        return False, f"configured signing key not found on the Mux account (HTTP {r.status_code})"
    try:
        jwt.encode({"sub": "selftest", "aud": "v", "exp": int(_t.time()) + 60, "kid": key_id},
                   base64.b64decode(private_key_b64), algorithm="RS256")
    except Exception as exc:
        return False, f"private key cannot sign RS256 tokens ({type(exc).__name__})"
    return True, "signing key exists on the account and the private key signs RS256 tokens locally"


def verify_signed_playback(playback_id, key_id, private_key_b64, session=requests):
    """(ok, detail). Functional proof for a signed playback ID: the manifest is refused without a token and
    served with a freshly signed one. The token is never returned or logged."""
    import base64
    import time as _t
    import jwt
    url = f"https://stream.mux.com/{playback_id}.m3u8"
    no_token = session.get(url, timeout=30).status_code
    tok = jwt.encode({"sub": playback_id, "aud": "v", "exp": int(_t.time()) + 300, "kid": key_id},
                     base64.b64decode(private_key_b64), algorithm="RS256")
    with_token = session.get(url, params={"token": tok}, timeout=30).status_code
    ok = no_token in (401, 403) and with_token == 200
    return ok, f"without token HTTP {no_token}; with signed token HTTP {with_token}"


# ------------------------------------------------------------------------- client


class MuxClient:
    def __init__(self, token_id, token_secret, drm_configuration_id, session=None, sleep=time.sleep):
        self.auth = (token_id, token_secret)
        self.drm_configuration_id = drm_configuration_id
        self.session = session or requests
        self.sleep = sleep

    def _request(self, method, path, retry_5xx, **kw):
        url = BASE_URL + path
        delay = 5
        for attempt in range(1, 7):
            try:
                r = self.session.request(method, url, auth=self.auth, timeout=30, **kw)
            except requests.RequestException as exc:
                if retry_5xx and attempt < 6:
                    self.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise MuxError(f"{method} {path} failed: {type(exc).__name__}") from exc
            if r.status_code == 429 or (retry_5xx and r.status_code >= 500):
                if attempt < 6:
                    wait = delay
                    try:
                        wait = int(r.headers.get("Retry-After", delay))
                    except (TypeError, ValueError, AttributeError):
                        pass
                    self.sleep(min(wait, 60))
                    delay = min(delay * 2, 60)
                    continue
            return r
        return r

    def _json(self, r, what):
        if r.status_code >= 300:
            raise MuxError(f"{what} failed: HTTP {r.status_code} {r.text[:300]}")
        return r.json()

    # -- config checks (read-only)
    def check_credentials(self):
        try:
            r = self._request("GET", "/assets", True, params={"limit": 1})
        except MuxError as exc:
            return False, str(exc)
        return r.status_code == 200, f"GET /assets?limit=1 -> {r.status_code}"

    def check_drm_configuration(self):
        """(True|False|None, detail): True = configured ID is among the account's DRM configurations."""
        if not self.drm_configuration_id:
            return False, "DRM_CONFIGURATION_ID is not set"
        try:
            ids, page = [], 1
            while True:
                r = self._request("GET", "/drm-configurations", True, params={"limit": 100, "page": page})
                if r.status_code != 200:
                    return None, f"GET /drm-configurations -> HTTP {r.status_code}; cannot confirm the ID"
                data = r.json().get("data", [])
                ids.extend(d.get("id") for d in data)
                if len(data) < 100:
                    break
                page += 1
        except MuxError as exc:
            return None, str(exc)
        if self.drm_configuration_id in ids:
            return True, f"configured DRM configuration found among {len(ids)} on the account"
        return False, f"configured DRM configuration ID not found among {len(ids)} on the account"

    # -- assets
    def create_asset(self, video_url, title, passthrough) -> dict:
        payload = build_asset_payload(video_url, title, passthrough, self.drm_configuration_id)
        r = self._request("POST", "/assets", False, json=payload)
        return self._json(r, "create asset")["data"]

    def get_asset(self, asset_id) -> dict:
        return self._json(self._request("GET", f"/assets/{asset_id}", True), f"get asset {asset_id}")["data"]

    def list_assets(self) -> list[dict]:
        out, page = [], 1
        while True:
            r = self._request("GET", "/assets", True, params={"limit": 100, "page": page})
            data = self._json(r, "list assets").get("data", [])
            out.extend(data)
            if len(data) < 100:
                return out
            page += 1

    def ensure_playback_ids(self, asset_id, wanted=POLICIES):
        """Creates ONLY the policies the asset does not already have (never a second ID for a policy).
        Returns (asset, [policies created])."""
        asset = self.get_asset(asset_id)
        have = {p.get("policy") for p in asset.get("playback_ids") or []}
        created = []
        for pol in wanted:
            if pol in have:
                continue
            body = {"policy": pol}
            if pol == "drm":
                if not self.drm_configuration_id:
                    raise DrmConfigError("DRM_CONFIGURATION_ID is not set; cannot add a DRM playback ID")
                body["drm_configuration_id"] = self.drm_configuration_id
            self._json(self._request("POST", f"/assets/{asset_id}/playback-ids", False, json=body),
                       f"add {pol} playback ID")
            created.append(pol)
        return (self.get_asset(asset_id) if created else asset), created

    def add_track(self, asset_id, payload) -> dict:
        r = self._request("POST", f"/assets/{asset_id}/tracks", False, json=payload)
        return self._json(r, f"add {payload.get('type')} track")["data"]

    def wait_for_asset(self, asset_id, timeout=3600, interval=10) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            asset = self.get_asset(asset_id)
            if asset.get("status") == "ready":
                return asset
            if asset.get("status") == "errored":
                raise MuxError(f"asset {asset_id} errored: {asset.get('errors')}")
            if time.monotonic() > deadline:
                raise MuxError(f"asset {asset_id} not ready after {timeout}s (status {asset.get('status')})")
            self.sleep(interval)

    def wait_for_track(self, asset_id, track_id, timeout=900, interval=5) -> dict:
        """Polls until the track is no longer 'preparing'. Returns the track dict (status ready/errored/other)."""
        deadline = time.monotonic() + timeout
        while True:
            asset = self.get_asset(asset_id)
            t = next((x for x in asset.get("tracks") or [] if x.get("id") == track_id), None)
            if t and t.get("status") in ("ready", "errored"):
                return t
            if time.monotonic() > deadline:
                return t or {"id": track_id, "status": "missing"}
            self.sleep(interval)


class AssetIndex:
    """Passthrough -> [assets], loaded once per run; used to adopt an asset created before a crash."""

    def __init__(self, mux):
        self.mux = mux
        self._by_pt = None

    def load(self):
        self._by_pt = {}
        for a in self.mux.list_assets():
            if a.get("passthrough"):
                self._by_pt.setdefault(a["passthrough"], []).append(a)

    def find(self, passthrough) -> list[dict]:
        if self._by_pt is None:
            self.load()
        return list(self._by_pt.get(passthrough, []))

    def add(self, asset):
        if self._by_pt is not None and asset.get("passthrough"):
            self._by_pt.setdefault(asset["passthrough"], []).append(asset)

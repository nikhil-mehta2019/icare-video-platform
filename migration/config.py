"""Static configuration for the Folder 1 migration plus secret redaction."""
import os
import re

FOLDER_ID = "25228807"
FOLDER_URL = "https://vimeo.com/user/141270659/folder/25228807?isPrivate=false"

# Every video is expected to carry a dubbed audio track and a caption track in each of these.
EXPECTED_LANGUAGES = ("en", "fr", "it", "pt", "ro", "es")
LANGUAGE_NAMES = {
    "en": "English", "fr": "French", "it": "Italian",
    "pt": "Portuguese", "ro": "Romanian", "es": "Spanish",
}

# Stored on every Mux asset so a crash between "asset created" and "DB saved" is recoverable.
PASSTHROUGH_PREFIX = "vimeo:"
# Same quality tier the existing DRM path already uses (DRM needs plus/premium).
VIDEO_QUALITY = "premium"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STATE_DB_URL = "sqlite:///" + os.path.join(BASE_DIR, "migration_folder1.db").replace("\\", "/")
DEFAULT_OUTPUT_DIR = os.path.join(BASE_DIR, "migration_output")
STOP_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migration_output", "STOP")
WORK_DIR = os.path.join(BASE_DIR, "migration_output", "audio_work")
LOG_DIR = os.path.join(BASE_DIR, "logs")


def base_lang(code: str | None) -> str:
    return (code or "").lower().replace("_", "-").split("-")[0]


def language_name(code: str | None) -> str:
    return LANGUAGE_NAMES.get(base_lang(code), code or "Unknown")


def passthrough_for(vimeo_id: str) -> str:
    return f"{PASSTHROUGH_PREFIX}{vimeo_id}"


_SECRET_ENV = ("VIMEO_ACCESS_TOKEN", "MUX_TOKEN_SECRET", "MUX_TOKEN_ID", "MUX_PRIVATE_KEY",
               "JWT_SECRET", "API_KEY", "MUX_WEBHOOK_SECRET", "MUX_SIGNING_KEY_ID")
_URL_QUERY = re.compile(r"(https?://[^\s\"'?]+)\?[^\s\"']*")


def redact(text) -> str:
    """Strip URL query strings (signed links) and any configured secret values."""
    s = str(text)
    s = _URL_QUERY.sub(r"\1?<redacted>", s)
    for name in _SECRET_ENV:
        val = os.environ.get(name)
        if val and len(val) >= 6:
            s = s.replace(val, "***")
    return s

import os
import sys

# Must be set before any `app` import: app.config validates env vars at import time and the real
# .env must never be used (or its secrets touched) by the unit tests.
for k, v in {"DATABASE_URL": "sqlite:///:memory:", "MUX_TOKEN_ID": "test-id", "MUX_TOKEN_SECRET": "test-secret",
             "VIMEO_ACCESS_TOKEN": "test-vimeo-token", "DRM_CONFIGURATION_ID": "drm-test"}.items():
    os.environ[k] = v

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import os
import logging
from dotenv import load_dotenv

load_dotenv()

MUX_TOKEN_ID = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
MUX_SIGNING_KEY_ID = os.getenv("MUX_SIGNING_KEY_ID")
MUX_PRIVATE_KEY = os.getenv("MUX_PRIVATE_KEY")
DRM_CONFIGURATION_ID = os.getenv("DRM_CONFIGURATION_ID")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./migration.db")
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", "http://localhost:8000")
VIMEO_ACCESS_TOKEN = os.getenv("VIMEO_ACCESS_TOKEN")
MUX_WEBHOOK_SECRET = os.getenv("MUX_WEBHOOK_SECRET")

missing = [v for v in ["MUX_TOKEN_ID", "MUX_TOKEN_SECRET", "VIMEO_ACCESS_TOKEN"] if not os.getenv(v)]
if missing:
    raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

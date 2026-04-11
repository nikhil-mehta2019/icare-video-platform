import os
import logging
from dotenv import load_dotenv

load_dotenv()

MUX_TOKEN_ID = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
MUX_SIGNING_KEY_ID = os.getenv("MUX_SIGNING_KEY_ID")
MUX_PRIVATE_KEY = os.getenv("MUX_PRIVATE_KEY")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("FATAL: DATABASE_URL environment variable is not set")

JWT_SECRET = os.getenv("JWT_SECRET", "icare-secret")
VIMEO_ACCESS_TOKEN = os.getenv("VIMEO_ACCESS_TOKEN")

# Strict Environment Variable Validation
missing_vars = []
if not MUX_TOKEN_ID: missing_vars.append("MUX_TOKEN_ID")
if not MUX_TOKEN_SECRET: missing_vars.append("MUX_TOKEN_SECRET")
if not VIMEO_ACCESS_TOKEN: missing_vars.append("VIMEO_ACCESS_TOKEN")

if missing_vars:
    error_msg = f"FATAL: Missing required environment variables: {', '.join(missing_vars)}"
    logging.error(error_msg)
    raise ValueError(error_msg)
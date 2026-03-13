import os
from dotenv import load_dotenv

load_dotenv()

MUX_TOKEN_ID = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///icare.db")

JWT_SECRET = os.getenv("JWT_SECRET", "icare-secret")
VIMEO_ACCESS_TOKEN = os.getenv("VIMEO_ACCESS_TOKEN")
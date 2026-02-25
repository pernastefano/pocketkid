from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "pocketkid.db"
LOCALES_DIR = BASE_DIR / "locales"

SUPPORTED_LANGUAGES = ("en", "it")


class Settings:
    SECRET_KEY = os.getenv("SECRET_KEY", "pocketkid-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{DB_PATH}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

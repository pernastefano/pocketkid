from __future__ import annotations

import os
from pathlib import Path
from datetime import timedelta

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "pocketkid.db"
LOCALES_DIR = BASE_DIR / "locales"

SUPPORTED_LANGUAGES = ("en", "it")
APP_VERSION = os.getenv("APP_VERSION", "1.0.2")
APP_CREDITS = os.getenv("APP_CREDITS", "Stefano Perna")
APP_REPO_URL = os.getenv("APP_REPO_URL", "https://github.com/pernastefano/pocketkid")


class Settings:
    SECRET_KEY = os.getenv("SECRET_KEY", "pocketkid-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{DB_PATH}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    PERMANENT_SESSION_LIFETIME = timedelta(days=int(os.getenv("SESSION_DAYS", "30")))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"

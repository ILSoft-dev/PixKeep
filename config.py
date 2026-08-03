"""
config.py
v3.0 - central configuration (Yandex.Disk backend)

Changelog:
- v3.0: switched storage backend Google Drive -> Yandex.Disk.
- v2.x: Google Drive multi-user.
"""
import os


class Config:
    BOT_TOKEN = os.environ["BOT_TOKEN"]

    # One Yandex OAuth app (registered at oauth.yandex.ru / yandex.ru/dev),
    # shared by all users. Each user authorizes their OWN Yandex.Disk.
    YANDEX_CLIENT_ID = os.environ["YANDEX_CLIENT_ID"]
    YANDEX_CLIENT_SECRET = os.environ["YANDEX_CLIENT_SECRET"]
    # Must exactly match the Callback URL set in the Yandex app, e.g.
    # https://your-app.onrender.com/oauth/callback
    OAUTH_REDIRECT_URI = os.environ["OAUTH_REDIRECT_URI"]

    SUPABASE_URL = os.environ["SUPABASE_URL"]
    SUPABASE_KEY = os.environ["SUPABASE_KEY"]

    PORT = int(os.environ.get("PORT", "10000"))

    # Restricted scope: bot can only touch its own app folder
    # (appears as "Приложения/<AppName>" on the user's disk), not the whole disk.
    YANDEX_SCOPE = "cloud_api:disk.app_folder"
    # Path prefix for the app folder in the Yandex Disk REST API.
    APP_ROOT = "app:/"

    TEMP_DIR = "/tmp/cleandrive_bot"


config = Config()

"""
config.py
v5.0 - central configuration (Google Drive backend, multi-account, admin-only access)

Changelog:
- v5.0: added ADMIN_ID — only this Telegram user ID can use the bot at all.
        Everyone else is silently ignored at the dispatcher level, before
        any handler (including /start) even runs.
- v4.1: added userinfo.email scope for multi-account support (need email
        to label accounts in /accounts).
- v4.0: switched storage backend Yandex.Disk -> Google Drive (drive.file
        scope, non-sensitive, app published "In Production" so no 100-user
        cap and no 7-day refresh-token expiry).
- v3.0: Yandex.Disk backend (accessible from BY/CIS).
"""
import os


class Config:
    BOT_TOKEN = os.environ["BOT_TOKEN"]

    # Only this Telegram user ID may use the bot at all — everyone else is
    # silently ignored (no reply, no acknowledgment the bot even exists).
    ADMIN_ID = int(os.environ["ADMIN_ID"])

    # One Google Cloud OAuth "Web application" client, shared by all users.
    # Each user authorizes their OWN Google Drive via the consent screen.
    GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
    GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
    # Must exactly match a redirect URI registered in Google Cloud Console, e.g.
    # https://your-app.onrender.com/oauth/callback
    OAUTH_REDIRECT_URI = os.environ["OAUTH_REDIRECT_URI"]

    SUPABASE_URL = os.environ["SUPABASE_URL"]
    SUPABASE_KEY = os.environ["SUPABASE_KEY"]

    PORT = int(os.environ.get("PORT", "10000"))

    # drive.file: app can only see/edit files IT created or that the user
    # explicitly opened with it — not the whole Drive.
    # userinfo.email: lets us fetch the account's email so multiple
    # connected Google accounts can be told apart in /accounts.
    GOOGLE_SCOPE = (
        "https://www.googleapis.com/auth/drive.file "
        "https://www.googleapis.com/auth/userinfo.email"
    )

    TEMP_DIR = "/tmp/cleandrive_bot"


config = Config()

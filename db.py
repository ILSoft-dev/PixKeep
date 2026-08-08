"""
db.py
v5.2 - multiple Google accounts per Telegram user + standing settings, in Supabase

Tables (create once, SQL in README):
    user_settings(telegram_id bigint primary key,
                  clean_metadata boolean default true,
                  anonymize_names boolean default false,
                  auto_date_folder boolean default false)

    google_accounts(id bigserial primary key,
                    telegram_id bigint not null,
                    email text not null,
                    access_token text not null,
                    refresh_token text not null,
                    is_active boolean not null default false,
                    created_at timestamptz default now(),
                    unique (telegram_id, email))

Changelog:
- v5.2: BUGFIX — get_settings() was still SELECTing only the original two
        columns, so auto_date_folder (added in v5.1) never actually came
        back from Supabase; it silently fell back to its default every
        time, which is why the /settings checkbox never reflected a real
        toggle. Select list is now built from DEFAULT_SETTINGS.keys() so
        this can't go stale again when a future setting is added.
- v5.1: added auto_date_folder setting — when on, the bot skips asking for
        a folder name and creates/uses one named after the files' capture
        date (DD.MM.YY) automatically.
- v5.0: split into two tables — settings are independent of which Google
        account is active, and a user can now have several connected
        accounts with exactly one marked is_active at a time.
- v4.1: single-account disk_users table (access_token/refresh_token +
        clean_metadata/anonymize_names all in one row per telegram_id).
"""
from supabase import create_client

from config import config

_supabase = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)

DEFAULT_SETTINGS = {
    "clean_metadata": True,
    "anonymize_names": False,
    "auto_date_folder": False,
}


# --------------------------------------------------------------- settings ---
def get_settings(telegram_id: int) -> dict:
    # Select columns built from DEFAULT_SETTINGS.keys() on purpose — adding a
    # new setting there automatically includes it here too, so this can't
    # silently go stale again the way it did with auto_date_folder.
    columns = ", ".join(DEFAULT_SETTINGS.keys())
    resp = (
        _supabase.table("user_settings")
        .select(columns)
        .eq("telegram_id", telegram_id)
        .limit(1)
        .execute()
    )
    if not resp.data:
        return dict(DEFAULT_SETTINGS)
    row = resp.data[0]
    return {
        key: (row.get(key) if row.get(key) is not None else default)
        for key, default in DEFAULT_SETTINGS.items()
    }


def set_setting(telegram_id: int, field: str, value: bool) -> None:
    if field not in DEFAULT_SETTINGS:
        raise ValueError(f"Unknown setting: {field}")
    _supabase.table("user_settings").upsert({
        "telegram_id": telegram_id,
        field: value,
    }).execute()


# ---------------------------------------------------------- Google accounts --
def add_or_update_account(telegram_id: int, email: str,
                          access_token: str, refresh_token: str) -> None:
    """Connect a new Google account, or refresh tokens if this email is
    already connected for this Telegram user. Either way, this account
    becomes the active one (deactivating any others)."""
    _supabase.table("google_accounts").update({"is_active": False}).eq(
        "telegram_id", telegram_id
    ).execute()
    _supabase.table("google_accounts").upsert({
        "telegram_id": telegram_id,
        "email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "is_active": True,
    }, on_conflict="telegram_id,email").execute()


def list_accounts(telegram_id: int) -> list[dict]:
    resp = (
        _supabase.table("google_accounts")
        .select("id, email, is_active")
        .eq("telegram_id", telegram_id)
        .order("created_at")
        .execute()
    )
    return resp.data or []


def get_active_account(telegram_id: int) -> dict | None:
    resp = (
        _supabase.table("google_accounts")
        .select("id, email, access_token, refresh_token")
        .eq("telegram_id", telegram_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if resp.data:
        return resp.data[0]
    return None


def set_active_account(telegram_id: int, account_id: int) -> None:
    _supabase.table("google_accounts").update({"is_active": False}).eq(
        "telegram_id", telegram_id
    ).execute()
    _supabase.table("google_accounts").update({"is_active": True}).eq(
        "id", account_id
    ).eq("telegram_id", telegram_id).execute()


def update_account_tokens(account_id: int, access_token: str, refresh_token: str) -> None:
    _supabase.table("google_accounts").update({
        "access_token": access_token,
        "refresh_token": refresh_token,
    }).eq("id", account_id).execute()


def remove_all_accounts(telegram_id: int) -> None:
    _supabase.table("google_accounts").delete().eq("telegram_id", telegram_id).execute()

"""
db.py
v3.0 - per-user Yandex OAuth token storage in Supabase

Table (create once, SQL in README):
    disk_users(telegram_id bigint primary key,
               access_token text,
               refresh_token text,
               created_at timestamptz default now())
"""
from supabase import create_client

from config import config

_supabase = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)


def save_tokens(telegram_id: int, access_token: str, refresh_token: str) -> None:
    _supabase.table("disk_users").upsert({
        "telegram_id": telegram_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
    }).execute()


def get_tokens(telegram_id: int) -> dict | None:
    resp = (
        _supabase.table("disk_users")
        .select("access_token, refresh_token")
        .eq("telegram_id", telegram_id)
        .limit(1)
        .execute()
    )
    if resp.data:
        return resp.data[0]
    return None


def delete_user(telegram_id: int) -> None:
    _supabase.table("disk_users").delete().eq("telegram_id", telegram_id).execute()

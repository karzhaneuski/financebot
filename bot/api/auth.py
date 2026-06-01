import hashlib
import hmac
import json
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException


def verify_telegram_init_data(init_data: str, bot_token: str) -> dict | None:
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            return None

        user_str = parsed.get("user")
        return json.loads(user_str) if user_str else parsed
    except Exception:
        return None


async def get_current_user(authorization: str = Header()) -> int:
    from bot.config import settings

    if settings.DEV_TOKEN and authorization == f"Bearer {settings.DEV_TOKEN}":
        return settings.DEV_USER_ID

    if not authorization.startswith("tma "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    user = verify_telegram_init_data(authorization[4:], settings.BOT_TOKEN)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid initData")

    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="No user id in initData")

    return int(user_id)

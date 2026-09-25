from fastapi import APIRouter, Depends

from bot.api.auth import get_current_user
from bot.api.deps import get_user_language

router = APIRouter()


@router.get("/me")
async def me(
    user_id: int = Depends(get_current_user),
    language: str = Depends(get_user_language),
):
    """Current user's settings; the Mini App uses `language` for its UI."""
    return {"user_id": user_id, "language": language}

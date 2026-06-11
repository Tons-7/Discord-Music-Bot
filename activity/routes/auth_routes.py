from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from activity.auth import exchange_code
from activity.dependencies import guild_member
from activity.session import SESSION_TTL, mint_session_token

router = APIRouter(prefix="/api", tags=["auth"])


class TokenRequest(BaseModel):
    code: str


@router.post("/token")
async def token_exchange(body: TokenRequest):
    data = await exchange_code(body.code)
    if "access_token" not in data:
        raise HTTPException(status_code=400, detail=data.get("error_description", "Token exchange failed"))
    return {"access_token": data["access_token"]}


@router.get("/guild/{guild_id}/session-token")
async def session_token(guild_id: int, user=Depends(guild_member)):
    return {
        "token": mint_session_token(int(user["id"]), guild_id),
        "expires_in": SESSION_TTL,
    }

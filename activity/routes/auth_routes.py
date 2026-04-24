from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from activity.auth import exchange_code

router = APIRouter(prefix="/api", tags=["auth"])


class TokenRequest(BaseModel):
    code: str


@router.post("/token")
async def token_exchange(body: TokenRequest):
    data = await exchange_code(body.code)
    if "access_token" not in data:
        raise HTTPException(status_code=400, detail=data.get("error_description", "Token exchange failed"))
    return {"access_token": data["access_token"]}

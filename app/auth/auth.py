import os
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

API_KEY_HEADER = APIKeyHeader(name="Authorization", auto_error=True)


def get_valid_keys() -> set:
    raw = os.getenv("API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


async def verify_api_key(api_key: str = Security(API_KEY_HEADER)) -> str:
    # Strip "Bearer " prefix if present
    key = api_key.replace("Bearer ", "").strip()
    valid_keys = get_valid_keys()

    if not valid_keys:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No API keys configured on server",
        )

    if key not in valid_keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return key


# Shorthand dependency
RequireAuth = Security(verify_api_key)

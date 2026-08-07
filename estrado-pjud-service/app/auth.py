import hmac

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

_bearer = HTTPBearer(auto_error=False)


def is_valid_api_token(token: str) -> bool:
    return hmac.compare_digest(token, get_settings().API_KEY)


def has_valid_authorization_header(value: str | None) -> bool:
    if value is None:
        return False
    scheme, separator, token = value.partition(" ")
    return separator == " " and scheme.lower() == "bearer" and is_valid_api_token(token)


async def _verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid authorization header")

    if not is_valid_api_token(credentials.credentials):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    return credentials.credentials


verify_api_key = Depends(_verify_api_key)

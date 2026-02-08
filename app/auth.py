from __future__ import annotations
from passlib.context import CryptContext
from itsdangerous import URLSafeSerializer
from fastapi import Request
from typing import Optional

# NOTE:
# - macOS Python 3.13 환경에서 bcrypt 백엔드 호환 이슈가 자주 발생합니다.
# - 그래서 MVP에서는 pbkdf2_sha256(순수 파이썬/안정적)로 비밀번호 해싱을 사용합니다.
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

COOKIE_NAME = "wsapp_session"
serializer = URLSafeSerializer("CHANGE_ME_TO_A_RANDOM_SECRET", salt="wsapp")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)

def set_session(response, user_id: int):
    token = serializer.dumps({"uid": user_id})
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax")

def clear_session(response):
    response.delete_cookie(COOKIE_NAME)

def get_user_id_from_request(request: Request) -> Optional[int]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        data = serializer.loads(token)
        return int(data.get("uid"))
    except Exception:
        return None

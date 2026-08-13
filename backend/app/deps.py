from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import Factory, User
from .security import decode_token

bearer = HTTPBearer(auto_error=False)


def current_user(
    db: Annotated[Session, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub") if payload else None
    user = db.scalar(select(User).where(User.id == user_id)) if user_id else None
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    return user


def owned_factory(factory_id: str, db: Session, user: User) -> Factory:
    factory = db.scalar(
        select(Factory).where(Factory.id == factory_id, Factory.owner_id == user.id)
    )
    if not factory:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="factory not found")
    return factory

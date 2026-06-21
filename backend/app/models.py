from pydantic import BaseModel
from typing import Optional
from datetime import datetime

DEFAULT_CATEGORY = "uncategorized"
FORBIDDEN_URL_MSG = "Forbidden URL: access to internal addresses is not allowed."


class VideoCreate(BaseModel):
    url: str
    category: str = DEFAULT_CATEGORY
    title: Optional[str] = None
    duration: Optional[float] = None
    thumbnail: Optional[str] = None

class VideoOut(BaseModel):
    id: int
    url: str
    category: str
    title: Optional[str]
    duration: Optional[float]
    thumbnail: Optional[str]
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}

class AuthRequest(BaseModel):
    password: str

class AuthResponse(BaseModel):
    token: str

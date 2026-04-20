from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class VideoCreate(BaseModel):
    url: str
    category: str
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
    created_at: datetime

    model_config = {"from_attributes": True}

class AuthRequest(BaseModel):
    password: str

class AuthResponse(BaseModel):
    token: str

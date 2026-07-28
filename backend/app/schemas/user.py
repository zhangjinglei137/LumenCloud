from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class SubscriptionInfo(BaseModel):
    id: str
    media_id: str
    media_title: str
    media_poster: Optional[str] = None
    voted: bool = False
    created_at: datetime


class RatingInfo(BaseModel):
    media_id: str
    score: int
    updated_at: datetime


class RatingRequest(BaseModel):
    score: int


class UserInteractionStatus(BaseModel):
    media_id: str
    status: str


class AdminSubscriptionItem(BaseModel):
    id: str
    user_id: str
    username: str
    media_id: str
    media_title: str
    media_type: str
    vote_count: int
    created_at: datetime

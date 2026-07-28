from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class DownloadTaskInfo(BaseModel):
    id: str
    media_id: str
    media_title: Optional[str] = None
    episode_range: Optional[str] = None
    file_name: Optional[str] = None
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None


class NotificationItem(BaseModel):
    id: str
    type: str
    title: str
    body: Optional[str] = None
    is_read: bool
    related_media_id: Optional[str] = None
    created_at: datetime


class ApproveRequest(BaseModel):
    media_id: str
    scan_frequency_hours: int = 24

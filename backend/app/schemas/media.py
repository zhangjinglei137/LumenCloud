from pydantic import BaseModel
from datetime import date, datetime
from typing import Optional


class MediaListItem(BaseModel):
    id: str
    tmdb_id: int
    title: str
    media_type: str
    poster_path: Optional[str] = None
    release_date: Optional[date] = None
    vote_average: Optional[float] = None
    status: str
    subscription_count: int = 0
    watch_count: int = 0
    created_at: datetime


class EpisodeDetail(BaseModel):
    id: str
    episode_number: int
    name: Optional[str] = None
    air_date: Optional[date] = None
    in_emby: bool = False


class SeasonDetail(BaseModel):
    id: str
    season_number: int
    name: Optional[str] = None
    episodes: list[EpisodeDetail] = []


class MediaDetail(BaseModel):
    id: str
    tmdb_id: int
    title: str
    original_title: Optional[str] = None
    media_type: str
    overview: Optional[str] = None
    poster_path: Optional[str] = None
    backdrop_path: Optional[str] = None
    release_date: Optional[date] = None
    vote_average: Optional[float] = None
    status: str
    subscription_count: int = 0
    watch_count: int = 0
    seasons: list[SeasonDetail] = []

import uuid
import enum
from datetime import datetime, date
from sqlalchemy import String, Integer, Boolean, DateTime, Date, Text, Float, ForeignKey, Enum, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class MediaType(str, enum.Enum):
    MOVIE = "movie"
    TV = "tv"


class MediaStatus(str, enum.Enum):
    UPCOMING = "upcoming"
    TRACKING = "tracking"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    PAUSED = "paused"


class Media(Base):
    __tablename__ = "media"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tmdb_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    original_title: Mapped[str] = mapped_column(String(512), nullable=True)
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType), nullable=False)
    overview: Mapped[str] = mapped_column(Text, nullable=True)
    poster_path: Mapped[str] = mapped_column(String(512), nullable=True)
    backdrop_path: Mapped[str] = mapped_column(String(512), nullable=True)
    release_date: Mapped[date] = mapped_column(Date, nullable=True)
    vote_average: Mapped[float] = mapped_column(Float, nullable=True)
    status: Mapped[MediaStatus] = mapped_column(Enum(MediaStatus), default=MediaStatus.UPCOMING)
    scan_frequency_hours: Mapped[int] = mapped_column(Integer, default=24)
    last_scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    seasons: Mapped[list["Season"]] = relationship(back_populates="media", cascade="all, delete-orphan")


class Season(Base):
    __tablename__ = "seasons"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    media_id: Mapped[str] = mapped_column(String(36), ForeignKey("media.id"), nullable=False)
    season_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=True)
    episode_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    media: Mapped["Media"] = relationship(back_populates="seasons")
    episodes: Mapped[list["Episode"]] = relationship(back_populates="season", cascade="all, delete-orphan")


class Episode(Base):
    __tablename__ = "episodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    season_id: Mapped[str] = mapped_column(String(36), ForeignKey("seasons.id"), nullable=False)
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=True)
    air_date: Mapped[date] = mapped_column(Date, nullable=True)
    in_emby: Mapped[bool] = mapped_column(Boolean, default=False)
    emby_item_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    season: Mapped["Season"] = relationship(back_populates="episodes")

import json 
from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy import String, Integer, Text, DateTime, ForeignKey, select, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

DATABASE_URL = 'sqlite+aiosqlite:///conduction.db'

# Create Async Session Factory
engine = create_async_engine(DATABASE_URL, echo=False)

AsyncSessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(225), nullable=False)
    path: Mapped[str] = mapped_column(String(1024),unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now(timezone.utc))
    last_scanned_at: Mapped[Optional[datetime]] = mapped_column(DateTime,nullable=True)
    tracks: Mapped[List["Track"]] = relationship("Track", back_populates="project", cascade="all, delete-orpha")


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    projectId: Mapped[int] = mapped_column(Integer, ForeignKey=("projects.id", ondelete="CASCADE"), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(String(50), default="feature")
    status: Mapped[str] = mapped_column(String(50), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now(timezone.utc))
    alingment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    depends_on: Mapped[dict] = mapped_column(JSON, default=list)
    blocks: Mapped[dict] = mapped_column(JSON, default=list)
    notes: Mapped[dict] = mapped_column(JSON, default=list)
    plan_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    spec_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

async def init_models():
    """Initialize database schemas"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

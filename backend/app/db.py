from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timezone
from app.config import get_settings
from app.models import DEFAULT_CATEGORY

settings = get_settings()
SQLALCHEMY_DATABASE_URL = settings.database_url

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Video(Base):
    __tablename__ = "videos"
    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, nullable=False)
    category = Column(String, nullable=True, default=DEFAULT_CATEGORY)
    title = Column(String, nullable=True)
    duration = Column(Float, nullable=True)  # duration in seconds
    thumbnail = Column(String, nullable=True)  # URL or base64
    source = Column(String, nullable=False, default="url")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_video_record(
    db,
    url: str,
    *,
    title: str | None = None,
    category: str = DEFAULT_CATEGORY,
    duration: float | None = None,
    thumbnail: str | None = None,
    source: str = "url",
) -> Video:
    """Create, persist, and return a standardized Video database record."""
    norm_url = url.strip().lower() if url else ""
    video = Video(
        url=norm_url,
        title=title,
        category=category or DEFAULT_CATEGORY,
        duration=duration,
        thumbnail=thumbnail,
        source=source,
    )
    db.add(video)
    db.commit()
    db.refresh(video)
    return video


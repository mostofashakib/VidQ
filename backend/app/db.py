from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from app.config import get_settings

settings = get_settings()
SQLALCHEMY_DATABASE_URL = settings.database_url

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Video(Base):
    __tablename__ = "videos"
    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, nullable=False)
    category = Column(String, nullable=False)
    title = Column(String, nullable=True)
    duration = Column(Float, nullable=True)  # duration in seconds
    thumbnail = Column(String, nullable=True)  # URL or base64
    created_at = Column(DateTime, default=datetime.utcnow)

# For local dev: drop and recreate table to add 'duration' column
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

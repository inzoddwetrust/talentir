from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database import Base
import config


def get_session():
    """Создает и возвращает фабрику сессий SQLAlchemy и движок базы данных"""
    engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False})
    session_factory = sessionmaker(bind=engine)
    return session_factory, engine


def init_tables(engine):
    """Инициализирует таблицы базы данных"""
    Base.metadata.create_all(engine)


# Для обратной совместимости с существующим кодом
Session, _engine = get_session()

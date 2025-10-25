from sqlalchemy import Column, Integer, String, Float, Text, UniqueConstraint
from models.base import Base


class Project(Base):
    __tablename__ = 'projects'

    # Автоинкрементный ID для внутреннего использования
    id = Column(Integer, primary_key=True, autoincrement=True)

    # Бизнес-ключи
    projectID = Column(Integer, nullable=False, index=True)
    lang = Column(String, nullable=False)

    # Остальные поля
    projectName = Column(String, nullable=False)
    projectTitle = Column(String)
    fullText = Column(Text)
    status = Column(String)
    rate = Column(Float)
    linkImage = Column(String)
    linkPres = Column(String)
    linkVideo = Column(String)
    docsFolder = Column(String)

    # Уникальное ограничение на пару projectID + lang
    __table_args__ = (
        UniqueConstraint('projectID', 'lang', name='_project_lang_uc'),
    )
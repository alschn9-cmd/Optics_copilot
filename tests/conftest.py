import pytest
from sqlmodel import Session, SQLModel, create_engine

from app import models as _models  # noqa: F401


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session

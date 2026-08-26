import os

from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Innstilling

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def hent_innstilling(session: Session) -> Innstilling:
    """Henter innstillingsraden, og oppretter den med standardverdier første gang."""
    innstilling = session.exec(select(Innstilling)).first()
    if innstilling is None:
        innstilling = Innstilling()
        session.add(innstilling)
        session.commit()
        session.refresh(innstilling)
    return innstilling


def get_session():
    with Session(engine) as session:
        yield session

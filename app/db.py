import os

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)


def _migrer_arrangement_tabell() -> None:
    """Lettvekts-migrering: legger til/omdøper kolonner på en allerede eksisterende
    'arrangement'-tabell etter modellendringer, uten å slette eksisterende rader.
    create_all() oppretter kun tabeller som mangler helt, og endrer aldri kolonner på
    en tabell som allerede finnes."""
    inspector = inspect(engine)
    if "arrangement" not in inspector.get_table_names():
        return

    kolonner = {kol["name"] for kol in inspector.get_columns("arrangement")}
    with engine.begin() as conn:
        if "original_tekst" not in kolonner:
            if "beskrivelse" in kolonner:
                conn.execute(text("ALTER TABLE arrangement RENAME COLUMN beskrivelse TO original_tekst"))
            else:
                conn.execute(text("ALTER TABLE arrangement ADD COLUMN original_tekst TEXT NOT NULL DEFAULT ''"))
        if "tekst_bekreftet" not in kolonner:
            conn.execute(text("ALTER TABLE arrangement ADD COLUMN tekst_bekreftet BOOLEAN NOT NULL DEFAULT 0"))


def _migrer_kilde_tabell() -> None:
    """Lettvekts-migrering: legger til manglende kolonner på en allerede eksisterende
    'kilde'-tabell, uten å slette eksisterende rader (kildelisten er kuratert av brukeren
    og skal aldri gå tapt ved skjemaendringer)."""
    inspector = inspect(engine)
    if "kilde" not in inspector.get_table_names():
        return

    kolonner = {kol["name"] for kol in inspector.get_columns("kilde")}
    with engine.begin() as conn:
        if "antall_vellykkede_hentinger" not in kolonner:
            conn.execute(text("ALTER TABLE kilde ADD COLUMN antall_vellykkede_hentinger INTEGER NOT NULL DEFAULT 0"))
        if "antall_feilede_hentinger" not in kolonner:
            conn.execute(text("ALTER TABLE kilde ADD COLUMN antall_feilede_hentinger INTEGER NOT NULL DEFAULT 0"))


def _migrer_artikkel_tabell() -> None:
    """'artikkel' er kun avledet, disponibel data (regenereres alltid fra bunnen av jobb 3-
    knappen), så ved skjemaendringer dropper vi den trygt fremfor å migrere kolonne for
    kolonne — det finnes ingen brukerdata her å ta vare på."""
    inspector = inspect(engine)
    if "artikkel" not in inspector.get_table_names():
        return

    kolonner = {kol["name"] for kol in inspector.get_columns("artikkel")}
    avsnitt_kolonner = (
        {kol["name"] for kol in inspector.get_columns("artikkelavsnitt")}
        if "artikkelavsnitt" in inspector.get_table_names()
        else set()
    )
    utdatert = (
        "tittel" not in kolonner
        or "ingress" not in kolonner
        or "mulig_kopiert" in avsnitt_kolonner
        or (avsnitt_kolonner and "kategori" not in avsnitt_kolonner)
    )
    if utdatert:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS artikkelavsnitt"))
            conn.execute(text("DROP TABLE artikkel"))


def init_db() -> None:
    _migrer_arrangement_tabell()
    _migrer_kilde_tabell()
    _migrer_artikkel_tabell()
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session

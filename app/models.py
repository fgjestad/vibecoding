from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class FagligTirsdag(SQLModel, table=True):
    """Én Faglig tirsdag, lagret per måned.

    Radene opprettes først når noen faktisk redigerer eller sletter en måned. Alle
    andre måneder finnes likevel i oversikten, med tredje tirsdag som dato — det er
    regelen, ikke databasen, som bestemmer hvilke dager som finnes."""

    id: Optional[int] = Field(default=None, primary_key=True)
    maaned: str = Field(index=True, unique=True, description="YYYY-MM")
    dato: Optional[str] = Field(
        default=None,
        description="Overstyrt dato (YYYY-MM-DD). None betyr tredje tirsdag i måneden.",
    )
    start: str = Field(default="", description="HH:MM. Tomt betyr standard klokkeslett.")
    slutt: str = Field(default="", description="HH:MM. Tomt betyr standard klokkeslett.")
    sted: str = Field(default="", description="Tomt betyr standard sted.")
    tema: str = Field(default="")
    foredragsholdere: str = Field(default="")
    notat: str = Field(default="")
    skjult: bool = Field(
        default=False,
        description="Slettet av admin — vises ikke for lesere og er ikke med i kalenderfila.",
    )
    opprettet_at: datetime = Field(default_factory=datetime.utcnow)
    oppdatert_at: datetime = Field(default_factory=datetime.utcnow)


class Innstilling(SQLModel, table=True):
    """Standardverdier nye dager arver. Alltid nøyaktig én rad (id = 1)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    standard_start: str = Field(default="08:30")
    standard_slutt: str = Field(default="09:30")
    standard_sted: str = Field(default="Raumnes-redaksjonen")
    oppdatert_at: datetime = Field(default_factory=datetime.utcnow)

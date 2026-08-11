from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Kilde(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    navn: str
    url: str
    aktiv: bool = True
    manuell_prioritet: int = Field(default=0, description="Satt av bruker")
    automatisk_prioritet: float = Field(
        default=0.0, description="Beregnet score basert på relevante treff (jobb 2)"
    )
    opprettet_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def samlet_sortering(self) -> float:
        return self.manuell_prioritet + self.automatisk_prioritet


class KildeForslag(SQLModel, table=True):
    """Kandidat-kilde foreslått av 'oppdag nye kilder', venter på godkjenning."""

    id: Optional[int] = Field(default=None, primary_key=True)
    navn: str
    url: str
    begrunnelse: str
    status: str = Field(default="ny")  # ny | godkjent | avvist
    opprettet_at: datetime = Field(default_factory=datetime.utcnow)

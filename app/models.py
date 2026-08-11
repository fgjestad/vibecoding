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


class Arrangement(SQLModel, table=True):
    """Ett arrangement i gjeldende utkast (jobb 2). Tømmes og fylles på nytt for hver kjøring."""

    id: Optional[int] = Field(default=None, primary_key=True)
    arrangement_id: str = Field(index=True, description="Hash av tittel+dato+sted, for dedup")
    tittel: str
    dato: str  # YYYY-MM-DD
    klokkeslett: Optional[str] = None
    sted: str
    arrangor: Optional[str] = None
    original_tekst: str = Field(description="Ordrett tekst hentet fra kilden, uredigert")
    tekst_bekreftet: bool = Field(
        default=False,
        description="True hvis koden har verifisert at teksten er ordrett identisk med kildesiden",
    )
    kategori: Optional[str] = Field(default=None, description="Settes i jobb 3")
    kilde_type: str  # nettsok | fast_kalender | skjermdump | pdf
    kilde_url: Optional[str] = None
    geografisk_relevans: str  # bekreftet | sannsynlig | usikker
    signatur: str = Field(
        index=True, description="Normalisert tittel+arrangør+ukedag, for eksklusjonshukommelse"
    )
    valgt: bool = True
    forhandsvalgt_bort: bool = Field(
        default=False, description="True hvis default-avhuket pga. kjent eksklusjonssignatur"
    )
    opprettet_at: datetime = Field(default_factory=datetime.utcnow)


class EkskludertSignatur(SQLModel, table=True):
    """Eksklusjonshukommelse: signaturer brukeren har fjernet fra tidligere utkast."""

    id: Optional[int] = Field(default=None, primary_key=True)
    signatur: str = Field(index=True)
    tittel_eksempel: str
    sist_fjernet_at: datetime = Field(default_factory=datetime.utcnow)

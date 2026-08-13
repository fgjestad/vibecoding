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
    antall_vellykkede_hentinger: int = Field(
        default=0, description="Antall innhøstingskjøringer denne kilden er hentet uten feil fra"
    )
    antall_feilede_hentinger: int = Field(
        default=0, description="Antall innhøstingskjøringer denne kilden har feilet under"
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
    dato: str  # YYYY-MM-DD (startdato hvis flerdagers)
    til_dato: Optional[str] = Field(
        default=None, description="Sluttdato (YYYY-MM-DD) hvis flerdagers, ellers None"
    )
    flere_datoer: Optional[str] = Field(
        default=None,
        description="JSON-liste med ALLE datoer (YYYY-MM-DD, sortert) et gjentakende "
        "arrangement på samme sted skjer på, satt når disse ikke nødvendigvis er "
        "sammenhengende dag for dag (f.eks. kun i helgene). dato/til_dato er da satt til "
        "hhv. tidligste og seneste av disse, for filtrering/sortering — selve visningen bruker "
        "denne lista i stedet, slik at det ikke ser ut som én sammenhengende periode.",
    )
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
    duplikat_gruppe: Optional[str] = Field(
        default=None,
        index=True,
        description="Delt id for alle rå kildefunn og en eventuell sammenslått oppføring i en duplikat-gruppe",
    )
    er_sammenslatt: bool = Field(
        default=False,
        description="True for en AI-sammenslått oppføring som kombinerer flere duplikate kildefunn til én",
    )
    opprettet_at: datetime = Field(default_factory=datetime.utcnow)


class EkskludertSignatur(SQLModel, table=True):
    """Eksklusjonshukommelse: signaturer brukeren har fjernet fra tidligere utkast."""

    id: Optional[int] = Field(default=None, primary_key=True)
    signatur: str = Field(index=True)
    tittel_eksempel: str
    sist_fjernet_at: datetime = Field(default_factory=datetime.utcnow)


class IkkeDuplikatPar(SQLModel, table=True):
    """Huker at to spesifikke signaturer IKKE skal regnes som duplikater av hverandre, selv
    om de matcher harvest.er_tittel_duplikat sin fuzzy tittel-sammenligning — satt når
    brukeren sier fra at en automatisk sammenslåing var feil (se
    /innhosting/{id}/ikke-duplikat), slik at de samme to arrangementene ikke slås sammen
    på nytt i en senere innhøsting."""

    id: Optional[int] = Field(default=None, primary_key=True)
    signatur_a: str = Field(index=True)
    signatur_b: str = Field(index=True)
    opprettet_at: datetime = Field(default_factory=datetime.utcnow)


class Artikkel(SQLModel, table=True):
    """Generert artikkelutkast for hele perioden (jobb 3). Erstattes helt ved hver nye kjøring."""

    id: Optional[int] = Field(default=None, primary_key=True)
    tittel: str
    ingress: str
    opprettet_at: datetime = Field(default_factory=datetime.utcnow)


class ArtikkelAvsnitt(SQLModel, table=True):
    """Ett avsnitt i artikkelen — tilsvarer ett arrangement. Redigerbart og kan flyttes."""

    id: Optional[int] = Field(default=None, primary_key=True)
    artikkel_id: int = Field(foreign_key="artikkel.id", index=True)
    arrangement_id: int = Field(foreign_key="arrangement.id")
    tekst: str = Field(description="Omskrevet avsnittstekst. **dobbel stjerne** markerer fet skrift")
    kategori: str = Field(description="Fritt valgt av Claude, brukes til mellomtitler i artikkelen")
    rekkefolge: int = Field(default=0, index=True)


class Innstilling(SQLModel, table=True):
    """Enkelt nøkkel-verdi-lager for globale innstillinger. Kun én rad (id=1) brukes."""

    id: Optional[int] = Field(default=None, primary_key=True)
    antall_dager: int = Field(
        default=14, description="Hvor mange dager fram i tid (fra i morgen) perioden dekker"
    )
    auto_innhosting_aktiv: bool = Field(
        default=False, description="True hvis 'Kjør innhøsting' skal kjøres automatisk på et fast tidsskjema"
    )
    auto_innhosting_frekvens: str = Field(
        default="daglig", description="daglig | ukentlig"
    )
    auto_innhosting_ukedag: int = Field(
        default=0, description="Ukedag for ukentlig autojobb: 0=mandag ... 6=søndag"
    )
    auto_innhosting_klokkeslett: str = Field(
        default="06:00", description="Klokkeslett (HH:MM) autojobben kjører på"
    )
    artikkel_instruks: Optional[str] = Field(
        default=None,
        description="Admin-lagret overstyring av stilinstruksen for artikkelgenerering. "
        "None/tom betyr at den innebygde standarden (standard_artikkel_instruks) brukes.",
    )

"""Datoregning, ICS-eksport og Google-lenker for Faglig tirsdag."""

from calendar import monthrange
from datetime import date, datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

OSLO = ZoneInfo("Europe/Oslo")

MAANEDER = [
    "januar", "februar", "mars", "april", "mai", "juni",
    "juli", "august", "september", "oktober", "november", "desember",
]
UKEDAGER = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
UKEDAGER_KORT = ["ma", "ti", "on", "to", "fr", "lø", "sø"]

TIRSDAG = 1  # date.weekday(): mandag = 0


def tredje_tirsdag(aar: int, maaned: int) -> date:
    """Den tredje tirsdagen i måneden — utgangspunktet for hver Faglig tirsdag."""
    foerste = date(aar, maaned, 1)
    foerste_tirsdag = 1 + (TIRSDAG - foerste.weekday()) % 7
    return date(aar, maaned, foerste_tirsdag + 14)


def maaned_celler(aar: int, maaned: int) -> list[date | None]:
    """Rutene i et månedsrutenett, radvis fra mandag. None er de tomme rutene før
    den 1. og etter den siste, slik at ukene alltid blir hele rader på sju."""
    foerste = date(aar, maaned, 1)
    celler: list[date | None] = [None] * foerste.weekday()
    celler += [date(aar, maaned, dag) for dag in range(1, monthrange(aar, maaned)[1] + 1)]
    while len(celler) % 7:
        celler.append(None)
    return celler


def maaned_navn(maaned: int) -> str:
    return MAANEDER[maaned - 1]


def les_maaned_noekkel(noekkel: str) -> tuple[int, int]:
    aar, maaned = noekkel.split("-")
    return int(aar), int(maaned)


def formater_dato(dato: date, med_aar: bool = True) -> str:
    tekst = f"{UKEDAGER[dato.weekday()]} {dato.day}. {maaned_navn(dato.month)}"
    return f"{tekst} {dato.year}" if med_aar else tekst


def les_klokkeslett(verdi: str, standard: time) -> time:
    """Tolker 'HH:MM' fra skjema eller database, og faller tilbake til standarden
    hvis feltet er tomt eller ugyldig — et skrivefeil-klokkeslett skal ikke kunne
    velte hele oversikten."""
    try:
        timer, minutter = verdi.strip().split(":")
        return time(int(timer), int(minutter))
    except (ValueError, AttributeError):
        return standard


def _utc(dato: date, klokkeslett: time) -> datetime:
    """Norsk lokaltid til UTC. Kalenderfiler oppgir tidspunktene i UTC, slik at
    sommer-/vintertid blir riktig uten at ICS-fila må bære med seg en hel
    tidssonedefinisjon."""
    return datetime.combine(dato, klokkeslett, tzinfo=OSLO).astimezone(ZoneInfo("UTC"))


def _ics_tid(dato: date, klokkeslett: time) -> str:
    return _utc(dato, klokkeslett).strftime("%Y%m%dT%H%M%SZ")


def _ics_tekst(verdi: str) -> str:
    """Escaper tegn som har spesialbetydning i ICS-formatet (RFC 5545)."""
    return (
        verdi.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _brett(linje: str) -> str:
    """ICS-linjer skal ikke være lengre enn 75 oktetter; resten brettes over på
    neste linje med et innledende mellomrom."""
    raa = linje.encode("utf-8")
    if len(raa) <= 75:
        return linje
    biter, gjeldende = [], b""
    for tegn in linje:
        kodet = tegn.encode("utf-8")
        grense = 75 if not biter else 74
        if len(gjeldende) + len(kodet) > grense:
            biter.append(gjeldende)
            gjeldende = b""
        gjeldende += kodet
    biter.append(gjeldende)
    return "\r\n ".join(bit.decode("utf-8") for bit in biter)


def ics_dokument(hendelser: list[dict], navn: str = "Faglig tirsdag") -> str:
    """Bygger en komplett ICS-fil. Hver hendelse er en dict med nøklene
    uid, dato, start, slutt, tittel, beskrivelse, sted og sekvens."""
    linjer = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Raumnes//Faglig tirsdag//NO",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_tekst(navn)}",
        "X-WR-TIMEZONE:Europe/Oslo",
        # Hint til Google/Outlook om hvor ofte abonnementet bør hentes på nytt.
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    stemplet = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    for h in hendelser:
        linjer += [
            "BEGIN:VEVENT",
            f"UID:{h['uid']}",
            f"DTSTAMP:{stemplet}",
            f"DTSTART:{_ics_tid(h['dato'], h['start'])}",
            f"DTEND:{_ics_tid(h['dato'], h['slutt'])}",
            f"SEQUENCE:{h.get('sekvens', 0)}",
            f"SUMMARY:{_ics_tekst(h['tittel'])}",
        ]
        if h.get("beskrivelse"):
            linjer.append(f"DESCRIPTION:{_ics_tekst(h['beskrivelse'])}")
        if h.get("sted"):
            linjer.append(f"LOCATION:{_ics_tekst(h['sted'])}")
        linjer.append("END:VEVENT")
    linjer.append("END:VCALENDAR")
    return "\r\n".join(_brett(linje) for linje in linjer) + "\r\n"


def google_lenke(dato: date, start: time, slutt: time, tittel: str, beskrivelse: str, sted: str) -> str:
    """Lenke som åpner Google Kalender med et ferdig utfylt nytt oppføringsskjema.

    Dette er «koplingen» mot Google: den krever ingen API-nøkkel eller
    tilgangsstyring — brukeren må bare være innlogget i Google i samme nettleser."""
    tidsrom = f"{_ics_tid(dato, start)}/{_ics_tid(dato, slutt)}"
    deler = [
        "action=TEMPLATE",
        f"text={quote(tittel)}",
        f"dates={tidsrom}",
        f"details={quote(beskrivelse)}",
        f"location={quote(sted)}",
        "ctz=Europe/Oslo",
    ]
    return "https://calendar.google.com/calendar/render?" + "&".join(deler)

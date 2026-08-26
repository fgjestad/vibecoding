"""Norske helligdager og andre dager det er verdt å advare mot, beregnet lokalt.

Alt regnes ut fra påskedagen med Meeus/Jones/Butcher-algoritmen, slik at appen aldri
trenger nett eller et oppdatert datasett for å vite hvilke dager som er røde — den
virker like godt for 2031 som for i år.
"""

from datetime import date, timedelta
from functools import lru_cache


def paaskedag(aar: int) -> date:
    """Første påskedag i det gregorianske året (Meeus/Jones/Butcher)."""
    a = aar % 19
    b, c = divmod(aar, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    maaned, dag = divmod(h + m - 7 * n + 114, 31)
    return date(aar, maaned, dag + 1)


@lru_cache(maxsize=64)
def roede_dager(aar: int) -> dict[date, str]:
    """De offisielle røde dagene (helligdager og høytidsdager) i et år.

    Vanlige søndager er røde de også, men tas ikke med her: Faglig tirsdag havner
    aldri på en søndag med mindre datoen overstyres manuelt, og da er det åpenbart
    for den som overstyrer."""
    paaske = paaskedag(aar)
    dager: dict[date, str] = {}
    for dato, navn in (
        (date(aar, 1, 1), "første nyttårsdag"),
        (paaske - timedelta(days=3), "skjærtorsdag"),
        (paaske - timedelta(days=2), "langfredag"),
        (paaske, "første påskedag"),
        (paaske + timedelta(days=1), "andre påskedag"),
        (date(aar, 5, 1), "offentlig høytidsdag (1. mai)"),
        (date(aar, 5, 17), "grunnlovsdagen (17. mai)"),
        (paaske + timedelta(days=39), "Kristi himmelfartsdag"),
        (paaske + timedelta(days=49), "første pinsedag"),
        (paaske + timedelta(days=50), "andre pinsedag"),
        (date(aar, 12, 25), "første juledag"),
        (date(aar, 12, 26), "andre juledag"),
    ):
        # 1. og 17. mai er faste datoer, mens påske- og pinsedagene flytter seg, så
        # de kan lande på hverandre (17. mai 2027 er også andre pinsedag). Da skal
        # begge navnene fram, ikke bare det siste som ble satt inn.
        dager[dato] = f"{dager[dato]} og {navn}" if dato in dager else navn
    return dager


def roed_dag(dato: date) -> str | None:
    """Navnet på helligdagen hvis datoen er en rød dag, ellers None."""
    return roede_dager(dato.year).get(dato)


def ferieadvarsel(dato: date) -> str | None:
    """Dager som ikke er røde, men der det erfaringsmessig er få folk på jobb.

    Dette er kun et hint i grensesnittet — det er alltid brukeren som avgjør om
    dagen skal slettes."""
    if dato.month == 7:
        return "Fellesferie"
    if dato.month == 12 and dato.day >= 22:
        return "Romjul/juleferie"
    if dato.month == 1 and dato.day == 1:
        return "Nyttårsferie"
    if (dato.month, dato.day) in {(12, 24), (12, 31)}:
        return "Kort arbeidsdag"

    # Uken fra og med skjærtorsdag er offisielt rød fra torsdag, men mange tar ut
    # hele påskeuken — mandag til onsdag før skjærtorsdag markeres derfor mykt.
    paaske = paaskedag(dato.year)
    if paaske - timedelta(days=6) <= dato <= paaske - timedelta(days=4):
        return "Påskeuken"
    return None

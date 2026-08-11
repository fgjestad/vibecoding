import base64
import hashlib
import json
import os
import re
import unicodedata
from datetime import date, timedelta

from anthropic import Anthropic

from app.models import Kilde

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")

UKEDAGER = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]

STED_BESKRIVELSE = (
    "Nes kommune på Romerike i Akershus, Norge (kjente steder: Årnes, Vormsund, Fenstad, "
    "Auli, Neskollen, Udnes, Skogbygda, Runni. IKKE Nes i Hallingdal/Buskerud eller Nesodden.)"
)


def beregn_periode(i_dag: date | None = None) -> tuple[date, date]:
    """Returnerer (forste_dag, siste_dag): fra førstkommende fredag og 14 dager frem."""
    i_dag = i_dag or date.today()
    dager_til_fredag = (4 - i_dag.weekday()) % 7
    forste_dag = i_dag + timedelta(days=dager_til_fredag)
    siste_dag = forste_dag + timedelta(days=13)
    return forste_dag, siste_dag


def _normaliser_tekst(tekst: str | None) -> str:
    tekst = unicodedata.normalize("NFKD", (tekst or "").lower().strip())
    tekst = re.sub(r"[^\w\s]", "", tekst)
    tekst = re.sub(r"\s+", " ", tekst)
    return tekst


def beregn_arrangement_id(tittel: str, dato_str: str, sted: str) -> str:
    """Innholdsbasert id for å gjenkjenne samme arrangement fra flere kilder."""
    grunnlag = f"{_normaliser_tekst(tittel)}|{dato_str}|{_normaliser_tekst(sted)}"
    return hashlib.sha256(grunnlag.encode()).hexdigest()[:16]


def beregn_signatur(tittel: str, arrangor: str | None, dato_str: str) -> str:
    """Ukedagsbasert signatur (ikke eksakt dato) for eksklusjonshukommelse på tvers av uker."""
    try:
        ukedag = UKEDAGER[date.fromisoformat(dato_str).weekday()]
    except (ValueError, TypeError):
        ukedag = ""
    grunnlag = f"{_normaliser_tekst(tittel)}|{_normaliser_tekst(arrangor)}|{ukedag}"
    return hashlib.sha256(grunnlag.encode()).hexdigest()[:16]


def _parse_json_liste(tekst: str) -> list[dict]:
    match = re.search(r"\[.*\]", tekst, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("tittel") and e.get("dato")]


def standard_instruks(forste_dag: date, siste_dag: date) -> str:
    """Standard-instruksen som sendes til Claude for å hente ut arrangementer.

    Vises redigerbar i UI (jobb 2) slik at brukeren kan finpusse den før en kjøring.
    """
    return f"""Perioden vi er interessert i: {forste_dag.isoformat()} til {siste_dag.isoformat()} \
(begge datoer inkludert).

For hvert arrangement i denne perioden, hent ut:
- "tittel": kort tittel på arrangementet
- "dato": dato i format YYYY-MM-DD
- "klokkeslett": klokkeslett hvis oppgitt (f.eks. "18:00"), ellers null
- "sted": stedsnavn/adresse, så spesifikt som mulig
- "arrangor": arrangør/avsender hvis oppgitt, ellers null
- "beskrivelse": 1-2 setninger, KORT OMSKREVET MED EGNE ORD (ikke kopiert direkte fra kilden)
- "geografisk_relevans": "bekreftet" hvis stedet tydelig er i {STED_BESKRIVELSE}, \
"sannsynlig" hvis uklart men trolig lokalt, "usikker" hvis reelt i tvil

Ikke ta med arrangementer som tydelig skjer utenfor Nes kommune, eller utenfor perioden over.

Svar til slutt KUN med et gyldig JSON-array, ingen tekst før eller etter. Hvis du ikke finner \
noen relevante arrangementer, svar med et tomt array: []"""


def hent_fra_kilde(
    kilde: Kilde,
    forste_dag: date,
    siste_dag: date,
    instruks: str | None = None,
) -> list[dict]:
    """Henter arrangementer fra én kilde-URL ved hjelp av Claude med web_fetch."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return []

    instruks = instruks if instruks is not None else standard_instruks(forste_dag, siste_dag)
    client = Anthropic()
    prompt = f"""Gå til denne nettsiden og finn lokale arrangementer: {kilde.url}

{instruks}
"""
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        tools=[{"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3}],
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": prompt}],
    )

    tekst = "".join(b.text for b in response.content if b.type == "text")
    arrangementer = _parse_json_liste(tekst)
    for a in arrangementer:
        a["kilde_type"] = "fast_kalender"
        a["kilde_url"] = kilde.url
    return arrangementer


def hent_fra_bilde(
    data: bytes,
    media_type: str,
    forste_dag: date,
    siste_dag: date,
    instruks: str | None = None,
) -> list[dict]:
    """Tolker en skjermdump (bilde) og henter ut arrangementer."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return []

    instruks = instruks if instruks is not None else standard_instruks(forste_dag, siste_dag)
    client = Anthropic()
    b64 = base64.standard_b64encode(data).decode("utf-8")
    prompt = f"""Dette er en skjermdump av et innlegg/en side om ett eller flere arrangementer \
(f.eks. fra Facebook). Se på bildet og hent ut arrangementene som er omtalt.

{instruks}
"""
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        output_config={"effort": "medium"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    tekst = "".join(b.text for b in response.content if b.type == "text")
    arrangementer = _parse_json_liste(tekst)
    for a in arrangementer:
        a["kilde_type"] = "skjermdump"
        a["kilde_url"] = None
    return arrangementer


def hent_fra_pdf(
    data: bytes,
    forste_dag: date,
    siste_dag: date,
    instruks: str | None = None,
) -> list[dict]:
    """Tolker en PDF (f.eks. papiravis-annonse) og henter ut arrangementer."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return []

    instruks = instruks if instruks is not None else standard_instruks(forste_dag, siste_dag)
    client = Anthropic()
    b64 = base64.standard_b64encode(data).decode("utf-8")
    prompt = f"""Dette er en PDF med én eller flere arrangementsannonser (f.eks. fra \
papiravisen). Se gjennom dokumentet og hent ut arrangementene som er omtalt.

{instruks}
"""
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        output_config={"effort": "medium"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    tekst = "".join(b.text for b in response.content if b.type == "text")
    arrangementer = _parse_json_liste(tekst)
    for a in arrangementer:
        a["kilde_type"] = "pdf"
        a["kilde_url"] = None
    return arrangementer

import base64
import hashlib
import json
import os
import re
import unicodedata
from datetime import date, timedelta
from html.parser import HTMLParser

import httpx
from anthropic import Anthropic

from app.models import Kilde

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

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


class _TekstUttrekker(HTMLParser):
    """Enkel HTML-til-tekst-konverterer (stdlib, ingen ekstern avhengighet)."""

    _HOPP_OVER_TAGGER = {"script", "style", "noscript", "template"}

    def __init__(self):
        super().__init__()
        self._biter: list[str] = []
        self._hopp_over_dybde = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._HOPP_OVER_TAGGER:
            self._hopp_over_dybde += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._biter.append("\n")

    def handle_endtag(self, tag):
        if tag in self._HOPP_OVER_TAGGER and self._hopp_over_dybde > 0:
            self._hopp_over_dybde -= 1

    def handle_data(self, data):
        if self._hopp_over_dybde == 0:
            self._biter.append(data)

    def hent_tekst(self) -> str:
        rå = "".join(self._biter)
        linjer = [linje.strip() for linje in rå.splitlines()]
        return "\n".join(linje for linje in linjer if linje)


def _html_til_tekst(html: str) -> str:
    uttrekker = _TekstUttrekker()
    uttrekker.feed(html)
    return uttrekker.hent_tekst()


def _hent_side_tekst(url: str) -> str | None:
    """Henter en URL programmatisk og returnerer synlig tekst. None ved feil."""
    try:
        respons = httpx.get(
            url,
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RaumnesArrangementer/1.0)"},
        )
        respons.raise_for_status()
    except httpx.HTTPError:
        return None

    content_type = respons.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        return None

    return _html_til_tekst(respons.text)


def _normaliser_for_sammenligning(tekst: str) -> str:
    tekst = unicodedata.normalize("NFKD", (tekst or "").lower())
    tekst = re.sub(r"\s+", " ", tekst).strip()
    return tekst


def _er_ordrett(utdrag: str, kildetekst: str) -> bool:
    """Sjekker om utdraget forekommer ordrett (case/whitespace-uavhengig) i kildeteksten."""
    if not utdrag or not kildetekst:
        return False
    return _normaliser_for_sammenligning(utdrag) in _normaliser_for_sammenligning(kildetekst)


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
- "original_tekst": teksten som beskriver arrangementet, KOPIERT ORDRETT fra kilden (ikke \
omskrevet, ikke forkortet, ikke oppsummert — den skal være identisk med teksten slik den står \
på kilden, tegn for tegn)
- "geografisk_relevans": "bekreftet" hvis stedet tydelig er i {STED_BESKRIVELSE}, \
"sannsynlig" hvis uklart men trolig lokalt, "usikker" hvis reelt i tvil

Ikke ta med arrangementer som tydelig skjer utenfor Nes kommune, eller utenfor perioden over.

Svar til slutt KUN med et gyldig JSON-array, ingen tekst før eller etter. Hvis du ikke finner \
noen relevante arrangementer, svar med et tomt array: []"""


def _hent_fra_kilde_via_sidetekst(
    kilde: Kilde,
    sidetekst: str,
    instruks: str,
) -> list[dict] | None:
    """Sender allerede hentet sidetekst til Claude for uttrekk. None hvis API-kallet feiler."""
    client = Anthropic()
    prompt = f"""Under er den rå teksten fra nettsiden {kilde.url}, hentet automatisk. Bruk KUN \
denne teksten som kilde — ikke gjett eller fyll inn informasjon som ikke står der.

--- START SIDETEKST ---
{sidetekst[:100000]}
--- SLUTT SIDETEKST ---

{instruks}
"""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return None

    tekst = "".join(b.text for b in response.content if b.type == "text")
    return _parse_json_liste(tekst)


def hent_fra_kilde(
    kilde: Kilde,
    forste_dag: date,
    siste_dag: date,
    instruks: str | None = None,
) -> list[dict]:
    """Henter arrangementer fra én kilde-URL.

    Prøver først å hente siden programmatisk og la Claude lese av den rå teksten — da kan
    hvert "original_tekst"-utdrag verifiseres kode-messig mot det som faktisk står på siden
    (tekst_bekreftet=True). Hvis den programmatiske hentingen feiler (f.eks. siden krever
    JavaScript), faller vi tilbake til at Claude selv henter siden med web_fetch-verktøyet;
    da kan vi ikke verifisere ordrett samsvar i kode, så tekst_bekreftet settes til False.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return []

    instruks = instruks if instruks is not None else standard_instruks(forste_dag, siste_dag)

    sidetekst = _hent_side_tekst(kilde.url)
    if sidetekst:
        arrangementer = _hent_fra_kilde_via_sidetekst(kilde, sidetekst, instruks)
        if arrangementer is not None:
            for a in arrangementer:
                a["kilde_type"] = "fast_kalender"
                a["kilde_url"] = kilde.url
                a["tekst_bekreftet"] = _er_ordrett(a.get("original_tekst", ""), sidetekst)
            return arrangementer

    return _hent_fra_kilde_via_web_fetch(kilde, instruks)


def _hent_fra_kilde_via_web_fetch(kilde: Kilde, instruks: str) -> list[dict]:
    """Reserveløsning: Claude henter siden selv via web_fetch-verktøyet.

    Brukes kun når programmatisk henting av siden feiler. Ordrett samsvar kan da ikke
    verifiseres i kode, så alle treff får tekst_bekreftet=False.
    """
    client = Anthropic()
    prompt = f"""Gå til denne nettsiden og finn lokale arrangementer: {kilde.url}

{instruks}
"""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            tools=[{"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3}],
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return []

    tekst = "".join(b.text for b in response.content if b.type == "text")
    arrangementer = _parse_json_liste(tekst)
    for a in arrangementer:
        a["kilde_type"] = "fast_kalender"
        a["kilde_url"] = kilde.url
        a["tekst_bekreftet"] = False
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
        a["tekst_bekreftet"] = False
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
        a["tekst_bekreftet"] = False
    return arrangementer

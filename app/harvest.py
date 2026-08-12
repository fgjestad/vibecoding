import base64
import hashlib
import json
import os
import re
import unicodedata
from datetime import date, timedelta
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from anthropic import Anthropic

from app.models import Kilde

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

UKEDAGER = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]

STED_BESKRIVELSE = (
    "Nes kommune på Romerike i Akershus, Norge (kjente steder: Årnes, Vormsund, Fenstad, "
    "Auli, Neskollen, Udnes, Skogbygda, Runni. IKKE Nes i Hallingdal/Buskerud eller Nesodden.)"
)

MINSTE_SIDETEKST_LENGDE = 500

# Nes kommunes aktivitetskalender (og noen nabokommuner på samme "ØRU"-plattform) er en
# JavaScript-widget: selve siden inneholder ingen arrangementer i den rå HTML-en — de hentes
# separat fra dette API-et etter at siden er lastet i en nettleser. Siden vi ikke kan kjøre
# JavaScript, henter vi fra dette API-et direkte i stedet for å prøve å skrape (eller be
# Claude prøve å skrape) den tomme siden. Bonus: ingen AI-kall trengs for denne kilden.
PROKOM_KALENDER_KOMMUNER = {"nes.kommune.no": "Nes"}
PROKOM_KALENDER_API = "https://sspkalender.prokom.no/api/tidspunkt"
PROKOM_BESKRIVELSE_NOKLER = ("beskriv", "description", "ingress", "omtale", "tekst", "info", "innhold")


def _er_prokom_kalender(url: str) -> str | None:
    """Returnerer kommunenavnet (til API-parameteren) hvis URL-en er en kjent Prokom/ØRU-
    aktivitetskalender, ellers None."""
    vert = urlparse(url).hostname or ""
    for kommune_vert, kommune_navn in PROKOM_KALENDER_KOMMUNER.items():
        if vert.endswith(kommune_vert) and "aktivitetskalender" in url:
            return kommune_navn
    return None


def _finn_prokom_beskrivelse(kalenderobjekt: dict) -> str | None:
    for nokkel, verdi in kalenderobjekt.items():
        if isinstance(verdi, str) and verdi.strip() and any(n in nokkel.lower() for n in PROKOM_BESKRIVELSE_NOKLER):
            return verdi.strip()
    return None


def _hent_fra_prokom_kalender(
    kilde: Kilde, kommune_navn: str, forste_dag: date, siste_dag: date
) -> list[dict]:
    """Henter strukturerte arrangementsdata direkte fra Prokom/ØRU-kalenderens eget API.

    Dataene kommer strukturert og direkte fra kildens egen database (ingen AI-omskriving),
    så original_tekst kan trygt merkes tekst_bekreftet=True."""
    fra_str = forste_dag.strftime("%d.%m.%Y")
    til_str = siste_dag.strftime("%d.%m.%Y")
    url = (
        f"{PROKOM_KALENDER_API}?Categories=0&SearchText=&DateFrom={fra_str}&DateTo={til_str}"
        f"&Municipalities={kommune_navn}&Kunde=oru&Id=&ItemDate=&WeekDays=&List=&Count=200&Distributor="
    )
    respons = httpx.get(
        url, timeout=20.0, headers={"User-Agent": "Mozilla/5.0 (compatible; RaumnesArrangementer/1.0)"}
    )
    respons.raise_for_status()
    data = respons.json()

    hendelser = []
    for maned in data.get("MonthWithEvents") or []:
        for dag in maned.get("DaysWithEvents") or []:
            hendelser.extend(dag.get("Events") or [])

    parsed = urlparse(kilde.url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    arrangementer = []
    for hendelse in hendelser:
        objekt = hendelse.get("KalenderObjekt") or {}
        if objekt.get("Avlyst"):
            continue

        tittel = str(objekt.get("Name") or "").strip()
        fra_dato = str(hendelse.get("FraDato") or "")
        if not tittel or not fra_dato:
            continue

        dato_del, _, tid_del = fra_dato.partition("T")
        try:
            hendelse_dato = date.fromisoformat(dato_del)
        except ValueError:
            continue
        if not (forste_dag <= hendelse_dato <= siste_dag):
            continue

        klokkeslett = tid_del[:5] if tid_del else None
        sted = str((hendelse.get("Lokasjon") or {}).get("Name") or "").strip()

        beskrivelse = _finn_prokom_beskrivelse(objekt)
        if not beskrivelse:
            tid_tekst = f" kl. {klokkeslett}" if klokkeslett else ""
            beskrivelse = f"{tittel} – {dato_del}{tid_tekst}, {sted}.".strip()

        hendelse_id = hendelse.get("Id")
        detalj_url = f"{base_url}/aktivitetskalender/event#{hendelse_id}" if hendelse_id else kilde.url

        arrangementer.append(
            {
                "tittel": tittel,
                "dato": dato_del,
                "klokkeslett": klokkeslett,
                "sted": sted,
                "arrangor": None,
                "original_tekst": beskrivelse,
                "geografisk_relevans": "bekreftet",
                "kilde_type": "fast_kalender",
                "kilde_url": detalj_url,
                "tekst_bekreftet": True,
            }
        )
    return arrangementer


def beregn_periode(antall_dager: int = 14, i_dag: date | None = None) -> tuple[date, date]:
    """Returnerer (forste_dag, siste_dag): fra i morgen og "antall_dager" dager frem."""
    i_dag = i_dag or date.today()
    forste_dag = i_dag + timedelta(days=1)
    siste_dag = forste_dag + timedelta(days=max(antall_dager, 1) - 1)
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


def beregn_duplikat_nokkel(tittel: str, dato_str: str) -> str:
    """Løsere nøkkel enn arrangement_id (som også krever eksakt samme stedstekst) — brukes
    til å oppdage sannsynlige duplikater på tvers av kilder, f.eks. samme konsert oppført med
    litt ulik stedsformulering to steder."""
    return f"{_normaliser_tekst(tittel)}|{dato_str}"


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


def _hent_side_tekst(url: str) -> tuple[str, str] | None:
    """Henter en URL programmatisk. Returnerer (synlig_tekst, endelig_url_etter_omdirigering),
    eller None ved feil."""
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

    return _html_til_tekst(respons.text), str(respons.url)


def _vertsnavn(url: str) -> str:
    vert = urlparse(url).hostname or ""
    return vert[4:] if vert.startswith("www.") else vert


def _samme_domene(url_a: str, url_b: str) -> bool:
    return bool(_vertsnavn(url_a)) and _vertsnavn(url_a) == _vertsnavn(url_b)


def normaliser_url_for_dedup(url: str) -> str:
    """Normaliserer en URL for duplikatsjekk (ignorerer protokoll, www og etterslengende /)."""
    url = (url or "").strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.rstrip("/")


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
) -> tuple[list[dict], str | None]:
    """Henter arrangementer fra én kilde-URL. Returnerer (arrangementer, foreslått_url).

    foreslått_url er en mer presis URL for kilden (f.eks. etter omdirigering, eller
    foreslått av Claude) hvis vi fant en, ellers None — brukes til å oppdatere kildelisten
    slik at neste kjøring kan hente direkte uten omveier.

    Kjente Prokom/ØRU-kalender-widgets (f.eks. Nes kommunes aktivitetskalender) hentes
    direkte fra kildens eget API — se _hent_fra_prokom_kalender.

    Ellers prøver vi først å hente siden programmatisk og la Claude lese av den rå teksten —
    da kan hvert "original_tekst"-utdrag verifiseres kode-messig mot det som faktisk står på
    siden (tekst_bekreftet=True). Hvis den programmatiske hentingen feiler (f.eks. siden
    krever JavaScript), faller vi tilbake til at Claude selv henter siden med web_fetch-
    verktøyet; da kan vi ikke verifisere ordrett samsvar i kode, så tekst_bekreftet settes
    til False.
    """
    kommune_navn = _er_prokom_kalender(kilde.url)
    if kommune_navn:
        return _hent_fra_prokom_kalender(kilde, kommune_navn, forste_dag, siste_dag), None

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return [], None

    instruks = instruks if instruks is not None else standard_instruks(forste_dag, siste_dag)

    resultat = _hent_side_tekst(kilde.url)
    if resultat and len(resultat[0]) < MINSTE_SIDETEKST_LENGDE:
        # Mistenkelig lite tekst — sannsynligvis en side der innholdet lastes inn med
        # JavaScript etter at siden er hentet (f.eks. en kalender-widget), så den
        # programmatiske hentingen har ikke fått med de faktiske arrangementene. Gå rett
        # til reserveløsningen i stedet for å tolke en nesten tom side som "ingen treff".
        resultat = None
    if resultat:
        sidetekst, endelig_url = resultat
        arrangementer = _hent_fra_kilde_via_sidetekst(kilde, sidetekst, instruks)
        if arrangementer is not None:
            for a in arrangementer:
                a["kilde_type"] = "fast_kalender"
                a["kilde_url"] = kilde.url
                a["tekst_bekreftet"] = _er_ordrett(a.get("original_tekst", ""), sidetekst)
            foreslatt_url = None
            if endelig_url and endelig_url != kilde.url and _samme_domene(endelig_url, kilde.url):
                foreslatt_url = endelig_url
            return arrangementer, foreslatt_url

    return _hent_fra_kilde_via_web_fetch(kilde, instruks)


def _hent_fra_kilde_via_web_fetch(kilde: Kilde, instruks: str) -> tuple[list[dict], str | None]:
    """Reserveløsning: Claude henter siden selv via web_fetch-verktøyet.

    Brukes kun når programmatisk henting av siden feiler. Ordrett samsvar kan da ikke
    verifiseres i kode, så alle treff får tekst_bekreftet=False.
    """
    client = Anthropic()
    prompt = f"""Gå til denne nettsiden og finn lokale arrangementer: {kilde.url}

Hvis den faktiske arrangementsoversikten ligger på en mer presis underside enn URL-en over \
(f.eks. en egen kalender-side du ble ledet til), skriv dette på en egen linje FØRST i svaret, \
i formatet: FAKTISK_URL: <url>. Hvis utgangspunktet allerede er presist nok, hopp over denne \
linjen helt.

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
        return [], None

    tekst = "".join(b.text for b in response.content if b.type == "text")

    foreslatt_url = None
    url_treff = re.search(r"FAKTISK_URL:\s*(\S+)", tekst)
    if url_treff:
        kandidat = url_treff.group(1).strip().rstrip(".,)")
        if kandidat.startswith("http") and kandidat != kilde.url and _samme_domene(kandidat, kilde.url):
            foreslatt_url = kandidat

    arrangementer = _parse_json_liste(tekst)
    for a in arrangementer:
        a["kilde_type"] = "fast_kalender"
        a["kilde_url"] = kilde.url
        a["tekst_bekreftet"] = False
    return arrangementer, foreslatt_url


def hent_mer_info(
    kilde_url: str | None,
    tittel: str,
    dato: str,
    klokkeslett: str | None,
    sted: str,
) -> tuple[str | None, bool, str | None]:
    """Prøver å finne mer utfyllende tekst for ETT bestemt arrangement, f.eks. ved å følge
    en lenke fra oversiktssiden til arrangementets egen detaljside.

    Brukes manuelt (knapp per arrangement) fremfor automatisk for alle, siden det koster ett
    ekstra AI-kall per arrangement og ville gjort innhøstingen tregere for alle om den kjørte
    for hvert treff.

    Returnerer (ny_tekst, bekreftet, ny_kilde_url). ny_tekst er None hvis ingenting nytt ble
    funnet. bekreftet er True kun hvis vi klarte å kode-verifisere teksten mot en side vi
    hentet selv (samme prinsipp som i hent_fra_kilde).
    """
    if not os.environ.get("ANTHROPIC_API_KEY") or not kilde_url:
        return None, False, None

    client = Anthropic()
    detaljer = f'kl. {klokkeslett}, ' if klokkeslett else ""
    prompt = f"""Gå til denne siden: {kilde_url}

Finn arrangementet med tittel "{tittel}" på dato {dato}, {detaljer}sted: {sted}.

Hvis dette arrangementet har en egen detaljside/underside med mer informasjon (ikke bare en \
kort omtale på oversiktssiden du startet på), gå dit.

Svar med nøyaktig to deler, i denne rekkefølgen:
1. På egen linje: URL: <adressen til siden du endte opp på>
2. Deretter all tekst du finner om nettopp dette arrangementet, KOPIERT ORDRETT (ikke \
omskrevet, ikke oppsummert). Hvis du ikke finner noe mer utfyllende enn det som allerede er \
oppgitt over, skriv kun ordet INGEN_NY_INFO i stedet for tekst.
"""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            tools=[{"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5}],
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return None, False, None

    tekst = "".join(b.text for b in response.content if b.type == "text")
    if "INGEN_NY_INFO" in tekst:
        return None, False, None

    url_treff = re.search(r"URL:\s*(\S+)", tekst)
    funnet_url = url_treff.group(1).strip() if url_treff else None
    ny_tekst = re.sub(r"URL:\s*\S+", "", tekst, count=1).strip()
    if not ny_tekst:
        return None, False, None

    bekreftet = False
    endelig_url = None
    if funnet_url and funnet_url.startswith("http"):
        resultat = _hent_side_tekst(funnet_url)
        if resultat:
            sidetekst, endelig = resultat
            bekreftet = _er_ordrett(ny_tekst, sidetekst)
            if _samme_domene(endelig, kilde_url):
                endelig_url = endelig

    return ny_tekst, bekreftet, endelig_url


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


def hent_fra_tekst(
    tekst: str,
    forste_dag: date,
    siste_dag: date,
    instruks: str | None = None,
) -> list[dict]:
    """Tolker ren tekst limt inn av brukeren (f.eks. kopiert fra et Facebook-innlegg) og
    henter ut arrangementer. I motsetning til skjermdump/PDF kan vi her faktisk verifisere
    ordrett samsvar i kode, siden vi allerede har hele kildeteksten liggende."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return []

    instruks = instruks if instruks is not None else standard_instruks(forste_dag, siste_dag)
    client = Anthropic()
    prompt = f"""Dette er tekst limt inn av brukeren (f.eks. kopiert fra et innlegg om ett \
eller flere arrangementer, som Facebook). Les gjennom og hent ut arrangementene som er omtalt.

--- LIMT INN TEKST ---
{tekst[:100000]}
--- SLUTT LIMT INN TEKST ---

{instruks}
"""
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": prompt}],
    )

    svar = "".join(b.text for b in response.content if b.type == "text")
    arrangementer = _parse_json_liste(svar)
    for a in arrangementer:
        a["kilde_type"] = "limt_inn_tekst"
        a["kilde_url"] = None
        a["tekst_bekreftet"] = _er_ordrett(a.get("original_tekst", ""), tekst)
    return arrangementer

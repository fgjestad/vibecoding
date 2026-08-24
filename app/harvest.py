import base64
import hashlib
import html
import json
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import httpx
import icalendar
import recurring_ical_events
from anthropic import Anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL_INNHOSTING", "claude-haiku-4-5-20251001")

UKEDAGER = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]

# Kjente stedsnavn i Nes kommune — brukes både i instruksen til Claude og til å filtrere
# strukturerte kilder som dekker et større område enn bare Nes (f.eks. VisitGreaterOslo).
# Hvam og Oppaker bekreftet via Nes kommunes egen "Municipalities"-liste på ØRU-plattformen.
NES_STEDER = (
    "Årnes", "Vormsund", "Fenstad", "Auli", "Neskollen", "Udnes", "Skogbygda", "Runni",
    "Hvam", "Oppaker", "Rånåsfoss", "Brårud", "Rakeie", "Bjørknes",
)
STED_BESKRIVELSE = (
    f"Nes kommune på Romerike i Akershus, Norge (kjente steder: {', '.join(NES_STEDER)}. "
    "IKKE Nes i Hallingdal/Buskerud eller Nesodden.)"
)

# Nes kommunes egen aktivitetskalender — den viktigste enkeltkilden. Siden bruker IKKE
# lenger den gamle "startCalendar({...})"-widgeten som _finn_prokom_widget leter etter —
# den ble bygget om til å hente data direkte med et vanlig JS fetch()-kall (funnet ved å
# lese sidekilden på nytt). API-et er likevel samme Prokom/ØRU-plattform og samme JSON-form
# som _hent_fra_prokom_kalender allerede håndterer, bare på en annen adresse
# (sspkalender.prokom.no i stedet for et *.prokom.no funnet via widget-konfigurasjonen).
# API-URL-en hardkodes derfor direkte, i stedet for å oppdages fra sidens HTML — det har
# den ekstra fordelen at vi slipper å besøke nes.kommune.no i det hele tatt for selve
# datahentingen (kommunens egen brannmur har vist seg å blokkere serverens forespørsler dit).
NES_KOMMUNE_KALENDER_URL = "https://www.nes.kommune.no/aktivitetskalender/"
NES_KOMMUNE_KALENDER_API = (
    "https://sspkalender.prokom.no/api/tidspunkt"
    "?Categories=0&SearchText=&DateFrom=&DateTo=&Municipalities=Nes&Kunde=oru"
    "&Id=&ItemDate=&WeekDays=&List=&Count=100&Distributor="
)
NES_KOMMUNE_KALENDER_STI = "/aktivitetskalender/"

MINSTE_SIDETEKST_LENGDE = 500

# Flere kommuner på Øvre Romerike (Nes, Gjerdrum, Nannestad, Hurdal, Eidsvoll m.fl.) bruker
# samme "Prokom/ØRU"-kalenderwidget ("startCalendar({...})"), som er JavaScript-drevet: selve
# siden inneholder ingen arrangementer i den rå HTML-en — de hentes separat fra et eget API
# etter at siden er lastet i en nettleser. Widgeten kan ligge på hvilken som helst side på
# plattformen (ikke bare en dedikert "aktivitetskalender"-side — f.eks. har et biblioteks
# egen underside sin egen widget-instans, filtrert til bare bibliotekrelaterte kategorier).
# I stedet for å hardkode kommune-URL-er leser vi konfigurasjonen widgeten selv bruker rett
# ut av sidens rå HTML og bruker den direkte. Bonus: ingen AI-kall trengs for denne kilden,
# og det fungerer automatisk for alle sider/kommuner på plattformen.
_PROKOM_STARTCALENDAR_RE = re.compile(r"startCalendar\(\s*\{(.*?)\}\s*\)", re.DOTALL)
_PROKOM_WHERE_RE = re.compile(r"where\s*:\s*'([^']+)'")
_PROKOM_CALENDARURL_RE = re.compile(r"calendarurl\s*:\s*'([^']+)'")
PROKOM_BESKRIVELSE_NOKLER = ("beskriv", "description", "ingress", "omtale", "tekst", "info", "innhold")


def _finn_prokom_widget(rå_html: str) -> tuple[str, str] | None:
    """Leter etter en innebygd Prokom/ØRU-kalenderwidget i sidens rå HTML. Returnerer
    (api_url_mal, kalender_sti) hvis funnet, ellers None.

    api_url_mal er hele API-URL-en widgeten selv sender (inkludert Municipalities/
    Categories tilpasset akkurat denne widget-instansen) — vi bytter kun ut dato-
    parametrene før vi kaller den. kalender_sti er stien til kalenderens egen
    detaljvisning på denne siden (varierer, f.eks. "/aktivitetskalender/" eller
    "/aktivitetskalender2/" for et bibliotek)."""
    treff = _PROKOM_STARTCALENDAR_RE.search(rå_html)
    if not treff:
        return None
    konfig = treff.group(1)
    where_treff = _PROKOM_WHERE_RE.search(konfig)
    if not where_treff or "prokom.no" not in where_treff.group(1):
        return None
    sti_treff = _PROKOM_CALENDARURL_RE.search(konfig)
    kalender_sti = sti_treff.group(1) if sti_treff else "/aktivitetskalender/"
    return where_treff.group(1), kalender_sti


def _finn_prokom_beskrivelse(kalenderobjekt: dict) -> str | None:
    for nokkel, verdi in kalenderobjekt.items():
        if isinstance(verdi, str) and verdi.strip() and any(n in nokkel.lower() for n in PROKOM_BESKRIVELSE_NOKLER):
            # Feltet inneholder rik HTML fra kildens CMS (f.eks. <p>, <b>, &aring;), ikke
            # ren tekst — gjør den om til lesbar tekst på samme måte som for skrapte sider.
            tekst = _html_til_tekst(verdi.strip())
            if tekst:
                return tekst
    return None


def _hent_fra_prokom_kalender(
    kilde_url: str, api_url_mal: str, kalender_sti: str, forste_dag: date, siste_dag: date
) -> list[dict]:
    """Henter strukturerte arrangementsdata direkte fra Prokom/ØRU-kalenderens eget API, ved
    å gjenbruke API-URL-en widgeten på siden selv sender (kun med egne datoer og en romsligere
    treffgrense) — dermed beholdes akkurat de Municipalities/Categories-filtrene som gjelder
    for denne konkrete widget-instansen.

    Dataene kommer strukturert og direkte fra kildens egen database (ingen AI-omskriving),
    så original_tekst kan trygt merkes tekst_bekreftet=True."""
    deler = urlsplit(api_url_mal)
    parametre = dict(parse_qsl(deler.query, keep_blank_values=True))
    parametre["DateFrom"] = forste_dag.strftime("%d.%m.%Y")
    parametre["DateTo"] = siste_dag.strftime("%d.%m.%Y")
    parametre["Count"] = "200"
    url = urlunsplit(deler._replace(query=urlencode(parametre)))

    respons = httpx.get(
        url, timeout=20.0, headers={"User-Agent": "Mozilla/5.0 (compatible; RaumnesArrangementer/1.0)"}
    )
    respons.raise_for_status()
    data = respons.json()

    hendelser = []
    for maned in data.get("MonthWithEvents") or []:
        for dag in maned.get("DaysWithEvents") or []:
            hendelser.extend(dag.get("Events") or [])

    parsed = urlparse(kilde_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    kalender_sti = "/" + kalender_sti.strip("/") + "/"

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
        detalj_url = f"{base_url}{kalender_sti}event#{hendelse_id}" if hendelse_id else kilde_url

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


# Visit Greater Oslo har sin egen kalender for regionen Romerike (som Nes kommune er en del
# av), bygget på WordPress med et åpent REST-API for arrangementer (funnet ved å inspisere
# nettverkstrafikken på https://www.visitgreateroslo.com/no/romerike/hva-skjer). API-et
# dekker et mye større område enn bare Nes kommune, så vi henter alt fra Romerike-kategorien
# og filtrerer lokalt til kjente Nes-steder (samme liste som brukes i AI-instruksen) — ellers
# hadde utkastet druknet i arrangementer fra resten av Romerike.
VISITGREATEROSLO_EVENTS_API = "https://wordpress.visitgreateroslo.com/wp-json/wp/v2/events"
VISITGREATEROSLO_ROMERIKE_KATEGORI = 17
_VISITGREATEROSLO_UKEDAG_TIL_INDEKS = {
    "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
    "Friday": 4, "Saturday": 5, "Sunday": 6,
}


def _er_visitgreateroslo_kilde(url: str) -> bool:
    vert = urlparse(url).hostname or ""
    return vert.endswith("visitgreateroslo.com")


def er_dedikert_kilde(url: str) -> bool:
    """True hvis kilden hentes via en dedikert, direkte hente-vei (Nes kommunes
    aktivitetskalender, Visit Greater Oslo, eller Kirken i Nes) i stedet for den generiske
    AI-baserte hentingen. Slike kilder vises i kildelisten for åpenhetens skyld (så det er
    tydelig at de faktisk dekkes), men kan ikke deaktiveres i UI-et — de hentes uansett alltid
    via "Kjør innhøsting", som selv ruter dem til riktig dedikert hente-vei internt (se
    hent_fra_kilde)."""
    vert = (urlparse(url).hostname or "").lower()
    nes_vert = (urlparse(NES_KOMMUNE_KALENDER_URL).hostname or "").lower()
    kirkenines_vert = (urlparse(KIRKENINES_KALENDER_URL).hostname or "").lower()
    betelkirken_vert = (urlparse(BETELKIRKEN_KALENDER_URL).hostname or "").lower()
    return (
        _er_visitgreateroslo_kilde(url)
        or vert == nes_vert
        or vert == kirkenines_vert
        or vert == betelkirken_vert
    )


def _er_nes_sted(sted: str) -> bool:
    sted_norm = normaliser_tekst(sted)
    if not sted_norm:
        return False
    return any(normaliser_tekst(kjent) == sted_norm for kjent in NES_STEDER)


def _visitgreateroslo_hendelser_for_post(
    post: dict, kilde_url: str, forste_dag: date, siste_dag: date
) -> list[dict]:
    """Gjør ett WordPress-"events"-innlegg om til én arrangement-forekomst per faktiske
    åpningsdag innenfor perioden (et innlegg kan dekke flere datoer med ulike klokkeslett,
    f.eks. et marked åpent lørdag og søndag med samme åpningstider)."""
    acf = post.get("acf") or {}
    sted = str(acf.get("location") or "").strip()
    if not _er_nes_sted(sted):
        return []

    tittel = html.unescape(str((post.get("title") or {}).get("rendered") or "")).strip()
    if not tittel:
        return []

    arrangor = str(acf.get("organiser") or "").strip() or None
    adresse = str(acf.get("address") or "").strip()
    sted_tekst = f"{sted}, {adresse}" if adresse else sted
    original_tekst = f"{tittel} – {sted_tekst}.".strip()
    kilde_url = str(post.get("link") or "").strip() or kilde_url

    hendelser = []
    for periode in acf.get("opening_times") or []:
        try:
            periode_start = date.fromisoformat(str(periode.get("start_date")))
            periode_slutt = date.fromisoformat(str(periode.get("end_date")))
        except (ValueError, TypeError):
            continue

        dag_ved_ukedag = {
            _VISITGREATEROSLO_UKEDAG_TIL_INDEKS[d["day_name"]]: d
            for d in (periode.get("days") or [])
            if d.get("day_name") in _VISITGREATEROSLO_UKEDAG_TIL_INDEKS
        }

        dag = max(periode_start, forste_dag)
        slutt = min(periode_slutt, siste_dag)
        while dag <= slutt:
            dagsinfo = dag_ved_ukedag.get(dag.weekday())
            tider = (dagsinfo or {}).get("opening_times") or []
            if dagsinfo and dagsinfo.get("is_open") and tider:
                for tid in tider:
                    klokkeslett = None
                    åpningstid = str(tid.get("opening_time") or "")
                    if "T" in åpningstid:
                        klokkeslett = åpningstid.split("T", 1)[1][:5]
                    hendelser.append(
                        {
                            "tittel": tittel,
                            "dato": dag.isoformat(),
                            "klokkeslett": klokkeslett,
                            "sted": sted_tekst,
                            "arrangor": arrangor,
                            "original_tekst": original_tekst,
                            "geografisk_relevans": "bekreftet",
                            "kilde_type": "fast_kalender",
                            "kilde_url": kilde_url,
                            "tekst_bekreftet": True,
                        }
                    )
            dag += timedelta(days=1)
    return hendelser


def _hent_fra_visitgreateroslo(kilde_url: str, forste_dag: date, siste_dag: date) -> list[dict]:
    """Henter strukturerte arrangementsdata direkte fra Visit Greater Oslo sitt eget
    WordPress REST-API (samme "events"-endepunkt widgeten på nettsiden selv bruker).

    Dataene kommer strukturert direkte fra kildens egen database (ingen AI-omskriving), så
    original_tekst kan trygt merkes tekst_bekreftet=True."""
    arrangementer = []
    side = 1
    while side <= 20:
        respons = httpx.get(
            VISITGREATEROSLO_EVENTS_API,
            params={
                "city_category": VISITGREATEROSLO_ROMERIKE_KATEGORI,
                "lang": "no",
                "per_page": 100,
                "page": side,
            },
            timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RaumnesArrangementer/1.0)"},
        )
        if respons.status_code == 400:
            # WordPress svarer 400 når man ber om en side forbi den siste.
            break
        respons.raise_for_status()
        poster = respons.json()
        if not poster:
            break

        for post in poster:
            arrangementer.extend(_visitgreateroslo_hendelser_for_post(post, kilde_url, forste_dag, siste_dag))

        if len(poster) < 100:
            break
        side += 1
    return arrangementer


# Kirken i Nes (Nes kirkelige fellesråd) sin kalenderside kjører DNN/Agrando-kalendermodulen
# og har INGEN separat API å kalle. I stedet er kalenderdataene for standardvalget («Nes
# kirkelige fellesråd», som dekker alle sokn/kirker) ferdig rendret som HTML og bakt rett inn
# i en <script>-tag på selve siden — «var data = {"d": "<div>...</div>"}; OutputCalendar(...)»
# — funnet ved å lese sidekilden (samme fremgangsmåte som for Nes kommune). Vi trenger derfor
# bare det ene sidekallet: ingen egen API-URL, i motsetning til Prokom-kalenderen.
KIRKENINES_KALENDER_URL = "https://www.kirkenines.no/Kalender"
_KIRKENINES_DATA_RE = re.compile(r"var data = (\{.*\});\s*OutputCalendar\(data,", re.DOTALL)
_KIRKENINES_MANEDER = {
    "januar": 1, "februar": 2, "mars": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12,
}


class _KirkeninesKalenderParser(HTMLParser):
    """Parser for HTML-fragmentet i "d"-feltet fra Kirken i Nes' kalenderdata (se
    _hent_fra_kirkenines_kalender) — en flat rekke av calendar-month/calendar-item-blokker.
    Bruker en dybde-stemplet stack for å vite hvilket felt tekstinnhold hører til, og hvilket
    </div>/</p>/</span> som faktisk avslutter det (uten en fullverdig DOM å navigere i)."""

    def __init__(self):
        super().__init__()
        self._depth = 0
        self._stack: list[tuple[int, str]] = []
        self._maned = None
        self._dag = ""
        self._dato = ""
        self._events: list[dict] = []
        self._current_event: dict | None = None
        self.rader: list[dict] = []

    def _felt(self) -> str | None:
        return self._stack[-1][1] if self._stack else None

    def handle_starttag(self, tag, attrs):
        klass = dict(attrs).get("class", "") or ""
        self._depth += 1

        if tag == "div" and klass == "calendar-item":
            self._dag, self._dato, self._events = "", "", []
            self._stack.append((self._depth, "item"))
        elif tag == "div" and klass == "calendar-month":
            self._maned = ""
            self._stack.append((self._depth, "maned"))
        elif tag == "div" and klass == "calendar-day":
            self._stack.append((self._depth, "dag"))
        elif tag == "div" and klass == "calendar-date":
            self._stack.append((self._depth, "dato"))
        elif tag == "div" and klass == "event":
            self._current_event = {"tid": "", "tittel": "", "href": None, "kategori": "", "sted": ""}
            self._stack.append((self._depth, "event"))
        elif tag == "div" and klass == "event-time":
            self._stack.append((self._depth, "tid"))
        elif tag == "p" and klass == "info-text":
            self._stack.append((self._depth, "tittel"))
        elif tag == "span" and klass == "calendar-label":
            self._stack.append((self._depth, "kategori"))
        elif tag == "span" and klass == "calendar-location":
            self._stack.append((self._depth, "sted"))
        elif tag == "a" and self._felt() == "tittel" and self._current_event is not None:
            href = dict(attrs).get("href")
            if href:
                self._current_event["href"] = href

    def handle_endtag(self, tag):
        while self._stack and self._stack[-1][0] == self._depth:
            _, felt = self._stack.pop()
            if felt == "item" and self._dag and self._dato:
                self.rader.append({"maned": self._maned, "dato": self._dato.strip(), "events": self._events})
            elif felt == "event" and self._current_event is not None:
                self._events.append(self._current_event)
                self._current_event = None
        self._depth -= 1

    def handle_data(self, data):
        felt = self._felt()
        if felt == "maned":
            self._maned = (self._maned or "") + data
        elif felt == "dag":
            self._dag += data
        elif felt == "dato":
            self._dato += data
        elif felt in ("tid", "tittel", "kategori", "sted") and self._current_event is not None:
            self._current_event[felt] += data


def _kirkenines_bygg_arrangementer(
    rader: list[dict], aar_start: int, kilde_url: str, forste_dag: date, siste_dag: date
) -> list[dict]:
    arrangementer = []
    aar = aar_start
    forrige_maned_nr = None
    for rad in rader:
        maned_nr = _KIRKENINES_MANEDER.get((rad["maned"] or "").strip().lower())
        if maned_nr is None:
            continue
        if forrige_maned_nr is not None and maned_nr < forrige_maned_nr:
            aar += 1
        forrige_maned_nr = maned_nr

        dag_del = rad["dato"].split(".")
        if len(dag_del) < 2 or not dag_del[0].strip().isdigit():
            continue
        try:
            dato_obj = date(aar, maned_nr, int(dag_del[0]))
        except ValueError:
            continue
        if not (forste_dag <= dato_obj <= siste_dag):
            continue

        for hendelse in rad["events"]:
            tittel = hendelse["tittel"].strip()
            if not tittel:
                continue

            klokkeslett = None
            tid_treff = re.match(r"kl\.?\s*(\d{1,2})[.:](\d{2})", hendelse["tid"].strip(), re.IGNORECASE)
            if tid_treff:
                klokkeslett = f"{int(tid_treff.group(1)):02d}:{tid_treff.group(2)}"

            kategori = hendelse["kategori"].strip()
            sted = hendelse["sted"].strip()
            original_tekst = f"{tittel} – {kategori}" if kategori and kategori != tittel else tittel
            if sted:
                original_tekst += f", {sted}"
            original_tekst += "."

            href = hendelse.get("href")
            kilde_url_detalj = urljoin(kilde_url, href) if href else kilde_url

            arrangementer.append(
                {
                    "tittel": tittel,
                    "dato": dato_obj.isoformat(),
                    "klokkeslett": klokkeslett,
                    "sted": sted,
                    "arrangor": None,
                    "original_tekst": original_tekst,
                    "geografisk_relevans": "bekreftet",
                    "kilde_type": "fast_kalender",
                    "kilde_url": kilde_url_detalj,
                    "tekst_bekreftet": True,
                }
            )
    return arrangementer


def _hent_fra_kirkenines_kalender(forste_dag: date, siste_dag: date) -> list[dict]:
    """Henter strukturerte arrangementsdata (gudstjenester m.m.) direkte fra Kirken i Nes sin
    kalenderside — se kommentaren over KIRKENINES_KALENDER_URL for hvordan dataene faktisk
    ligger i sidekilden.

    Dataene kommer strukturert direkte fra kildens egen database (ingen AI-omskriving), så
    original_tekst kan trygt merkes tekst_bekreftet=True."""
    respons = httpx.get(
        KIRKENINES_KALENDER_URL, timeout=20.0,
        headers={"User-Agent": "Mozilla/5.0 (compatible; RaumnesArrangementer/1.0)"},
    )
    respons.raise_for_status()

    treff = _KIRKENINES_DATA_RE.search(respons.text)
    if not treff:
        return []
    try:
        data = json.loads(treff.group(1))
    except json.JSONDecodeError:
        return []
    fragment = data.get("d") or ""

    aar_treff = re.search(r'class="year">(\d{4})', fragment)
    aar_start = int(aar_treff.group(1)) if aar_treff else forste_dag.year

    parser = _KirkeninesKalenderParser()
    parser.feed(fragment)
    return _kirkenines_bygg_arrangementer(parser.rader, aar_start, KIRKENINES_KALENDER_URL, forste_dag, siste_dag)


def diagnostiser_kirkenines_kalender(forste_dag: date, siste_dag: date) -> tuple[list[dict], str]:
    """Som diagnostiser_nes_kalender, men for Kirken i Nes sin kalenderside. Returnerer
    (arrangementer, diagnose) — diagnose er tom streng ved suksess."""
    direkte_feil = None
    try:
        arrangementer = _hent_fra_kirkenines_kalender(forste_dag, siste_dag)
        if arrangementer:
            return arrangementer, ""
        direkte_feil = f"Siden ble hentet, men ga ingen treff for perioden {forste_dag} – {siste_dag}."
    except httpx.HTTPError as e:
        direkte_feil = f"Klarte ikke å koble til kirkenines.no direkte: {type(e).__name__}: {e}"
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        direkte_feil = f"Klarte å hente siden, men ikke tolke kalenderdataene i den: {type(e).__name__}: {e}"

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return [], direkte_feil or "Ukjent feil."

    instruks = standard_instruks(forste_dag, siste_dag)
    arrangementer, fallback_diagnose = _hent_fra_kilde_via_web_fetch_med_diagnose(KIRKENINES_KALENDER_URL, instruks)
    if arrangementer:
        return arrangementer, ""
    return [], f"{direkte_feil} Reserveløsning: {fallback_diagnose}"


# Betelkirken (Årnes og Auli) publiserer arrangementene sine i egne Google-kalendere, embedet
# på betelkirken.no/kalendar via en tredjeparts Wix-app som injiserer selve kalender-iframen
# med JavaScript ved kjøretid (ikke synlig i sidekilden). Kalender-ID-ene ble i stedet funnet
# via "Legg til kalender" i en vanlig Google Calendar-konto, og feedene er offentlige og
# fungerer uavhengig av Wix-siden — hentes derfor direkte som iCal (.ics), samme mønster som
# Kirken i Nes over. To fysiske lokasjoner (Årnes og Auli) vises som duplikate arrangementer,
# så begge feeder hentes og slås sammen til gjeldende kildelinje.
BETELKIRKEN_KALENDER_URL = "https://www.betelkirken.no/kalendar"
BETELKIRKEN_ICS_URLER = (
    "https://calendar.google.com/calendar/ical/betelkirken1920%40gmail.com/public/basic.ics",
    "https://calendar.google.com/calendar/ical/"
    "5ba22c64a9b7d3026cec2377a254e56972048a82d39e810dc5e0d9472ad54ebd%40group.calendar.google.com"
    "/public/basic.ics",
)


def _hent_fra_betelkirken_kalender(forste_dag: date, siste_dag: date) -> list[dict]:
    """Henter strukturerte arrangementsdata direkte fra Betelkirkens to offentlige Google-
    kalender-feeder (Årnes og Auli) som iCal, se kommentaren over BETELKIRKEN_KALENDER_URL.

    Dataene kommer strukturert direkte fra kildens egen kalender (ingen AI-omskriving), så
    original_tekst kan trygt merkes tekst_bekreftet=True."""
    arrangementer = []
    for ics_url in BETELKIRKEN_ICS_URLER:
        respons = httpx.get(
            ics_url, timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RaumnesArrangementer/1.0)"},
        )
        respons.raise_for_status()
        kalender = icalendar.Calendar.from_ical(respons.content)
        hendelser = recurring_ical_events.of(kalender).between(forste_dag, siste_dag + timedelta(days=1))

        for hendelse in hendelser:
            tittel = str(hendelse.get("SUMMARY") or "").strip()
            if not tittel:
                continue
            start = hendelse.get("DTSTART").dt
            if isinstance(start, date) and not hasattr(start, "hour"):
                dato_obj, klokkeslett = start, None
            else:
                dato_obj, klokkeslett = start.date(), start.strftime("%H:%M")
            if not (forste_dag <= dato_obj <= siste_dag):
                continue

            sted = str(hendelse.get("LOCATION") or "").strip()
            original_tekst = f"{tittel} – {sted}.".strip() if sted else f"{tittel}."

            arrangementer.append(
                {
                    "tittel": tittel,
                    "dato": dato_obj.isoformat(),
                    "klokkeslett": klokkeslett,
                    "sted": sted,
                    "arrangor": None,
                    "original_tekst": original_tekst,
                    "geografisk_relevans": "bekreftet",
                    "kilde_type": "fast_kalender",
                    "kilde_url": BETELKIRKEN_KALENDER_URL,
                    "tekst_bekreftet": True,
                }
            )
    return arrangementer


# Fotballkamper for lokale idrettslag, hentet direkte fra fotball.no (NFFs egen sportslige
# plattform). Siden er faktisk vanlig, server-rendret HTML (ikke en JS-widget) og støtter
# filtrering på både dato og klubb via URL-parametre — funnet ved å inspisere sidekilden på
# https://www.fotball.no/fotballdata/dagens-kamper/. Klubb-ID-ene er hentet fra sidens egen
# "Velg klubb"-nedtrekksliste for Akershus (kretsId 3), der Nes-klubbene ligger.
#
# Bare hjemmekamper tas med (det er det man faktisk kan gå og se lokalt) — kampfilteret på
# fotball.no returnerer alle kamper klubben er involvert i (både hjemme og borte), så
# hjemme/borte avgjøres lokalt ved å sjekke om klubbens navn står i Hjemmelag-kolonnen.
#
# For flere idretter senere: følg samme mønster (finn tilsvarende kamptjeneste, kartlegg
# klubb-ID-er, gjenbruk _fotball_lagnavn_normalisert-stilen for hjemme/borte-sjekk).
FOTBALL_KRETS_AKERSHUS = 3
FOTBALL_DAGENS_KAMPER_URL = "https://www.fotball.no/fotballdata/dagens-kamper/"
FOTBALL_KLUBBER = {
    "Funnefoss Vormsund Idrettslag": 150,
    "Raumnes & Årnes Idrettslag": 153,
    "Fenstad Fotballklubb": 148,
    "Haga Idrettsforening": 151,
    "Hvam Idrettslag": 152,
    "Skogbygda Idrettsforening": 154,
}
_FOTBALL_KLUBB_SUFFIKSER = (" idrettslag", " fotballklubb", " idrettsforening", " sportsklubb")


def _fotball_kortnavn(offisielt_navn: str) -> str:
    """«Raumnes & Årnes Idrettslag» -> «Raumnes & Årnes» — kamptabellens Hjemmelag-kolonne
    bruker klubbens korte visningsnavn, uten den formelle organisasjonsformen på slutten."""
    kort = offisielt_navn.strip()
    kort_lav = kort.lower()
    for suffiks in _FOTBALL_KLUBB_SUFFIKSER:
        if kort_lav.endswith(suffiks):
            return kort[: -len(suffiks)].strip()
    return kort


def _fotball_sted_kort(bane: str) -> str:
    """Trekker ut stedsnavnet fra en banetekst (f.eks. «Årnes kg 9er A» -> «Årnes»,
    «Lillestrøm stadion 6 7er» -> «Lillestrøm») — presise bane-/underlagsdetaljer er mindre
    relevante i tittelen enn hvilket sted kampen faktisk spilles på."""
    bane = bane.strip()
    return bane.split()[0] if bane else bane


def _fotball_lagnavn_normalisert(navn: str) -> str:
    """Som normaliser_tekst, men erstatter skilletegn (f.eks. «/» i «Funnefoss/Vormsund»)
    med mellomrom i stedet for å fjerne dem, slik at det fortsatt matcher klubbens navn med
    mellomrom («Funnefoss Vormsund»)."""
    navn = unicodedata.normalize("NFKD", (navn or "").lower())
    navn = re.sub(r"[^\w\s]", " ", navn)
    navn = re.sub(r"\s+", " ", navn).strip()
    return navn


class _FotballKampTabellParser(HTMLParser):
    """Parser for kamptabellen på fotball.no sin "dagens kamper"-side. Bygger opp rader som
    lister av celler, hver celle med tekstinnhold og en eventuell lenke-href (brukt til å
    hente ut kampens fiksId)."""

    def __init__(self):
        super().__init__()
        self._i_tbody = False
        self._i_td = False
        self._rad: list[dict] | None = None
        self.rader: list[list[dict]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "tbody":
            self._i_tbody = True
        elif tag == "tr" and self._i_tbody:
            self._rad = []
        elif tag == "td" and self._rad is not None:
            self._i_td = True
            self._rad.append({"tekst": "", "href": None})
        elif tag == "a" and self._i_td and self._rad:
            href = dict(attrs).get("href")
            if href:
                self._rad[-1]["href"] = href

    def handle_endtag(self, tag):
        if tag == "tbody":
            self._i_tbody = False
        elif tag == "tr" and self._rad is not None:
            self.rader.append(self._rad)
            self._rad = None
        elif tag == "td":
            self._i_td = False

    def handle_data(self, data):
        if self._i_td and self._rad:
            self._rad[-1]["tekst"] += data


def _hent_fotballkamper_for_klubb_og_dag(klubb_navn: str, klubb_id: int, dag: date) -> list[dict]:
    """Henter alle fotballkamper for én klubb på én dato, og returnerer kun hjemmekampene."""
    respons = httpx.get(
        FOTBALL_DAGENS_KAMPER_URL,
        params={
            "d": FOTBALL_KRETS_AKERSHUS,
            "c": klubb_id,
            "startDate": dag.isoformat(),
            "includeChildren": "false",
            "includeYouth": "false",
            "includeAdult": "false",
        },
        timeout=20.0,
        headers={"User-Agent": "Mozilla/5.0 (compatible; RaumnesArrangementer/1.0)"},
    )
    respons.raise_for_status()

    parser = _FotballKampTabellParser()
    parser.feed(respons.text)

    kort_navn_normalisert = _fotball_lagnavn_normalisert(_fotball_kortnavn(klubb_navn))

    arrangementer = []
    for rad in parser.rader:
        if len(rad) < 6:
            continue
        turnering = rad[0]["tekst"].strip()
        tid = rad[1]["tekst"].strip()
        hjemmelag = rad[2]["tekst"].strip()
        resultat = rad[3]["tekst"].strip()
        bortelag = rad[4]["tekst"].strip()
        bane = rad[5]["tekst"].strip()

        if not hjemmelag or not bortelag:
            continue
        if resultat == "Utsatt":
            continue
        if not _fotball_lagnavn_normalisert(hjemmelag).startswith(kort_navn_normalisert):
            continue  # bortekamp for denne klubben — ikke noe man kan gå og se lokalt

        kamp_href = rad[3]["href"] or (rad[6]["href"] if len(rad) > 6 else None)
        fiks_id_treff = re.search(r"fiksId=(\d+)", kamp_href or "")
        kilde_url = (
            f"https://www.fotball.no/fotballdata/kamp/?fiksId={fiks_id_treff.group(1)}"
            if fiks_id_treff
            else FOTBALL_DAGENS_KAMPER_URL
        )

        original_tekst = f"{turnering}: {hjemmelag} – {bortelag}, {bane}.".strip()

        kategori = turnering.split()[0] if turnering else ""
        sted_kort = _fotball_sted_kort(bane)
        tittel = f"Fotballkamp {sted_kort} {kategori}".strip() if kategori else f"Fotballkamp {sted_kort}"

        arrangementer.append(
            {
                "tittel": tittel,
                "dato": dag.isoformat(),
                "klokkeslett": tid or None,
                "sted": bane,
                "arrangor": klubb_navn,
                "original_tekst": original_tekst,
                "geografisk_relevans": "bekreftet",
                "kilde_type": "fast_kalender",
                "kilde_url": kilde_url,
                "tekst_bekreftet": True,
            }
        )
    return arrangementer


def hent_fotballkamper(forste_dag: date, siste_dag: date) -> tuple[list[dict], int]:
    """Henter hjemmekamper for alle klubbene i FOTBALL_KLUBBER i hele perioden, ett oppslag
    per klubb per dag (fotball.no støtter kun ett datofilter om gangen). Returnerer
    (arrangementer, antall_feilede_oppslag) — enkeltoppslag som feiler hopper vi over i
    stedet for å la hele innhentingen falle, siden det uansett blir mange oppslag."""
    dager = []
    dag = forste_dag
    while dag <= siste_dag:
        dager.append(dag)
        dag += timedelta(days=1)

    oppgaver = [(navn, id_, dag) for dag in dager for navn, id_ in FOTBALL_KLUBBER.items()]

    arrangementer: list[dict] = []
    antall_feilet = 0
    with ThreadPoolExecutor(max_workers=5) as executor:
        fremtider = [
            executor.submit(_hent_fotballkamper_for_klubb_og_dag, navn, id_, dag)
            for navn, id_, dag in oppgaver
        ]
        for fremtid in as_completed(fremtider):
            try:
                arrangementer.extend(fremtid.result())
            except Exception:
                antall_feilet += 1
    return arrangementer, antall_feilet


def beregn_periode(antall_dager: int = 14, i_dag: date | None = None) -> tuple[date, date]:
    """Returnerer (forste_dag, siste_dag): fra i morgen og "antall_dager" dager frem."""
    i_dag = i_dag or date.today()
    forste_dag = i_dag + timedelta(days=1)
    siste_dag = forste_dag + timedelta(days=max(antall_dager, 1) - 1)
    return forste_dag, siste_dag


def normaliser_tekst(tekst: str | None) -> str:
    tekst = unicodedata.normalize("NFKD", (tekst or "").lower().strip())
    tekst = re.sub(r"[^\w\s]", "", tekst)
    tekst = re.sub(r"\s+", " ", tekst)
    return tekst


def beregn_arrangement_id(tittel: str, dato_str: str, sted: str) -> str:
    """Innholdsbasert id for å gjenkjenne samme arrangement fra flere kilder."""
    grunnlag = f"{normaliser_tekst(tittel)}|{dato_str}|{normaliser_tekst(sted)}"
    return hashlib.sha256(grunnlag.encode()).hexdigest()[:16]


def tekstlikhet(a: str, b: str) -> float:
    """Fuzzy tekstlikhet mellom 0.0 og 1.0 (normalisert — case/tegnsetting-uavhengig).

    Offentlig, gjenbrukt av main.py bl.a. til å oppdage at samme flerdagers-arrangement er
    oppført som separate endags-rader (se main._finn_flerdagsmatch)."""
    return SequenceMatcher(None, normaliser_tekst(a), normaliser_tekst(b)).ratio()


def er_tittel_duplikat(
    tittel_a: str, klokkeslett_a: str | None, tittel_b: str, klokkeslett_b: str | None
) -> bool:
    """Sjekker om to arrangementer på SAMME DATO sannsynligvis er det samme, hentet fra
    ulike kilder. Kalles kun for par som allerede er kjent å ha lik dato.

    Kombinerer flere signaler i stedet for å kreve eksakt lik tittel-streng:
    - Tekstlikhet i tittelen (fanger opp at kilder ofte formulerer samme arrangement litt
      ulikt, f.eks. "Bingo på Auli grendehus" vs. "Bingokveld, Auli Grendehus").
    - Klokkeslett, som brukes til å justere hvor streng tekstlikheten må være: samme
      klokkeslett er et sterkt signal (lavere terskel holder), mens ulikt klokkeslett taler
      imot at det er samme arrangement.
    - Tall i tittelen (f.eks. aldersklasse eller runde). Rene tegn-for-tegn-sammenligninger
      lar seg lure av titler som «Nes IL G14 - Eidsvoll IL G14» vs. «... G16 ...», som er
      nesten identiske bortsett fra ett siffer — men er to helt ulike kamper. Har begge
      titler tall, og de ikke er de samme, og klokkeslettet heller ikke stemmer overens,
      regnes det IKKE som duplikat uansett tekstlikhet."""
    a_tid = (klokkeslett_a or "").strip()
    b_tid = (klokkeslett_b or "").strip()
    samme_tid = bool(a_tid) and bool(b_tid) and a_tid == b_tid

    tall_a = set(re.findall(r"\d+", tittel_a))
    tall_b = set(re.findall(r"\d+", tittel_b))
    if tall_a and tall_b and tall_a != tall_b and not samme_tid:
        return False

    likhet = tekstlikhet(tittel_a, tittel_b)
    if a_tid and b_tid:
        return likhet >= 0.55 if samme_tid else likhet >= 0.92
    return likhet >= 0.75


def generer_sammenslatt_arrangement(medlemmer: list[dict]) -> dict | None:
    """Bruker Claude til å skrive ÉN sammenslått beskrivelse av et duplikat-funn fra flere
    kilder, ved å kombinere det beste/mest komplette fra hver (f.eks. klokkeslett fra én
    kilde, en detalj som bare står hos en annen). Teksten er da IKKE lenger ordrett fra én
    bestemt kilde — brukes derfor kun til den synlige, sammenslåtte oppføringen i en
    duplikat-gruppe, mens de rå enkeltkilde-funnene (fortsatt kode-verifiserbare hver for
    seg) ligger tilgjengelig som alternativer brukeren kan velge i stedet.

    medlemmer: liste av dict med tittel/dato/klokkeslett/sted/arrangor/original_tekst/
    kilde_url for hvert rå funn i gruppen (minst 2).

    Returnerer et dict med samme nøkler som et vanlig hentet arrangement, eller None hvis
    sammenslåingen feiler av noen grunn (ingen API-nøkkel, API-feil, uparsbart svar) — da
    beholdes de rå funnene som separate oppføringer i stedet, uendret."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None

    kilder_tekst = "\n\n".join(
        f"--- Kilde {i + 1} ({m.get('kilde_url') or 'ukjent URL'}) ---\n"
        f"Tittel: {m['tittel']}\n"
        f"Klokkeslett: {m.get('klokkeslett') or 'ikke oppgitt'}\n"
        f"Sted: {m.get('sted') or 'ikke oppgitt'}\n"
        f"Arrangør: {m.get('arrangor') or 'ikke oppgitt'}\n"
        f"Tekst: {m['original_tekst']}"
        for i, m in enumerate(medlemmer)
    )
    prompt = f"""Disse {len(medlemmer)} oppføringene er funnet av et automatisk system for \
lokale arrangementer, og antas å beskrive DET SAMME arrangementet, hentet fra ulike kilder:

{kilder_tekst}

Skriv én samlet, presis beskrivelse av arrangementet som kombinerer det beste og mest \
komplette fra kildene over — f.eks. bruk klokkeslettet fra den kilden som faktisk oppgir \
det, ta med en detalj som bare står hos én av kildene, osv. IKKE finn på informasjon som \
ikke står i noen av kildene over.

Svar KUN med et gyldig JSON-objekt, ingen tekst før eller etter, med nøklene:
- "tittel": kort tittel
- "dato": YYYY-MM-DD
- "klokkeslett": f.eks. "18:00", eller null hvis ingen kilde oppgir det
- "sted": stedsnavn/adresse
- "arrangor": arrangør, eller null hvis ingen kilde oppgir det
- "original_tekst": den sammenslåtte beskrivelsen"""
    client = Anthropic()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return None

    tekst = "".join(b.text for b in response.content if b.type == "text")
    match = re.search(r"\{.*\}", tekst, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("tittel") or not data.get("dato"):
        return None
    return data


def beregn_signatur(tittel: str, arrangor: str | None, dato_str: str) -> str:
    """Ukedagsbasert signatur (ikke eksakt dato) for eksklusjonshukommelse på tvers av uker."""
    try:
        ukedag = UKEDAGER[date.fromisoformat(dato_str).weekday()]
    except (ValueError, TypeError):
        ukedag = ""
    grunnlag = f"{normaliser_tekst(tittel)}|{normaliser_tekst(arrangor)}|{ukedag}"
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


def _hent_side_raw(url: str) -> tuple[str, str] | None:
    """Henter en URL programmatisk. Returnerer (rå_html, endelig_url_etter_omdirigering),
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

    return respons.text, str(respons.url)


def _hent_side_tekst(url: str) -> tuple[str, str] | None:
    """Henter en URL programmatisk og gjør den om til synlig tekst. Returnerer
    (synlig_tekst, endelig_url_etter_omdirigering), eller None ved feil."""
    resultat = _hent_side_raw(url)
    if not resultat:
        return None
    rå_html, endelig_url = resultat
    return _html_til_tekst(rå_html), endelig_url


def vertsnavn(url: str) -> str:
    vert = urlparse(url).hostname or ""
    return vert[4:] if vert.startswith("www.") else vert


def _samme_domene(url_a: str, url_b: str) -> bool:
    return bool(vertsnavn(url_a)) and vertsnavn(url_a) == vertsnavn(url_b)


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
    return f"""Dagens dato er {date.today().isoformat()}. Perioden vi er interessert i: \
{forste_dag.isoformat()} til {siste_dag.isoformat()} (begge datoer inkludert).

Hvis en dato i kilden ikke oppgir årstall eksplisitt (f.eks. bare "21. august" uten år), anta \
det årstallet som gir en dato i eller nær perioden over — ALDRI et årstall som ville plassert \
arrangementet i fortiden (før dagens dato), med mindre kilden eksplisitt oppgir et slikt \
årstall selv. Vær spesielt obs på at bilder/PDF-er av annonser/plakater ofte gjenbruker en \
gammel mal med feil/utdatert årstall trykt på seg (f.eks. i en logo eller footer) — stol IKKE \
blindt på et slikt trykt årstall hvis det ikke stemmer med dagens dato og perioden over.

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


def instruks_for_manuell_kilde() -> str:
    """Som standard_instruks, men UTEN periodebegrensning — brukes for de vedvarende manuelle
    kildene (skjermdump/PDF/limt inn tekst, se modellen ManuellKilde). Disse skal identifisere
    ALLE arrangementer i dokumentet uansett dato, ikke bare de som tilfeldigvis er innenfor et
    gjeldende datovindu — datofiltrering mot perioden gjøres i etterkant i kode (se
    main._filtrer_til_periode), slik at et arrangement som først lå utenfor vinduet fanges opp
    automatisk når vinduet senere dekker datoen dets, i stedet for å gå tapt for godt. Brukes
    også til å beregne kildens utløpsdato (seneste arrangement-dato funnet)."""
    return f"""Dagens dato er {date.today().isoformat()}.

Hvis en dato i kilden ikke oppgir årstall eksplisitt (f.eks. bare "21. august" uten år), anta \
det årstallet som gir NÆRMESTE FREMTIDIGE forekomst av den datoen fra i dag — ALDRI et årstall \
som ville plassert arrangementet i fortiden (før dagens dato), med mindre kilden eksplisitt \
oppgir et slikt årstall selv. Vær spesielt obs på at bilder/PDF-er av annonser/plakater ofte \
gjenbruker en gammel mal med feil/utdatert årstall trykt på seg (f.eks. i en logo eller \
footer) — stol IKKE blindt på et slikt trykt årstall hvis det ikke stemmer med dagens dato.

For hvert arrangement som er omtalt, UANSETT DATO, hent ut:
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

Ikke ta med arrangementer som tydelig skjer utenfor Nes kommune. IKKE begrens til noen bestemt \
tidsperiode — ta med alle arrangementer som er omtalt i dokumentet, uansett hvor langt fram \
eller tilbake i tid de er.

Svar til slutt KUN med et gyldig JSON-array, ingen tekst før eller etter. Hvis du ikke finner \
noen relevante arrangementer, svar med et tomt array: []"""


def _hent_fra_kilde_via_sidetekst(
    kilde_url: str,
    sidetekst: str,
    instruks: str,
) -> list[dict] | None:
    """Sender allerede hentet sidetekst til Claude for uttrekk. None hvis API-kallet feiler."""
    client = Anthropic()
    prompt = f"""Under er den rå teksten fra nettsiden {kilde_url}, hentet automatisk. Bruk KUN \
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
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return None

    tekst = "".join(b.text for b in response.content if b.type == "text")
    return _parse_json_liste(tekst)


def hent_fra_kilde(
    kilde_url: str,
    forste_dag: date,
    siste_dag: date,
    instruks: str | None = None,
) -> tuple[list[dict], str | None]:
    """Henter arrangementer fra én kilde-URL. Returnerer (arrangementer, foreslått_url).

    foreslått_url er en mer presis URL for kilden (f.eks. etter omdirigering, eller
    foreslått av Claude) hvis vi fant en, ellers None — brukes til å oppdatere kildelisten
    slik at neste kjøring kan hente direkte uten omveier.

    Kjente strukturerte kilder hentes direkte fra sitt eget API, uten AI:
    - Nes kommunes aktivitetskalender — se diagnostiser_nes_kalender.
    - Andre Prokom/ØRU-kalender-widgets (f.eks. et biblioteks egen underside) — se
      _finn_prokom_widget / _hent_fra_prokom_kalender.
    - Visit Greater Oslo — se _hent_fra_visitgreateroslo.

    Ellers prøver vi først å hente siden programmatisk og la Claude lese av den rå teksten —
    da kan hvert "original_tekst"-utdrag verifiseres kode-messig mot det som faktisk står på
    siden (tekst_bekreftet=True). Hvis den programmatiske hentingen feiler (f.eks. siden
    krever JavaScript), faller vi tilbake til at Claude selv henter siden med web_fetch-
    verktøyet; da kan vi ikke verifisere ordrett samsvar i kode, så tekst_bekreftet settes
    til False.
    """
    if _er_visitgreateroslo_kilde(kilde_url):
        return _hent_fra_visitgreateroslo(kilde_url, forste_dag, siste_dag), None

    if (urlparse(kilde_url).hostname or "").lower() == (urlparse(NES_KOMMUNE_KALENDER_URL).hostname or "").lower():
        # Nes kommunes aktivitetskalender har sluttet å bruke den gamle Prokom-widgeten
        # (se diagnostiser_nes_kalender), så den generiske widget-deteksjonen lenger ned
        # finner den ikke lenger — ruter derfor direkte til den kjente fungerende API-veien.
        arrangementer, _diagnose = diagnostiser_nes_kalender(forste_dag, siste_dag)
        return arrangementer, None

    if (urlparse(kilde_url).hostname or "").lower() == (urlparse(KIRKENINES_KALENDER_URL).hostname or "").lower():
        # Kirken i Nes' kalenderside har ingen gjenkjennbar widget/API — kalenderdataene ligger
        # ferdig rendret i en <script>-tag på siden (se diagnostiser_kirkenines_kalender).
        arrangementer, _diagnose = diagnostiser_kirkenines_kalender(forste_dag, siste_dag)
        return arrangementer, None

    if (
        urlparse(kilde_url).hostname or ""
    ).lower() == (urlparse(BETELKIRKEN_KALENDER_URL).hostname or "").lower():
        # betelkirken.no/kalendar viser selve kalenderen via en tredjeparts Wix-app som
        # injiserer innholdet med JavaScript ved kjøretid — dataene hentes derfor i stedet
        # direkte fra kirkens to offentlige Google-kalender-feeder, se
        # _hent_fra_betelkirken_kalender.
        return _hent_fra_betelkirken_kalender(forste_dag, siste_dag), None

    rå_resultat = _hent_side_raw(kilde_url)
    if rå_resultat:
        widget = _finn_prokom_widget(rå_resultat[0])
        if widget:
            api_url_mal, kalender_sti = widget
            return _hent_fra_prokom_kalender(kilde_url, api_url_mal, kalender_sti, forste_dag, siste_dag), None

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return [], None

    instruks = instruks if instruks is not None else standard_instruks(forste_dag, siste_dag)

    resultat = None
    if rå_resultat:
        sidetekst = _html_til_tekst(rå_resultat[0])
        if len(sidetekst) >= MINSTE_SIDETEKST_LENGDE:
            # Mindre enn dette er mistenkelig lite tekst — sannsynligvis en side der
            # innholdet lastes inn med JavaScript etter at siden er hentet (f.eks. en
            # kalender-widget uten en gjenkjennbar Prokom-signatur), så den programmatiske
            # hentingen har ikke fått med det faktiske innholdet. Gå rett til
            # reserveløsningen i stedet for å tolke en nesten tom side som "ingen treff".
            resultat = (sidetekst, rå_resultat[1])
    if resultat:
        sidetekst, endelig_url = resultat
        arrangementer = _hent_fra_kilde_via_sidetekst(kilde_url, sidetekst, instruks)
        if arrangementer is not None:
            for a in arrangementer:
                a["kilde_type"] = "fast_kalender"
                a["kilde_url"] = kilde_url
                a["tekst_bekreftet"] = _er_ordrett(a.get("original_tekst", ""), sidetekst)
            foreslatt_url = None
            if endelig_url and endelig_url != kilde_url and _samme_domene(endelig_url, kilde_url):
                foreslatt_url = endelig_url
            return arrangementer, foreslatt_url

    return _hent_fra_kilde_via_web_fetch(kilde_url, instruks)


def diagnostiser_nes_kalender(forste_dag: date, siste_dag: date) -> tuple[list[dict], str]:
    """Som hent_fra_kilde, men spesifikt for Nes kommunes aktivitetskalender (den viktigste
    enkeltkilden) og med en diagnosemelding som forklarer HVOR i kjeden noe eventuelt gikk
    galt, i stedet for et stille nulltreff.

    Kaller NES_KOMMUNE_KALENDER_API (sspkalender.prokom.no) direkte — en delt tredjeparts-
    tjeneste, IKKE kommunens egen nettside — så vi slipper å besøke nes.kommune.no i det
    hele tatt for selve datahentingen (kommunens egen brannmur har vist seg å blokkere
    serverens forespørsler dit). Hvis DEN direkte API-veien likevel feiler, faller vi
    tilbake til samme reserveløsning som den vanlige innhøstingen bruker: Claude henter
    siden selv med web_fetch-verktøyet, fra en helt annen nettverksrute. Da kan ikke
    original_tekst kode-verifiseres (tekst_bekreftet=False), men det er bedre enn ingenting.

    Returnerer (arrangementer, diagnose) — diagnose er tom streng ved suksess."""
    direkte_feil = None
    try:
        arrangementer = _hent_fra_prokom_kalender(
            NES_KOMMUNE_KALENDER_URL, NES_KOMMUNE_KALENDER_API, NES_KOMMUNE_KALENDER_STI, forste_dag, siste_dag
        )
        if arrangementer:
            return arrangementer, ""
        direkte_feil = f"API-et ble spurt direkte, men ga ingen treff for perioden {forste_dag} – {siste_dag}."
    except httpx.HTTPError as e:
        direkte_feil = (
            f"Klarte ikke å koble til sspkalender.prokom.no direkte: {type(e).__name__}: {e}"
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return [], direkte_feil or "Ukjent feil."

    instruks = standard_instruks(forste_dag, siste_dag)
    arrangementer, fallback_diagnose = _hent_fra_kilde_via_web_fetch_med_diagnose(
        NES_KOMMUNE_KALENDER_URL, instruks
    )
    if arrangementer:
        return arrangementer, ""
    return [], f"{direkte_feil} Reserveløsning: {fallback_diagnose}"


def _hent_fra_kilde_via_web_fetch_med_diagnose(kilde_url: str, instruks: str) -> tuple[list[dict], str]:
    """Som _hent_fra_kilde_via_web_fetch, men svelger ikke feil stille — returnerer i stedet
    en diagnosemelding som forklarer hva som gikk galt (selve Claude-kallet feilet, eller
    svaret inneholdt ingen gjenkjennbare arrangementer). Brukt av diagnostiser_nes_kalender,
    der et stille nulltreff er vanskelig å feilsøke for brukeren siden dette er den viktigste
    enkeltkilden."""
    client = Anthropic()
    prompt = f"""Gå til denne nettsiden og finn lokale arrangementer: {kilde_url}

{instruks}
"""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            tools=[{"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        return [], f"Claude-kallet feilet: {type(e).__name__}: {e}"

    tekst = "".join(b.text for b in response.content if b.type == "text")
    arrangementer = _parse_json_liste(tekst)
    for a in arrangementer:
        a["kilde_type"] = "fast_kalender"
        a["kilde_url"] = kilde_url
        a["tekst_bekreftet"] = False

    if not arrangementer:
        utdrag = tekst.strip()[:300]
        if utdrag:
            return [], f"Claude fant ingen arrangementer på siden. Svar (utdrag): {utdrag!r}"
        return [], "Claude ga et tomt svar (kan tyde på at web_fetch-verktøyet ikke fikk hentet siden)."
    return arrangementer, ""


def _hent_fra_kilde_via_web_fetch(kilde_url: str, instruks: str) -> tuple[list[dict], str | None]:
    """Reserveløsning: Claude henter siden selv via web_fetch-verktøyet.

    Brukes kun når programmatisk henting av siden feiler. Ordrett samsvar kan da ikke
    verifiseres i kode, så alle treff får tekst_bekreftet=False.
    """
    client = Anthropic()
    prompt = f"""Gå til denne nettsiden og finn lokale arrangementer: {kilde_url}

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
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return [], None

    tekst = "".join(b.text for b in response.content if b.type == "text")

    foreslatt_url = None
    url_treff = re.search(r"FAKTISK_URL:\s*(\S+)", tekst)
    if url_treff:
        kandidat = url_treff.group(1).strip().rstrip(".,)")
        if kandidat.startswith("http") and kandidat != kilde_url and _samme_domene(kandidat, kilde_url):
            foreslatt_url = kandidat

    arrangementer = _parse_json_liste(tekst)
    for a in arrangementer:
        a["kilde_type"] = "fast_kalender"
        a["kilde_url"] = kilde_url
        a["tekst_bekreftet"] = False
    return arrangementer, foreslatt_url


def diagnostiser_kilde(kilde_url: str) -> str:
    """Bruker Claude med web_fetch-verktøyet til å undersøke en kilde som ikke fungerer godt
    nok via de vanlige hente-veiene, og rapporterer tilbake hva den fant — f.eks. om siden
    bruker et gjenkjennbart mønster (Prokom-widget, WordPress REST-API, iCal/RSS-feed, et
    eget JS fetch()-kall som for Nes kommunes kalender) og en konkret API-URL. Tanken er å
    automatisere selve etterforskningsjobben (slik den ble gjort manuelt for Nes kommune og
    Visit Greater Oslo), slik at det raskt kan bygges en dedikert henting for kilden basert
    på rapporten — se NES_KOMMUNE_KALENDER_API / VISITGREATEROSLO_EVENTS_API for eksempler.

    Undersøker og rapporterer bare — endrer eller lagrer ingenting selv."""
    client = Anthropic()
    prompt = f"""Du skal undersøke denne nettsiden for et automatisk innhøstingssystem for \
lokale arrangementer, som en lokalavis bruker: {kilde_url}

Gå til siden med web_fetch-verktøyet og se etter:
1. Bruker siden et gjenkjennbart mønster for å hente arrangementsdata? Se spesielt etter:
   - Et JavaScript fetch()- eller XHR-kall mot et eget API (se etter "fetch(", "axios", \
eller en synlig JSON-URL i <script>-tagger)
   - En Prokom/ØRU-kalenderwidget ("startCalendar(" eller lignende)
   - Et WordPress REST-API (wp-json)
   - En iCal-feed (.ics) eller RSS-feed
   - Andre strukturerte datakilder
2. Hvis du finner et slikt API: hva er den fullstendige URL-en, inkludert alle parametre?
3. Fikk du i det hele tatt tilgang til siden, eller ble du blokkert/omdirigert/avvist?

Svar med en kort, konkret rapport (maks 10 linjer) beregnet på en utvikler som skal vurdere \
om det er verdt å bygge en dedikert, direkte henting for denne kilden. Vær presis om URL-er \
og mønstre du faktisk fant — ikke gjett eller anta noe du ikke har verifisert."""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            tools=[{"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        return f"Kunne ikke undersøke kilden: {type(e).__name__}: {e}"

    tekst = "".join(b.text for b in response.content if b.type == "text").strip()
    return tekst or "Fikk ikke noe svar fra undersøkelsen."


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
        messages=[{"role": "user", "content": prompt}],
    )

    svar = "".join(b.text for b in response.content if b.type == "text")
    arrangementer = _parse_json_liste(svar)
    for a in arrangementer:
        a["kilde_type"] = "limt_inn_tekst"
        a["kilde_url"] = None
        a["tekst_bekreftet"] = _er_ordrett(a.get("original_tekst", ""), tekst)
    return arrangementer

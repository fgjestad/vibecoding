import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.artikkel import generer_hel_artikkel, skriv_om_ett_avsnitt, standard_artikkel_instruks
from app.auth import sjekk_admin, sjekk_passord
from app.db import engine, get_session, init_db
from app.harvest import (
    NES_KOMMUNE_KALENDER_URL,
    beregn_arrangement_id,
    beregn_periode,
    beregn_signatur,
    diagnostiser_kilde,
    diagnostiser_nes_kalender,
    er_dedikert_kilde,
    er_tittel_duplikat,
    generer_sammenslatt_arrangement,
    hent_fotballkamper,
    hent_fra_bilde,
    hent_fra_kilde,
    hent_fra_pdf,
    hent_fra_tekst,
    hent_mer_info,
    instruks_for_manuell_kilde,
    normaliser_tekst,
    normaliser_url_for_dedup,
    standard_instruks,
    tekstlikhet,
    vertsnavn,
)
from app.llm import foreslå_kilder
from app.models import (
    Arrangement,
    Artikkel,
    ArtikkelAvsnitt,
    EkskludertSignatur,
    IkkeDuplikatPar,
    Innstilling,
    Kilde,
    KildeForslag,
    ManuellKilde,
)

OSLO_TZ = ZoneInfo("Europe/Oslo")


def oslo_tid(verdi: datetime, format: str = "%d.%m.%Y kl. %H:%M") -> str:
    """Formaterer et tidspunkt i norsk lokal tid (Europe/Oslo, håndterer sommer-/vintertid
    automatisk). Alle opprettet_at-felt lagres som naiv UTC (datetime.utcnow()), så en rå
    .strftime() på dem direkte ville vist UTC/GMT-klokkeslett i UI-et — feil med 1-2 timer for
    norske brukere avhengig av årstid."""
    if verdi.tzinfo is None:
        verdi = verdi.replace(tzinfo=ZoneInfo("UTC"))
    return verdi.astimezone(OSLO_TZ).strftime(format)


def _formater_dato_liste(datoer: list[str]) -> str:
    """Formaterer en sortert liste med ISO-datoer kompakt: slår sammen dag-for-dag-
    sammenhengende strekk til "YYYY-MM-DD – YYYY-MM-DD", og skiller ikke-sammenhengende strekk
    med komma — i stedet for én fra-til-periode som ville sett ut som at arrangementet skjer
    hver eneste dag i hele spennet, selv om det egentlig f.eks. bare er i helgene."""
    if not datoer:
        return ""
    parsed = [date.fromisoformat(d) for d in datoer]
    grupper = [[parsed[0]]]
    for d in parsed[1:]:
        if d == grupper[-1][-1] + timedelta(days=1):
            grupper[-1].append(d)
        else:
            grupper.append([d])
    return ", ".join(
        gruppe[0].isoformat() if len(gruppe) == 1 else f"{gruppe[0].isoformat()} – {gruppe[-1].isoformat()}"
        for gruppe in grupper
    )


def datoperiode_tekst(a: Arrangement) -> str:
    """Viser datoen/datoene et arrangement skjer på — se flere_datoer på Arrangement-modellen
    og _finn_gjentakende_arrangementer for hvordan denne bygges opp. Faller tilbake til det
    gamle enkle dato/til_dato-parret for rader uten flere_datoer (f.eks. data fra før denne
    funksjonen fantes)."""
    if a.flere_datoer:
        try:
            datoer = json.loads(a.flere_datoer)
        except (json.JSONDecodeError, TypeError):
            datoer = None
        if datoer:
            return _formater_dato_liste(sorted(datoer))
    if a.til_dato:
        return f"{a.dato} – {a.til_dato}"
    return a.dato


app = FastAPI(title="Raumnes kalendergenerator")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["domene"] = vertsnavn
templates.env.filters["dedikert"] = er_dedikert_kilde
templates.env.tests["dedikert"] = er_dedikert_kilde
templates.env.filters["oslo_tid"] = oslo_tid
templates.env.filters["datoperiode"] = datoperiode_tekst

MAKS_SAMTIDIGE_KILDER = 5

KALENDER_FOTNOTE = (
    "Denne kalenderen er laget ved hjelp av kunstig intelligens og er gått gjennom av en "
    "journalist i Raumnes. Ønsker du oppføringer i denne kalenderen, legg det inn i Nes "
    "kommune sin aktivitetskalender: https://www.nes.kommune.no/aktivitetskalender/"
)

scheduler = BackgroundScheduler(timezone="Europe/Oslo")
_AUTO_INNHOSTING_JOBB_ID = "auto-innhosting"


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    scheduler.start()
    _planlegg_auto_innhosting()


def _hent_innstilling(session: Session) -> Innstilling:
    innstilling = session.get(Innstilling, 1)
    if not innstilling:
        innstilling = Innstilling(id=1)
        session.add(innstilling)
        session.commit()
        session.refresh(innstilling)
    return innstilling


def _planlegg_auto_innhosting() -> None:
    """Leser gjeldende autojobb-innstillinger fra databasen og (re)planlegger den automatiske
    innhøstingsjobben i tidsplanleggeren. Kalles ved oppstart, og av admin-ruten under hver
    gang innstillingene lagres, slik at en endring trer i kraft med én gang uten omstart."""
    with Session(engine) as session:
        innstilling = _hent_innstilling(session)

    if not innstilling.auto_innhosting_aktiv:
        if scheduler.get_job(_AUTO_INNHOSTING_JOBB_ID):
            scheduler.remove_job(_AUTO_INNHOSTING_JOBB_ID)
        return

    try:
        time, minutt = (int(d) for d in innstilling.auto_innhosting_klokkeslett.split(":"))
    except (ValueError, AttributeError):
        time, minutt = 6, 0

    if innstilling.auto_innhosting_frekvens == "ukentlig":
        trigger = CronTrigger(day_of_week=innstilling.auto_innhosting_ukedag, hour=time, minute=minutt)
    else:
        trigger = CronTrigger(hour=time, minute=minutt)
    scheduler.add_job(_kjor_automatisk_innhosting, trigger, id=_AUTO_INNHOSTING_JOBB_ID, replace_existing=True)


def _kjor_automatisk_innhosting() -> None:
    """Selve autojobben — kjøres av tidsplanleggeren, ikke av en HTTP-forespørsel. Bruker
    standardinstruksen (samme som forhåndsutfylt i "Kjør innhøsting"-feltet), siden det ikke
    finnes noen bruker til stede som kan redigere den."""
    with Session(engine) as session:
        innstilling = _hent_innstilling(session)
        forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)
        _utfor_innhosting(session, standard_instruks(forste_dag, siste_dag))


@app.post("/innstillinger")
def oppdater_innstillinger(
    antall_dager: int = Form(...),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    innstilling = _hent_innstilling(session)
    innstilling.antall_dager = max(1, min(antall_dager, 90))
    session.add(innstilling)
    session.commit()
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/innstillinger/auto-innhosting")
def oppdater_auto_innhosting(
    klokkeslett: str = Form("06:00"),
    frekvens: str = Form("daglig"),
    ukedag: int = Form(0),
    aktiv: bool = Form(False),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_admin),
):
    innstilling = _hent_innstilling(session)
    innstilling.auto_innhosting_aktiv = aktiv
    innstilling.auto_innhosting_frekvens = "ukentlig" if frekvens == "ukentlig" else "daglig"
    innstilling.auto_innhosting_ukedag = max(0, min(ukedag, 6))
    if re.match(r"^\d{1,2}:\d{2}$", klokkeslett or ""):
        innstilling.auto_innhosting_klokkeslett = klokkeslett
    session.add(innstilling)
    session.commit()
    _planlegg_auto_innhosting()
    return RedirectResponse(url="/innhosting", status_code=303)


def _kildeliste_respons(request: Request, session: Session, melding: str | None = None):
    kilder = session.exec(select(Kilde)).all()
    kilder = sorted(kilder, key=lambda k: k.samlet_sortering, reverse=True)
    return templates.TemplateResponse(
        "_kildeliste.html", {"request": request, "kilder": kilder, "melding": melding}
    )


def _forslagliste_respons(request: Request, session: Session):
    forslag = session.exec(
        select(KildeForslag).where(KildeForslag.status == "ny")
    ).all()
    return templates.TemplateResponse(
        "_forslagliste.html", {"request": request, "forslag": forslag}
    )


@app.get("/")
def forside(
    request: Request,
    session: Session = Depends(get_session),
    rolle: str = Depends(sjekk_passord),
):
    if rolle != "admin":
        # Journalist-brukere har ikke tilgang til kildelisten — send dem til startsiden
        # de faktisk skal bruke, i stedet for en forvirrende feilmelding.
        return RedirectResponse(url="/innhosting", status_code=303)
    kilder = session.exec(select(Kilde)).all()
    kilder = sorted(kilder, key=lambda k: k.samlet_sortering, reverse=True)
    forslag = session.exec(
        select(KildeForslag).where(KildeForslag.status == "ny")
    ).all()
    return templates.TemplateResponse(
        "kilder.html",
        {"request": request, "kilder": kilder, "forslag": forslag},
    )


@app.post("/kilder")
def opprett_kilde(
    navn: str = Form(...),
    url: str = Form(...),
    manuell_prioritet: int = Form(0),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_admin),
):
    kilde = Kilde(navn=navn.strip(), url=url.strip(), manuell_prioritet=manuell_prioritet)
    session.add(kilde)
    session.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/kilder/{kilde_id}/rediger")
def rediger_kilde(
    kilde_id: int,
    request: Request,
    navn: str = Form(...),
    url: str = Form(...),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_admin),
):
    kilde = session.get(Kilde, kilde_id)
    if kilde:
        kilde.navn = navn.strip()
        kilde.url = url.strip()
        session.add(kilde)
        session.commit()
    return _kildeliste_respons(request, session)


@app.post("/kilder/{kilde_id}/prioritet")
def sett_prioritet(
    kilde_id: int,
    request: Request,
    manuell_prioritet: int = Form(0),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_admin),
):
    kilde = session.get(Kilde, kilde_id)
    if kilde:
        kilde.manuell_prioritet = manuell_prioritet
        session.add(kilde)
        session.commit()
    return _kildeliste_respons(request, session)


@app.post("/kilder/{kilde_id}/aktiver")
def aktiver_kilde(
    kilde_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_admin),
):
    kilde = session.get(Kilde, kilde_id)
    if kilde:
        kilde.aktiv = True
        session.add(kilde)
        session.commit()
    return _kildeliste_respons(request, session)


@app.post("/kilder/{kilde_id}/deaktiver")
def deaktiver_kilde(
    kilde_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_admin),
):
    kilde = session.get(Kilde, kilde_id)
    if kilde and not er_dedikert_kilde(kilde.url):
        kilde.aktiv = False
        session.add(kilde)
        session.commit()
    return _kildeliste_respons(request, session)


@app.post("/kilder/{kilde_id}/undersok")
def undersok_kilde(
    kilde_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_admin),
):
    """Ber Claude undersøke kilden (via web_fetch) og rapportere om den bruker et
    gjenkjennbart mønster (Prokom-widget, WordPress REST-API, iCal/RSS, egen JS fetch()
    osv.) — samme etterforskningsjobb som ble gjort manuelt for Nes kommune og Visit
    Greater Oslo, automatisert. Se harvest.diagnostiser_kilde."""
    kilde = session.get(Kilde, kilde_id)
    if not kilde:
        return _kildeliste_respons(request, session)
    try:
        rapport = diagnostiser_kilde(kilde.url)
    except Exception as e:
        rapport = f"Kunne ikke undersøke kilden: {e}"
    melding = f"Undersøkelse av «{kilde.navn}»:\n\n{rapport}"
    return _kildeliste_respons(request, session, melding)


@app.post("/kilder/fjern-duplikater")
def fjern_duplikater(
    request: Request,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_admin),
):
    """Fjerner kilder med samme URL (etter normalisering), og beholder den med høyest prioritet."""
    kilder = session.exec(select(Kilde)).all()
    grupper: dict[str, list[Kilde]] = {}
    for k in kilder:
        nokkel = normaliser_url_for_dedup(k.url)
        grupper.setdefault(nokkel, []).append(k)

    antall_fjernet = 0
    for gruppe in grupper.values():
        if len(gruppe) <= 1:
            continue
        gruppe.sort(key=lambda k: k.samlet_sortering, reverse=True)
        for duplikat in gruppe[1:]:
            session.delete(duplikat)
            antall_fjernet += 1
    session.commit()

    melding = f"{antall_fjernet} duplikat(er) fjernet." if antall_fjernet else "Ingen duplikater funnet."
    return _kildeliste_respons(request, session, melding)


@app.post("/kilder/oppdag")
def oppdag_nye_kilder(
    ekstra_instruks: str = Form(""),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_admin),
):
    eksisterende_kilder = session.exec(select(Kilde)).all()
    eksisterende_urler = {k.url for k in eksisterende_kilder}
    eksisterende_navn = {k.navn.strip().lower() for k in eksisterende_kilder}
    eksisterende_forslag = session.exec(select(KildeForslag)).all()
    eksisterende_forslag_urler = {f.url for f in eksisterende_forslag}
    eksisterende_forslag_navn = {f.navn.strip().lower() for f in eksisterende_forslag}

    forslag = foreslå_kilder(
        ekstra_instruks=ekstra_instruks,
        eksisterende_kilder=[k.navn for k in eksisterende_kilder],
    )
    for f in forslag:
        navn_normalisert = f["navn"].strip().lower()
        if (
            f["url"] in eksisterende_urler
            or f["url"] in eksisterende_forslag_urler
            or navn_normalisert in eksisterende_navn
            or navn_normalisert in eksisterende_forslag_navn
        ):
            continue
        session.add(
            KildeForslag(
                navn=f["navn"], url=f["url"], begrunnelse=f.get("begrunnelse", "")
            )
        )
        eksisterende_forslag_urler.add(f["url"])
        eksisterende_forslag_navn.add(navn_normalisert)
    session.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/forslag/{forslag_id}/godkjenn")
def godkjenn_forslag(
    forslag_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_admin),
):
    forslag = session.get(KildeForslag, forslag_id)
    if forslag and forslag.status == "ny":
        session.add(Kilde(navn=forslag.navn, url=forslag.url))
        forslag.status = "godkjent"
        session.add(forslag)
        session.commit()
    return _forslagliste_respons(request, session)


@app.post("/forslag/{forslag_id}/avvis")
def avvis_forslag(
    forslag_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_admin),
):
    forslag = session.get(KildeForslag, forslag_id)
    if forslag:
        forslag.status = "avvist"
        session.add(forslag)
        session.commit()
    return _forslagliste_respons(request, session)


@app.post("/kilder/hent-fotballkamper")
def hent_fotballkamper_rute(
    request: Request,
    session: Session = Depends(get_session),
    rolle: str = Depends(sjekk_passord),
):
    innstilling = _hent_innstilling(session)
    forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)

    try:
        rå, antall_feilet = hent_fotballkamper(forste_dag, siste_dag)
    except Exception as e:
        return templates.TemplateResponse(
            "innhosting.html",
            _innhosting_kontekst(request, session, f"Kunne ikke hente fotballkamper: {e}", rolle),
        )

    eksisterende = session.exec(select(Arrangement)).all()
    sett_ider = {a.arrangement_id for a in eksisterende}
    hittil_pr_dato = _hittil_pr_dato(eksisterende)
    flerdags_kandidater = [a for a in eksisterende if not a.er_sammenslatt]
    ekskluderte = {e.signatur for e in session.exec(select(EkskludertSignatur)).all()}
    ikke_duplikat_par = _hent_ikke_duplikat_par(session)
    berorte_grupper: set[str] = set()
    for a in rå:
        _lagre_arrangement(
            session, a, sett_ider, ekskluderte, hittil_pr_dato, flerdags_kandidater, berorte_grupper,
            ikke_duplikat_par, sjekk_duplikater=False,
        )
    session.commit()
    _oppdater_sammenslatte_grupper(session, berorte_grupper, ekskluderte)

    if antall_feilet:
        return templates.TemplateResponse(
            "innhosting.html",
            _innhosting_kontekst(
                request,
                session,
                f"{antall_feilet} oppslag mot fotball.no feilet og ble hoppet over. "
                "Resten ble lagt til i utkastet.",
                rolle,
            ),
        )
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/kilder/hent-nes-kalender")
def hent_nes_kalender_rute(
    request: Request,
    session: Session = Depends(get_session),
    rolle: str = Depends(sjekk_passord),
):
    innstilling = _hent_innstilling(session)
    forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)

    try:
        rå, diagnose = diagnostiser_nes_kalender(forste_dag, siste_dag)
    except Exception as e:
        return templates.TemplateResponse(
            "innhosting.html",
            _innhosting_kontekst(
                request, session, f"Kunne ikke hente fra Nes kommunes aktivitetskalender: {e}", rolle
            ),
        )

    eksisterende = session.exec(select(Arrangement)).all()
    sett_ider = {a.arrangement_id for a in eksisterende}
    hittil_pr_dato = _hittil_pr_dato(eksisterende)
    flerdags_kandidater = [a for a in eksisterende if not a.er_sammenslatt]
    ekskluderte = {e.signatur for e in session.exec(select(EkskludertSignatur)).all()}
    ikke_duplikat_par = _hent_ikke_duplikat_par(session)
    berorte_grupper: set[str] = set()
    for a in rå:
        _lagre_arrangement(
            session, a, sett_ider, ekskluderte, hittil_pr_dato, flerdags_kandidater, berorte_grupper, ikke_duplikat_par
        )
    session.commit()
    _oppdater_sammenslatte_grupper(session, berorte_grupper, ekskluderte)

    if not rå and diagnose:
        return templates.TemplateResponse(
            "innhosting.html",
            _innhosting_kontekst(request, session, f"Nes kommunes aktivitetskalender: {diagnose}", rolle),
        )
    return RedirectResponse(url="/innhosting", status_code=303)


def _finn_gjentakende_arrangementer(
    tittel: str, tekst: str, sted: str, dato_str: str, kandidater: list[Arrangement]
) -> list[Arrangement]:
    """Finner ALLE eksisterende arrangementer som er samme arrangement som dette, bare på en
    annen dato — samme sted og nesten ordrett lik tittel/tekst. IKKE noe krav om at datoene
    henger sammen dag for dag: et arrangement som går f.eks. bare i helgene (lørdag+søndag,
    med hull på hverdager) eller bare på hverdager skal fortsatt samles til én oppføring, ikke
    vises som mange nesten identiske, forvirrende rader i utkastet.

    (Eksakt samme dato som en kandidat hopper vi over her — det er en duplikatsjekk mellom
    ulike KILDER samme dag, se i stedet er_tittel_duplikat/duplikat_gruppe for det tilfellet.)

    Returnerer en LISTE (ikke bare det første treffet): en enkelt ny dato kan matche flere
    allerede lagrede forekomster på én gang. Kalleren i _lagre_arrangement slår sammen alle
    treffene og den nye datoen til én rad, med den fullstendige datolisten lagret i
    flere_datoer (se der) — IKKE en enkel fra-til-periode, siden det ville sett ut som at
    arrangementet skjer hver eneste dag i hele spennet."""
    treff = []
    for eksisterende in kandidater:
        if eksisterende.dato == dato_str:
            continue
        if normaliser_tekst(sted) != normaliser_tekst(eksisterende.sted):
            continue
        if tekstlikhet(tittel, eksisterende.tittel) < 0.85:
            continue
        if tekstlikhet(tekst, eksisterende.original_tekst) < 0.85:
            continue
        treff.append(eksisterende)
    return treff


def _hent_ikke_duplikat_par(session: Session) -> set[frozenset[str]]:
    """Laster hukommelsen over signatur-par brukeren har sagt fra om ikke er duplikater av
    hverandre (se /innhosting/{id}/ikke-duplikat), som et sett av uordnede par — slik at
    _lagre_arrangement kan hoppe over akkurat disse to ved fremtidig duplikatgruppering,
    uansett hvilken rekkefølge de dukker opp i."""
    return {
        frozenset({p.signatur_a, p.signatur_b})
        for p in session.exec(select(IkkeDuplikatPar)).all()
    }


def _lagre_arrangement(
    session: Session,
    data: dict,
    sett_ider: set[str],
    ekskluderte_signaturer: set[str],
    hittil_pr_dato: dict[str, list[Arrangement]],
    flerdags_kandidater: list[Arrangement],
    berorte_grupper: set[str],
    ikke_duplikat_par: set[frozenset[str]],
    sjekk_duplikater: bool = True,
) -> bool:
    """Normaliserer og lagrer ett arrangement, med id-basert dedup. Returnerer True hvis lagret
    (eller slått sammen inn i et eksisterende gjentakende arrangement).

    Eksakte duplikater (samme tittel+dato+sted) droppes stille via sett_ider, som før —
    uavhengig av sjekk_duplikater under, siden dette bare fanger opp at NØYAKTIG samme funn
    kommer inn to ganger (f.eks. om innhøsting kjøres to ganger), ikke fuzzy kryss-kilde-
    duplikater.

    Hvis dette tydelig er samme arrangement som et allerede lagret, bare på en annen dato (se
    _finn_gjentakende_arrangementer), utvides det eksisterendes flere_datoer i stedet for å
    opprette en ny rad — slik unngås at et arrangement som gjentas flere ganger (sammenhengende
    eller ikke, f.eks. bare i helgene) vises som mange separate, forvirrende endags-rader.

    Ellers gjelder vanlig duplikatsjekk MED MINDRE sjekk_duplikater=False: sannsynlige
    duplikater (fanget opp av er_tittel_duplikat — fuzzy tittel-sammenligning mot alt annet
    lagret på samme dato, se harvest.er_tittel_duplikat) knyttes sammen i en delt
    duplikat_gruppe (ny, eller en de allerede tilhører) og avhukes alle av — berorte_grupper
    samler opp hvilke grupper som fikk et nytt medlem denne kjøringen, slik at den
    sammenslåtte AI-oppføringen for gruppen kan (re)genereres étt samlet gang etter at hele
    batchen er lagret (se _oppdater_sammenslatte_grupper), i stedet for ett AI-kall per nytt
    duplikat-funn. sjekk_duplikater=False brukes for fotballkamper (se
    hent_fotballkamper_rute) — der er fotball.no eneste kilde og hver kamp forekommer i kun
    én versjon, så fuzzy kryss-kilde-duplikatsjekk (og et unødvendig AI-sammenslåingskall) gir
    ingen verdi og risikerer bare å feilaktig slå en fotballkamp sammen med et urelatert
    arrangement fra en annen kilde med lignende tittel/klokkeslett samme dag."""
    tittel = str(data.get("tittel") or "").strip()
    dato_str = str(data.get("dato") or "").strip()
    sted = str(data.get("sted") or "").strip()
    original_tekst = str(data.get("original_tekst") or "").strip()
    if not tittel or not dato_str:
        return False
    try:
        date.fromisoformat(dato_str)
    except ValueError:
        return False

    arrangement_id = beregn_arrangement_id(tittel, dato_str, sted)
    if arrangement_id in sett_ider:
        return False
    sett_ider.add(arrangement_id)

    gjentakende = _finn_gjentakende_arrangementer(tittel, original_tekst, sted, dato_str, flerdags_kandidater)
    if gjentakende:
        # Slå sammen den nye datoen og ALLE treffene til én rad, med den FULLSTENDIGE lista
        # over datoer bevart i flere_datoer — ikke bare min/maks som en fra-til-periode, siden
        # det ville sett ut som at arrangementet skjer hver eneste dag i hele spennet selv om
        # det egentlig bare er f.eks. helger. dato/til_dato settes likevel til tidligste/
        # seneste, brukt til filtrering og sortering.
        alle_datoer: set[str] = {dato_str}
        for annen in gjentakende:
            if annen.flere_datoer:
                try:
                    alle_datoer.update(json.loads(annen.flere_datoer))
                except (json.JSONDecodeError, TypeError):
                    pass
            alle_datoer.add(annen.dato)
            if annen.til_dato:
                alle_datoer.add(annen.til_dato)
        sorterte_datoer = sorted(alle_datoer)

        hoved = gjentakende[0]
        hoved.dato = sorterte_datoer[0]
        hoved.til_dato = sorterte_datoer[-1]
        hoved.flere_datoer = json.dumps(sorterte_datoer)
        session.add(hoved)

        for annen in gjentakende[1:]:
            if annen in flerdags_kandidater:
                flerdags_kandidater.remove(annen)
            samme_dato_annen = hittil_pr_dato.get(annen.dato)
            if samme_dato_annen and annen in samme_dato_annen:
                samme_dato_annen.remove(annen)
            if annen in session.new:
                # Lagt til (session.add) tidligere i SAMME kjøring, men ennå ikke skrevet til
                # databasen (ingen commit har skjedd midt i løkken) — session.delete() krever
                # en persistert rad, så en slik rad må i stedet bare fjernes fra sesjonen.
                session.expunge(annen)
            else:
                session.delete(annen)

        return True

    signatur = beregn_signatur(tittel, data.get("arrangor"), dato_str)
    forhandsvalgt_bort = signatur in ekskluderte_signaturer

    klokkeslett = data.get("klokkeslett")
    samme_dato = hittil_pr_dato.setdefault(dato_str, [])
    duplikat_gruppe = None
    if sjekk_duplikater:
        matchende = [
            annen for annen in samme_dato
            if er_tittel_duplikat(tittel, klokkeslett, annen.tittel, annen.klokkeslett)
            and frozenset({signatur, annen.signatur}) not in ikke_duplikat_par
        ]
        if matchende:
            duplikat_gruppe = next((m.duplikat_gruppe for m in matchende if m.duplikat_gruppe), None)
            if not duplikat_gruppe:
                duplikat_gruppe = f"g-{uuid.uuid4().hex[:12]}"
            for annen in matchende:
                if not annen.duplikat_gruppe:
                    annen.duplikat_gruppe = duplikat_gruppe
                    session.add(annen)
                if annen.valgt:
                    annen.valgt = False
                    session.add(annen)
            berorte_grupper.add(duplikat_gruppe)

    nytt = Arrangement(
        arrangement_id=arrangement_id,
        tittel=tittel,
        dato=dato_str,
        klokkeslett=klokkeslett,
        sted=sted,
        arrangor=data.get("arrangor"),
        original_tekst=original_tekst,
        tekst_bekreftet=bool(data.get("tekst_bekreftet", False)),
        kilde_type=data.get("kilde_type", "nettsok"),
        kilde_url=data.get("kilde_url"),
        geografisk_relevans=data.get("geografisk_relevans", "usikker"),
        signatur=signatur,
        valgt=not forhandsvalgt_bort and not duplikat_gruppe,
        forhandsvalgt_bort=forhandsvalgt_bort,
        duplikat_gruppe=duplikat_gruppe,
    )
    session.add(nytt)
    samme_dato.append(nytt)
    flerdags_kandidater.append(nytt)
    return True


_GEOGRAFISK_RANGERING = {"bekreftet": 0, "sannsynlig": 1, "usikker": 2}


def _oppdater_sammenslatte_grupper(
    session: Session, berorte_grupper: set[str], ekskluderte_signaturer: set[str]
) -> None:
    """Regenererer den sammenslåtte AI-oppføringen (se harvest.generer_sammenslatt_arrangement)
    for hver duplikat-gruppe som fikk et nytt rått medlem i denne kjøringen. Denne
    oppføringen er den som faktisk vises/velges i utkastet for gruppen — de rå
    enkeltkilde-funnene ligger fortsatt i databasen uendret, tilgjengelige som alternativer.

    Hvis sammenslåingen feiler (ingen API-nøkkel, API-feil, uparsbart svar), gjøres
    ingenting med den gruppen — de rå funnene vises da i stedet hver for seg, merket som
    "mulig duplikat uten sammenslåing", inntil et senere forsøk lykkes."""
    for gruppe_id in berorte_grupper:
        medlemmer = session.exec(
            select(Arrangement).where(
                Arrangement.duplikat_gruppe == gruppe_id,
                Arrangement.er_sammenslatt == False,  # noqa: E712
            )
        ).all()
        if len(medlemmer) < 2:
            continue

        data_liste = [
            {
                "tittel": m.tittel,
                "dato": m.dato,
                "klokkeslett": m.klokkeslett,
                "sted": m.sted,
                "arrangor": m.arrangor,
                "original_tekst": m.original_tekst,
                "kilde_url": m.kilde_url,
            }
            for m in medlemmer
        ]
        try:
            sammenslatt = generer_sammenslatt_arrangement(data_liste)
        except Exception:
            sammenslatt = None
        if not sammenslatt:
            continue

        tittel = str(sammenslatt.get("tittel") or "").strip()
        dato_str = str(sammenslatt.get("dato") or "").strip()
        if not tittel or not dato_str:
            continue
        try:
            date.fromisoformat(dato_str)
        except ValueError:
            continue

        # Claude får kun ÉN dato per medlem (se data_liste over) og kan derfor ikke selv
        # bevare flerdags-/gjentakende datoer i det sammenslåtte svaret sitt. Bygg i stedet
        # den fullstendige datolista fra de rå medlemmenes egne dato/til_dato/flere_datoer
        # (samme fremgangsmåte som _finn_gjentakende_arrangementer/_lagre_arrangement), og la
        # den overstyre AI-svarets enkeltdato.
        alle_datoer: set[str] = set()
        for m in medlemmer:
            alle_datoer.add(m.dato)
            if m.til_dato:
                alle_datoer.add(m.til_dato)
            if m.flere_datoer:
                try:
                    alle_datoer.update(json.loads(m.flere_datoer))
                except (json.JSONDecodeError, TypeError):
                    pass
        sorterte_datoer = sorted(alle_datoer)
        dato_str = sorterte_datoer[0]
        til_dato = sorterte_datoer[-1] if len(sorterte_datoer) > 1 else None
        flere_datoer_json = json.dumps(sorterte_datoer) if len(sorterte_datoer) > 1 else None

        beste_relevans = min(
            (m.geografisk_relevans for m in medlemmer),
            key=lambda r: _GEOGRAFISK_RANGERING.get(r, 3),
            default="usikker",
        )
        signatur = beregn_signatur(tittel, sammenslatt.get("arrangor"), dato_str)

        # Den sammenslåtte oppføringen har ingen egen kilde-URL (teksten er skrevet av Claude
        # ut fra FLERE kilder, ikke ordrett fra én bestemt), men artikkel-kopieringen ("Kopier
        # med formatering") lenker det fete nøkkelordet til nettopp denne URL-en — uten en URL
        # her mister brukeren lenken helt for sammenslåtte arrangementer. Bruk derfor URL-en
        # til ett av de rå kildefunnene i stedet: en fast, dedikert kalenderkilde foretrekkes
        # (mer stabil/relevant enn et generisk nettsøk-treff) hvis noen av medlemmene har det,
        # ellers den første tilgjengelige.
        kilde_url = next(
            (m.kilde_url for m in medlemmer if m.kilde_url and m.kilde_type == "fast_kalender"),
            next((m.kilde_url for m in medlemmer if m.kilde_url), None),
        )

        eksisterende_sammenslatt = session.exec(
            select(Arrangement).where(
                Arrangement.duplikat_gruppe == gruppe_id,
                Arrangement.er_sammenslatt == True,  # noqa: E712
            )
        ).first()
        if eksisterende_sammenslatt:
            eksisterende_sammenslatt.tittel = tittel
            eksisterende_sammenslatt.dato = dato_str
            eksisterende_sammenslatt.til_dato = til_dato
            eksisterende_sammenslatt.flere_datoer = flere_datoer_json
            eksisterende_sammenslatt.klokkeslett = sammenslatt.get("klokkeslett")
            eksisterende_sammenslatt.sted = str(sammenslatt.get("sted") or "")
            eksisterende_sammenslatt.arrangor = sammenslatt.get("arrangor")
            eksisterende_sammenslatt.original_tekst = str(sammenslatt.get("original_tekst") or "")
            eksisterende_sammenslatt.geografisk_relevans = beste_relevans
            eksisterende_sammenslatt.signatur = signatur
            eksisterende_sammenslatt.kilde_url = kilde_url
            session.add(eksisterende_sammenslatt)
        else:
            forhandsvalgt_bort = signatur in ekskluderte_signaturer
            session.add(
                Arrangement(
                    arrangement_id=f"sammenslatt-{gruppe_id}",
                    tittel=tittel,
                    dato=dato_str,
                    til_dato=til_dato,
                    flere_datoer=flere_datoer_json,
                    klokkeslett=sammenslatt.get("klokkeslett"),
                    sted=str(sammenslatt.get("sted") or ""),
                    arrangor=sammenslatt.get("arrangor"),
                    original_tekst=str(sammenslatt.get("original_tekst") or ""),
                    tekst_bekreftet=False,
                    kilde_type="sammenslatt",
                    kilde_url=kilde_url,
                    geografisk_relevans=beste_relevans,
                    signatur=signatur,
                    valgt=not forhandsvalgt_bort,
                    forhandsvalgt_bort=forhandsvalgt_bort,
                    duplikat_gruppe=gruppe_id,
                    er_sammenslatt=True,
                )
            )
    session.commit()


def _duplikatgrupper_uten_sammenslaing(session: Session) -> set[str]:
    """Finner duplikat-grupper som har minst to rå kildefunn, men ennå ingen sammenslått
    AI-oppføring — enten fordi sammenslåingen aldri er forsøkt, eller fordi et tidligere
    forsøk feilet (se _oppdater_sammenslatte_grupper). Brukes av "Slå sammen duplikater"-
    knappen til å vite hvilke grupper som skal (re)forsøkes."""
    arrangementer = session.exec(
        select(Arrangement).where(Arrangement.duplikat_gruppe.is_not(None))
    ).all()
    per_gruppe: dict[str, list[Arrangement]] = {}
    for a in arrangementer:
        per_gruppe.setdefault(a.duplikat_gruppe, []).append(a)
    return {
        gruppe_id
        for gruppe_id, medlemmer in per_gruppe.items()
        if not any(m.er_sammenslatt for m in medlemmer)
        and len(medlemmer) >= 2
    }


def _hittil_pr_dato(eksisterende: list[Arrangement]) -> dict[str, list[Arrangement]]:
    """Bygger oppstartstilstanden for duplikatsjekken i _lagre_arrangement, ut fra rå
    arrangementer som allerede finnes i utkastet (sammenslåtte AI-oppføringer holdes utenfor
    — nye funn skal alltid sammenlignes mot de opprinnelige kildefunnene, ikke mot en
    allerede omskrevet tekst)."""
    hittil_pr_dato: dict[str, list[Arrangement]] = {}
    for a in eksisterende:
        if a.er_sammenslatt:
            continue
        hittil_pr_dato.setdefault(a.dato, []).append(a)
    return hittil_pr_dato


def _sorteringsnokkel(a: Arrangement) -> tuple:
    """Sorterer på dato, deretter klokkeslett tolket som klokketid (ikke tekst — "9:00" skal
    komme før "18:00" samme dag, selv om det ikke er nullutfylt). Arrangementer uten oppgitt
    klokkeslett kommer sist på sin dato."""
    if a.klokkeslett:
        treff = re.match(r"(\d{1,2})[:.](\d{2})", a.klokkeslett.strip())
        if treff:
            return (a.dato, 0, int(treff.group(1)), int(treff.group(2)))
    return (a.dato, 1, 0, 0)


def _grupper_for_visning(arrangementer: list[Arrangement]) -> list[dict]:
    """Bygger visningsstrukturen for Innhøsting-siden: en duplikat-gruppe som har fått en
    ferdig sammenslått AI-oppføring (se _oppdater_sammenslatte_grupper) vises som ÉTT synlig
    kort — den sammenslåtte — med de rå enkeltkilde-funnene tilgjengelig som skjulte
    alternativer bak en pil. Brukeren kan åpne og velge et av dem i stedet, via de vanlige
    avkrysningsboksene (som allerede styrer hva som havner i artikkelen — å hake av et
    alternativ og hake vekk den sammenslåtte er alt som trengs).

    En duplikat-gruppe UTEN en (ennå) sammenslått oppføring — f.eks. fordi sammenslåingen
    feilet — vises i stedet som separate kort, hver merket som mulig duplikat uten
    sammenslåing, slik det alltid har gjort.

    Returnerer en liste av {"hoved": Arrangement, "alternativer": [Arrangement, ...],
    "duplikat_uten_sammenslaing": bool}, én rad per SYNLIG kort (ikke én per arrangement)."""
    per_gruppe: dict[str, list[Arrangement]] = {}
    for a in arrangementer:
        if a.duplikat_gruppe:
            per_gruppe.setdefault(a.duplikat_gruppe, []).append(a)

    skjult_ider: set[int] = set()
    resultat = []
    for a in arrangementer:
        if a.id in skjult_ider:
            continue
        gruppe = per_gruppe.get(a.duplikat_gruppe) if a.duplikat_gruppe else None
        if gruppe:
            sammenslatt = next((g for g in gruppe if g.er_sammenslatt), None)
            if sammenslatt:
                if a.id != sammenslatt.id:
                    continue  # vises kun som alternativ inni hovedkortet, ikke på toppnivå
                alternativer = [g for g in gruppe if not g.er_sammenslatt]
                skjult_ider.update(g.id for g in alternativer)
                resultat.append(
                    {"hoved": a, "alternativer": alternativer, "duplikat_uten_sammenslaing": False}
                )
                continue
            resultat.append({"hoved": a, "alternativer": [], "duplikat_uten_sammenslaing": True})
            continue
        resultat.append({"hoved": a, "alternativer": [], "duplikat_uten_sammenslaing": False})
    return resultat


def _sorter_grupper(grupper: list[dict]) -> list[dict]:
    return sorted(grupper, key=lambda g: _sorteringsnokkel(g["hoved"]))


def _innhosting_kontekst(
    request: Request, session: Session, feilmelding: str | None = None, rolle: str = "journalist"
) -> dict:
    innstilling = _hent_innstilling(session)
    arrangementer = session.exec(select(Arrangement)).all()

    nes_kalender_vert = vertsnavn(NES_KOMMUNE_KALENDER_URL)
    antall_fra_nes_kalender = sum(
        1 for a in arrangementer if a.kilde_url and vertsnavn(a.kilde_url) == nes_kalender_vert
    )

    grupper = _sorter_grupper(_grupper_for_visning(arrangementer))
    manuelle_kilder = sorted(
        session.exec(select(ManuellKilde)).all(), key=lambda m: m.opprettet_at, reverse=True
    )

    forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)
    return {
        "request": request,
        "grupper": grupper,
        "antall_fra_nes_kalender": antall_fra_nes_kalender,
        "manuelle_kilder": manuelle_kilder,
        "forste_dag": forste_dag,
        "siste_dag": siste_dag,
        "antall_dager": innstilling.antall_dager,
        "standard_instruks_verdi": standard_instruks(forste_dag, siste_dag),
        "feilmelding": feilmelding,
        "rolle": rolle,
        "auto_innhosting_aktiv": innstilling.auto_innhosting_aktiv,
        "auto_innhosting_frekvens": innstilling.auto_innhosting_frekvens,
        "auto_innhosting_ukedag": innstilling.auto_innhosting_ukedag,
        "auto_innhosting_klokkeslett": innstilling.auto_innhosting_klokkeslett,
    }


@app.get("/innhosting")
def innhosting_side(
    request: Request,
    session: Session = Depends(get_session),
    rolle: str = Depends(sjekk_passord),
):
    return templates.TemplateResponse(
        "innhosting.html", _innhosting_kontekst(request, session, rolle=rolle)
    )


def _beregn_utlopsdato(arrangementer: list[dict]) -> str | None:
    """Seneste dato blant arrangementene som ble funnet (over HELE dokumentet, ikke bare de
    som falt innenfor et gjeldende datovindu) — brukes som utløpsdato på en ManuellKilde: når
    denne datoen er passert finnes det ingenting mer å hente fra kilden, og den deaktiveres."""
    datoer = [str(a.get("dato") or "") for a in arrangementer]
    datoer = [d for d in datoer if d]
    return max(datoer) if datoer else None


def _filtrer_til_periode(arrangementer: list[dict], forste_dag: date, siste_dag: date) -> list[dict]:
    """Filtrerer en ubegrenset arrangementliste (se harvest.instruks_for_manuell_kilde) ned til
    de som faller i den gjeldende innhøstingsperioden, slik en vanlig periodebegrenset kilde
    ville gjort — resten forblir bevart i den lagrede ManuellKilde-en for en senere kjøring."""
    fra, til = forste_dag.isoformat(), siste_dag.isoformat()
    return [a for a in arrangementer if fra <= str(a.get("dato") or "") <= til]


def _kjor_manuelle_kilder(
    session: Session,
    forste_dag: date,
    siste_dag: date,
    sett_ider: set[str],
    ekskluderte_signaturer: set[str],
    hittil_pr_dato: dict[str, list[Arrangement]],
    flerdags_kandidater: list[Arrangement],
    berorte_grupper: set[str],
    ikke_duplikat_par: set[frozenset[str]],
) -> list[str]:
    """Kjører alle aktive, ikke-utløpte manuelle kilder (se modellen ManuellKilde) på nytt for
    hver innhøsting, filtrert mot gjeldende periode — akkurat som URL-kilder allerede fungerer,
    bare at "master" her er det lagrede innholdet (skjermdump/PDF/limt inn tekst) i stedet for
    en nettside. Utpakkingen selv er ubegrenset i tid (instruks_for_manuell_kilde), slik at
    arrangementer som lå utenfor et tidligere datovindu fanges opp automatisk når vinduet
    senere dekker datoen deres, i stedet for å gå tapt for godt. Oppdaterer utløpsdatoen ved
    hver kjøring og deaktiverer kilden automatisk når siste kjente arrangement-dato er passert.
    Returnerer feilmeldinger for kilder som feilet."""
    i_dag = date.today().isoformat()
    feil: list[str] = []
    manuelle = session.exec(select(ManuellKilde).where(ManuellKilde.aktiv == True)).all()  # noqa: E712
    for mk in manuelle:
        if mk.utlopsdato and mk.utlopsdato < i_dag:
            mk.aktiv = False
            session.add(mk)
            continue

        try:
            instruks_uten_periode = instruks_for_manuell_kilde()
            if mk.kilde_type == "skjermdump":
                rå = hent_fra_bilde(
                    mk.innhold_bytes, mk.innhold_media_type, forste_dag, siste_dag,
                    instruks=instruks_uten_periode,
                )
            elif mk.kilde_type == "pdf":
                rå = hent_fra_pdf(mk.innhold_bytes, forste_dag, siste_dag, instruks=instruks_uten_periode)
            else:
                rå = hent_fra_tekst(mk.innhold_tekst, forste_dag, siste_dag, instruks=instruks_uten_periode)
        except Exception as e:
            feil.append(f"{mk.navn} ({e})")
            continue

        mk.utlopsdato = _beregn_utlopsdato(rå) or mk.utlopsdato
        mk.sist_kjort_at = datetime.utcnow()
        if mk.utlopsdato and mk.utlopsdato < i_dag:
            mk.aktiv = False
        session.add(mk)
        session.commit()

        for a in _filtrer_til_periode(rå, forste_dag, siste_dag):
            _lagre_arrangement(
                session, a, sett_ider, ekskluderte_signaturer, hittil_pr_dato, flerdags_kandidater,
                berorte_grupper, ikke_duplikat_par,
            )
        session.commit()
    return feil


def _utfor_innhosting(session: Session, instruks: str) -> list[str]:
    """Selve innhøstingen: henter fra alle aktive kilder parallelt, lagrer nye funn og
    oppdaterer duplikat-sammenslåinger. Delt mellom "Kjør innhøsting"-knappen (kjor_innhosting)
    og den automatiske daglige/ukentlige autojobben (_kjor_automatisk_innhosting), slik at de
    to kjøreveiene aldri kan drifte fra hverandre. Returnerer en liste med feilmeldinger for
    kilder som feilet (tom liste = alt gikk bra)."""
    innstilling = _hent_innstilling(session)
    forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)

    ekskluderte = {e.signatur for e in session.exec(select(EkskludertSignatur)).all()}
    ikke_duplikat_par = _hent_ikke_duplikat_par(session)
    eksisterende = session.exec(select(Arrangement)).all()
    sett_ider = {a.arrangement_id for a in eksisterende}
    hittil_pr_dato = _hittil_pr_dato(eksisterende)
    flerdags_kandidater = [a for a in eksisterende if not a.er_sammenslatt]
    berorte_grupper: set[str] = set()

    # Dedikerte kilder (Nes kommune, Visit Greater Oslo) tas med her som alle andre —
    # hent_fra_kilde ruter dem selv til sin riktige, dedikerte hente-vei internt (se
    # diagnostiser_nes_kalender / _hent_fra_visitgreateroslo). De har i tillegg egne knapper
    # på Innhøsting-siden for å hente kun akkurat den kilden, uten å kjøre alt.
    aktive_kilder = session.exec(select(Kilde).where(Kilde.aktiv == True)).all()  # noqa: E712
    aktive_kilder = sorted(aktive_kilder, key=lambda k: k.samlet_sortering, reverse=True)

    feil_kilder = []
    if aktive_kilder:
        with ThreadPoolExecutor(max_workers=min(MAKS_SAMTIDIGE_KILDER, len(aktive_kilder))) as executor:
            fremtid_til_kilde = {
                executor.submit(hent_fra_kilde, kilde.url, forste_dag, siste_dag, instruks): kilde
                for kilde in aktive_kilder
            }
            for fremtid in as_completed(fremtid_til_kilde):
                kilde = fremtid_til_kilde[fremtid]
                try:
                    rå, foreslatt_url = fremtid.result()
                    henting_ok = True
                except Exception as e:
                    rå, foreslatt_url = [], None
                    henting_ok = False
                    feil_kilder.append(f"{kilde.navn} ({e})")

                # Lagre og commit denne kildens resultater med en gang den er ferdig, i
                # stedet for å vente på at alle kildene skal bli ferdige. Da beholdes alt
                # som allerede er hentet selv om en senere kilde eller hele kjøringen skulle
                # stoppe opp underveis. Egen try/except rundt selve lagringen: hvis noe
                # feiler her må sesjonen rulles tilbake før neste kilde behandles — ellers
                # blir den ubrukelig for resten av kjøringen, og alle senere kilder ville
                # feile med en forvirrende "session is in 'prepared' state"-feil som egentlig
                # skyldes denne ene kilden.
                try:
                    antall = sum(
                        1
                        for a in rå
                        if _lagre_arrangement(
                            session, a, sett_ider, ekskluderte, hittil_pr_dato, flerdags_kandidater,
                            berorte_grupper, ikke_duplikat_par,
                        )
                    )
                    kilde.automatisk_prioritet = float(antall)
                    if foreslatt_url:
                        kilde.url = foreslatt_url
                    if henting_ok:
                        kilde.antall_vellykkede_hentinger += 1
                    else:
                        kilde.antall_feilede_hentinger += 1
                    session.add(kilde)
                    session.commit()
                except Exception as e:
                    session.rollback()
                    if henting_ok:
                        feil_kilder.append(f"{kilde.navn} ({e})")
                    frisk_kilde = session.get(Kilde, kilde.id)
                    if frisk_kilde:
                        frisk_kilde.antall_feilede_hentinger += 1
                        session.add(frisk_kilde)
                        session.commit()

    feil_kilder.extend(
        _kjor_manuelle_kilder(
            session, forste_dag, siste_dag, sett_ider, ekskluderte, hittil_pr_dato,
            flerdags_kandidater, berorte_grupper, ikke_duplikat_par,
        )
    )
    _oppdater_sammenslatte_grupper(session, berorte_grupper, ekskluderte)
    return feil_kilder


@app.post("/innhosting/kjor")
def kjor_innhosting(
    request: Request,
    instruks: str = Form(...),
    session: Session = Depends(get_session),
    rolle: str = Depends(sjekk_passord),
):
    feil_kilder = _utfor_innhosting(session, instruks)

    if feil_kilder:
        feilmelding = "Disse kildene feilet under innhøsting: " + "; ".join(feil_kilder)
        return templates.TemplateResponse(
            "innhosting.html", _innhosting_kontekst(request, session, feilmelding, rolle)
        )
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/innhosting/slaa-sammen-duplikater")
def slaa_sammen_duplikater(
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    ekskluderte = {e.signatur for e in session.exec(select(EkskludertSignatur)).all()}
    berorte_grupper = _duplikatgrupper_uten_sammenslaing(session)
    _oppdater_sammenslatte_grupper(session, berorte_grupper, ekskluderte)
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/innhosting/{arrangement_id}/ikke-duplikat")
def marker_ikke_duplikat(
    arrangement_id: int,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    """Retter opp en feilaktig sammenslåing: sletter den sammenslåtte AI-oppføringen, viser de
    rå kildefunnene som egne kort igjen (alle forhåndsvalgt), og husker paret av signaturer som
    "ikke duplikater" (se IkkeDuplikatPar) slik at de aldri slås sammen igjen i en senere
    innhøsting, selv om er_tittel_duplikat fortsatt ville fanget dem opp som en fuzzy-match."""
    sammenslatt = session.get(Arrangement, arrangement_id)
    if not sammenslatt or not sammenslatt.er_sammenslatt or not sammenslatt.duplikat_gruppe:
        return RedirectResponse(url="/innhosting", status_code=303)

    medlemmer = session.exec(
        select(Arrangement).where(
            Arrangement.duplikat_gruppe == sammenslatt.duplikat_gruppe,
            Arrangement.er_sammenslatt == False,  # noqa: E712
        )
    ).all()

    signaturer = [m.signatur for m in medlemmer]
    for i in range(len(signaturer)):
        for j in range(i + 1, len(signaturer)):
            if frozenset({signaturer[i], signaturer[j]}) not in _hent_ikke_duplikat_par(session):
                session.add(IkkeDuplikatPar(signatur_a=signaturer[i], signatur_b=signaturer[j]))

    for medlem in medlemmer:
        medlem.duplikat_gruppe = None
        medlem.valgt = True
        session.add(medlem)

    session.delete(sammenslatt)
    session.commit()
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/innhosting/{arrangement_id}/hent-mer")
def hent_mer_for_arrangement(
    arrangement_id: int,
    request: Request,
    session: Session = Depends(get_session),
    rolle: str = Depends(sjekk_passord),
):
    a = session.get(Arrangement, arrangement_id)
    if not a:
        return RedirectResponse(url="/innhosting", status_code=303)

    try:
        ny_tekst, bekreftet, ny_url = hent_mer_info(
            a.kilde_url, a.tittel, a.dato, a.klokkeslett, a.sted
        )
    except Exception as e:
        return templates.TemplateResponse(
            "innhosting.html",
            _innhosting_kontekst(
                request, session, f"Kunne ikke hente mer info for «{a.tittel}»: {e}", rolle
            ),
        )

    if ny_tekst:
        a.original_tekst = ny_tekst
        a.tekst_bekreftet = bekreftet
        if ny_url:
            a.kilde_url = ny_url
        session.add(a)
        session.commit()
        return templates.TemplateResponse(
            "innhosting.html", _innhosting_kontekst(request, session, rolle=rolle)
        )

    return templates.TemplateResponse(
        "innhosting.html",
        _innhosting_kontekst(
            request,
            session,
            f"Fant ikke mer informasjon om «{a.tittel}» enn det som allerede er hentet.",
            rolle,
        ),
    )


@app.post("/innhosting/tom")
def tom_utkast(
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    for a in session.exec(select(Arrangement)).all():
        session.delete(a)
    session.commit()
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/innhosting/slett-gamle")
def slett_gamle_arrangementer(
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    """Sletter arrangementer som allerede er avsluttet (sluttdato før i dag). Løpende
    arrangementer (startet før i dag, men ikke avsluttet ennå) beholdes, men får
    startdatoen flyttet fram til i morgen — samme konvensjon som resten av appen bruker
    for "kommende" periode."""
    i_dag = date.today()
    i_morgen = i_dag + timedelta(days=1)
    for a in session.exec(select(Arrangement)).all():
        try:
            start = date.fromisoformat(a.dato)
        except ValueError:
            continue
        try:
            slutt = date.fromisoformat(a.til_dato) if a.til_dato else start
        except ValueError:
            slutt = start

        if slutt < i_dag:
            session.delete(a)
        elif start < i_dag:
            if i_morgen > slutt:
                session.delete(a)
            else:
                a.dato = i_morgen.isoformat()
                session.add(a)
    session.commit()
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/innhosting/last-opp")
async def last_opp_fil(
    request: Request,
    fil: UploadFile = File(...),
    session: Session = Depends(get_session),
    rolle: str = Depends(sjekk_passord),
):
    innstilling = _hent_innstilling(session)
    forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)
    innhold = await fil.read()

    STOTTEDE_BILDETYPER = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    er_pdf = fil.content_type == "application/pdf"
    er_bilde = fil.content_type in STOTTEDE_BILDETYPER
    if not er_pdf and not er_bilde:
        return templates.TemplateResponse(
            "innhosting.html",
            _innhosting_kontekst(
                request,
                session,
                f"Filtypen '{fil.content_type}' støttes ikke. Bruk JPEG, PNG, WEBP, "
                "GIF eller PDF.",
                rolle,
            ),
        )

    instruks_uten_periode = instruks_for_manuell_kilde()
    try:
        if er_pdf:
            rå = hent_fra_pdf(innhold, forste_dag, siste_dag, instruks=instruks_uten_periode)
        else:
            rå = hent_fra_bilde(innhold, fil.content_type, forste_dag, siste_dag, instruks=instruks_uten_periode)
    except Exception as e:
        return templates.TemplateResponse(
            "innhosting.html",
            _innhosting_kontekst(
                request, session, f"Kunne ikke tolke filen '{fil.filename}': {e}", rolle
            ),
        )

    # Lagres som en vedvarende manuell kilde (se ManuellKilde) i stedet for å bare brukes til
    # denne ene innhøstingen — slik at den kan gjenbrukes senere i stedet for å måtte lastes
    # opp på nytt, og slik at arrangementer utenfor gjeldende datovindu ikke går tapt (se
    # _kjor_manuelle_kilder).
    session.add(
        ManuellKilde(
            kilde_type="pdf" if er_pdf else "skjermdump",
            navn=fil.filename or "Opplastet fil",
            innhold_bytes=innhold,
            innhold_media_type=None if er_pdf else fil.content_type,
            utlopsdato=_beregn_utlopsdato(rå),
            sist_kjort_at=datetime.utcnow(),
        )
    )

    eksisterende = session.exec(select(Arrangement)).all()
    sett_ider = {a.arrangement_id for a in eksisterende}
    hittil_pr_dato = _hittil_pr_dato(eksisterende)
    flerdags_kandidater = [a for a in eksisterende if not a.er_sammenslatt]
    ekskluderte = {e.signatur for e in session.exec(select(EkskludertSignatur)).all()}
    ikke_duplikat_par = _hent_ikke_duplikat_par(session)
    berorte_grupper: set[str] = set()
    for a in _filtrer_til_periode(rå, forste_dag, siste_dag):
        _lagre_arrangement(
            session, a, sett_ider, ekskluderte, hittil_pr_dato, flerdags_kandidater, berorte_grupper, ikke_duplikat_par
        )
    session.commit()
    _oppdater_sammenslatte_grupper(session, berorte_grupper, ekskluderte)
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/innhosting/lim-inn-tekst")
async def lim_inn_tekst(
    request: Request,
    tekst: str = Form(...),
    session: Session = Depends(get_session),
    rolle: str = Depends(sjekk_passord),
):
    innstilling = _hent_innstilling(session)
    forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)

    if not tekst.strip():
        return templates.TemplateResponse(
            "innhosting.html",
            _innhosting_kontekst(request, session, "Ingen tekst ble limt inn.", rolle),
        )

    try:
        rå = hent_fra_tekst(tekst, forste_dag, siste_dag, instruks=instruks_for_manuell_kilde())
    except Exception as e:
        return templates.TemplateResponse(
            "innhosting.html",
            _innhosting_kontekst(request, session, f"Kunne ikke tolke den limte inn teksten: {e}", rolle),
        )

    session.add(
        ManuellKilde(
            kilde_type="limt_inn_tekst",
            navn=(tekst.strip()[:60] + "…") if len(tekst.strip()) > 60 else tekst.strip(),
            innhold_tekst=tekst,
            utlopsdato=_beregn_utlopsdato(rå),
            sist_kjort_at=datetime.utcnow(),
        )
    )

    eksisterende = session.exec(select(Arrangement)).all()
    sett_ider = {a.arrangement_id for a in eksisterende}
    hittil_pr_dato = _hittil_pr_dato(eksisterende)
    flerdags_kandidater = [a for a in eksisterende if not a.er_sammenslatt]
    ekskluderte = {e.signatur for e in session.exec(select(EkskludertSignatur)).all()}
    ikke_duplikat_par = _hent_ikke_duplikat_par(session)
    berorte_grupper: set[str] = set()
    for a in _filtrer_til_periode(rå, forste_dag, siste_dag):
        _lagre_arrangement(
            session, a, sett_ider, ekskluderte, hittil_pr_dato, flerdags_kandidater, berorte_grupper, ikke_duplikat_par
        )
    session.commit()
    _oppdater_sammenslatte_grupper(session, berorte_grupper, ekskluderte)
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/manuelle-kilder/{manuell_kilde_id}/aktiver")
def aktiver_manuell_kilde(
    manuell_kilde_id: int,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    mk = session.get(ManuellKilde, manuell_kilde_id)
    if mk:
        mk.aktiv = True
        session.add(mk)
        session.commit()
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/manuelle-kilder/{manuell_kilde_id}/deaktiver")
def deaktiver_manuell_kilde(
    manuell_kilde_id: int,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    mk = session.get(ManuellKilde, manuell_kilde_id)
    if mk:
        mk.aktiv = False
        session.add(mk)
        session.commit()
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/manuelle-kilder/{manuell_kilde_id}/slett")
def slett_manuell_kilde(
    manuell_kilde_id: int,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    mk = session.get(ManuellKilde, manuell_kilde_id)
    if mk:
        session.delete(mk)
        session.commit()
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/innhosting/lagre-utvalg")
def lagre_utvalg(
    valgt_ider: list[int] = Form(default=[]),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    valgt_sett = set(valgt_ider)
    for a in session.exec(select(Arrangement)).all():
        var_valgt_for = a.valgt
        a.valgt = a.id in valgt_sett
        if var_valgt_for and not a.valgt:
            eksisterende = session.exec(
                select(EkskludertSignatur).where(EkskludertSignatur.signatur == a.signatur)
            ).first()
            if eksisterende:
                eksisterende.sist_fjernet_at = datetime.utcnow()
                session.add(eksisterende)
            else:
                session.add(
                    EkskludertSignatur(signatur=a.signatur, tittel_eksempel=a.tittel)
                )
        session.add(a)
    session.commit()
    return RedirectResponse(url="/innhosting", status_code=303)


def _hent_gjeldende_artikkel(session: Session) -> Artikkel | None:
    """Den nyeste artikkelen — eldre artikler slettes ikke lenger, bare arkiveres implisitt
    ved at en nyere finnes."""
    return session.exec(select(Artikkel).order_by(Artikkel.id.desc())).first()


def _avsnitt_for_artikkel(session: Session, artikkel: Artikkel) -> list[dict]:
    rader = session.exec(
        select(ArtikkelAvsnitt)
        .where(ArtikkelAvsnitt.artikkel_id == artikkel.id)
        .order_by(ArtikkelAvsnitt.rekkefolge)
    ).all()
    avsnitt_liste = []
    forrige_kategori = None
    for rad in rader:
        avsnitt_liste.append(
            {
                "id": rad.id,
                "tekst": rad.tekst,
                "kategori": rad.kategori,
                "ny_kategori": rad.kategori != forrige_kategori,
                "kilde_url": rad.kilde_url,
                "kilde_tittel": rad.kilde_tittel,
            }
        )
        forrige_kategori = rad.kategori
    return avsnitt_liste


def _artikkel_instruks_verdi(innstilling: Innstilling) -> str:
    """Admin kan lagre en egen standard-instruks for artikkelgenerering (se
    /artikler/instruks) — den overstyrer da den innebygde standarden fra
    standard_artikkel_instruks() som forhåndsutfylling på Artikler-siden."""
    return innstilling.artikkel_instruks or standard_artikkel_instruks()


def _artikler_kontekst(
    request: Request, session: Session, feilmelding: str | None = None, rolle: str = "journalist"
) -> dict:
    innstilling = _hent_innstilling(session)
    forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)

    alle_artikler = session.exec(select(Artikkel).order_by(Artikkel.id.desc())).all()
    artikkel = alle_artikler[0] if alle_artikler else None
    avsnitt_liste = _avsnitt_for_artikkel(session, artikkel) if artikkel else []

    tidligere_artikler = [
        {
            "id": eldre.id,
            "tittel": eldre.tittel,
            "ingress": eldre.ingress,
            "opprettet_at": eldre.opprettet_at,
            "avsnitt": _avsnitt_for_artikkel(session, eldre),
        }
        for eldre in alle_artikler[1:]
    ]

    return {
        "request": request,
        "artikkel": artikkel,
        "avsnitt": avsnitt_liste,
        "tidligere_artikler": tidligere_artikler,
        "forste_dag": forste_dag,
        "siste_dag": siste_dag,
        "artikkel_instruks_verdi": _artikkel_instruks_verdi(innstilling),
        "feilmelding": feilmelding,
        "rolle": rolle,
        "kalender_fotnote": KALENDER_FOTNOTE,
    }


@app.get("/artikler")
def artikler_side(
    request: Request,
    session: Session = Depends(get_session),
    rolle: str = Depends(sjekk_passord),
):
    return templates.TemplateResponse("artikler.html", _artikler_kontekst(request, session, rolle=rolle))


@app.post("/artikler/instruks")
def lagre_artikkel_instruks(
    instruks: str = Form(...),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_admin),
):
    innstilling = _hent_innstilling(session)
    innstilling.artikkel_instruks = instruks.strip() or None
    session.add(innstilling)
    session.commit()
    return RedirectResponse(url="/artikler", status_code=303)


@app.post("/artikler/generer")
def generer_artikkel_rute(
    request: Request,
    instruks: str = Form(...),
    session: Session = Depends(get_session),
    rolle: str = Depends(sjekk_passord),
):
    innstilling = _hent_innstilling(session)
    forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)
    valgte = session.exec(
        select(Arrangement).where(
            Arrangement.valgt == True,  # noqa: E712
            Arrangement.dato >= forste_dag.isoformat(),
            Arrangement.dato <= siste_dag.isoformat(),
        )
    ).all()

    if not valgte:
        return templates.TemplateResponse(
            "artikler.html",
            _artikler_kontekst(
                request,
                session,
                "Ingen valgte arrangementer i utkastet for gjeldende periode. Gå til "
                "Innhøsting og velg noen først.",
                rolle,
            ),
        )

    try:
        resultat, diagnose = generer_hel_artikkel(valgte, instruks=instruks)
    except Exception as e:
        return templates.TemplateResponse(
            "artikler.html",
            _artikler_kontekst(request, session, f"Kunne ikke generere artikkelen: {e}", rolle),
        )

    if not resultat:
        return templates.TemplateResponse(
            "artikler.html",
            _artikler_kontekst(request, session, f"Fikk ikke generert noen artikkel. {diagnose}", rolle),
        )

    ny_artikkel = Artikkel(tittel=resultat["tittel"], ingress=resultat["ingress"])
    session.add(ny_artikkel)
    session.commit()
    session.refresh(ny_artikkel)

    valgte_pr_id = {a.id: a for a in valgte}
    for rekkefolge, avsnitt in enumerate(resultat["avsnitt"]):
        kilde_arrangement = valgte_pr_id.get(avsnitt["arrangement_id"])
        session.add(
            ArtikkelAvsnitt(
                artikkel_id=ny_artikkel.id,
                arrangement_id=avsnitt["arrangement_id"],
                tekst=avsnitt["tekst"],
                kategori=avsnitt["kategori"],
                rekkefolge=rekkefolge,
                kilde_url=kilde_arrangement.kilde_url if kilde_arrangement else None,
                kilde_tittel=kilde_arrangement.tittel if kilde_arrangement else "",
            )
        )
    session.commit()
    return RedirectResponse(url="/artikler", status_code=303)


@app.post("/artikler/lagre")
async def lagre_artikkel(
    request: Request,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    skjema = await request.form()
    artikkel = _hent_gjeldende_artikkel(session)
    if not artikkel:
        return RedirectResponse(url="/artikler", status_code=303)

    tittel = str(skjema.get("tittel") or "").strip()
    ingress = str(skjema.get("ingress") or "").strip()
    if tittel:
        artikkel.tittel = tittel
    if ingress:
        artikkel.ingress = ingress
    session.add(artikkel)

    for rad in session.exec(
        select(ArtikkelAvsnitt).where(ArtikkelAvsnitt.artikkel_id == artikkel.id)
    ).all():
        ny_tekst = skjema.get(f"avsnitt_{rad.id}")
        if ny_tekst is not None:
            rad.tekst = str(ny_tekst).strip()
            session.add(rad)
    session.commit()
    return RedirectResponse(url="/artikler", status_code=303)


def _flytt_avsnitt(session: Session, avsnitt_id: int, retning: int) -> None:
    """retning: -1 for å flytte opp, +1 for å flytte ned."""
    rad = session.get(ArtikkelAvsnitt, avsnitt_id)
    if not rad:
        return
    naboer = session.exec(
        select(ArtikkelAvsnitt)
        .where(ArtikkelAvsnitt.artikkel_id == rad.artikkel_id)
        .order_by(ArtikkelAvsnitt.rekkefolge)
    ).all()
    indeks = next((i for i, n in enumerate(naboer) if n.id == rad.id), None)
    if indeks is None:
        return
    bytte_indeks = indeks + retning
    if not (0 <= bytte_indeks < len(naboer)):
        return
    nabo = naboer[bytte_indeks]
    rad.rekkefolge, nabo.rekkefolge = nabo.rekkefolge, rad.rekkefolge
    session.add(rad)
    session.add(nabo)
    session.commit()


@app.post("/artikler/avsnitt/{avsnitt_id}/opp")
def flytt_avsnitt_opp(
    avsnitt_id: int,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    _flytt_avsnitt(session, avsnitt_id, -1)
    return RedirectResponse(url="/artikler", status_code=303)


@app.post("/artikler/avsnitt/{avsnitt_id}/ned")
def flytt_avsnitt_ned(
    avsnitt_id: int,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    _flytt_avsnitt(session, avsnitt_id, 1)
    return RedirectResponse(url="/artikler", status_code=303)


def _flytt_avsnitt_til_posisjon(session: Session, avsnitt_id: int, ny_posisjon: int) -> None:
    """Flytter et avsnitt direkte til en gitt 1-indeksert posisjon i artikkelen — de andre
    avsnittene skyves tilsvarende, i stedet for å måtte klikke opp/ned-pilene gjentatte
    ganger for lange forflytninger."""
    rad = session.get(ArtikkelAvsnitt, avsnitt_id)
    if not rad:
        return
    naboer = session.exec(
        select(ArtikkelAvsnitt)
        .where(ArtikkelAvsnitt.artikkel_id == rad.artikkel_id)
        .order_by(ArtikkelAvsnitt.rekkefolge)
    ).all()
    uten_rad = [n for n in naboer if n.id != rad.id]
    ny_indeks = max(0, min(ny_posisjon - 1, len(uten_rad)))
    uten_rad.insert(ny_indeks, rad)
    for rekkefolge, avsnitt in enumerate(uten_rad):
        avsnitt.rekkefolge = rekkefolge
        session.add(avsnitt)
    session.commit()


@app.post("/artikler/avsnitt/{avsnitt_id}/flytt-til")
async def flytt_avsnitt_til(
    avsnitt_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    # Skjemafeltet er navngitt unikt per avsnitt (posisjon_{id}) siden opp/ned/slett/flytt-
    # knappene for ALLE avsnitt deler samme omsluttende <form> (se artikler.html) — uten det
    # ville ett felles feltnavn kollidert på tvers av avsnittene.
    skjema = await request.form()
    try:
        ny_posisjon = int(str(skjema.get(f"posisjon_{avsnitt_id}") or ""))
    except ValueError:
        return RedirectResponse(url="/artikler", status_code=303)
    _flytt_avsnitt_til_posisjon(session, avsnitt_id, ny_posisjon)
    return RedirectResponse(url="/artikler", status_code=303)


@app.post("/artikler/avsnitt/{avsnitt_id}/slett")
def slett_avsnitt(
    avsnitt_id: int,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    rad = session.get(ArtikkelAvsnitt, avsnitt_id)
    if rad:
        session.delete(rad)
        session.commit()
    return RedirectResponse(url="/artikler", status_code=303)


@app.post("/artikler/avsnitt/{avsnitt_id}/hent-mer")
def hent_mer_for_avsnitt(
    avsnitt_id: int,
    request: Request,
    session: Session = Depends(get_session),
    rolle: str = Depends(sjekk_passord),
):
    rad = session.get(ArtikkelAvsnitt, avsnitt_id)
    if not rad:
        return RedirectResponse(url="/artikler", status_code=303)
    arrangement = session.get(Arrangement, rad.arrangement_id)
    if not arrangement:
        return RedirectResponse(url="/artikler", status_code=303)

    try:
        ny_tekst, bekreftet, ny_url = hent_mer_info(
            arrangement.kilde_url,
            arrangement.tittel,
            arrangement.dato,
            arrangement.klokkeslett,
            arrangement.sted,
        )
    except Exception as e:
        return templates.TemplateResponse(
            "artikler.html",
            _artikler_kontekst(
                request, session, f"Kunne ikke hente mer info for «{arrangement.tittel}»: {e}", rolle
            ),
        )

    if not ny_tekst:
        return templates.TemplateResponse(
            "artikler.html",
            _artikler_kontekst(
                request,
                session,
                f"Fant ikke mer informasjon om «{arrangement.tittel}» enn det som allerede "
                "er hentet.",
                rolle,
            ),
        )

    arrangement.original_tekst = ny_tekst
    arrangement.tekst_bekreftet = bekreftet
    if ny_url:
        arrangement.kilde_url = ny_url
        rad.kilde_url = ny_url
        session.add(rad)
    session.add(arrangement)
    session.commit()

    try:
        omskrevet = skriv_om_ett_avsnitt(arrangement)
    except Exception as e:
        return templates.TemplateResponse(
            "artikler.html",
            _artikler_kontekst(
                request, session, f"Hentet mer info, men klarte ikke omskrive avsnittet: {e}", rolle
            ),
        )

    if omskrevet:
        rad.tekst = omskrevet
        session.add(rad)
        session.commit()

    return templates.TemplateResponse("artikler.html", _artikler_kontekst(request, session, rolle=rolle))

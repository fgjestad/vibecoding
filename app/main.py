import json
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import Session, select

from app import auth
from app.db import get_session, hent_innstilling, init_db
from app.helligdager import ferieadvarsel, roed_dag
from app.kalender import (
    OSLO,
    UKEDAGER_KORT,
    formater_dato,
    google_lenke,
    ics_dokument,
    les_klokkeslett,
    les_maaned_noekkel,
    maaned_celler,
    maaned_navn,
    tredje_tirsdag,
)
from app.models import EkstraSamling, FagligTirsdag, Innstilling

load_dotenv()

# Hvor langt kalenderfila og «neste Faglig tirsdag» ser bakover og framover. Google
# henter hele fila på nytt hver gang, så vinduet holdes lite nok til at fila forblir
# rask, og stort nok til at man kan planlegge to år fram.
MAANEDER_BAKOVER = 12
MAANEDER_FRAMOVER = 24


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Faglig tirsdag", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def i_dag() -> date:
    """Dagens dato i norsk tid — serveren på Render står i UTC, og rundt midnatt
    ville det ellers gjort at «neste Faglig tirsdag» skiftet en dag for tidlig."""
    return datetime.now(OSLO).date()


def base_url(request: Request) -> str:
    """Appens egen adresse utenfra, brukt i kalenderabonnementet og i lenkene som
    ligger i kalenderoppføringene. Render står bak en proxy som terminerer HTTPS,
    så vi stoler på x-forwarded-proto framfor skjemaet uvicorn ser."""
    protokoll = request.headers.get("x-forwarded-proto", request.url.scheme)
    vert = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{protokoll}://{vert}".rstrip("/")


def _flytt_maaned(utgangspunkt: date, antall: int) -> str:
    aar, maaned = divmod((utgangspunkt.year * 12 + utgangspunkt.month - 1) + antall, 12)
    return f"{aar:04d}-{maaned + 1:02d}"


def arkiv_start(session: Session) -> str:
    """Første måned arkivet dekker: den eldste raden i databasen, eller
    MAANEDER_BAKOVER tilbake hvis den er eldre. Da forsvinner ingenting du har lagt
    inn ut av arkivet, uansett hvor lenge siden det er."""
    gulv = _flytt_maaned(i_dag(), -MAANEDER_BAKOVER)
    eldste = session.exec(select(func.min(FagligTirsdag.maaned))).first()
    return min(eldste, gulv) if eldste else gulv


def maaneder_fra_til(fra: str, til: str) -> list[str]:
    """Alle månedsnøkler fra og med «fra» til og med «til»."""
    fra_aar, fra_maaned = les_maaned_noekkel(fra)
    til_aar, til_maaned = les_maaned_noekkel(til)
    start = fra_aar * 12 + fra_maaned - 1
    return [
        f"{aar:04d}-{maaned + 1:02d}"
        for aar, maaned in (
            divmod(n, 12) for n in range(start, til_aar * 12 + til_maaned)
        )
    ]


def valider_noekkel(noekkel: str) -> tuple[int, int]:
    """Månedsnøkler kommer rett fra adressefeltet, så en tullete verdi skal gi en
    ryddig 404 og ikke en feilside."""
    try:
        aar, maaned = les_maaned_noekkel(noekkel)
    except ValueError:
        raise HTTPException(status_code=404, detail="Ukjent måned")
    if not (2000 <= aar <= 2100 and 1 <= maaned <= 12):
        raise HTTPException(status_code=404, detail="Ukjent måned")
    return aar, maaned


def hent_rader(session: Session, noekler: list[str]) -> dict[str, FagligTirsdag]:
    rader = session.exec(select(FagligTirsdag).where(FagligTirsdag.maaned.in_(noekler))).all()
    return {rad.maaned: rad for rad in rader}


def _tider(rad: FagligTirsdag | EkstraSamling | None, innstilling: Innstilling) -> tuple[time, time]:
    standard_start = les_klokkeslett(innstilling.standard_start, time(8, 30))
    standard_slutt = les_klokkeslett(innstilling.standard_slutt, time(9, 30))
    start = les_klokkeslett(rad.start, standard_start) if rad and rad.start else standard_start
    slutt = les_klokkeslett(rad.slutt, standard_slutt) if rad and rad.slutt else standard_slutt
    if slutt <= start:
        # En slutt før start ville gitt en ugyldig kalenderoppføring. Vi retter det
        # opp ved visning i stedet for å avvise lagringen, så ingen mister teksten
        # de nettopp skrev inn på grunn av en skrivefeil i klokkeslettet.
        senere = datetime.combine(date.today(), start) + timedelta(hours=1)
        slutt = senere.time() if senere.date() == date.today() else time(23, 59)
    return start, slutt


def _felles_visning(
    dato: date,
    start: time,
    slutt: time,
    sted: str,
    tema: str,
    foredragsholdere: str,
    notat: str,
    request: Request,
    samlingsdatoer: set[date],
) -> dict:
    """Alt som ser likt ut på et kort, enten samlingen er en fast tredje tirsdag
    eller en ekstra samling lagt inn ved siden av."""
    tittel = f"Faglig tirsdag: {tema}" if tema else "Faglig tirsdag"
    beskrivelse_linjer = []
    if foredragsholdere:
        beskrivelse_linjer.append(f"Foredragsholdere: {foredragsholdere}")
    if notat:
        beskrivelse_linjer.append(notat)
    beskrivelse_linjer.append(f"Oversikt: {base_url(request)}/")
    beskrivelse = "\n".join(beskrivelse_linjer)

    # Rutenettet tegnes for måneden datoen faktisk ligger i. Er samlingen flyttet over
    # et månedsskifte, ville rutenettet for nøkkelmåneden vært uten uthevet dag i det
    # hele tatt — derfor står måneden også skrevet over rutenettet.
    i_dag_dato = i_dag()
    rutenett = [
        None
        if celle is None
        else {
            "dag": celle.day,
            "er_samling": celle == dato,
            # Er det flere samlinger i måneden, skal de også synes — ellers ville
            # rutenettet gitt inntrykk av at det bare skjer én ting den måneden.
            "er_annen_samling": celle != dato and celle in samlingsdatoer,
            "roed": roed_dag(celle),
            "er_i_dag": celle == i_dag_dato,
            "er_helg": celle.weekday() >= 5,
        }
        for celle in maaned_celler(dato.year, dato.month)
    ]

    return {
        "dato": dato,
        "dato_iso": dato.isoformat(),
        "dato_tekst": formater_dato(dato),
        "maaned_navn": maaned_navn(dato.month),
        "aar": dato.year,
        "start": start.strftime("%H:%M"),
        "slutt": slutt.strftime("%H:%M"),
        "sted": sted,
        "tema": tema,
        "foredragsholdere": foredragsholdere,
        "notat": notat,
        "roed_dag": roed_dag(dato),
        "ferie": ferieadvarsel(dato),
        "er_i_dag": dato == i_dag_dato,
        "rutenett": rutenett,
        "rutenett_tittel": f"{maaned_navn(dato.month)} {dato.year}",
        "har_innhold": bool(tema or foredragsholdere or notat),
        "tittel": tittel,
        "beskrivelse": beskrivelse,
        "google_url": google_lenke(dato, start, slutt, tittel, beskrivelse, sted),
    }


def dato_for_maaned(noekkel: str, rad: FagligTirsdag | None) -> date:
    """Datoen en fast samling faktisk har: den overstyrte datoen hvis den finnes,
    ellers tredje tirsdag."""
    if rad and rad.dato:
        return date.fromisoformat(rad.dato)
    return tredje_tirsdag(*les_maaned_noekkel(noekkel))


def bygg_dag(
    noekkel: str,
    rad: FagligTirsdag | None,
    innstilling: Innstilling,
    request: Request,
    samlingsdatoer: set[date],
) -> dict:
    """Setter sammen en fast Faglig tirsdag, uansett om den finnes i databasen eller
    bare følger regelen om tredje tirsdag."""
    standard_dato = tredje_tirsdag(*les_maaned_noekkel(noekkel))
    dato = dato_for_maaned(noekkel, rad)
    start, slutt = _tider(rad, innstilling)
    visning = _felles_visning(
        dato,
        start,
        slutt,
        (rad.sted if rad and rad.sted else innstilling.standard_sted).strip(),
        (rad.tema if rad else "").strip(),
        (rad.foredragsholdere if rad else "").strip(),
        (rad.notat if rad else "").strip(),
        request,
        samlingsdatoer,
    )
    visning.update(
        {
            "noekkel": noekkel,
            "url_base": f"/dag/{noekkel}",
            "ics_url": f"/dag/{noekkel}.ics",
            "ekstra": False,
            "maaned_navn": maaned_navn(les_maaned_noekkel(noekkel)[1]),
            "standard_dato_iso": standard_dato.isoformat(),
            "standard_dato_tekst": formater_dato(standard_dato),
            "er_flyttet": dato != standard_dato,
            "start_felt": rad.start if rad else "",
            "slutt_felt": rad.slutt if rad else "",
            "sted_felt": rad.sted if rad else "",
            "skjult": bool(rad and rad.skjult),
            "uid": f"faglig-tirsdag-{noekkel}@raumnes",
            "sekvens": int(rad.oppdatert_at.timestamp()) if rad else 0,
        }
    )
    return visning


def bygg_ekstra(
    rad: EkstraSamling,
    innstilling: Innstilling,
    request: Request,
    samlingsdatoer: set[date],
) -> dict:
    """Setter sammen en ekstra samling. Samme kort som en fast dag, men uten regelen
    om tredje tirsdag å vise til eller falle tilbake på."""
    dato = date.fromisoformat(rad.dato)
    start, slutt = _tider(rad, innstilling)
    visning = _felles_visning(
        dato,
        start,
        slutt,
        (rad.sted or innstilling.standard_sted).strip(),
        rad.tema.strip(),
        rad.foredragsholdere.strip(),
        rad.notat.strip(),
        request,
        samlingsdatoer,
    )
    visning.update(
        {
            "noekkel": f"ekstra-{rad.id}",
            "url_base": f"/ekstra/{rad.id}",
            "ics_url": f"/ekstra/{rad.id}.ics",
            "ekstra": True,
            "er_flyttet": False,
            "skjult": False,
            "uid": f"faglig-tirsdag-ekstra-{rad.id}@raumnes",
            "sekvens": int(rad.oppdatert_at.timestamp()),
        }
    )
    return visning


def hent_dager(session: Session, noekler: list[str], request: Request) -> list[dict]:
    """Bygger de faste dagene for de oppgitte månedene, sammen med alle ekstra
    samlinger. Ekstra samlinger er alltid med, uansett hvilket vindu som spørres om —
    de er lagt inn for hånd, og skal aldri kunne falle ut av oversikten fordi de
    havnet utenfor et vindu."""
    innstilling = hent_innstilling(session)
    rader = hent_rader(session, noekler)
    ekstra = session.exec(select(EkstraSamling).order_by(EkstraSamling.dato)).all()

    # Alle datoer det skjer noe på, slik at månedsrutenettet kan vise de andre
    # samlingene i samme måned og ikke bare kortets egen dag.
    samlingsdatoer = {
        dato_for_maaned(noekkel, rader.get(noekkel))
        for noekkel in noekler
        if not (rader.get(noekkel) and rader[noekkel].skjult)
    }
    samlingsdatoer |= {date.fromisoformat(rad.dato) for rad in ekstra}

    dager = [
        bygg_dag(noekkel, rader.get(noekkel), innstilling, request, samlingsdatoer)
        for noekkel in noekler
    ]
    dager += [bygg_ekstra(rad, innstilling, request, samlingsdatoer) for rad in ekstra]
    return dager


def alle_dager(session: Session, request: Request) -> list[dict]:
    """Hver Faglig tirsdag appen kjenner til, fra arkivets begynnelse til enden av
    planleggingsvinduet, sortert på den datoen samlingen faktisk har. Sorteringen
    følger datoen og ikke månedsnøkkelen, slik at en samling som er flyttet over et
    månedsskifte havner riktig i rekkefølgen."""
    noekler = maaneder_fra_til(arkiv_start(session), _flytt_maaned(i_dag(), MAANEDER_FRAMOVER))
    return sorted(hent_dager(session, noekler, request), key=lambda d: d["dato"])


def grupper_etter_aar(dager: list[dict], apne: set[int] | None = None) -> list[dict]:
    """Deler dagene i årbolker, i den rekkefølgen de allerede ligger. Bolker som ikke
    står i «apne» vises sammenlagt — planleggingsvinduet er to år langt, og uten
    dette blir sida en veldig lang rull gjennom måneder ingen har fylt ut ennå."""
    grupper: list[dict] = []
    for d in dager:
        if not grupper or grupper[-1]["aar"] != d["dato"].year:
            grupper.append({"aar": d["dato"].year, "dager": []})
        grupper[-1]["dager"].append(d)
    for gruppe in grupper:
        gruppe["apen"] = apne is None or gruppe["aar"] in apne
    return grupper


def _hent_eller_lag_rad(session: Session, noekkel: str) -> FagligTirsdag:
    valider_noekkel(noekkel)
    rad = session.exec(select(FagligTirsdag).where(FagligTirsdag.maaned == noekkel)).first()
    if rad is None:
        rad = FagligTirsdag(maaned=noekkel)
        session.add(rad)
    return rad


def trygg_retur(retur: str, noekkel: str = "") -> str:
    """Adressen en handling sender deg tilbake til. Verdien kommer fra et skjemafelt,
    så bare de to fanene godtas — ellers kunne feltet pekt hvor som helst."""
    sti = retur if retur in ("/", "/arkiv") else "/"
    return f"{sti}#{noekkel}" if noekkel else sti


def _kontekst(request: Request, session: Session, **ekstra) -> dict:
    grunnlag = {
        "request": request,
        "uthev_neste": False,
        "neste": None,
        "retur": "/",
        "ukedager_kort": UKEDAGER_KORT,
        "admin": auth.er_admin(request),
        "passord_mangler": not auth.passord_er_satt(),
        "innstilling": hent_innstilling(session),
        "base_url": base_url(request),
    }
    grunnlag.update(ekstra)
    return grunnlag


@app.get("/")
def oversikt(request: Request, session: Session = Depends(get_session)):
    """Bare det som kommer. Dagen i dag regnes som kommende — samlingen har ikke
    vært før den er over."""
    kommende = [d for d in alle_dager(session, request) if d["dato"] >= i_dag()]
    neste = next((d for d in kommende if not d["skjult"]), None)
    # I år og neste år står åpne — det er der planleggingen faktisk skjer.
    apne = {i_dag().year, i_dag().year + 1}
    return templates.TemplateResponse(
        "oversikt.html",
        _kontekst(
            request,
            session,
            grupper=grupper_etter_aar(kommende, apne),
            neste=neste,
            uthev_neste=True,
            retur="/",
        ),
    )


@app.get("/arkiv")
def arkiv(request: Request, session: Session = Depends(get_session)):
    """Det som har vært, nyeste først."""
    tidligere = [d for d in alle_dager(session, request) if d["dato"] < i_dag()]
    tidligere.reverse()
    # Bare det året vi nettopp har lagt bak oss står åpent; eldre år er sammenlagt.
    apne = {tidligere[0]["dato"].year} if tidligere else set()
    return templates.TemplateResponse(
        "arkiv.html",
        _kontekst(
            request,
            session,
            grupper=grupper_etter_aar(tidligere, apne),
            neste=None,
            uthev_neste=False,
            retur="/arkiv",
        ),
    )


@app.get("/hjelp")
def hjelp(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse("hjelp.html", _kontekst(request, session))


@app.get("/logg-inn")
def logg_inn_skjema(request: Request, session: Session = Depends(get_session), feil: str = ""):
    if auth.er_admin(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("logg_inn.html", _kontekst(request, session, feil=feil))


@app.post("/logg-inn")
def logg_inn(request: Request, passord: str = Form(""), session: Session = Depends(get_session)):
    if not auth.sjekk_passord(passord):
        return templates.TemplateResponse(
            "logg_inn.html",
            _kontekst(request, session, feil="Feil passord."),
            status_code=401,
        )
    svar = RedirectResponse("/", status_code=303)
    sikker = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    svar.set_cookie(
        auth.COOKIE_NAVN,
        auth.lag_token(),
        max_age=auth.VARIGHET,
        httponly=True,
        samesite="lax",
        secure=sikker,
    )
    return svar


@app.post("/logg-ut")
def logg_ut():
    svar = RedirectResponse("/", status_code=303)
    svar.delete_cookie(auth.COOKIE_NAVN)
    return svar


def _sett_felter(rad, innstilling: Innstilling, start: str, slutt: str, sted: str,
                 tema: str, foredragsholdere: str, notat: str) -> None:
    """Skriver skjemafeltene til en rad. Verdier som er like standarden lagres tomme,
    slik at dagen fortsetter å følge den — bytter du standard klokkeslett senere,
    henger den med i stedet for å bli låst til det som sto i skjemaet."""
    rad.start = "" if start.strip() == innstilling.standard_start else start.strip()[:5]
    rad.slutt = "" if slutt.strip() == innstilling.standard_slutt else slutt.strip()[:5]
    rad.sted = "" if sted.strip() == innstilling.standard_sted else sted.strip()[:200]
    rad.tema = tema.strip()[:200]
    rad.foredragsholdere = foredragsholdere.strip()[:300]
    rad.notat = notat.strip()[:2000]
    rad.oppdatert_at = datetime.utcnow()


def les_dato(verdi: str) -> date:
    try:
        return date.fromisoformat(verdi.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Ugyldig dato")


def hent_ekstra(session: Session, ekstra_id: int) -> EkstraSamling:
    rad = session.get(EkstraSamling, ekstra_id)
    if rad is None:
        raise HTTPException(status_code=404, detail="Ukjent samling")
    return rad


@app.post("/ekstra")
def ny_ekstra(
    request: Request,
    dato: str = Form(...),
    tema: str = Form(""),
    foredragsholdere: str = Form(""),
    start: str = Form(""),
    slutt: str = Form(""),
    sted: str = Form(""),
    notat: str = Form(""),
    retur: str = Form("/"),
    session: Session = Depends(get_session),
):
    """Legger til en samling utenom rekka."""
    auth.krev_admin(request)
    rad = EkstraSamling(dato=les_dato(dato).isoformat())
    _sett_felter(rad, hent_innstilling(session), start, slutt, sted, tema, foredragsholdere, notat)
    session.add(rad)
    session.commit()
    session.refresh(rad)
    return RedirectResponse(trygg_retur(retur, f"ekstra-{rad.id}"), status_code=303)


@app.post("/ekstra/{ekstra_id}")
def lagre_ekstra(
    ekstra_id: int,
    request: Request,
    dato: str = Form(...),
    tema: str = Form(""),
    foredragsholdere: str = Form(""),
    start: str = Form(""),
    slutt: str = Form(""),
    sted: str = Form(""),
    notat: str = Form(""),
    retur: str = Form("/"),
    session: Session = Depends(get_session),
):
    auth.krev_admin(request)
    rad = hent_ekstra(session, ekstra_id)
    rad.dato = les_dato(dato).isoformat()
    _sett_felter(rad, hent_innstilling(session), start, slutt, sted, tema, foredragsholdere, notat)
    session.add(rad)
    session.commit()
    return RedirectResponse(trygg_retur(retur, f"ekstra-{ekstra_id}"), status_code=303)


@app.post("/ekstra/{ekstra_id}/slett")
def slett_ekstra(
    ekstra_id: int,
    request: Request,
    retur: str = Form("/"),
    session: Session = Depends(get_session),
):
    """Sletter for godt. En ekstra samling kom aldri av seg selv, så det finnes ingen
    regel den ville dukket opp igjen fra — i motsetning til de faste dagene, som blir
    liggende med et merke."""
    auth.krev_admin(request)
    session.delete(hent_ekstra(session, ekstra_id))
    session.commit()
    return RedirectResponse(trygg_retur(retur), status_code=303)


@app.get("/ekstra/{ekstra_id}.ics")
def ekstrafil(ekstra_id: int, request: Request, session: Session = Depends(get_session)):
    rad = hent_ekstra(session, ekstra_id)
    d = bygg_ekstra(rad, hent_innstilling(session), request, set())
    return _ics_svar([d], d["tittel"], f"faglig-tirsdag-ekstra-{ekstra_id}.ics")


@app.post("/dag/{noekkel}")
def lagre_dag(
    noekkel: str,
    request: Request,
    dato: str = Form(""),
    start: str = Form(""),
    slutt: str = Form(""),
    sted: str = Form(""),
    tema: str = Form(""),
    foredragsholdere: str = Form(""),
    notat: str = Form(""),
    retur: str = Form("/"),
    session: Session = Depends(get_session),
):
    auth.krev_admin(request)
    aar, maaned = valider_noekkel(noekkel)
    rad = _hent_eller_lag_rad(session, noekkel)
    innstilling = hent_innstilling(session)

    ny_dato = dato.strip()
    if ny_dato:
        try:
            date.fromisoformat(ny_dato)
        except ValueError:
            raise HTTPException(status_code=400, detail="Ugyldig dato")
    # Verdier som er like regelen eller standarden lagres tomme. Da fortsetter dagen
    # å følge dem: bytter du standard klokkeslett senere, henger den med — i stedet
    # for å bli låst til det som tilfeldigvis sto i skjemaet da du sist trykket lagre.
    rad.dato = ny_dato if ny_dato and ny_dato != tredje_tirsdag(aar, maaned).isoformat() else None
    _sett_felter(rad, innstilling, start, slutt, sted, tema, foredragsholdere, notat)
    session.add(rad)
    session.commit()
    return RedirectResponse(trygg_retur(retur, noekkel), status_code=303)


@app.post("/dag/{noekkel}/slett")
def slett_dag(
    noekkel: str,
    request: Request,
    retur: str = Form("/"),
    session: Session = Depends(get_session),
):
    """Sletter dagen fra oversikten og kalenderen. Raden blir liggende, slik at
    dagen ikke dukker opp igjen av seg selv — og slik at den kan hentes tilbake."""
    auth.krev_admin(request)
    rad = _hent_eller_lag_rad(session, noekkel)
    rad.skjult = True
    rad.oppdatert_at = datetime.utcnow()
    session.add(rad)
    session.commit()
    return RedirectResponse(trygg_retur(retur, noekkel), status_code=303)


@app.post("/dag/{noekkel}/gjenopprett")
def gjenopprett_dag(
    noekkel: str,
    request: Request,
    retur: str = Form("/"),
    session: Session = Depends(get_session),
):
    auth.krev_admin(request)
    rad = _hent_eller_lag_rad(session, noekkel)
    rad.skjult = False
    rad.oppdatert_at = datetime.utcnow()
    session.add(rad)
    session.commit()
    return RedirectResponse(trygg_retur(retur, noekkel), status_code=303)


@app.post("/dag/{noekkel}/tilbakestill")
def tilbakestill_dag(
    noekkel: str,
    request: Request,
    retur: str = Form("/"),
    session: Session = Depends(get_session),
):
    """Setter dagen tilbake til tredje tirsdag og standard klokkeslett og sted.
    Tema, foredragsholdere og notat røres ikke."""
    auth.krev_admin(request)
    rad = _hent_eller_lag_rad(session, noekkel)
    rad.dato = None
    rad.start = ""
    rad.slutt = ""
    rad.sted = ""
    rad.oppdatert_at = datetime.utcnow()
    session.add(rad)
    session.commit()
    return RedirectResponse(trygg_retur(retur, noekkel), status_code=303)


@app.post("/innstillinger")
def lagre_innstillinger(
    request: Request,
    standard_start: str = Form("08:30"),
    standard_slutt: str = Form("09:30"),
    standard_sted: str = Form(""),
    session: Session = Depends(get_session),
):
    auth.krev_admin(request)
    innstilling = hent_innstilling(session)
    innstilling.standard_start = standard_start.strip()[:5]
    innstilling.standard_slutt = standard_slutt.strip()[:5]
    innstilling.standard_sted = standard_sted.strip()[:200]
    innstilling.oppdatert_at = datetime.utcnow()
    session.add(innstilling)
    session.commit()
    return RedirectResponse("/", status_code=303)


def _hendelse(d: dict) -> dict:
    return {
        "uid": d["uid"],
        "dato": d["dato"],
        "start": les_klokkeslett(d["start"], time(8, 30)),
        "slutt": les_klokkeslett(d["slutt"], time(9, 30)),
        "tittel": d["tittel"],
        "beskrivelse": d["beskrivelse"],
        "sted": d["sted"],
        "sekvens": d["sekvens"],
    }


def _ics_svar(dager: list[dict], navn: str, filnavn: str, last_ned: bool = True) -> Response:
    innhold = ics_dokument([_hendelse(d) for d in dager], navn=navn)
    disposisjon = "attachment" if last_ned else "inline"
    return Response(
        content=innhold,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'{disposisjon}; filename="{filnavn}"'},
    )


@app.get("/kalender.ics")
def kalenderfil(request: Request, session: Session = Depends(get_session)):
    """Hele serien som én kalenderfil — den Google Kalender abonnerer på. Ekstra
    samlinger er med på lik linje med de faste dagene."""
    dager = [d for d in alle_dager(session, request) if not d["skjult"]]
    return _ics_svar(dager, "Faglig tirsdag", "faglig-tirsdag.ics", last_ned=False)


@app.get("/dag/{noekkel}.ics")
def dagsfil(noekkel: str, request: Request, session: Session = Depends(get_session)):
    valider_noekkel(noekkel)
    innstilling = hent_innstilling(session)
    rad = session.exec(select(FagligTirsdag).where(FagligTirsdag.maaned == noekkel)).first()
    d = bygg_dag(noekkel, rad, innstilling, request, set())
    return _ics_svar([d], d["tittel"], f"faglig-tirsdag-{noekkel}.ics")


@app.get("/eksport.json")
def eksporter(request: Request, session: Session = Depends(get_session)):
    """Sikkerhetskopi av alt som er lagt inn. Verdt å ta av og til hvis appen
    kjører uten fast disk på Render — se README."""
    auth.krev_admin(request)
    innstilling = hent_innstilling(session)
    rader = session.exec(select(FagligTirsdag).order_by(FagligTirsdag.maaned)).all()
    data = {
        "versjon": 1,
        "eksportert": datetime.utcnow().isoformat(timespec="seconds"),
        "innstilling": {
            "standard_start": innstilling.standard_start,
            "standard_slutt": innstilling.standard_slutt,
            "standard_sted": innstilling.standard_sted,
        },
        "ekstra": [
            {
                "dato": r.dato,
                "start": r.start,
                "slutt": r.slutt,
                "sted": r.sted,
                "tema": r.tema,
                "foredragsholdere": r.foredragsholdere,
                "notat": r.notat,
            }
            for r in session.exec(select(EkstraSamling).order_by(EkstraSamling.dato)).all()
        ],
        "dager": [
            {
                "maaned": r.maaned,
                "dato": r.dato,
                "start": r.start,
                "slutt": r.slutt,
                "sted": r.sted,
                "tema": r.tema,
                "foredragsholdere": r.foredragsholdere,
                "notat": r.notat,
                "skjult": r.skjult,
            }
            for r in rader
        ],
    }
    return JSONResponse(
        data,
        headers={
            "Content-Disposition": f'attachment; filename="faglig-tirsdag-{i_dag().isoformat()}.json"'
        },
    )


@app.post("/importer")
def importer(request: Request, fil: UploadFile = File(...), session: Session = Depends(get_session)):
    """Leser inn en tidligere eksport. Måneder som finnes fra før overskrives,
    måneder som ikke er med i fila står urørt."""
    auth.krev_admin(request)
    try:
        data = json.loads(fil.file.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Fila er ikke gyldig JSON fra denne appen.")
    if not isinstance(data, dict) or not isinstance(data.get("dager"), list):
        raise HTTPException(status_code=400, detail="Fant ingen dager i fila.")

    for post in data["dager"]:
        if not isinstance(post, dict) or not isinstance(post.get("maaned"), str):
            continue
        try:
            les_maaned_noekkel(post["maaned"])
        except ValueError:
            continue
        rad = _hent_eller_lag_rad(session, post["maaned"])
        rad.dato = post.get("dato") or None
        rad.start = str(post.get("start") or "")[:5]
        rad.slutt = str(post.get("slutt") or "")[:5]
        rad.sted = str(post.get("sted") or "")[:200]
        rad.tema = str(post.get("tema") or "")[:200]
        rad.foredragsholdere = str(post.get("foredragsholdere") or "")[:300]
        rad.notat = str(post.get("notat") or "")[:2000]
        rad.skjult = bool(post.get("skjult"))
        rad.oppdatert_at = datetime.utcnow()
        session.add(rad)

    # Ekstra samlinger har ingen naturlig nøkkel å slå sammen på, så de erstattes
    # under ett: alle nåværende fjernes, og fila legges inn. Ellers ville hver import
    # av samme fil lagt inn duplikater.
    if isinstance(data.get("ekstra"), list):
        for gammel_ekstra in session.exec(select(EkstraSamling)).all():
            session.delete(gammel_ekstra)
        for post in data["ekstra"]:
            if not isinstance(post, dict):
                continue
            try:
                dato = date.fromisoformat(str(post.get("dato", "")))
            except ValueError:
                continue
            session.add(
                EkstraSamling(
                    dato=dato.isoformat(),
                    start=str(post.get("start") or "")[:5],
                    slutt=str(post.get("slutt") or "")[:5],
                    sted=str(post.get("sted") or "")[:200],
                    tema=str(post.get("tema") or "")[:200],
                    foredragsholdere=str(post.get("foredragsholdere") or "")[:300],
                    notat=str(post.get("notat") or "")[:2000],
                )
            )

    innstilling_data = data.get("innstilling")
    if isinstance(innstilling_data, dict):
        innstilling = hent_innstilling(session)
        innstilling.standard_start = str(innstilling_data.get("standard_start") or "08:30")[:5]
        innstilling.standard_slutt = str(innstilling_data.get("standard_slutt") or "09:30")[:5]
        innstilling.standard_sted = str(innstilling_data.get("standard_sted") or "")[:200]
        session.add(innstilling)

    session.commit()
    return RedirectResponse("/", status_code=303)

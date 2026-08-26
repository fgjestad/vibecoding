import json
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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
from app.models import FagligTirsdag, Innstilling

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


def maaneder_i_vindu() -> list[str]:
    """Månedsnøklene kalenderfila og «neste dag» ser på."""
    i_dag_dato = i_dag()
    return [
        _flytt_maaned(i_dag_dato, forskyvning)
        for forskyvning in range(-MAANEDER_BAKOVER, MAANEDER_FRAMOVER + 1)
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


def _tider(rad: FagligTirsdag | None, innstilling: Innstilling) -> tuple[time, time]:
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


def bygg_dag(noekkel: str, rad: FagligTirsdag | None, innstilling: Innstilling, request: Request) -> dict:
    """Setter sammen alt en Faglig tirsdag skal vise, uansett om den finnes i
    databasen eller bare følger regelen om tredje tirsdag."""
    aar, maaned = les_maaned_noekkel(noekkel)
    standard_dato = tredje_tirsdag(aar, maaned)
    dato = date.fromisoformat(rad.dato) if rad and rad.dato else standard_dato
    start, slutt = _tider(rad, innstilling)
    sted = (rad.sted if rad and rad.sted else innstilling.standard_sted).strip()
    tema = (rad.tema if rad else "").strip()
    foredragsholdere = (rad.foredragsholdere if rad else "").strip()
    notat = (rad.notat if rad else "").strip()

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
            "roed": roed_dag(celle),
            "er_i_dag": celle == i_dag_dato,
            "er_helg": celle.weekday() >= 5,
        }
        for celle in maaned_celler(dato.year, dato.month)
    ]

    return {
        "noekkel": noekkel,
        "aar": aar,
        "maaned_navn": maaned_navn(maaned),
        "dato": dato,
        "dato_iso": dato.isoformat(),
        "dato_tekst": formater_dato(dato),
        "standard_dato_iso": standard_dato.isoformat(),
        "standard_dato_tekst": formater_dato(standard_dato),
        "er_flyttet": dato != standard_dato,
        "start": start.strftime("%H:%M"),
        "slutt": slutt.strftime("%H:%M"),
        "start_felt": rad.start if rad else "",
        "slutt_felt": rad.slutt if rad else "",
        "sted": sted,
        "sted_felt": rad.sted if rad else "",
        "tema": tema,
        "foredragsholdere": foredragsholdere,
        "notat": notat,
        "skjult": bool(rad and rad.skjult),
        "roed_dag": roed_dag(dato),
        "ferie": ferieadvarsel(dato),
        "er_fortid": dato < i_dag_dato,
        "er_i_dag": dato == i_dag_dato,
        "rutenett": rutenett,
        "rutenett_tittel": f"{maaned_navn(dato.month)} {dato.year}",
        "har_innhold": bool(tema or foredragsholdere or notat),
        "tittel": tittel,
        "beskrivelse": beskrivelse,
        "sekvens": int(rad.oppdatert_at.timestamp()) if rad else 0,
        "google_url": google_lenke(dato, start, slutt, tittel, beskrivelse, sted),
        "ics_url": f"/dag/{noekkel}.ics",
    }


def hent_dager(session: Session, noekler: list[str], request: Request) -> list[dict]:
    innstilling = hent_innstilling(session)
    rader = hent_rader(session, noekler)
    return [bygg_dag(noekkel, rader.get(noekkel), innstilling, request) for noekkel in noekler]


def neste_dag(session: Session, request: Request) -> dict | None:
    """Første kommende Faglig tirsdag som ikke er slettet."""
    dager = [d for d in hent_dager(session, maaneder_i_vindu(), request) if not d["skjult"]]
    kommende = sorted((d for d in dager if d["dato"] >= i_dag()), key=lambda d: d["dato"])
    return kommende[0] if kommende else None


def _hent_eller_lag_rad(session: Session, noekkel: str) -> FagligTirsdag:
    valider_noekkel(noekkel)
    rad = session.exec(select(FagligTirsdag).where(FagligTirsdag.maaned == noekkel)).first()
    if rad is None:
        rad = FagligTirsdag(maaned=noekkel)
        session.add(rad)
    return rad


def _kontekst(request: Request, session: Session, **ekstra) -> dict:
    grunnlag = {
        "request": request,
        "ukedager_kort": UKEDAGER_KORT,
        "admin": auth.er_admin(request),
        "passord_mangler": not auth.passord_er_satt(),
        "innstilling": hent_innstilling(session),
        "base_url": base_url(request),
    }
    grunnlag.update(ekstra)
    return grunnlag


@app.get("/")
def oversikt(request: Request, aar: int | None = None, session: Session = Depends(get_session)):
    valgt_aar = aar or i_dag().year
    if not 2000 <= valgt_aar <= 2100:
        raise HTTPException(status_code=400, detail="Ugyldig årstall")
    noekler = [f"{valgt_aar:04d}-{maaned:02d}" for maaned in range(1, 13)]
    dager = hent_dager(session, noekler, request)
    return templates.TemplateResponse(
        "oversikt.html",
        _kontekst(
            request,
            session,
            aar=valgt_aar,
            dager=dager,
            neste=neste_dag(session, request),
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
    rad.start = "" if start.strip() == innstilling.standard_start else start.strip()[:5]
    rad.slutt = "" if slutt.strip() == innstilling.standard_slutt else slutt.strip()[:5]
    rad.sted = "" if sted.strip() == innstilling.standard_sted else sted.strip()[:200]
    rad.tema = tema.strip()[:200]
    rad.foredragsholdere = foredragsholdere.strip()[:300]
    rad.notat = notat.strip()[:2000]
    rad.oppdatert_at = datetime.utcnow()
    session.add(rad)
    session.commit()
    return RedirectResponse(f"/?aar={rad.maaned[:4]}#{noekkel}", status_code=303)


@app.post("/dag/{noekkel}/slett")
def slett_dag(noekkel: str, request: Request, session: Session = Depends(get_session)):
    """Sletter dagen fra oversikten og kalenderen. Raden blir liggende, slik at
    dagen ikke dukker opp igjen av seg selv — og slik at den kan hentes tilbake."""
    auth.krev_admin(request)
    rad = _hent_eller_lag_rad(session, noekkel)
    rad.skjult = True
    rad.oppdatert_at = datetime.utcnow()
    session.add(rad)
    session.commit()
    return RedirectResponse(f"/?aar={noekkel[:4]}#{noekkel}", status_code=303)


@app.post("/dag/{noekkel}/gjenopprett")
def gjenopprett_dag(noekkel: str, request: Request, session: Session = Depends(get_session)):
    auth.krev_admin(request)
    rad = _hent_eller_lag_rad(session, noekkel)
    rad.skjult = False
    rad.oppdatert_at = datetime.utcnow()
    session.add(rad)
    session.commit()
    return RedirectResponse(f"/?aar={noekkel[:4]}#{noekkel}", status_code=303)


@app.post("/dag/{noekkel}/tilbakestill")
def tilbakestill_dag(noekkel: str, request: Request, session: Session = Depends(get_session)):
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
    return RedirectResponse(f"/?aar={noekkel[:4]}#{noekkel}", status_code=303)


@app.post("/innstillinger")
def lagre_innstillinger(
    request: Request,
    standard_start: str = Form("08:30"),
    standard_slutt: str = Form("09:30"),
    standard_sted: str = Form(""),
    retur_aar: int = Form(0),
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
    aar = retur_aar if 2000 <= retur_aar <= 2100 else i_dag().year
    return RedirectResponse(f"/?aar={aar}", status_code=303)


@app.get("/kalender.ics")
def kalenderfil(request: Request, session: Session = Depends(get_session)):
    """Hele serien som én kalenderfil — den Google Kalender abonnerer på."""
    dager = [d for d in hent_dager(session, maaneder_i_vindu(), request) if not d["skjult"]]
    hendelser = [
        {
            "uid": f"faglig-tirsdag-{d['noekkel']}@raumnes",
            "dato": d["dato"],
            "start": les_klokkeslett(d["start"], time(8, 30)),
            "slutt": les_klokkeslett(d["slutt"], time(9, 30)),
            "tittel": d["tittel"],
            "beskrivelse": d["beskrivelse"],
            "sted": d["sted"],
            "sekvens": d["sekvens"],
        }
        for d in sorted(dager, key=lambda d: d["dato"])
    ]
    return Response(
        content=ics_dokument(hendelser),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="faglig-tirsdag.ics"'},
    )


@app.get("/dag/{noekkel}.ics")
def dagsfil(noekkel: str, request: Request, session: Session = Depends(get_session)):
    valider_noekkel(noekkel)
    dager = hent_dager(session, [noekkel], request)
    d = dager[0]
    hendelse = {
        "uid": f"faglig-tirsdag-{d['noekkel']}@raumnes",
        "dato": d["dato"],
        "start": les_klokkeslett(d["start"], time(8, 30)),
        "slutt": les_klokkeslett(d["slutt"], time(9, 30)),
        "tittel": d["tittel"],
        "beskrivelse": d["beskrivelse"],
        "sted": d["sted"],
        "sekvens": d["sekvens"],
    }
    return Response(
        content=ics_dokument([hendelse], navn=d["tittel"]),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="faglig-tirsdag-{noekkel}.ics"'},
    )


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

    innstilling_data = data.get("innstilling")
    if isinstance(innstilling_data, dict):
        innstilling = hent_innstilling(session)
        innstilling.standard_start = str(innstilling_data.get("standard_start") or "08:30")[:5]
        innstilling.standard_slutt = str(innstilling_data.get("standard_slutt") or "09:30")[:5]
        innstilling.standard_sted = str(innstilling_data.get("standard_sted") or "")[:200]
        session.add(innstilling)

    session.commit()
    return RedirectResponse("/", status_code=303)

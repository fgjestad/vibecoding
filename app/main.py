from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import sjekk_passord
from app.db import get_session, init_db
from app.llm import foreslå_kilder
from app.models import Kilde, KildeForslag

app = FastAPI(title="Raumnes arrangementer")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def _kildeliste_respons(request: Request, session: Session):
    kilder = session.exec(select(Kilde)).all()
    kilder = sorted(kilder, key=lambda k: k.samlet_sortering, reverse=True)
    return templates.TemplateResponse(
        "_kildeliste.html", {"request": request, "kilder": kilder}
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
    _: str = Depends(sjekk_passord),
):
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
    _: str = Depends(sjekk_passord),
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
    _: str = Depends(sjekk_passord),
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
    _: str = Depends(sjekk_passord),
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
    _: str = Depends(sjekk_passord),
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
    _: str = Depends(sjekk_passord),
):
    kilde = session.get(Kilde, kilde_id)
    if kilde:
        kilde.aktiv = False
        session.add(kilde)
        session.commit()
    return _kildeliste_respons(request, session)


@app.post("/kilder/oppdag")
def oppdag_nye_kilder(
    ekstra_instruks: str = Form(""),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
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
    _: str = Depends(sjekk_passord),
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
    _: str = Depends(sjekk_passord),
):
    forslag = session.get(KildeForslag, forslag_id)
    if forslag:
        forslag.status = "avvist"
        session.add(forslag)
        session.commit()
    return _forslagliste_respons(request, session)

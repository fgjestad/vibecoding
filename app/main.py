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
    navn: str = Form(...),
    url: str = Form(...),
    manuell_prioritet: int = Form(0),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    kilde = session.get(Kilde, kilde_id)
    if kilde:
        kilde.navn = navn.strip()
        kilde.url = url.strip()
        kilde.manuell_prioritet = manuell_prioritet
        session.add(kilde)
        session.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/kilder/{kilde_id}/aktiver")
def aktiver_kilde(
    kilde_id: int,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    kilde = session.get(Kilde, kilde_id)
    if kilde:
        kilde.aktiv = True
        session.add(kilde)
        session.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/kilder/{kilde_id}/deaktiver")
def deaktiver_kilde(
    kilde_id: int,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    kilde = session.get(Kilde, kilde_id)
    if kilde:
        kilde.aktiv = False
        session.add(kilde)
        session.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/kilder/oppdag")
def oppdag_nye_kilder(
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    eksisterende_urler = {k.url for k in session.exec(select(Kilde)).all()}
    eksisterende_forslag_urler = {
        f.url for f in session.exec(select(KildeForslag)).all()
    }
    forslag = foreslå_kilder()
    for f in forslag:
        if f["url"] in eksisterende_urler or f["url"] in eksisterende_forslag_urler:
            continue
        session.add(
            KildeForslag(
                navn=f["navn"], url=f["url"], begrunnelse=f.get("begrunnelse", "")
            )
        )
        eksisterende_forslag_urler.add(f["url"])
    session.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/forslag/{forslag_id}/godkjenn")
def godkjenn_forslag(
    forslag_id: int,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    forslag = session.get(KildeForslag, forslag_id)
    if forslag and forslag.status == "ny":
        session.add(Kilde(navn=forslag.navn, url=forslag.url))
        forslag.status = "godkjent"
        session.add(forslag)
        session.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/forslag/{forslag_id}/avvis")
def avvis_forslag(
    forslag_id: int,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    forslag = session.get(KildeForslag, forslag_id)
    if forslag:
        forslag.status = "avvist"
        session.add(forslag)
        session.commit()
    return RedirectResponse(url="/", status_code=303)

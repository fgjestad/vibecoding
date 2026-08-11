from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import sjekk_passord
from app.db import get_session, init_db
from app.harvest import (
    beregn_arrangement_id,
    beregn_periode,
    beregn_signatur,
    hent_fra_bilde,
    hent_fra_kilde,
    hent_fra_pdf,
    normaliser_url_for_dedup,
    standard_instruks,
)
from app.llm import foreslå_kilder
from app.models import Arrangement, EkskludertSignatur, Kilde, KildeForslag

app = FastAPI(title="Raumnes arrangementer")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

MAKS_SAMTIDIGE_KILDER = 5


@app.on_event("startup")
def on_startup() -> None:
    init_db()


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


@app.post("/kilder/fjern-duplikater")
def fjern_duplikater(
    request: Request,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
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


def _lagre_arrangement(
    session: Session,
    data: dict,
    sett_ider: set[str],
    ekskluderte_signaturer: set[str],
) -> bool:
    """Normaliserer og lagrer ett arrangement, med id-basert dedup. Returnerer True hvis lagret."""
    tittel = str(data.get("tittel") or "").strip()
    dato_str = str(data.get("dato") or "").strip()
    sted = str(data.get("sted") or "").strip()
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

    signatur = beregn_signatur(tittel, data.get("arrangor"), dato_str)
    forhandsvalgt_bort = signatur in ekskluderte_signaturer

    session.add(
        Arrangement(
            arrangement_id=arrangement_id,
            tittel=tittel,
            dato=dato_str,
            klokkeslett=data.get("klokkeslett"),
            sted=sted,
            arrangor=data.get("arrangor"),
            original_tekst=str(data.get("original_tekst") or "").strip(),
            tekst_bekreftet=bool(data.get("tekst_bekreftet", False)),
            kilde_type=data.get("kilde_type", "nettsok"),
            kilde_url=data.get("kilde_url"),
            geografisk_relevans=data.get("geografisk_relevans", "usikker"),
            signatur=signatur,
            valgt=not forhandsvalgt_bort,
            forhandsvalgt_bort=forhandsvalgt_bort,
        )
    )
    return True


def _innhosting_kontekst(request: Request, session: Session, feilmelding: str | None = None) -> dict:
    arrangementer = session.exec(
        select(Arrangement).order_by(Arrangement.dato, Arrangement.klokkeslett)
    ).all()
    forste_dag, siste_dag = beregn_periode()
    return {
        "request": request,
        "arrangementer": arrangementer,
        "forste_dag": forste_dag,
        "siste_dag": siste_dag,
        "standard_instruks_verdi": standard_instruks(forste_dag, siste_dag),
        "feilmelding": feilmelding,
    }


@app.get("/innhosting")
def innhosting_side(
    request: Request,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    return templates.TemplateResponse(
        "innhosting.html", _innhosting_kontekst(request, session)
    )


@app.post("/innhosting/kjor")
def kjor_innhosting(
    request: Request,
    instruks: str = Form(...),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    forste_dag, siste_dag = beregn_periode()

    ekskluderte = {e.signatur for e in session.exec(select(EkskludertSignatur)).all()}
    sett_ider = {a.arrangement_id for a in session.exec(select(Arrangement)).all()}

    aktive_kilder = session.exec(select(Kilde).where(Kilde.aktiv == True)).all()  # noqa: E712
    aktive_kilder = sorted(aktive_kilder, key=lambda k: k.samlet_sortering, reverse=True)

    feil_kilder = []
    resultater: dict[int, tuple[list[dict], str | None]] = {}
    if aktive_kilder:
        with ThreadPoolExecutor(max_workers=min(MAKS_SAMTIDIGE_KILDER, len(aktive_kilder))) as executor:
            fremtid_til_kilde = {
                executor.submit(hent_fra_kilde, kilde, forste_dag, siste_dag, instruks): kilde
                for kilde in aktive_kilder
            }
            for fremtid in as_completed(fremtid_til_kilde):
                kilde = fremtid_til_kilde[fremtid]
                try:
                    resultater[kilde.id] = fremtid.result()
                except Exception as e:
                    resultater[kilde.id] = ([], None)
                    feil_kilder.append(f"{kilde.navn} ({e})")

    for kilde in aktive_kilder:
        rå, foreslatt_url = resultater.get(kilde.id, ([], None))
        antall = sum(1 for a in rå if _lagre_arrangement(session, a, sett_ider, ekskluderte))
        kilde.automatisk_prioritet = float(antall)
        if foreslatt_url:
            kilde.url = foreslatt_url
        session.add(kilde)

    session.commit()

    if feil_kilder:
        feilmelding = "Disse kildene feilet under innhøsting: " + "; ".join(feil_kilder)
        return templates.TemplateResponse(
            "innhosting.html", _innhosting_kontekst(request, session, feilmelding)
        )
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/innhosting/tom")
def tom_utkast(
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    for a in session.exec(select(Arrangement)).all():
        session.delete(a)
    session.commit()
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/innhosting/last-opp")
async def last_opp_fil(
    request: Request,
    fil: UploadFile = File(...),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    forste_dag, siste_dag = beregn_periode()
    innhold = await fil.read()

    STOTTEDE_BILDETYPER = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    try:
        if fil.content_type == "application/pdf":
            rå = hent_fra_pdf(innhold, forste_dag, siste_dag)
        elif fil.content_type in STOTTEDE_BILDETYPER:
            rå = hent_fra_bilde(innhold, fil.content_type, forste_dag, siste_dag)
        else:
            return templates.TemplateResponse(
                "innhosting.html",
                _innhosting_kontekst(
                    request,
                    session,
                    f"Filtypen '{fil.content_type}' støttes ikke. Bruk JPEG, PNG, WEBP, "
                    "GIF eller PDF.",
                ),
            )
    except Exception as e:
        return templates.TemplateResponse(
            "innhosting.html",
            _innhosting_kontekst(
                request, session, f"Kunne ikke tolke filen '{fil.filename}': {e}"
            ),
        )

    sett_ider = {a.arrangement_id for a in session.exec(select(Arrangement)).all()}
    ekskluderte = {e.signatur for e in session.exec(select(EkskludertSignatur)).all()}
    for a in rå:
        _lagre_arrangement(session, a, sett_ider, ekskluderte)
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

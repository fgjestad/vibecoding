import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.artikkel import generer_hel_artikkel, skriv_om_ett_avsnitt, standard_artikkel_instruks
from app.auth import sjekk_passord
from app.db import get_session, init_db
from app.harvest import (
    beregn_arrangement_id,
    beregn_periode,
    beregn_signatur,
    er_tittel_duplikat,
    hent_fra_bilde,
    hent_fra_kilde,
    hent_fra_pdf,
    hent_fra_tekst,
    hent_mer_info,
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
    Innstilling,
    Kilde,
    KildeForslag,
)

app = FastAPI(title="Raumnes arrangementer")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["domene"] = vertsnavn

MAKS_SAMTIDIGE_KILDER = 5


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def _hent_innstilling(session: Session) -> Innstilling:
    innstilling = session.get(Innstilling, 1)
    if not innstilling:
        innstilling = Innstilling(id=1)
        session.add(innstilling)
        session.commit()
        session.refresh(innstilling)
    return innstilling


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


def _finn_flerdagsmatch(
    tittel: str, tekst: str, sted: str, dato_str: str, kandidater: list[Arrangement]
) -> Arrangement | None:
    """Finner et eksisterende arrangement som dette sannsynligvis er en direkte fortsettelse
    av — samme sted og nesten ordrett lik tittel/tekst, og datoen er nøyaktig én dag før
    startdatoen eller én dag etter sluttdatoen til det eksisterende. Fanger opp at enkelte
    kilder oppgir et flerdagers arrangement (f.eks. en utstilling) som separate
    endags-rader, én per dato, i stedet for én rad med en dato-periode.

    Kun eksakt dags-tilstøtende datoer slås sammen (aldri med hull) — det skiller trygt en
    faktisk sammenhengende periode fra f.eks. et ukentlig gjentagende arrangement med samme
    tittel, som ikke skal vises som én lang periode."""
    ny_dato = date.fromisoformat(dato_str)
    for eksisterende in kandidater:
        try:
            start = date.fromisoformat(eksisterende.dato)
            slutt = date.fromisoformat(eksisterende.til_dato or eksisterende.dato)
        except ValueError:
            continue
        if ny_dato not in (start - timedelta(days=1), slutt + timedelta(days=1)):
            continue
        if normaliser_tekst(sted) != normaliser_tekst(eksisterende.sted):
            continue
        if tekstlikhet(tittel, eksisterende.tittel) < 0.85:
            continue
        if tekstlikhet(tekst, eksisterende.original_tekst) < 0.85:
            continue
        return eksisterende
    return None


def _lagre_arrangement(
    session: Session,
    data: dict,
    sett_ider: set[str],
    ekskluderte_signaturer: set[str],
    hittil_pr_dato: dict[str, list[Arrangement]],
    flerdags_kandidater: list[Arrangement],
) -> bool:
    """Normaliserer og lagrer ett arrangement, med id-basert dedup. Returnerer True hvis lagret
    (eller slått sammen inn i et eksisterende flerdagers arrangement).

    Eksakte duplikater (samme tittel+dato+sted) droppes stille via sett_ider, som før.

    Hvis dette tydelig er samme arrangement som et allerede lagret, bare på nabodatoen (se
    _finn_flerdagsmatch), utvides det eksisterendes dato-periode i stedet for å opprette en
    ny rad — slik unngås at et flerdagers arrangement vises som mange separate endags-rader.

    Ellers gjelder vanlig duplikatsjekk: sannsynlige duplikater (fanget opp av
    er_tittel_duplikat — fuzzy tittel-sammenligning mot alt annet lagret på samme dato, se
    harvest.er_tittel_duplikat) legges til synlig, men avhukes av som standard — både det
    nylig lagrede og alt tidligere lagret (denne kjøringen eller før) som det matcher — slik
    at journalisten selv velger hvilket (om noen) i gruppen som skal brukes."""
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

    flerdagsmatch = _finn_flerdagsmatch(tittel, original_tekst, sted, dato_str, flerdags_kandidater)
    if flerdagsmatch:
        if date.fromisoformat(dato_str) < date.fromisoformat(flerdagsmatch.dato):
            flerdagsmatch.dato = dato_str
        else:
            flerdagsmatch.til_dato = dato_str
        session.add(flerdagsmatch)
        return True

    signatur = beregn_signatur(tittel, data.get("arrangor"), dato_str)
    forhandsvalgt_bort = signatur in ekskluderte_signaturer

    klokkeslett = data.get("klokkeslett")
    samme_dato = hittil_pr_dato.setdefault(dato_str, [])
    matchende = [
        annen for annen in samme_dato
        if er_tittel_duplikat(tittel, klokkeslett, annen.tittel, annen.klokkeslett)
    ]
    er_mulig_duplikat = bool(matchende)
    for annen in matchende:
        if annen.valgt:
            annen.valgt = False
            session.add(annen)

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
        valgt=not forhandsvalgt_bort and not er_mulig_duplikat,
        forhandsvalgt_bort=forhandsvalgt_bort,
    )
    session.add(nytt)
    samme_dato.append(nytt)
    flerdags_kandidater.append(nytt)
    return True


def _hittil_pr_dato(eksisterende: list[Arrangement]) -> dict[str, list[Arrangement]]:
    """Bygger oppstartstilstanden for duplikatsjekken i _lagre_arrangement, ut fra
    arrangementer som allerede finnes i utkastet."""
    hittil_pr_dato: dict[str, list[Arrangement]] = {}
    for a in eksisterende:
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


def _beregn_duplikatgrupper(arrangementer: list[Arrangement]) -> dict[int, int]:
    """Finner sannsynlige duplikater på tvers av kilder — se harvest.er_tittel_duplikat for
    hvordan par sammenlignes. Grupperer først etter dato (billig, hard grense), sammenligner
    deretter alle par innenfor samme dato parvis, og slår sammen matchende par til grupper
    med union-find (slik at f.eks. A~B og B~C samles i én gruppe selv om A og C ikke ble
    direkte sammenlignet som like).

    Returnerer {arrangement_id: gruppe_id} kun for arrangementer som faktisk inngår i en
    gruppe med mer enn ett medlem — gruppe_id er det laveste id-et i gruppen, og brukes
    både til visning ("Mulig duplikat") og til å sortere gruppens medlemmer ved siden av
    hverandre."""
    per_dato: dict[str, list[Arrangement]] = {}
    for a in arrangementer:
        per_dato.setdefault(a.dato, []).append(a)

    foreldre: dict[int, int] = {a.id: a.id for a in arrangementer}

    def finn(x: int) -> int:
        while foreldre[x] != x:
            foreldre[x] = foreldre[foreldre[x]]
            x = foreldre[x]
        return x

    for gruppe in per_dato.values():
        for i in range(len(gruppe)):
            for j in range(i + 1, len(gruppe)):
                if er_tittel_duplikat(
                    gruppe[i].tittel, gruppe[i].klokkeslett, gruppe[j].tittel, gruppe[j].klokkeslett
                ):
                    rot_i, rot_j = finn(gruppe[i].id), finn(gruppe[j].id)
                    if rot_i != rot_j:
                        foreldre[max(rot_i, rot_j)] = min(rot_i, rot_j)

    storrelse: dict[int, int] = {}
    for a_id in foreldre:
        rot = finn(a_id)
        storrelse[rot] = storrelse.get(rot, 0) + 1
    return {a_id: finn(a_id) for a_id in foreldre if storrelse[finn(a_id)] > 1}


def _sorter_med_duplikater_samlet(arrangementer: list[Arrangement]) -> tuple[list[Arrangement], set[int]]:
    """Som _sorteringsnokkel, men grupper som er flagget som sannsynlige duplikater av
    hverandre plasseres rett etter hverandre i lista (ankret til gruppens tidligste
    klokkeslett), i stedet for å kunne havne spredt utover dagens andre arrangementer.

    Returnerer (sortert_liste, duplikat_ider) — duplikat_ider er id-ene til alle
    arrangementer som inngår i en duplikat-gruppe, til bruk for "Mulig duplikat"-merket."""
    duplikatgrupper = _beregn_duplikatgrupper(arrangementer)

    gruppe_anker: dict[int, tuple] = {}
    for a in arrangementer:
        gruppe_id = duplikatgrupper.get(a.id)
        if gruppe_id is not None:
            nokkel = _sorteringsnokkel(a)
            if gruppe_id not in gruppe_anker or nokkel < gruppe_anker[gruppe_id]:
                gruppe_anker[gruppe_id] = nokkel

    def full_nokkel(a: Arrangement) -> tuple:
        egen = _sorteringsnokkel(a)
        gruppe_id = duplikatgrupper.get(a.id)
        if gruppe_id is not None:
            return gruppe_anker[gruppe_id] + (0, gruppe_id, egen)
        return egen + (1, 0, egen)

    sortert = sorted(arrangementer, key=full_nokkel)
    return sortert, set(duplikatgrupper.keys())


def _innhosting_kontekst(request: Request, session: Session, feilmelding: str | None = None) -> dict:
    innstilling = _hent_innstilling(session)
    arrangementer = session.exec(select(Arrangement)).all()
    arrangementer, duplikat_ider = _sorter_med_duplikater_samlet(arrangementer)

    forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)
    return {
        "request": request,
        "arrangementer": arrangementer,
        "duplikat_ider": duplikat_ider,
        "forste_dag": forste_dag,
        "siste_dag": siste_dag,
        "antall_dager": innstilling.antall_dager,
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
    innstilling = _hent_innstilling(session)
    forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)

    ekskluderte = {e.signatur for e in session.exec(select(EkskludertSignatur)).all()}
    eksisterende = session.exec(select(Arrangement)).all()
    sett_ider = {a.arrangement_id for a in eksisterende}
    hittil_pr_dato = _hittil_pr_dato(eksisterende)
    flerdags_kandidater = list(eksisterende)

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
                        if _lagre_arrangement(session, a, sett_ider, ekskluderte, hittil_pr_dato, flerdags_kandidater)
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

    if feil_kilder:
        feilmelding = "Disse kildene feilet under innhøsting: " + "; ".join(feil_kilder)
        return templates.TemplateResponse(
            "innhosting.html", _innhosting_kontekst(request, session, feilmelding)
        )
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/innhosting/{arrangement_id}/hent-mer")
def hent_mer_for_arrangement(
    arrangement_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
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
                request, session, f"Kunne ikke hente mer info for «{a.tittel}»: {e}"
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
            "innhosting.html", _innhosting_kontekst(request, session)
        )

    return templates.TemplateResponse(
        "innhosting.html",
        _innhosting_kontekst(
            request,
            session,
            f"Fant ikke mer informasjon om «{a.tittel}» enn det som allerede er hentet.",
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


@app.post("/innhosting/last-opp")
async def last_opp_fil(
    request: Request,
    fil: UploadFile = File(...),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    innstilling = _hent_innstilling(session)
    forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)
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

    eksisterende = session.exec(select(Arrangement)).all()
    sett_ider = {a.arrangement_id for a in eksisterende}
    hittil_pr_dato = _hittil_pr_dato(eksisterende)
    flerdags_kandidater = list(eksisterende)
    ekskluderte = {e.signatur for e in session.exec(select(EkskludertSignatur)).all()}
    for a in rå:
        _lagre_arrangement(session, a, sett_ider, ekskluderte, hittil_pr_dato, flerdags_kandidater)
    session.commit()
    return RedirectResponse(url="/innhosting", status_code=303)


@app.post("/innhosting/lim-inn-tekst")
async def lim_inn_tekst(
    request: Request,
    tekst: str = Form(...),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    innstilling = _hent_innstilling(session)
    forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)

    if not tekst.strip():
        return templates.TemplateResponse(
            "innhosting.html",
            _innhosting_kontekst(request, session, "Ingen tekst ble limt inn."),
        )

    try:
        rå = hent_fra_tekst(tekst, forste_dag, siste_dag)
    except Exception as e:
        return templates.TemplateResponse(
            "innhosting.html",
            _innhosting_kontekst(request, session, f"Kunne ikke tolke den limte inn teksten: {e}"),
        )

    eksisterende = session.exec(select(Arrangement)).all()
    sett_ider = {a.arrangement_id for a in eksisterende}
    hittil_pr_dato = _hittil_pr_dato(eksisterende)
    flerdags_kandidater = list(eksisterende)
    ekskluderte = {e.signatur for e in session.exec(select(EkskludertSignatur)).all()}
    for a in rå:
        _lagre_arrangement(session, a, sett_ider, ekskluderte, hittil_pr_dato, flerdags_kandidater)
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


def _avsnitt_for_artikkel(session: Session, artikkel: Artikkel, arrangement_oppslag: dict) -> list[dict]:
    rader = session.exec(
        select(ArtikkelAvsnitt)
        .where(ArtikkelAvsnitt.artikkel_id == artikkel.id)
        .order_by(ArtikkelAvsnitt.rekkefolge)
    ).all()
    avsnitt_liste = []
    forrige_kategori = None
    for rad in rader:
        kilde = arrangement_oppslag.get(rad.arrangement_id)
        avsnitt_liste.append(
            {
                "id": rad.id,
                "tekst": rad.tekst,
                "kategori": rad.kategori,
                "ny_kategori": rad.kategori != forrige_kategori,
                "kilde_url": kilde.kilde_url if kilde else None,
                "kilde_tittel": kilde.tittel if kilde else "",
            }
        )
        forrige_kategori = rad.kategori
    return avsnitt_liste


def _artikler_kontekst(request: Request, session: Session, feilmelding: str | None = None) -> dict:
    innstilling = _hent_innstilling(session)
    forste_dag, siste_dag = beregn_periode(antall_dager=innstilling.antall_dager)
    arrangement_oppslag = {a.id: a for a in session.exec(select(Arrangement)).all()}

    alle_artikler = session.exec(select(Artikkel).order_by(Artikkel.id.desc())).all()
    artikkel = alle_artikler[0] if alle_artikler else None
    avsnitt_liste = _avsnitt_for_artikkel(session, artikkel, arrangement_oppslag) if artikkel else []

    tidligere_artikler = [
        {
            "id": eldre.id,
            "tittel": eldre.tittel,
            "ingress": eldre.ingress,
            "opprettet_at": eldre.opprettet_at,
            "avsnitt": _avsnitt_for_artikkel(session, eldre, arrangement_oppslag),
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
        "artikkel_instruks_verdi": standard_artikkel_instruks(),
        "feilmelding": feilmelding,
    }


@app.get("/artikler")
def artikler_side(
    request: Request,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
):
    return templates.TemplateResponse("artikler.html", _artikler_kontekst(request, session))


@app.post("/artikler/generer")
def generer_artikkel_rute(
    request: Request,
    instruks: str = Form(...),
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
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
            ),
        )

    try:
        resultat = generer_hel_artikkel(valgte, instruks=instruks)
    except Exception as e:
        return templates.TemplateResponse(
            "artikler.html",
            _artikler_kontekst(request, session, f"Kunne ikke generere artikkelen: {e}"),
        )

    if not resultat:
        return templates.TemplateResponse(
            "artikler.html",
            _artikler_kontekst(request, session, "Fikk ikke generert noen artikkel. Prøv igjen."),
        )

    ny_artikkel = Artikkel(tittel=resultat["tittel"], ingress=resultat["ingress"])
    session.add(ny_artikkel)
    session.commit()
    session.refresh(ny_artikkel)

    for rekkefolge, avsnitt in enumerate(resultat["avsnitt"]):
        session.add(
            ArtikkelAvsnitt(
                artikkel_id=ny_artikkel.id,
                arrangement_id=avsnitt["arrangement_id"],
                tekst=avsnitt["tekst"],
                kategori=avsnitt["kategori"],
                rekkefolge=rekkefolge,
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


@app.post("/artikler/avsnitt/{avsnitt_id}/hent-mer")
def hent_mer_for_avsnitt(
    avsnitt_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _: str = Depends(sjekk_passord),
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
                request, session, f"Kunne ikke hente mer info for «{arrangement.tittel}»: {e}"
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
            ),
        )

    arrangement.original_tekst = ny_tekst
    arrangement.tekst_bekreftet = bekreftet
    if ny_url:
        arrangement.kilde_url = ny_url
    session.add(arrangement)
    session.commit()

    try:
        omskrevet = skriv_om_ett_avsnitt(arrangement)
    except Exception as e:
        return templates.TemplateResponse(
            "artikler.html",
            _artikler_kontekst(
                request, session, f"Hentet mer info, men klarte ikke omskrive avsnittet: {e}"
            ),
        )

    if omskrevet:
        rad.tekst = omskrevet
        session.add(rad)
        session.commit()

    return templates.TemplateResponse("artikler.html", _artikler_kontekst(request, session))

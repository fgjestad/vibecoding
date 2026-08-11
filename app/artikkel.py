import json
import os
import re
import unicodedata
from datetime import date, datetime, timedelta

from anthropic import Anthropic

from app.models import Arrangement

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

N_GRAM_LENGDE = 8

FELLES_STILINSTRUKS = """- Skriv presist og faktabasert — ikke finn på informasjon som ikke står i "original_tekst".
- Ta med tidspunkt, sted og hva slags type arrangement det er (konsert, basar, kamp, o.l.).
- Sett navnet på artist, arrangement eller lag/forening i fet skrift ved å omslutte det med \
doble stjerner, slik: **Navn**. Ikke bruk fet skrift på annet.
- IKKE ta med kommersiell informasjon som billettbestilling, priser eller kontaktperson.
- Ikke kopier setninger direkte fra "original_tekst" — formuler alt med egne ord."""


def _ord_liste(tekst: str | None) -> list[str]:
    tekst = unicodedata.normalize("NFKD", (tekst or "").lower())
    tekst = re.sub(r"[^\w\s]", "", tekst)
    return tekst.split()


def har_mulig_kopiert_setning(original_tekst: str, avsnitt_tekst: str) -> bool:
    """Enkel etterkontroll: leter etter lange ordrette utdrag (8+ ord på rad) fra kildeteksten
    igjen i det skrevne avsnittet. Slikt bør ikke forekomme siden teksten skal være omskrevet
    — dette er et beste-forsøk-varsel, ingen garanti."""
    kilde_ord = _ord_liste(original_tekst)
    avsnitt_ord = _ord_liste(avsnitt_tekst)
    if len(kilde_ord) < N_GRAM_LENGDE or len(avsnitt_ord) < N_GRAM_LENGDE:
        return False
    kilde_ngrammer = {
        tuple(kilde_ord[i : i + N_GRAM_LENGDE]) for i in range(len(kilde_ord) - N_GRAM_LENGDE + 1)
    }
    for i in range(len(avsnitt_ord) - N_GRAM_LENGDE + 1):
        if tuple(avsnitt_ord[i : i + N_GRAM_LENGDE]) in kilde_ngrammer:
            return True
    return False


def _klokkeslett_som_tid(klokkeslett: str | None) -> tuple[int, int]:
    if klokkeslett:
        treff = re.match(r"(\d{1,2})[:.](\d{2})", klokkeslett.strip())
        if treff:
            return int(treff.group(1)), int(treff.group(2))
    return (0, 0)


def sorteringsnokkel(a: Arrangement, na: datetime | None = None) -> tuple:
    """Sorterer på starttidspunkt. Arrangementer som allerede har startet (dato/klokkeslett
    er passert) sorteres som om de starter i morgen, slik at de ikke forsvinner bakerst eller
    havner feilplassert langt fram i tid."""
    na = na or datetime.now()
    try:
        d = date.fromisoformat(a.dato)
    except (ValueError, TypeError):
        d = date.max

    time, minutt = _klokkeslett_som_tid(a.klokkeslett)
    try:
        start = datetime(d.year, d.month, d.day, time, minutt)
    except ValueError:
        start = datetime.max

    if start <= na:
        d = na.date() + timedelta(days=1)

    return (d.isoformat(), time, minutt)


def sorter_arrangementer(arrangementer: list[Arrangement]) -> list[Arrangement]:
    return sorted(arrangementer, key=sorteringsnokkel)


def generer_hel_artikkel(arrangementer: list[Arrangement]) -> dict | None:
    """Skriver én artikkel for hele perioden, med ett avsnitt per arrangement.

    Returnerer {"tittel", "ingress", "avsnitt": [{"arrangement_id", "tekst", "mulig_kopiert"}]}
    i riktig kronologisk rekkefølge, eller None ved feil/tomt input.
    """
    if not os.environ.get("ANTHROPIC_API_KEY") or not arrangementer:
        return None

    sorterte = sorter_arrangementer(arrangementer)
    grunnlag = [
        {
            "id": a.id,
            "tittel": a.tittel,
            "dato": a.dato,
            "klokkeslett": a.klokkeslett,
            "sted": a.sted,
            "arrangor": a.arrangor,
            "original_tekst": a.original_tekst,
        }
        for a in sorterte
    ]

    client = Anthropic()
    prompt = f"""Du er journalist i lokalavisen Raumnes, som dekker Nes kommune på Romerike i \
Akershus. Under er en liste med kommende arrangementer i kronologisk rekkefølge (som JSON), \
hver med en id og tekst hentet fra arrangørens egen nettside.

ARRANGEMENTER (i rekkefølgen artikkelen skal ha):
{json.dumps(grunnlag, ensure_ascii=False, indent=2)}

Skriv ÉN samlet artikkel som dekker alle arrangementene i listen, én etter én i den oppgitte \
rekkefølgen. Du skal levere:
- "tittel": en fengende tittel for artikkelen
- "ingress": en kort ingress (2-3 setninger) som vinkler mot det mest spektakulære, \
eksklusive eller oppsiktsvekkende blant arrangementene i perioden
- "avsnitt": ett avsnitt PER arrangement i listen (samme antall, samme rekkefølge, med riktig \
"arrangement_id") — ikke slå sammen flere arrangementer i ett avsnitt, og ikke hopp over noen

For hvert avsnitt gjelder:
{FELLES_STILINSTRUKS}

Svar KUN med et gyldig JSON-objekt, ingen tekst før eller etter, i formatet:
{{"tittel": "...", "ingress": "...", "avsnitt": [{{"arrangement_id": 1, "tekst": "..."}}]}}
"""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8192,
            output_config={"effort": "medium"},
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
    if not isinstance(data, dict):
        return None

    tittel = str(data.get("tittel") or "").strip()
    ingress = str(data.get("ingress") or "").strip()
    if not tittel or not ingress:
        return None

    arrangement_pr_id = {a.id: a for a in sorterte}
    mottatt_tekst_pr_id: dict[int, str] = {}
    for rad in data.get("avsnitt", []):
        if not isinstance(rad, dict):
            continue
        aid = rad.get("arrangement_id")
        innhold = str(rad.get("tekst") or "").strip()
        if isinstance(aid, int) and aid in arrangement_pr_id and innhold:
            mottatt_tekst_pr_id[aid] = innhold

    avsnitt = []
    for a in sorterte:
        avsnitt_tekst = mottatt_tekst_pr_id.get(a.id)
        if not avsnitt_tekst:
            # Reserveløsning: Claude hoppet over dette arrangementet — ta med enkel,
            # ikke-omskrevet faktatekst fremfor å miste det stille.
            tid = f" kl. {a.klokkeslett}" if a.klokkeslett else ""
            avsnitt_tekst = f"**{a.tittel}** – {a.dato}{tid}, {a.sted}."
        avsnitt.append(
            {
                "arrangement_id": a.id,
                "tekst": avsnitt_tekst,
                "mulig_kopiert": har_mulig_kopiert_setning(a.original_tekst, avsnitt_tekst),
            }
        )

    return {"tittel": tittel, "ingress": ingress, "avsnitt": avsnitt}


def skriv_om_ett_avsnitt(a: Arrangement) -> tuple[str, bool] | None:
    """Skriver om ett enkelt avsnitt på nytt, typisk etter at 'hent mer informasjon' har gitt
    en rikere original_tekst for akkurat dette arrangementet.

    Returnerer (tekst, mulig_kopiert), eller None ved feil."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None

    grunnlag = {
        "tittel": a.tittel,
        "dato": a.dato,
        "klokkeslett": a.klokkeslett,
        "sted": a.sted,
        "arrangor": a.arrangor,
        "original_tekst": a.original_tekst,
    }
    client = Anthropic()
    prompt = f"""Du er journalist i lokalavisen Raumnes. Skriv ETT avsnitt (til en samleartikkel \
om kommende arrangementer) om dette arrangementet:

{json.dumps(grunnlag, ensure_ascii=False, indent=2)}

{FELLES_STILINSTRUKS}

Svar KUN med selve avsnittsteksten, ingen annen tekst, ingen anførselstegn rundt."""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return None

    tekst = "".join(b.text for b in response.content if b.type == "text").strip()
    if not tekst:
        return None
    return tekst, har_mulig_kopiert_setning(a.original_tekst, tekst)

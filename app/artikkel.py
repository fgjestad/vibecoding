import json
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta

from anthropic import Anthropic

from app.models import Arrangement

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

MINSTE_KATEGORI_STORRELSE = 3


def standard_artikkel_instruks() -> str:
    """Stilinstruksen som brukes for hvert avsnitt i artikkelen.

    Vises redigerbar i UI (jobb 3) slik at brukeren kan finpusse den før en generering.
    """
    return """- Vær presis og faktabasert. Det viktigste er at det som gjengis fra kildeteksten \
er nøyaktig — det er ikke noe problem om ordlyden ligger tett opp til kilden, så lenge \
innholdet er korrekt.
- Ta med tidspunkt, sted og hva slags type arrangement det er (konsert, basar, kamp, o.l.).
- Sett navnet på artist, arrangement eller lag/forening i fet skrift ved å omslutte det med \
doble stjerner, slik: **Navn**. Ikke bruk fet skrift på annet.
- Nevn kilden for hvert arrangement på en naturlig måte i teksten (f.eks. "ifølge arrangøren").
- IKKE ta med kommersiell informasjon som billettbestilling, priser eller kontaktperson.
- IKKE ta med gateadresse — regn det som kommersiell informasjon på lik linje med det over. \
Stedsnavnet er nok (f.eks. "Kulturhuset", ikke gateadressen dit).
- Anta at leseren har lokalkunnskap om Nes kommune. Ikke forklar selvsagte geografiske \
forhold (f.eks. hvilken kommune eller hvilket fylke et kjent sted ligger i) — nevn stedet \
kort og direkte.
- Ikke finn på informasjon som ikke står i det oppgitte grunnlaget."""


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


def _kategori_prioritet(kategori: str) -> int:
    """Barn og unge først, deretter kultur, så alle andre kategorier — etter brukerens ønske."""
    normalisert = kategori.lower()
    if "barn" in normalisert or "unge" in normalisert or "ungdom" in normalisert:
        return 0
    if "kultur" in normalisert:
        return 1
    return 2


def generer_hel_artikkel(
    arrangementer: list[Arrangement], instruks: str | None = None
) -> tuple[dict | None, str]:
    """Skriver én artikkel for hele perioden, med ett avsnitt per arrangement, gruppert under
    mellomtitler per kategori. Barn og unge kommer først, deretter kultur, så resten.

    Kategori-instruksen (hvordan mellomtitlene velges) ligger her i koden, IKKE i den
    redigerbare stilinstruksen (standard_artikkel_instruks) — den gjelder kun teksten i hvert
    avsnitt, ikke kategoriseringen/grupperingen. En kategori som etter genereringen ender opp
    med færre enn MINSTE_KATEGORI_STORRELSE avsnitt slås sammen inn i "Annet" i etterkant her
    i koden — dette er et hardt, garantert krav (uavhengig av om Claude følger ledeteksten om
    å ikke lage for mange kategorier), for å unngå mellomtitler med bare ett eller to avsnitt
    under seg.

    Returnerer ({"tittel", "ingress", "avsnitt": [...]}, "") ved suksess, eller
    (None, diagnosemelding) ved feil — diagnosen er ment å vises til brukeren, så den forklarer
    konkret hva som gikk galt (og et utdrag av Claudes faktiske svar der det er relevant),
    fremfor en generisk "noe gikk galt".
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, "ANTHROPIC_API_KEY er ikke satt på serveren."
    if not arrangementer:
        return None, "Ingen arrangementer å skrive om."

    instruks = instruks if instruks is not None else standard_artikkel_instruks()
    sorterte = sorter_arrangementer(arrangementer)
    grunnlag = [
        {
            "id": a.id,
            "tittel": a.tittel,
            "dato": a.dato,
            "til_dato": a.til_dato,
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
hver med en id og tekst hentet fra arrangørens egen nettside. "til_dato" er kun satt for \
flerdagers arrangementer (f.eks. en utstilling) — betyr at det varer fra "dato" til og med \
"til_dato"; fraser dette naturlig i teksten (f.eks. "fra 22. til 26. august").

ARRANGEMENTER (i rekkefølgen artikkelen skal ha):
{json.dumps(grunnlag, ensure_ascii=False, indent=2)}

Skriv ÉN samlet artikkel som dekker alle arrangementene i listen. Du skal levere:
- "tittel": en fengende tittel for artikkelen
- "ingress": en kort ingress (2-3 setninger) som vinkler mot det mest spektakulære, \
eksklusive eller oppsiktsvekkende blant arrangementene i perioden
- "avsnitt": ett avsnitt PER arrangement i listen (samme antall, med riktig "arrangement_id") \
— ikke slå sammen flere arrangementer i ett avsnitt, og ikke hopp over noen. Hvert avsnitt \
skal også ha en "kategori" du velger fritt ut fra hva slags arrangement det er. Bruk "Barn og \
unge" for arrangementer rettet mot barn/ungdom, og "Kultur" for kulturarrangementer (konserter, \
utstillinger, teater o.l.) når det passer — ellers velg en kort, dekkende kategori selv \
(f.eks. idrett, frivillighet, livssyn). Disse kategoriene brukes som mellomtitler i artikkelen — \
hold antallet ULIKE kategorier lavt (færrest mulig, typisk 2-4 totalt for en vanlig periode): \
slå sammen beslektede arrangementer under samme, bredere kategori i stedet for å finne opp en \
ny for hver type. Bruk "Annet" for enkeltstående arrangementer som ikke naturlig hører til en \
av de andre kategoriene du har valgt.

For hvert avsnitt gjelder:
{instruks}

Svar KUN med et gyldig JSON-objekt, ingen tekst før eller etter, i formatet:
{{"tittel": "...", "ingress": "...", "avsnitt": [{{"arrangement_id": 1, "kategori": "...", "tekst": "..."}}]}}
"""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8192,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        return None, (
            f"Kallet til Claude feilet: {type(e).__name__}: {e}. "
            "Prøv igjen om litt — hvis det gjentar seg, kan det være en midlertidig feil hos "
            "Anthropic, eller at ANTHROPIC_API_KEY mangler gyldig kreditt/tilgang."
        )

    tekst = "".join(b.text for b in response.content if b.type == "text")
    match = re.search(r"\{.*\}", tekst, re.DOTALL)
    if not match:
        utdrag = tekst.strip()[:300]
        return None, (
            "Claude svarte, men svaret inneholdt ikke noe gjenkjennbart JSON-objekt. "
            f"Svar (utdrag): {utdrag!r}. Prøv igjen, eller prøv med en kortere/enklere "
            "stilinstruks — et svært langt eller uvanlig formulert instruks-felt kan noen "
            "ganger få Claude til å svare med forklarende tekst i stedet for bare JSON."
        )
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        utdrag = match.group(0)[:300]
        return None, (
            f"Claude svarte med noe som skulle være JSON, men det var ikke gyldig ({e}). "
            f"Utdrag: {utdrag!r}. Dette skjer typisk hvis svaret ble kuttet av fordi det ble "
            "for langt (prøv med færre valgte arrangementer, eller del opp i flere kortere "
            "perioder) — prøv igjen først, det er ofte forbigående."
        )
    if not isinstance(data, dict):
        return None, f"Claude svarte med gyldig JSON, men ikke et objekt (fikk {type(data).__name__}). Prøv igjen."

    tittel = str(data.get("tittel") or "").strip()
    ingress = str(data.get("ingress") or "").strip()
    if not tittel or not ingress:
        return None, (
            f"Claude sitt svar manglet tittel og/eller ingress (fikk feltene: "
            f"{', '.join(data.keys()) or 'ingen'}). Prøv igjen."
        )

    arrangement_pr_id = {a.id: a for a in sorterte}
    mottatt_pr_id: dict[int, dict] = {}
    for rad in data.get("avsnitt", []):
        if not isinstance(rad, dict):
            continue
        aid = rad.get("arrangement_id")
        innhold = str(rad.get("tekst") or "").strip()
        kategori = str(rad.get("kategori") or "").strip()
        if isinstance(aid, int) and aid in arrangement_pr_id and innhold:
            mottatt_pr_id[aid] = {"tekst": innhold, "kategori": kategori or "Annet"}

    avsnitt = []
    for a in sorterte:
        mottatt = mottatt_pr_id.get(a.id)
        if mottatt:
            avsnitt_tekst = mottatt["tekst"]
            kategori = mottatt["kategori"]
        else:
            # Reserveløsning: Claude hoppet over dette arrangementet — ta med enkel,
            # ikke-omskrevet faktatekst fremfor å miste det stille.
            tid = f" kl. {a.klokkeslett}" if a.klokkeslett else ""
            periode = f"{a.dato} – {a.til_dato}" if a.til_dato else a.dato
            avsnitt_tekst = f"**{a.tittel}** – {periode}{tid}, {a.sted}."
            kategori = "Annet"
        avsnitt.append({"arrangement_id": a.id, "tekst": avsnitt_tekst, "kategori": kategori})

    # Garanter minst MINSTE_KATEGORI_STORRELSE avsnitt bak enhver egen mellomtittel, uavhengig
    # av hvor mange (for) spesifikke kategorier Claude fant på — en kategori med færre enn det
    # slås sammen inn i "Annet" i stedet for å få sin egen, nesten tomme mellomtittel.
    kategori_antall = Counter(rad["kategori"] for rad in avsnitt)
    for rad in avsnitt:
        if kategori_antall[rad["kategori"]] < MINSTE_KATEGORI_STORRELSE:
            rad["kategori"] = "Annet"

    # Grupper etter kategori (barn/unge og kultur presset fremst), men behold kronologisk
    # rekkefølge innenfor hver gruppe (stabil sortering).
    kategori_forste_posisjon: dict[str, int] = {}
    for i, rad in enumerate(avsnitt):
        kategori_forste_posisjon.setdefault(rad["kategori"], i)
    avsnitt.sort(
        key=lambda rad: (_kategori_prioritet(rad["kategori"]), kategori_forste_posisjon[rad["kategori"]])
    )

    return {"tittel": tittel, "ingress": ingress, "avsnitt": avsnitt}, ""


def skriv_om_ett_avsnitt(a: Arrangement, instruks: str | None = None) -> str | None:
    """Skriver om ett enkelt avsnitt på nytt, typisk etter at 'hent mer informasjon' har gitt
    en rikere original_tekst for akkurat dette arrangementet. Returnerer teksten, eller None
    ved feil."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None

    instruks = instruks if instruks is not None else standard_artikkel_instruks()
    grunnlag = {
        "tittel": a.tittel,
        "dato": a.dato,
        "til_dato": a.til_dato,
        "klokkeslett": a.klokkeslett,
        "sted": a.sted,
        "arrangor": a.arrangor,
        "original_tekst": a.original_tekst,
    }
    client = Anthropic()
    prompt = f"""Du er journalist i lokalavisen Raumnes. Skriv ETT avsnitt (til en samleartikkel \
om kommende arrangementer) om dette arrangementet. "til_dato" er kun satt for flerdagers \
arrangementer — betyr at det varer fra "dato" til og med "til_dato"; fraser dette naturlig \
i teksten (f.eks. "fra 22. til 26. august").

{json.dumps(grunnlag, ensure_ascii=False, indent=2)}

{instruks}

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
    return tekst or None

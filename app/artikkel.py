import json
import os
import re
from datetime import date, datetime, timedelta

from anthropic import Anthropic

from app.harvest import UKEDAGER
from app.models import Arrangement

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

MAKS_TOKENS_ARTIKKEL = 8192

# Brukes med output_config.format i generer_hel_artikkel for å garantere at Claude sitt svar
# er syntaktisk gyldig JSON i nøyaktig denne formen — API-en validerer og håndhever formen
# server-side, i stedet for at koden må lete etter et JSON-objekt i fritekst og håpe at
# formatet stemmer (se historikk: dette feilet i praksis med en "Expecting ',' delimiter"-feil
# når svaret inneholdt et syntaksbrudd Claude selv innførte midt i en tekststreng).
_ARTIKKEL_SKJEMA = {
    "type": "object",
    "properties": {
        "tittel": {"type": "string"},
        "ingress": {"type": "string"},
        "avsnitt": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "arrangement_id": {"type": "integer"},
                    "tekst": {"type": "string"},
                },
                "required": ["arrangement_id", "tekst"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tittel", "ingress", "avsnitt"],
    "additionalProperties": False,
}


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


def _sikre_fet_navn(tekst: str, tittel: str) -> str:
    """Sikkerhetsnett: standard_artikkel_instruks ber allerede Claude fetstille arrangement-
    /artist-/lagnavnet i hvert avsnitt (se der), men dette er kun en tekstinstruks og følges
    ikke 100 % av gangene. "Kopier med formatering" (se markdownTilHtml i artikler.html) lenker
    det FØRSTE fete ordet til kildens URL — mangler fetstilingen helt, mister brukeren dermed
    både nøkkelordet og lenken for det avsnittet. Griper derfor bare inn når avsnittet ikke har
    noen fet skrift i det hele tatt: fetstiler første ordrette forekomst av tittelen i teksten
    hvis den finnes der, ellers stiller tittelen fremst i fet skrift."""
    if "**" in tekst or not tittel:
        return tekst
    treff = re.search(re.escape(tittel), tekst, re.IGNORECASE)
    if treff:
        return tekst[: treff.start()] + f"**{treff.group(0)}**" + tekst[treff.end() :]
    return f"**{tittel}** – {tekst}" if tekst else f"**{tittel}**"


def _dag_kategori(dato_str: str) -> str:
    """Mellomtittel for et avsnitt: kun ukedagsnavnet, f.eks. «Fredag» — beregnet i kode fra
    arrangementets faktiske dato, IKKE overlatt til Claude å gjette/formulere selv, slik at
    ukedagsnavnet alltid stemmer. To ulike dager med samme ukedagsnavn (f.eks. mandag i uke 29
    og mandag i uke 30) får bevisst samme mellomtittel-tekst — de vises likevel som to separate
    mellomtitler (ikke slått sammen til én), siden avsnitt-listen er kronologisk sortert og de
    da alltid skilles av andre ukedager i mellom (se _grupper_avsnitt_etter_kategori i main.py,
    som kun slår sammen SAMMENHENGENDE avsnitt med lik kategori)."""
    try:
        d = date.fromisoformat(dato_str)
    except (ValueError, TypeError):
        return "Annet"
    return UKEDAGER[d.weekday()].capitalize()


def generer_hel_artikkel(
    arrangementer: list[Arrangement], instruks: str | None = None
) -> tuple[dict | None, str]:
    """Skriver én artikkel for hele perioden, med ett avsnitt per arrangement, gruppert under
    mellomtitler per DAG (ukedag + dato, se _dag_kategori) i kronologisk rekkefølge.

    Kategoriseringen skjer utelukkende i kode, ut fra arrangementets faktiske dato — IKKE noe
    Claude blir bedt om å velge eller formulere selv, siden dagen allerede er kjent eksakt fra
    dataene. sorter_arrangementer sorterer allerede på (dato, klokkeslett), så avsnitt-listen
    grupperes naturlig per dag (dato er primær sorteringsnøkkel) uten noe eget grupperingssteg
    i etterkant.

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
            "flere_datoer": json.loads(a.flere_datoer) if a.flere_datoer else None,
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
"til_dato"; fraser dette naturlig i teksten (f.eks. "fra 22. til 26. august"). "flere_datoer" \
er satt i stedet for "til_dato" når arrangementet gjentas på bestemte datoer som IKKE \
nødvendigvis henger sammen dag for dag (f.eks. bare i helgene, eller bare på hverdager) — \
oppsummer da datoene naturlig ut fra mønsteret du ser (f.eks. "hver lørdag og søndag fra 15. \
til 30. august"), eller list dem opp hvis det ikke er noe tydelig mønster. IKKE fraser dette \
som én sammenhengende periode ("fra 15. til 30. august") — det ville gitt inntrykk av at det \
skjer hver eneste dag, når det egentlig bare er på de oppgitte datoene.

ARRANGEMENTER (i rekkefølgen artikkelen skal ha):
{json.dumps(grunnlag, ensure_ascii=False, indent=2)}

Skriv ÉN samlet artikkel som dekker alle arrangementene i listen. Du skal levere:
- "tittel": en fengende tittel for artikkelen
- "ingress": en kort ingress (2-3 setninger) som vinkler mot det mest spektakulære, \
eksklusive eller oppsiktsvekkende blant arrangementene i perioden. IKKE avslutt ingressen med \
en oppsummerende setning som «her er en oversikt over hva som skjer fra/i perioden ...» eller \
lignende, med eller uten datoer — det vises allerede separat andre steder
- "avsnitt": ett avsnitt PER arrangement i listen (samme antall, med riktig "arrangement_id") \
— ikke slå sammen flere arrangementer i ett avsnitt, og ikke hopp over noen.

For hvert avsnitt gjelder:
{instruks}
"""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAKS_TOKENS_ARTIKKEL,
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": _ARTIKKEL_SKJEMA},
            },
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        return None, (
            f"Kallet til Claude feilet: {type(e).__name__}: {e}. "
            "Prøv igjen om litt — hvis det gjentar seg, kan det være en midlertidig feil hos "
            "Anthropic, eller at ANTHROPIC_API_KEY mangler gyldig kreditt/tilgang."
        )

    if response.stop_reason == "refusal":
        return None, (
            "Claude avslo å generere artikkelen (av sikkerhetsårsaker). Prøv med en annen "
            "eller mindre uvanlig formulert stilinstruks."
        )
    if response.stop_reason == "max_tokens":
        return None, (
            "Svaret ble kuttet av fordi det ble for langt til å fullføres innenfor grensen. "
            "Prøv med færre valgte arrangementer, eller del opp perioden i flere kortere "
            "innhøstinger."
        )

    tekst = "".join(b.text for b in response.content if b.type == "text")
    if not tekst.strip():
        return None, "Claude svarte, men uten noe tekstinnhold. Prøv igjen."
    try:
        data = json.loads(tekst)
    except json.JSONDecodeError as e:
        # output_config.format garanterer normalt et syntaktisk gyldig svar i denne formen —
        # havner vi likevel her er det mest sannsynlig en midlertidig API-feil.
        utdrag = tekst.strip()[:300]
        return None, (
            f"Claude sitt svar kunne ikke tolkes som JSON ({e}), til tross for at svaret er "
            f"strukturert av API-en. Utdrag: {utdrag!r}. Dette er uvanlig — prøv igjen."
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
    tekst_pr_id: dict[int, str] = {}
    for rad in data.get("avsnitt", []):
        if not isinstance(rad, dict):
            continue
        aid = rad.get("arrangement_id")
        innhold = str(rad.get("tekst") or "").strip()
        if isinstance(aid, int) and aid in arrangement_pr_id and innhold:
            tekst_pr_id[aid] = innhold

    # sorterte er allerede sortert på (dato, klokkeslett) — avsnitt-listen bygges i samme
    # rekkefølge, så den grupperes naturlig per dag (og kronologisk innenfor hver dag) uten
    # noe eget grupperings-/sorteringssteg her.
    avsnitt = []
    for a in sorterte:
        innhold = tekst_pr_id.get(a.id)
        if innhold:
            avsnitt_tekst = _sikre_fet_navn(innhold, a.tittel)
        else:
            # Reserveløsning: Claude hoppet over dette arrangementet — ta med enkel,
            # ikke-omskrevet faktatekst fremfor å miste det stille.
            tid = f" kl. {a.klokkeslett}" if a.klokkeslett else ""
            periode = f"{a.dato} – {a.til_dato}" if a.til_dato else a.dato
            avsnitt_tekst = f"**{a.tittel}** – {periode}{tid}, {a.sted}."
        avsnitt.append({"arrangement_id": a.id, "tekst": avsnitt_tekst, "kategori": _dag_kategori(a.dato)})

    return {
        "tittel": tittel,
        "ingress": ingress,
        "avsnitt": avsnitt,
        # Faktisk antall output-tokens Claude brukte (inkl. egen resonnering), av grensen
        # MAKS_TOKENS_ARTIKKEL — vises i UI-et som andel, slik at brukeren kan vurdere om
        # perioden (antall dager/arrangementer) bør reduseres for å unngå at fremtidige
        # genereringer kuttes av (se stop_reason == "max_tokens" over).
        "token_brukt": response.usage.output_tokens,
        "token_maks": MAKS_TOKENS_ARTIKKEL,
    }, ""


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
        "flere_datoer": json.loads(a.flere_datoer) if a.flere_datoer else None,
        "klokkeslett": a.klokkeslett,
        "sted": a.sted,
        "arrangor": a.arrangor,
        "original_tekst": a.original_tekst,
    }
    client = Anthropic()
    prompt = f"""Du er journalist i lokalavisen Raumnes. Skriv ETT avsnitt (til en samleartikkel \
om kommende arrangementer) om dette arrangementet. "til_dato" er kun satt for flerdagers \
arrangementer — betyr at det varer fra "dato" til og med "til_dato"; fraser dette naturlig \
i teksten (f.eks. "fra 22. til 26. august"). "flere_datoer" er satt i stedet for "til_dato" \
når arrangementet gjentas på bestemte datoer som IKKE nødvendigvis henger sammen dag for dag \
(f.eks. bare i helgene) — oppsummer da naturlig ut fra mønsteret (f.eks. "hver lørdag og \
søndag fra 15. til 30. august") i stedet for å fremstille det som én sammenhengende periode.

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
    return _sikre_fet_navn(tekst, a.tittel) if tekst else None

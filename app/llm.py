import json
import os
import re

from anthropic import Anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")

DEFAULT_STED = "Nes kommune på Romerike i Akershus, Norge (ikke Nes i Hallingdal/Buskerud eller Nesodden)"


def foreslå_kilder(sted: str = DEFAULT_STED) -> list[dict]:
    """Bruker Claude med nettsøk til å foreslå kandidat-kilder for arrangementskalendere.

    Returnerer en liste med dicts: {"navn", "url", "begrunnelse"}. Tom liste ved feil
    eller manglende ANTHROPIC_API_KEY.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return []

    client = Anthropic()
    prompt = f"""Finn nettsteder som jevnlig publiserer oversikter over lokale arrangementer i {sted}.

Søk etter:
- kommunens egen arrangementskalender / hva-skjer-side
- kulturhus, bibliotek, ungdomsklubb
- idrettslag og idrettsråd i området
- frivilligsentral, menigheter, historielag
- lokalavisen Raumnes sin egen kalender (hvis den finnes)

Bare ta med nettsteder som faktisk dekker Nes kommune på Romerike spesifikt, ikke generelle \
regionale eller nasjonale kalendere. Flagg i begrunnelsen hvis du er usikker på geografisk \
relevans.

Svar til slutt KUN med et gyldig JSON-array, ingen tekst før eller etter. Hvert element skal ha:
- "navn": kort navn på nettstedet/kalenderen
- "url": direkte lenke til kalender- eller arrangementssiden (ikke bare forsiden hvis mulig)
- "begrunnelse": én kort setning om hvorfor dette er en relevant kilde

Eksempel: [{{"navn": "Nes kommune - Hva skjer", "url": "https://...", "begrunnelse": "..."}}]
"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}],
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": prompt}],
    )

    tekst = "".join(block.text for block in response.content if block.type == "text")
    match = re.search(r"\[.*\]", tekst, re.DOTALL)
    if not match:
        return []
    try:
        forslag = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []

    return [
        f
        for f in forslag
        if isinstance(f, dict) and f.get("navn") and f.get("url")
    ]

# vibecoding

## Raumnes arrangementer — jobb 1: Kildeliste

Kjør lokalt:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fyll inn APP_PASSORD (og ANTHROPIC_API_KEY for "oppdag nye kilder")
export $(cat .env | grep -v '^#' | xargs)
.venv/bin/uvicorn app.main:app --reload
```

Åpne http://127.0.0.1:8000 (brukernavn: verdien av `APP_BRUKER`, standard `raumnes`; passord: `APP_PASSORD`).

## Brukerroller: admin og journalist

Appen har to pålogginger med ulike rettigheter:

- **Admin** (`APP_BRUKER` / `APP_PASSORD`) — full tilgang, inkludert Kildeliste-siden
  (legge til/redigere/deaktivere kilder, "Undersøk kilde"-knappen).
- **Journalist** (`APP_BRUKER_JOURNALIST` / `APP_PASSORD_JOURNALIST`) — kun tilgang til
  Innhøsting og Artikler. Kildeliste-siden og alt som hører til den er skjult og sperret,
  selv ved direkte lenke.

Journalist-pålogging er **valgfri**. Så lenge `APP_BRUKER_JOURNALIST` og
`APP_PASSORD_JOURNALIST` ikke er satt, finnes det ingen journalist-innlogging i det hele
tatt — bare admin-passordet fungerer. Det er ikke noe hull: et tomt/usatt journalist-passord
gir aldri tilgang, uansett hva noen skriver inn.

### Sette opp eller endre journalist-bruker og -passord (Render)

Appen kjører på [Render](https://render.com), og miljøvariabler settes der — ikke i denne
koden, og ikke i `.env`-filen (den brukes kun til lokal utvikling på din egen maskin).

1. Logg inn på [dashboard.render.com](https://dashboard.render.com).
2. Klikk på tjenesten `raumnes-arrangementer` i listen.
3. Velg **Environment** i menyen til venstre.
4. For å **legge til** journalist-pålogging for første gang:
   - Klikk **Add Environment Variable**.
   - Key: `APP_BRUKER_JOURNALIST` — Value: ønsket brukernavn (f.eks. `journalist`).
   - Klikk **Add Environment Variable** på nytt.
   - Key: `APP_PASSORD_JOURNALIST` — Value: ønsket passord (velg noe du ikke bruker andre
     steder — det ligger lagret som ren tekst i Render sitt grensesnitt, tilgjengelig for
     alle med admin-tilgang til Render-kontoen).
5. For å **endre** et eksisterende passord eller brukernavn: finn raden med
   `APP_BRUKER_JOURNALIST` eller `APP_PASSORD_JOURNALIST` i lista, klikk på verdi-feltet,
   skriv inn den nye verdien, og bekreft.
6. Klikk **Save Changes** nederst. Render bygger og starter appen på nytt automatisk
   (tar vanligvis 1–2 minutter) — den nye påloggingen virker med én gang appen er oppe igjen.
7. For å **fjerne** journalist-tilgangen helt: slett begge de to miljøvariablene (søppelbøtte-
   ikonet ved siden av raden) og lagre. Da vil det ikke finnes noen journalist-pålogging
   lenger, kun admin.

Gi journalisten brukernavnet og passordet du valgte i steg 4/5, sammen med appens vanlige
nettadresse — de logger inn med akkurat samme innloggingsboks som admin, bare med andre
opplysninger, og ser automatisk den begrensede journalist-visningen.

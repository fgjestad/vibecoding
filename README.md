# Faglig tirsdag

Enkel kalenderapp for å planlegge Faglig tirsdag i Raumnes. Appen viser den tredje
tirsdagen i hver måned, og lar deg fylle inn tema og hvem som holder foredrag.

- **Alle kan se oversikten** uten passord.
- **Du logger inn** for å endre tema, foredragsholdere, tidspunkt og sted.
- **Datoen kan flyttes** — passer ikke tredje tirsdag, setter du en annen dag.
- **Måneder kan slettes** — for eksempel juli og desember. De kan hentes tilbake igjen.
- **Røde dager markeres i rødt**, og ferieperioder får et mykere varsel.
- **Google Kalender** kan abonnere på hele serien, eller du kan legge inn én og én dag.

Appen er en egen tjeneste, atskilt fra kalendergeneratoren: egen branch i dette
repoet (`claude/faglig-tirsdag-calendar-tzsm0p`) og en egen instans på Render. De to
deler ingenting annet enn git-repoet.

---

## Sette opp på Render

Dette gjør du én gang. Regn med et kvarter.

### 1. Opprett tjenesten

1. Logg inn på [dashboard.render.com](https://dashboard.render.com).
2. Trykk **New +** → **Web Service**.
3. Velg dette repoet (`fgjestad/vibecoding`). Ligger det ikke i lista, trykk
   **Configure account** og gi Render tilgang til repoet.
4. Fyll ut:
   - **Name:** `raumnes-faglig-tirsdag` *(må være noe annet enn kalendergeneratoren —
     navnet blir en del av nettadressen)*
   - **Branch:** `claude/faglig-tirsdag-calendar-tzsm0p` ← **viktig**, ikke `main`
   - **Region:** Frankfurt
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type:** Starter (se punkt 3 om hvorfor)

### 2. Miljøvariabler

Under **Environment** → **Add Environment Variable**:

| Nøkkel | Verdi |
| --- | --- |
| `ADMIN_PASSORD` | passordet du vil bruke for å logge inn |
| `SECRET_KEY` | en lang, tilfeldig streng (Render kan lage en med **Generate**) |
| `DATABASE_URL` | `sqlite:////data/data.db` *(fire skråstreker)* |
| `PYTHON_VERSION` | `3.12.7` |

`ADMIN_PASSORD` er det eneste passordet i appen. Uten det satt kan ingen logge inn,
og oversikten blir bare lesbar.

### 3. Disk — slik overlever dataene

Render sletter filsystemet hver gang tjenesten starter på nytt eller du legger ut en
ny versjon. Uten en fast disk forsvinner alt du har lagt inn.

Under **Disks** → **Add Disk**:

- **Name:** `data`
- **Mount Path:** `/data`
- **Size:** 1 GB

Disk krever Starter-planen (rundt 7 dollar i måneden). Vil du prøve på gratisplanen
først, hopp over disken og sett `DATABASE_URL` til `sqlite:///./data.db` — men da må
du regne med å miste innholdet ved hver omstart. Ta i så fall sikkerhetskopi ofte
(se lenger ned).

### 4. Trykk Create Web Service

Første bygg tar noen minutter. Når det står **Live**, ligger appen på
`https://raumnes-faglig-tirsdag.onrender.com` (eller det navnet du valgte).

Åpne den, trykk **Logg inn** og bruk passordet du satte i `ADMIN_PASSORD`.

> Alternativt kan du bruke **New +** → **Blueprint** og la Render lese `render.yaml`
> i dette repoet. Da settes alt opp automatisk, og du fyller bare inn `ADMIN_PASSORD`.
> Velg branchen over når Render spør.

### 5. Senere endringer

Hver gang det pushes til branchen `claude/faglig-tirsdag-calendar-tzsm0p`, bygger
Render appen på nytt av seg selv. Kalendergeneratoren står på sin egen branch og
merker ingenting.

---

## Google Kalender

Appen kobler seg aldri til Google-kontoen din. All kontakt skjer i nettleseren din,
og du velger selv hva som legges inn. Det er to måter:

**Abonner på hele serien (anbefalt).** Kopier adressen `/kalender.ics` (den står
ferdig på **Google Kalender**-siden i appen), åpne Google Kalender, trykk **+** ved
«Andre kalendere» → **Fra URL-adresse**, og lim inn. Da kommer alle samlingene inn,
og de oppdaterer seg av seg selv når noe endres. Google henter oppdateringer med noen
timers mellomrom, ikke med én gang.

**Legg inn én og én.** Knappen **Legg i Google Kalender** på hver dag åpner Google med
tema, tidspunkt og sted ferdig utfylt — du trykker bare **Lagre**. Dette virker med én
gang, men oppføringen oppdaterer seg ikke senere. Du må være innlogget i Google i
samme nettleser.

Bruker du Outlook eller Apple Kalender, laster du heller ned **.ics**-fila for dagen.

---

## Kjøre lokalt

```bash
pip install -r requirements.txt
cp .env.example .env        # fyll inn ADMIN_PASSORD
uvicorn app.main:app --reload
```

Appen ligger da på <http://127.0.0.1:8000>, og databasen havner i `data.db` i
prosjektmappa.

---

## Sikkerhetskopi

Når du er innlogget, ligger det en **Sikkerhetskopi**-boks nederst på oversikten.
**Last ned sikkerhetskopi** gir deg en JSON-fil med alt som er lagt inn, og **Les inn**
henter den tilbake. Måneder som ikke står i fila blir ikke rørt.

Kjører du uten fast disk på Render, er dette eneste måten å ta vare på innholdet på.

---

## Slik virker det

**Tredje tirsdag** regnes ut fra kalenderen, ikke fra databasen. Alle måneder finnes
derfor i oversikten fra dag én — også år fram i tid. En rad i databasen opprettes
først når du faktisk endrer eller sletter en måned.

**Sletting** fjerner dagen fra oversikten og kalenderfila, men raden blir liggende med
et merke. Det er derfor dagen ikke dukker opp igjen av seg selv, og derfor du kan
hente den tilbake.

**Røde dager** regnes ut i appen (`app/helligdager.py`), fra påskedagen og framover.
Ingen nettjeneste er involvert, så det virker like godt for 2040 som for i år. I
praksis er det bare 17. mai som kan treffe tredje tirsdag — neste gang er i 2033,
deretter 2039, 2044 og 2050.
Flytter du en dag manuelt til en rød dag, blir den også markert.

**Standardverdier** (klokkeslett og sted) settes nederst på oversikten når du er
innlogget. Alle dager der du ikke har satt noe eget, følger dem.

### Filene

| Fil | Innhold |
| --- | --- |
| `app/main.py` | Alle sidene og handlingene |
| `app/kalender.py` | Tredje tirsdag, ICS-fila og Google-lenkene |
| `app/helligdager.py` | Norske røde dager og ferievarsler |
| `app/models.py` | Databasetabellene |
| `app/auth.py` | De to brukernivåene |
| `app/templates/` | Sidene |
| `render.yaml` | Oppsettet på Render |

# møteskriver

Transkriberer kommunale møteopptak fra Flowplayer og skriver artikkelutkast
med kildeforankring tilbake til videoen.

**Status: v1.0 under arbeid.** Hele pipelinen kjører på mocks — uten GCP,
uten Flowplayer-token, uten ffmpeg. De eksterne koblingene er stubbet bak
grensesnitt og fylles inn når tilgangene er på plass.

```bash
npm install
npm run kjor -- 2eaf68dc-efe6-4a01-8838-614722592494
```

## Slik henger det sammen

```
Flowplayer video-ID  →  manifest via OVP-API
                     →  ffmpeg            → lyd.opus (mono 16 kHz)
                     →  ASR               → transkript.json   ← fasit
                     →  Claude            → talere, saksinndeling, artikkel
innkalling.pdf       →  Claude            → deltakere + sakskart + ordliste
```

Journalisten limer inn video-ID-en og laster opp innkallingen. To handlinger.

## Fire designvalg som bærer resten

**Lyden trekkes ut før opplasting.** All talegjenkjenning resampler internt
til 16 kHz mono, så alt utover det kastes uansett. En 4-timers video på 2 GB
blir ~43 MB Opus — 98 % mindre, og en opplasting som ikke ryker på 80 %.
Ligger lyden som egen rendisjon i HLS-manifesten, lastes videobytene aldri ned.

**Deltakerlista leses først, ikke sist.** Det intuitive er å bruke den til å
sette navn på talerne til slutt. Men navnelista er ordlista
talegjenkjenningen skal biases med — egennavn er nettopp det ASR bommer på, og
en deltakerliste er en ferdig kuratert liste over de navnene som blir sagt
hundre ganger i møtet. Samme dokument gir sakskartet gratis.

**`transkript.json` er eneste sannhet.** Produseres én gang, gjenbrukes av alle
senere steg. Journalisten kan kjøre fem forskjellige artikkelprompter mot
samme transkript uten å transkribere fire timer om igjen. Ord-nivå
tidsstempler og konfidens ligger der fra start — det er vondt å ettermontere,
og det er verifiseringsmekanismen hele produktet hviler på.

**Fakta og slutninger holdes fra hverandre.** `Roster` er et faktum om møtet.
`SpeakerMapping` er en slutning om hvem SPEAKER_03 er. Da kan slutningen
gjøres om igjen uten å røre faktaene, og i v2.0 byttes faktakilden til
automatisk henting uten at noe annet endres.

## Motorvalg

Alt eksternt ligger bak et grensesnitt med en mock, så pipelinen kan kjøres
og testes uten tilganger.

| | Runde 1 | Runde 2 |
|---|---|---|
| Talegjenkjenning | Gemini (Vertex) | Google Cloud Speech-to-Text |
| Tidsstempler | omtrentlige | målte, ord-nivå |
| Diarisering | nei | ja |

Gemini er valgt til runde 1 fordi den krever minst oppsett — ett kall, ingen
recognizer-config, ingen PhraseSet, samme prosjekt og autentisering som resten.
Byttet til Cloud STT er **planlagt arbeid, ikke en opsjon**: tidsstemplene er
journalistens verifiseringsverktøy, og de må være målt før dette møter en
publisert sak.

Claude gjør alt unntatt lyd-til-tekst — dokumentparsing, talermatching,
saksinndeling og artikkelskriving. Kjører via Vertex AI når `GCP_PROJECT` er
satt, ellers mot Claude API direkte.

## Google STT-sjekklista

Status per 2026-09-11, fra Googles dokumentasjon:

| Spørsmål | Svar |
|---|---|
| Modell | **`chirp_3`**, kun i Speech-to-Text **API v2** |
| Diarisering | Ja — men **bare i `BatchRecognize` og `Recognize`**, ikke streaming |
| Ord-tidsstempler | Ja, men må slås på eksplisitt — og Google varsler at det gir *noe* kvalitetstap |
| EU-region | **`eu` (multiregion) er GA.** `europe-west2`/`west3` er Preview. `europe-north1` er ikke nevnt |
| Norsk **med** diarisering | **Uavklart** — språklista er ikke gjengitt i kildene |

Vi er batch, så diariseringsbegrensningen treffer oss ikke. `eu` dekker
EU-kravet. Det ene som gjenstår er om norsk står på diariseringslista.

Det avgjøres ikke ved å lese mer dokumentasjon, men ved å spørre API-et:

```bash
./scripts/sjekk-google-stt.sh moete-utdrag.opus <gcp-prosjekt> eu
```

Skriptet ber om alt samtidig — norsk, `chirp_3`, diarisering og
ord-tidsstempler i EU. Går det gjennom, er alle spørsmålene besvart. Feiler
det, sier feilmeldingen hvilket krav som ikke holdt, og skriptet forklarer
hva du gjør videre.

Ti minutter lyd holder. Feilmodusene viser seg med én gang.

Faller det ut dårlig, er reserveløsningen NB-Whisper fra Nasjonalbiblioteket
på Cloud Run med GPU — den bryter ikke med Google-valget.

## Redaksjonelle regler som ligger i koden

- Ingen påstand uten `citation` med tidsstempel og ordrett sitat.
- Ingen taler får navn i en artikkel før et menneske har satt
  `confirmed: true`. Ubekreftede match sendes til modellen som «USIKKER», og
  systemprompten forbyr å tilskrive uttalelser til dem.
- En **innkalling** er skrevet før møtet og vet ikke hvem som kom;
  `present` settes til `null`, ikke gjettes. En **protokoll** vet det.
- Talere utenfor deltakerlista (innledere, eksterne, spørretimen) får alltid
  en fritekst-utvei. Det lukkede settet er et hjelpemiddel, ikke en tvangstrøye.

## Flowplayer

Oppslaget er implementert mot OVP API v3. Journalisten limer inn video-ID-en,
og `GET /v3/videos/{id}` gir tittel, varighet, mediefiler og kapittelmarkører.

```
FLOWPLAYER_API_KEY=...    # header: x-flowplayer-api-key
```

Av mediefilene velges HLS først — ligger lyden som egen rendisjon i manifesten,
laster ffmpeg kun lydsegmentene. Finnes ikke HLS, velges laveste bitrate: for
lyduttrekk er 240p like god som 1080p, men en brøkdel så stor.

Merk at API-et har en ratebegrensning på 1 forespørsel i sekundet (3 for
enterprise-organisasjoner).

### Flowplayers egen transkribering

Workspacet har `Enable transcriptions` og `Transcribe new videos automatically`
påslått med norsk som språk, så møteopptakene har som regel allerede et
transkript liggende som undertekst.

**Den brukes ikke som standard.** Erfaringen i redaksjonen er at kvaliteten er
for svak til å bygge journalistikk på, og den mangler dessuten
taleridentifikasjon og ord-nivå tidsstempler.

Den er implementert av én grunn: som gratis målestokk. Kjør samme møte med og
uten, og du har en konkret sammenligning mot en ekte ASR-motor i stedet for en
antakelse.

```bash
USE_EXISTING_SUBTITLES=true npm run kjor -- <video-id>
```

### Kapittelmarkører

`GET /v3/videos/{id}` returnerer `chapters` med tidspunkt og tittel. Merkes
møtene per sak i Flowplayer, er saksinndelingen allerede gjort av et menneske
— og den vektes tyngre enn noe systemet kan utlede fra transkriptet.

## Neste steg

- [ ] Verifiser Google STT-kombinasjonen over
- [x] Koble på Flowplayer OVP-API (`src/providers/video/flowplayer.ts`)
- [ ] Koble på Gemini (`src/providers/asr/gemini.ts`)
- [ ] Webapp: opplasting, navnebekreftelse, artikkelvisning med klikkbare kilder
- [ ] v2.0: hent saksdokumenter fra kommunens møtekalender som artikkelkontekst

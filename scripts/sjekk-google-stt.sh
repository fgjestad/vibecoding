#!/usr/bin/env bash
# Svarer på hele Google STT-sjekklista med ett kall.
#
#   ./scripts/sjekk-google-stt.sh <lydfil> <gcp-prosjekt> [region]
#
# I stedet for å lese dokumentasjon ber vi om alt vi trenger samtidig —
# norsk + diarisering + ord-tidsstempler + EU-region — og lar API-et si fra
# om kombinasjonen finnes. Går det gjennom, er alle fire spørsmål besvart ja.
# Feiler det, sier feilmeldingen hvilket krav som ikke holdt.
#
# Ti minutter lyd er nok. Feilmodusene viser seg med én gang, og du slipper
# å betale for fire timer før du vet om oppsettet duger.
set -euo pipefail

LYD="${1:?Bruk: $0 <lydfil> <gcp-prosjekt> [region]}"
PROSJEKT="${2:?Mangler GCP-prosjekt}"
REGION="${3:-eu}"                       # eu = multiregion, GA for chirp_3
SPRAK="${SPRAK:-nb-NO}"                 # prøv no-NO hvis nb-NO avvises
MODELL="${MODELL:-chirp_3}"
BOTTE="${BOTTE:-${PROSJEKT}-stt-probe}"

command -v gcloud >/dev/null || { echo "Mangler gcloud. Installer Google Cloud SDK."; exit 1; }
[[ -f "$LYD" ]] || { echo "Fant ikke $LYD"; exit 1; }

TOKEN="$(gcloud auth print-access-token)" || {
  echo "Ikke innlogget. Kjør: gcloud auth login && gcloud auth application-default login"; exit 1; }

echo "==> Prosjekt: $PROSJEKT   Region: $REGION   Modell: $MODELL   Språk: $SPRAK"

# --- Lyden må ligge i GCS for batch-gjenkjenning ------------------------
gcloud storage buckets describe "gs://$BOTTE" >/dev/null 2>&1 || {
  echo "==> Lager bøtte gs://$BOTTE i $REGION ..."
  gcloud storage buckets create "gs://$BOTTE" --project="$PROSJEKT" --location="$REGION"
}
NAVN="probe-$(date +%s)-$(basename "$LYD")"
echo "==> Laster opp $LYD ..."
gcloud storage cp "$LYD" "gs://$BOTTE/$NAVN" --quiet

# --- Be om alt på én gang ----------------------------------------------
VERT="${REGION}-speech.googleapis.com"
echo "==> Ber om transkripsjon med diarisering OG ord-tidsstempler ..."

SVAR="$(curl -sS -X POST \
  "https://${VERT}/v2/projects/${PROSJEKT}/locations/${REGION}/recognizers/_:batchRecognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "x-goog-user-project: $PROSJEKT" \
  -H "Content-Type: application/json" \
  -d @- <<JSON
{
  "config": {
    "model": "$MODELL",
    "languageCodes": ["$SPRAK"],
    "autoDecodingConfig": {},
    "features": {
      "enableWordTimeOffsets": true,
      "enableWordConfidence": true,
      "diarizationConfig": { "minSpeakerCount": 2, "maxSpeakerCount": 15 }
    }
  },
  "files": [{ "uri": "gs://$BOTTE/$NAVN" }],
  "recognitionOutputConfig": { "inlineResponseConfig": {} }
}
JSON
)"

echo "$SVAR" | head -c 3000
echo

if echo "$SVAR" | grep -qi '"error"'; then
  cat <<'HJELP'

── Forespørselen ble avvist ───────────────────────────────────────────
Les feilteksten over. De vanligste årsakene, og hva du gjør:

  "model ... not supported ... location"
      Modellen finnes ikke i denne regionen. Prøv:
        ./scripts/sjekk-google-stt.sh <lyd> <prosjekt> us
      Må du til US for å få norsk med diarisering, er det en avklaring
      for Amedia, ikke en teknisk detalj.

  "language ... not supported"
      Prøv det andre språkkoden:  SPRAK=no-NO ./scripts/sjekk-google-stt.sh ...

  "diarization ... not supported"
      Dette er svaret vi fryktet. Da må reserveløsningen fram:
      NB-Whisper + egen diarisering.

  "API has not been used ... disabled"
      Slå på API-et:
        gcloud services enable speech.googleapis.com --project=<prosjekt>
HJELP
  exit 1
fi

echo "
── Gikk gjennom ───────────────────────────────────────────────────────
Da er alle fire spørsmål besvart for denne kombinasjonen:
  norsk ($SPRAK) + $MODELL + diarisering + ord-tidsstempler i '$REGION'.

Se etter i svaret over:
  speakerLabel   → diariseringen virker
  startOffset    → ord-nivå tidsstempler finnes
  confidence     → konfidens per ord finnes

Mangler noen av dem, ble feltet stilltiende ignorert — og da er det den
funksjonen som ikke støttes, selv om kallet gikk gjennom.

Til slutt: les transkriptet. Er navn og dialekt gjenkjennelig, har vi en
motor. Er det ikke det, er det NB-Whisper som gjelder."

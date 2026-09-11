#!/usr/bin/env bash
# Henter kun lydsporet fra et strømmet kommunalt møte og koder det til Opus.
#
#   ./hent-lyd.sh 2eaf68dc-efe6-4a01-8838-614722592494        # bare video-ID
#   ./hent-lyd.sh <embed-url|m3u8-url> [utfil.opus]
#   ./hent-lyd.sh --probe <id|url>                            # bare undersøk, last ikke ned
#
# Miljøvariabler:
#   PI=<uuid>        publisher-id for kommunen (prøver uten først hvis usatt)
#   REFERER=<url>    hvis CDN-en krever Referer på segmentene
#   BITRATE=24k      Opus-bitrate
set -euo pipefail

PROBE=0
[[ "${1:-}" == "--probe" ]] && { PROBE=1; shift; }
INN="${1:?Bruk: ./hent-lyd.sh <video-id|embed-url|m3u8-url> [utfil.opus]}"
UT="${2:-lyd.opus}"
PI="${PI:-}"
REFERER="${REFERER:-}"
BITRATE="${BITRATE:-24k}"
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'

# --- 0. Bar UUID? Bygg embed-URL ----------------------------------------
if [[ "$INN" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
  BASE="https://ljsp.lwcdn.com/api/video/embed.jsp?id=$INN"
  if [[ -n "$PI" ]]; then
    KANDIDATER=("$BASE&pi=$PI" "$BASE")
  else
    KANDIDATER=("$BASE")          # test om pi i det hele tatt trengs
    echo "==> Ingen PI satt - prøver med bare video-ID"
  fi
else
  KANDIDATER=("$INN")
fi

# --- 1. Finn manifesten -------------------------------------------------
finn_manifest() {
  local u="$1"
  [[ "$u" == *.m3u8* || "$u" == *.mp4* ]] && { echo "$u"; return 0; }
  if command -v yt-dlp >/dev/null; then
    yt-dlp -g -f 'ba/worst' "$u" 2>/dev/null | head -1 && return 0
  fi
  curl -sSL --max-time 30 -A "$UA" "$u" 2>/dev/null \
    | grep -oE 'https?://[^"'"'"' \\]+\.m3u8[^"'"'"' \\]*' | head -1
}

STREAM=""
for k in "${KANDIDATER[@]}"; do
  echo "==> Prøver: $k"
  STREAM="$(finn_manifest "$k" || true)"
  [[ -n "$STREAM" ]] && { KILDE="$k"; break; }
done

if [[ -z "$STREAM" ]]; then
  echo "!! Fant ingen manifest. Dump av hva embed-siden refererer til:"
  curl -sSL --max-time 30 -A "$UA" "${KANDIDATER[0]}" 2>/dev/null \
    | grep -oE 'https?://[^"'"'"' \\]{10,160}' | sort -u | head -40
  echo "   Finn riktig URL i DevTools -> Network (filtrer m3u8 eller json) og send den inn direkte."
  exit 1
fi
echo "    manifest: $STREAM"
echo "    via:      $KILDE"

HDR=()
[[ -n "$REFERER" ]] && HDR=(-headers "Referer: $REFERER"$'\r\n')

# --- 2. Hva ligger der? -------------------------------------------------
echo "==> Spor i manifesten:"
ffprobe -hide_banner -v error "${HDR[@]}" \
        -show_entries stream=index,codec_type,codec_name,channels,sample_rate,bit_rate \
        -show_entries format=duration -of default=noprint_wrappers=1 "$STREAM" || true

[[ $PROBE -eq 1 ]] && { echo "==> --probe: stopper her."; exit 0; }
command -v ffmpeg >/dev/null || { echo "Mangler ffmpeg."; exit 1; }

# --- 3. Trekk ut lyd ----------------------------------------------------
echo "==> Trekker ut lyd -> $UT (Opus $BITRATE mono 16 kHz) ..."
START=$(date +%s)
ffmpeg -hide_banner -loglevel warning -stats "${HDR[@]}" \
  -i "$STREAM" -vn -sn -dn -map 0:a:0 \
  -ac 1 -ar 16000 -c:a libopus -b:a "$BITRATE" -application voip \
  -y "$UT"
BRUKT=$(( $(date +%s) - START ))

SEK=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$UT" | cut -d. -f1)
MB=$(echo "scale=1; $(wc -c < "$UT")/1048576" | bc -l)
printf '\n==> Ferdig på %d min %d sek\n' $((BRUKT/60)) $((BRUKT%60))
printf '    Lengde:    %d:%02d:%02d\n' $((SEK/3600)) $(((SEK%3600)/60)) $((SEK%60))
printf '    Størrelse: %s MB\n' "$MB"

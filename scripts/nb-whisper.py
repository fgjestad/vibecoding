#!/usr/bin/env python3
"""
Kjører NB-Whisper lokalt og skriver ut transkriptet i appens eget format.

    python scripts/nb-whisper.py lyd.opus > fixtures/nb-whisper.json

NB-Whisper er Nasjonalbibliotekets finetuning av Whisper på norsk, blant
annet på NRK-materiale. Den er det eneste alternativet som er *bygget* for
norsk dialekt i stedet for å støtte norsk blant hundre andre språk.

To ting den gir som skymotorene ikke nødvendigvis gir:
  - ord-nivå tidsstempler som er MÅLT, ikke anslått
  - ingen kostnad per møte, og ingen som kan sperre tilgangen din

Det den ikke gir, er taleridentifikasjon. Den må løses separat (pyannote),
og er ikke med her.

Førstegangsoppsett (Windows, i cmd eller PowerShell):

    pip install faster-whisper ctranslate2 transformers
    ct2-transformers-converter --model NbAiLab/nb-whisper-large ^
        --output_dir nb-whisper-large-ct2 --quantization int8

Konverteringen lastes ned én gang (noen GB) og tar noen minutter.
Har du ikke NVIDIA-kort, bruk --modell nb-whisper-small-ct2 i stedet, eller
regn med at fire timer tar natta. Til en kvalitetstest holder ti minutter.
"""
import argparse
import json
import sys


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("lydfil")
    p.add_argument("--modell", default="nb-whisper-large-ct2",
                   help="Mappe fra ct2-transformers-converter")
    p.add_argument("--sprak", default="no")
    args = p.parse_args()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("Mangler faster-whisper. Kjør: pip install faster-whisper",
              file=sys.stderr)
        return 1

    enhet, presisjon = velg_enhet()
    print(f"Kjører på {enhet} ({presisjon}) ...", file=sys.stderr)

    modell = WhisperModel(args.modell, device=enhet, compute_type=presisjon)

    # word_timestamps er hele poenget: MÅLTE ordtider, ikke anslåtte.
    # Uten dem kan ikke journalisten klikke et sitat og havne på sekundet.
    segmenter, info = modell.transcribe(
        args.lydfil,
        language=args.sprak,
        word_timestamps=True,
        vad_filter=True,          # hopper over stillhet – sparer mye tid
    )

    ut = []
    for s in segmenter:
        # Framdrift til stderr, så stdout holdes rent for JSON.
        print(f"  [{s.start:7.1f}s] {s.text[:70]}", file=sys.stderr)
        ut.append({
            "start": round(s.start, 2),
            "end": round(s.end, 2),
            # NB-Whisper skiller ikke talere. Vi later ikke som.
            "speaker": None,
            "text": s.text.strip(),
            "words": [
                {
                    "w": w.word.strip(),
                    "start": round(w.start, 2),
                    "end": round(w.end, 2),
                    "conf": round(w.probability, 3),
                }
                for w in (s.words or [])
            ],
        })

    json.dump({
        "language": info.language,
        "engine": "nb-whisper",
        "engineVersion": args.modell,
        # Målte ordtider – i motsetning til Gemini, som anslår dem.
        "timestampQuality": "exact",
        "durationSec": round(ut[-1]["end"], 2) if ut else 0,
        "segments": ut,
    }, sys.stdout, ensure_ascii=False, indent=2)
    return 0


def velg_enhet() -> tuple[str, str]:
    """NVIDIA-kort hvis det finnes, ellers CPU med kraftigere kvantisering."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda", "int8_float16"
    except ImportError:
        pass
    return "cpu", "int8"


if __name__ == "__main__":
    sys.exit(main())

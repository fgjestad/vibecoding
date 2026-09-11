import type { Segment, Transcript } from "../types.js";

/**
 * Bruker transkriptet Flowplayer allerede har laget.
 *
 * Amedia har «Transcribe new videos automatically» påslått med norsk som
 * språk, så møteopptakene har som regel et ferdig transkript liggende som
 * undertekst. Da trenger vi verken lyduttrekk eller egen talegjenkjenning
 * for å komme i gang.
 *
 * Begrensningene er reelle og må vises fram i grensesnittet:
 *   - Ingen taleridentifikasjon. Undertekster sier hva som ble sagt, ikke
 *     av hvem. Talermatching må gjøres fra konteksten i stedet.
 *   - Tidsstempler per replikk, ikke per ord. Godt nok til å hoppe til
 *     riktig sted i videoen, for grovt til å markere enkeltord som usikre.
 *
 * Derfor er dette runde 1, ikke endestasjon.
 */
export function parseVtt(vtt: string): Segment[] {
  const segments: Segment[] = [];
  // \r\n fra noen generatorer, og BOM forekommer.
  const linjer = vtt.replace(/^﻿/, "").split(/\r?\n/);

  let i = 0;
  while (i < linjer.length) {
    const linje = linjer[i]!;

    const tid = parseTimingLine(linje);
    if (!tid) {
      i++;
      continue;
    }

    // Teksten er alt fram til neste blanke linje.
    const tekstlinjer: string[] = [];
    i++;
    while (i < linjer.length && linjer[i]!.trim() !== "") {
      tekstlinjer.push(linjer[i]!);
      i++;
    }

    const { speaker, text } = stripVoiceTag(tekstlinjer.join(" "));
    if (text) {
      segments.push({
        start: tid.start,
        end: tid.end,
        speaker,
        text,
        // Undertekster har ingen ord-nivå oppløsning. Vi later ikke som.
        words: [],
      });
    }
  }

  return segments;
}

function parseTimingLine(linje: string): { start: number; end: number } | null {
  const m = linje.match(
    /^\s*(\d{1,3}:)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,3}:)?(\d{1,2}):(\d{2})[.,](\d{1,3})/,
  );
  if (!m) return null;
  return {
    start: tilSekunder(m[1], m[2]!, m[3]!, m[4]!),
    end: tilSekunder(m[5], m[6]!, m[7]!, m[8]!),
  };
}

function tilSekunder(t: string | undefined, m: string, s: string, ms: string): number {
  const timer = t ? parseInt(t, 10) : 0;
  // "5" betyr 500 ms, ikke 5 ms.
  const millis = parseInt(ms.padEnd(3, "0"), 10);
  return timer * 3600 + parseInt(m, 10) * 60 + parseInt(s, 10) + millis / 1000;
}

/** WebVTT kan merke taler som <v Kari Nordmann>tekst</v>. */
function stripVoiceTag(raw: string): { speaker: string | null; text: string } {
  const m = raw.match(/^\s*<v\s+([^>]+)>(.*)$/s);
  const speaker = m ? m[1]!.trim() : null;
  const body = m ? m[2]! : raw;
  return {
    speaker,
    text: body
      .replace(/<\/?[^>]+>/g, "")  // gjenværende tagger
      .replace(/\s+/g, " ")
      .trim(),
  };
}

export async function transcriptFromSubtitleUrl(
  url: string,
  language: string,
  durationSec: number | null,
): Promise<Transcript> {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`Klarte ikke hente undertekst (${res.status}) fra ${url}`);
  }
  const segments = parseVtt(await res.text());
  if (segments.length === 0) {
    throw new Error("Underteksten inneholdt ingen replikker.");
  }

  return {
    language,
    engine: "flowplayer-subtitles",
    engineVersion: "vtt",
    timestampQuality: "approximate",
    durationSec: durationSec ?? segments[segments.length - 1]!.end,
    segments,
  };
}

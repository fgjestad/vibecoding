import { readFile } from "node:fs/promises";
import type { AudioArtifact, Segment, Transcript } from "../../types.js";
import type { ASRProvider, TranscribeOptions } from "./index.js";

const BASE = "https://api.assemblyai.com/v2";

/** AssemblyAI oppgir alle tider i millisekunder. Vi regner i sekunder. */
const msTilSek = (ms: number): number => Math.round(ms) / 1000;

interface AaiWord {
  text: string;
  start: number;
  end: number;
  confidence: number;
  speaker?: string | null;
}

interface AaiTranscript {
  id: string;
  status: "queued" | "processing" | "completed" | "error";
  error?: string;
  language_code?: string;
  audio_duration?: number;
  text?: string;
  words?: AaiWord[];
  utterances?: (AaiWord & { words?: AaiWord[] })[];
}

/**
 * AssemblyAI – den utskiftbare ASR-en for runde 1 i produksjon.
 *
 * Valgt fordi den er selvbetjent (ingen IT-avdeling å vente på),
 * har taleridentifikasjon i samme kall, tar fire timer uten at fila må
 * deles opp, og gir målte ord-tidsstempler med konfidens.
 *
 * Ordlista fra deltakerlista sendes som `word_boost`. Det er en dedikert
 * mekanisme, ikke en fritekst-prompt, så navnene hallusineres ikke inn der
 * de ikke ble sagt – samme grunn som gjorde Googles PhraseSet attraktiv.
 */
export class AssemblyAIASR implements ASRProvider {
  readonly name = "assemblyai";
  readonly supportsDiarization = true;
  readonly timestampQuality = "exact" as const;

  constructor(
    private cfg: { apiKey: string; pollMs?: number; maxWaitMs?: number },
  ) {}

  private get headers() {
    // Nøkkelen sendes rå, uten "Bearer".
    return { authorization: this.cfg.apiKey };
  }

  async transcribe(
    audio: AudioArtifact,
    opts: TranscribeOptions,
  ): Promise<Transcript> {
    const audioUrl = await this.upload(audio.path);
    const jobb = await this.submit(audioUrl, opts);
    const ferdig = await this.poll(jobb.id);
    return this.tilTranscript(ferdig, opts.language);
  }

  /** Laster opp lydfila og får en midlertidig URL tilbake. */
  private async upload(path: string): Promise<string> {
    const res = await fetch(`${BASE}/upload`, {
      method: "POST",
      headers: { ...this.headers, "content-type": "application/octet-stream" },
      body: await readFile(path),
    });
    if (!res.ok) throw new Error(await forklar(res, "opplasting"));
    return ((await res.json()) as { upload_url: string }).upload_url;
  }

  private async submit(
    audioUrl: string,
    opts: TranscribeOptions,
  ): Promise<AaiTranscript> {
    const res = await fetch(`${BASE}/transcript`, {
      method: "POST",
      headers: { ...this.headers, "content-type": "application/json" },
      body: JSON.stringify({
        audio_url: audioUrl,
        language_code: kortSprak(opts.language),
        speaker_labels: opts.diarize,
        word_boost: boostliste(opts.vocabulary),
        boost_param: "high",
        punctuate: true,
        format_text: true,
      }),
    });
    if (!res.ok) throw new Error(await forklar(res, "oppstart"));
    return (await res.json()) as AaiTranscript;
  }

  /** Venter til jobben er ferdig. Fire timer lyd tar typisk noen minutter. */
  private async poll(id: string): Promise<AaiTranscript> {
    const intervall = this.cfg.pollMs ?? 5000;
    const frist = Date.now() + (this.cfg.maxWaitMs ?? 2 * 60 * 60 * 1000);

    while (Date.now() < frist) {
      const res = await fetch(`${BASE}/transcript/${id}`, { headers: this.headers });
      if (!res.ok) throw new Error(await forklar(res, "statussjekk"));
      const t = (await res.json()) as AaiTranscript;

      if (t.status === "completed") return t;
      if (t.status === "error") {
        throw new Error(`AssemblyAI feilet: ${t.error ?? "ukjent grunn"}`);
      }
      await new Promise((r) => setTimeout(r, intervall));
    }
    throw new Error(
      `Transkriberingen ble ikke ferdig innen fristen. Jobb-ID: ${id} – ` +
        "den kjører videre hos AssemblyAI og kan hentes senere.",
    );
  }

  private tilTranscript(t: AaiTranscript, sprak: string): Transcript {
    return {
      language: t.language_code ?? sprak,
      engine: this.name,
      engineVersion: "v2",
      timestampQuality: this.timestampQuality,
      durationSec: t.audio_duration ?? 0,
      segments: tilSegmenter(t),
    };
  }
}

/**
 * Bygger segmentene.
 *
 * Med taleridentifikasjon på returnerer AssemblyAI `utterances` – ytringer
 * allerede gruppert per taler, som er nøyaktig formen vi vil ha. Uten
 * diarisering finnes bare `words`, og da må vi gruppere selv.
 */
export function tilSegmenter(t: AaiTranscript): Segment[] {
  if (t.utterances?.length) {
    return t.utterances.map((u) => ({
      start: msTilSek(u.start),
      end: msTilSek(u.end),
      speaker: u.speaker ? `SPEAKER_${u.speaker}` : null,
      text: u.text.trim(),
      words: (u.words ?? []).map(tilOrd),
    }));
  }

  // Uten diarisering: del på setningsslutt, så segmentene blir lesbare.
  const segmenter: Segment[] = [];
  let buffer: AaiWord[] = [];
  for (const w of t.words ?? []) {
    buffer.push(w);
    if (/[.!?]$/.test(w.text)) {
      segmenter.push(samle(buffer));
      buffer = [];
    }
  }
  if (buffer.length) segmenter.push(samle(buffer));
  return segmenter;
}

function samle(ord: AaiWord[]): Segment {
  return {
    start: msTilSek(ord[0]!.start),
    end: msTilSek(ord[ord.length - 1]!.end),
    speaker: null,
    text: ord.map((w) => w.text).join(" "),
    words: ord.map(tilOrd),
  };
}

const tilOrd = (w: AaiWord) => ({
  w: w.text,
  start: msTilSek(w.start),
  end: msTilSek(w.end),
  conf: w.confidence,
});

/** "no-NO" → "no". AssemblyAI vil ha ren ISO-639-1. */
export function kortSprak(bcp47: string): string {
  return bcp47.split("-")[0]!.toLowerCase();
}

/**
 * Ordlista har grenser: maks 1000 oppføringer, og hver frase maks 6 ord.
 * Vi kutter i stedet for å la API-et avvise hele kallet.
 */
export function boostliste(ord: string[]): string[] {
  return [...new Set(ord)]
    .filter((o) => o.trim().split(/\s+/).length <= 6)
    .slice(0, 1000);
}

async function forklar(res: Response, steg: string): Promise<string> {
  const kropp = await res.text().catch(() => "");
  if (res.status === 401) {
    return `AssemblyAI avviste nøkkelen (${steg}). Sjekk ASSEMBLYAI_API_KEY.`;
  }
  if (res.status === 429) {
    return `For mange forespørsler mot AssemblyAI (${steg}). Vent litt.`;
  }
  return `AssemblyAI svarte ${res.status} under ${steg}. ${kropp.slice(0, 300)}`;
}

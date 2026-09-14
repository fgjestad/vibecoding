import type { ASRProvider } from "../providers/asr/index.js";
import type { AudioArtifact, Roster, Transcript } from "../types.js";
import { vocabularyFrom } from "./roster.js";

/**
 * Kjører talegjenkjenningen, med ordlista fra deltakerlista som bias.
 *
 * Motoren er utskiftbar med vilje: runde 1 er Gemini (minst oppsett),
 * runde 2 er Google STT (målte tidsstempler og ekte diarisering).
 * Resten av systemet merker ikke byttet.
 */
export async function transcribe(
  asr: ASRProvider,
  audio: AudioArtifact | undefined,
  roster: Roster | undefined,
  mediaUrl?: string,
): Promise<Transcript> {
  const opts = {
    language: "no-NO",
    vocabulary: roster ? vocabularyFrom(roster) : [],
    diarize: asr.supportsDiarization,
  };

  const t = mediaUrl && asr.transcribeUrl
    ? await asr.transcribeUrl(mediaUrl, opts)
    : await asr.transcribe(audio!, opts);

  // Motoren er sannhetskilden for hva tidsstemplene er verdt – ikke fixturen.
  return { ...t, engine: asr.name, timestampQuality: asr.timestampQuality };
}

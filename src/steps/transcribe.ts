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
  audio: AudioArtifact,
  roster: Roster | undefined,
): Promise<Transcript> {
  const vocabulary = roster ? vocabularyFrom(roster) : [];

  const t = await asr.transcribe(audio, {
    language: "no-NO",
    vocabulary,
    diarize: asr.supportsDiarization,
  });

  // Motoren er sannhetskilden for hva tidsstemplene er verdt – ikke fixturen.
  return { ...t, engine: asr.name, timestampQuality: asr.timestampQuality };
}

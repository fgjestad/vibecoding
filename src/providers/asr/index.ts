import type { AudioArtifact, Transcript } from "../../types.js";

export interface TranscribeOptions {
  /** BCP-47, "no-NO" for norsk bokmål. */
  language: string;
  /**
   * Ordlista fra deltakerlista: politikernavn, utvalgsnavn, stedsnavn.
   * Dette er den største enkeltspaken på kvalitet – egennavn er nettopp det
   * talegjenkjenning bommer på.
   *
   * NB: hold lista til navn og institusjonsbegreper. Dumper du hele
   * innkallingen inn her, hallusinerer Whisper-baserte modeller navn som
   * aldri ble sagt. Google STT har en dedikert PhraseSet-mekanisme som ikke
   * har det problemet; Gemini tar den som prompt og er mer utsatt.
   */
  vocabulary: string[];
  /** Be om taleridentifikasjon. Ikke alle motorer støtter det. */
  diarize: boolean;
}

export interface ASRProvider {
  readonly name: string;
  /** Sier fra om motoren faktisk kan skille talere, uavhengig av hva som bes om. */
  readonly supportsDiarization: boolean;
  /** Om tidsstemplene kan stoles på ned til ordet, eller bare omtrentlig. */
  readonly timestampQuality: "exact" | "approximate";
  transcribe(audio: AudioArtifact, opts: TranscribeOptions): Promise<Transcript>;
}

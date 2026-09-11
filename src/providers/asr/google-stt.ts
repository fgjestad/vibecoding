import type { AudioArtifact, Transcript } from "../../types.js";
import type { ASRProvider, TranscribeOptions } from "./index.js";

/**
 * Runde 2-motoren: Google Cloud Speech-to-Text, batch mot GCS.
 *
 * Dette er motoren produktet skal ende på, fordi den gir de to tingene
 * Gemini ikke gir: målte ord-nivå tidsstempler og akustisk diarisering.
 * PhraseSet-mekanismen tar dessuten ordlista fra deltakerlista uten
 * hallusineringsrisikoen en fritekst-prompt har.
 *
 * FØR DETTE IMPLEMENTERES – verifiser at denne kombinasjonen finnes:
 *
 *   1. Hvilken modellvariant dekker norsk (no-NO / nb-NO) i batch-modus?
 *   2. Støtter den varianten diarisering?
 *   3. Gir den ord-nivå tidsstempler og konfidens?
 *   4. Kjører den i europe-north1, eller tvinges vi til en US-region?
 *
 * Hvert krav er dekket for seg. Det er skjæringspunktet som må bekreftes,
 * og det avgjør om reserveløsningen (NB-Whisper på Cloud Run med GPU) må
 * hentes fram. Bruk `npm run kjor -- probe` når tilgangene er på plass.
 */
export class GoogleSTT implements ASRProvider {
  readonly name = "google-stt";
  readonly supportsDiarization = true;
  readonly timestampQuality = "exact" as const;

  constructor(
    private cfg: {
      projectId: string;
      region: string;
      recognizer: string;
      gcsBucket: string;
    },
  ) {}

  async transcribe(
    audio: AudioArtifact,
    opts: TranscribeOptions,
  ): Promise<Transcript> {
    void audio;
    void opts;
    throw new Error(
      "GoogleSTT er ikke implementert ennå – se sjekklista øverst i filen. " +
        `Konfigurert: ${this.cfg.projectId}/${this.cfg.region}.`,
    );
  }
}

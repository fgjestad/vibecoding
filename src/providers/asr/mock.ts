import { readFile } from "node:fs/promises";
import type { AudioArtifact, Transcript } from "../../types.js";
import type { ASRProvider, TranscribeOptions } from "./index.js";

/**
 * Leser et ferdig transkript fra disk i stedet for å kalle en motor.
 *
 * Finnes for at hele pipelinen skal kunne kjøres og testes uten nettverk,
 * uten GCP-tilgang og uten kostnad. Det er denne som gjør at resten av
 * systemet kan bygges ferdig før tilgangene er på plass.
 */
export class MockASR implements ASRProvider {
  readonly name = "mock";
  readonly supportsDiarization = true;
  readonly timestampQuality = "exact" as const;

  constructor(private fixturePath: string) {}

  async transcribe(
    _audio: AudioArtifact,
    _opts: TranscribeOptions,
  ): Promise<Transcript> {
    const raw = await readFile(this.fixturePath, "utf8");
    return JSON.parse(raw) as Transcript;
  }
}

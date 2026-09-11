import type { AudioArtifact, Transcript } from "../../types.js";
import type { ASRProvider, TranscribeOptions } from "./index.js";

/**
 * Runde 1-motoren: Gemini via Vertex AI.
 *
 * Valgt fordi den krever minst oppsett – ett kall, ingen recognizer-config,
 * ingen PhraseSet, ingen batch-jobb mot GCS – og fordi den bruker samme
 * prosjekt og autentisering som resten av stacken.
 *
 * To begrensninger som er årsaken til at dette er runde 1 og ikke endestasjon:
 *
 *   1. Tidsstemplene er omtrentlige. Modellen anslår dem, den måler dem ikke.
 *      Siden tidsstemplene er hele verifiseringsmekanismen for en journalist,
 *      må dette byttes til Google STT før produkt.
 *   2. Ingen akustisk diarisering. Modellen kan gjette talerbytter fra
 *      konteksten ("takk, ordfører"), men det er gjetning.
 *
 * Fire timer lyd ≈ 460 000 tokens og går inn i kontekstvinduet i én jafs,
 * så oppdeling er ikke nødvendig for møter av normal lengde.
 *
 * IKKE VERIFISERT: request-formen under er ikke kjørt mot Vertex ennå – den
 * må sjekkes mot gjeldende SDK-dokumentasjon før bruk. Alt som er spesifikt
 * for Google ligger isolert i denne ene filen nettopp derfor.
 */
export class GeminiASR implements ASRProvider {
  readonly name = "gemini";
  readonly supportsDiarization = false;
  readonly timestampQuality = "approximate" as const;

  constructor(
    private cfg: { projectId: string; region: string; model: string },
  ) {}

  async transcribe(
    audio: AudioArtifact,
    opts: TranscribeOptions,
  ): Promise<Transcript> {
    void audio;
    void opts;
    throw new Error(
      "GeminiASR er ikke koblet på ennå. Krever GCP-prosjekt og at " +
        "request-formen verifiseres mot Vertex-dokumentasjonen. " +
        `Konfigurert: ${this.cfg.projectId}/${this.cfg.region} (${this.cfg.model}). ` +
        "Kjør med ASR_PROVIDER=mock inntil videre.",
    );
  }

  /** Ordlista sendes som prompt-hint. Holdes kort med vilje – se TranscribeOptions. */
  protected buildPrompt(opts: TranscribeOptions): string {
    const navn = opts.vocabulary.slice(0, 100).join(", ");
    return [
      "Transkriber dette opptaket fra et norsk kommunalt politisk møte.",
      "Skriv ordrett på norsk bokmål. Ikke oppsummer, ikke utelat noe.",
      "Marker talerbytter. Gi tidsstempel for hver ytring.",
      navn ? `Disse navnene forekommer i møtet: ${navn}.` : "",
      "Ikke skriv inn navn du ikke faktisk hører.",
    ]
      .filter(Boolean)
      .join("\n");
  }
}

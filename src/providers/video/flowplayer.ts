import type { VideoRef } from "../../types.js";
import type { ResolvedMedia, VideoSource } from "./index.js";

/**
 * Slår opp et møteopptak i Amedias Flowplayer-konto.
 *
 * Journalisten limer inn video-ID-en – det eneste som endrer seg fra møte
 * til møte. Workspace-ID-en er konstant for hele Amedia og ligger i config.
 *
 * Manifest-URL-en som kommer ut herfra er ferskvare: den inneholder som
 * regel et tidsbegrenset token. Derfor lagres den aldri i jobben – vi
 * lagrer video-ID-en og slår opp på nytt hver gang jobben kjøres.
 *
 * IKKE VERIFISERT: krever API-token og at endepunktformen sjekkes mot
 * Flowplayers OVP-dokumentasjon.
 */
export class FlowplayerSource implements VideoSource {
  readonly name = "flowplayer";

  constructor(
    private cfg: { workspaceId: string; apiToken: string },
  ) {}

  async resolve(ref: VideoRef): Promise<ResolvedMedia> {
    void ref;
    throw new Error(
      "FlowplayerSource er ikke koblet på ennå. Krever API-token. " +
        `Workspace: ${this.cfg.workspaceId}. Kjør med VIDEO_PROVIDER=mock inntil videre.`,
    );
  }
}

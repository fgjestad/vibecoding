import type { VideoRef } from "../../types.js";
import type { ResolvedMedia, VideoSource } from "./index.js";

/** Peker på en lokal fil i stedet for å slå opp i Flowplayer. */
export class MockVideoSource implements VideoSource {
  readonly name = "mock";
  constructor(private localPath: string) {}

  async resolve(ref: VideoRef): Promise<ResolvedMedia> {
    return {
      url: this.localPath,
      hasSeparateAudio: false,
      durationSec: null,
      title: `Mock-møte ${ref.videoId}`,
    };
  }
}

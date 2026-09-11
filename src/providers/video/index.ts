import type { VideoRef } from "../../types.js";

export interface ResolvedMedia {
  /** Direkte URL til manifest eller mediefil. Kortlevd – lagres aldri. */
  url: string;
  /** Om lyden ligger som egen rendisjon. Da slipper vi å laste video i det
   *  hele tatt, og fire timer koster ~40 MB i stedet for flere GB. */
  hasSeparateAudio: boolean;
  durationSec: number | null;
  title: string | null;
  /** Noen CDN-er krever Referer på segmentene. */
  referer?: string;
}

export interface VideoSource {
  readonly name: string;
  resolve(ref: VideoRef): Promise<ResolvedMedia>;
}

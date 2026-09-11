import type { VideoRef } from "../../types.js";

/** Kapittelmarkør fra videoplattformen. Merkes møtet per sak, får vi
 *  saksinndelingen på tidslinja gratis. */
export interface Chapter {
  startsAt: number;
  title: string;
}

export interface ResolvedMedia {
  /** Direkte URL til manifest eller mediefil. Kortlevd – lagres aldri. */
  url: string;
  /** Om lyden kan hentes uten å laste ned videobytes. */
  hasSeparateAudio: boolean;
  durationSec: number | null;
  title: string | null;
  /** Noen CDN-er krever Referer på segmentene. */
  referer?: string;
  chapters?: Chapter[];
  /** URL-er til eksisterende undertekster (WebVTT), hvis plattformen
   *  allerede har transkribert møtet. */
  existingSubtitles?: { language: string; url: string }[];
}

export interface VideoSource {
  readonly name: string;
  resolve(ref: VideoRef): Promise<ResolvedMedia>;
}

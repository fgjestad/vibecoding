import type { VideoRef } from "../../types.js";
import type { Chapter, ResolvedMedia, VideoSource } from "./index.js";

const BASE = "https://api.flowplayer.com/platform";

/** Delmengden av Flowplayers Video-skjema vi faktisk bruker. */
interface Encoding {
  format?: string;
  bitrate?: number;
  audio_bitrate?: number;
  audio_codec?: string;
  size?: number;
  video_file_url?: string;
}

interface FlowplayerVideo {
  id: string;
  name?: string;
  duration?: number;
  state?: string;
  error_message?: string;
  audio_only?: boolean;
  encodings?: Encoding[];
  chapters?: { starts_at?: number; title?: string }[];
  subtitles?: { srclang?: string; url?: string; published?: boolean }[];
}

/**
 * Slår opp et møteopptak i Amedias Flowplayer-konto.
 *
 * Journalisten limer inn video-ID-en – det eneste som endrer seg fra møte til
 * møte. Resten kommer herfra: tittel, varighet, mediefiler og eventuelle
 * kapittelmarkører.
 *
 * Mediefil-URL-ene som kommer ut er ferskvare og lagres aldri i jobben. Vi
 * lagrer video-ID-en og slår opp på nytt hver gang.
 */
export class FlowplayerSource implements VideoSource {
  readonly name = "flowplayer";

  constructor(private cfg: { apiKey: string }) {}

  async resolve(ref: VideoRef): Promise<ResolvedMedia> {
    const video = await this.getVideo(ref.videoId);

    if (video.state && video.state !== "FINISHED") {
      throw new Error(
        `Videoen er ikke ferdig behandlet (state: ${video.state})` +
          (video.error_message ? ` – ${video.error_message}` : "") +
          ". Vent til den er FINISHED før transkribering.",
      );
    }

    const valgt = pickEncoding(video.encodings ?? []);
    if (!valgt?.video_file_url) {
      throw new Error(
        `Fant ingen avspillbar mediefil for ${ref.videoId}. ` +
          `Tilgjengelige formater: ${(video.encodings ?? [])
            .map((e) => e.format ?? "?")
            .join(", ") || "ingen"}`,
      );
    }

    const chapters: Chapter[] = (video.chapters ?? [])
      .filter((c): c is { starts_at: number; title: string } =>
        typeof c.starts_at === "number" && typeof c.title === "string")
      .map((c) => ({ startsAt: c.starts_at, title: c.title }))
      .sort((a, b) => a.startsAt - b.startsAt);

    return {
      url: valgt.video_file_url,
      // Er opptaket rent lyd, eller er kilden en HLS-manifest, kan ffmpeg
      // hente lyden uten å dra ned videobytes.
      hasSeparateAudio: video.audio_only === true || valgt.format === "hls",
      durationSec: video.duration ?? null,
      title: video.name ?? null,
      chapters: chapters.length > 0 ? chapters : undefined,
      existingSubtitles: (video.subtitles ?? [])
        .filter((s) => s.url && s.published !== false)
        .map((s) => ({ language: s.srclang ?? "?", url: s.url! })),
    };
  }

  private async getVideo(id: string): Promise<FlowplayerVideo> {
    const res = await fetch(`${BASE}/v3/videos/${encodeURIComponent(id)}`, {
      headers: {
        "x-flowplayer-api-key": this.cfg.apiKey,
        "Content-Type": "application/json",
      },
    });

    if (!res.ok) {
      throw new Error(forklarFeil(res.status, id));
    }
    return (await res.json()) as FlowplayerVideo;
  }
}

/**
 * Velger den mediefila som koster minst å hente lyd ut av.
 *
 * HLS først: ligger lyden som egen rendisjon i manifesten, laster ffmpeg
 * kun lydsegmentene og rører aldri videoen. Ellers laveste bitrate – for
 * lyduttrekk er 240p og 1080p like gode, men den ene er en brøkdel så stor.
 */
export function pickEncoding(encodings: Encoding[]): Encoding | undefined {
  const brukbare = encodings.filter((e) => e.video_file_url);
  if (brukbare.length === 0) return undefined;

  const hls = brukbare.find((e) => e.format === "hls");
  if (hls) return hls;

  return brukbare
    .slice()
    .sort((a, b) => (a.bitrate ?? Infinity) - (b.bitrate ?? Infinity))[0];
}

function forklarFeil(status: number, id: string): string {
  switch (status) {
    case 401:
      return "API-nøkkelen har ikke tilgang til dette endepunktet eller videoen.";
    case 403:
      return "Klarte ikke autentisere. Sjekk at FLOWPLAYER_API_KEY er riktig.";
    case 404:
      return `Fant ingen video med ID ${id}. Sjekk at ID-en er riktig og ` +
        "at nøkkelen hører til samme workspace.";
    case 429:
      // Flowplayer tillater 1 request/sekund som standard, 3 for enterprise.
      return "For mange forespørsler mot Flowplayer. Vent litt og prøv igjen.";
    default:
      return `Flowplayer svarte ${status}.`;
  }
}

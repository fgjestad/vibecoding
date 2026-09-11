import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import type { AudioArtifact } from "../types.js";
import type { ResolvedMedia } from "../providers/video/index.js";

function run(cmd: string, args: string[]): Promise<string> {
  return new Promise((res, rej) => {
    const p = spawn(cmd, args);
    let out = "";
    let err = "";
    p.stdout.on("data", (d) => (out += d));
    p.stderr.on("data", (d) => (err += d));
    p.on("error", (e) =>
      rej(new Error(`Fikk ikke kjørt ${cmd}: ${e.message}. Er den installert?`)),
    );
    p.on("close", (code) =>
      code === 0 ? res(out) : rej(new Error(`${cmd} feilet (${code}): ${err.slice(-800)}`)),
    );
  });
}

async function sha256(path: string): Promise<string> {
  const h = createHash("sha256");
  for await (const chunk of createReadStream(path)) h.update(chunk);
  return h.digest("hex");
}

/**
 * Trekker lyden ut av møtevideoen og koder den til Opus mono 16 kHz.
 *
 * Hvorfor dette steget finnes i det hele tatt: all talegjenkjenning
 * resampler internt til 16 kHz mono, så alt utover det kastes uansett.
 * En 4-timers video på 2 GB blir ~43 MB lyd – 98 % mindre å laste opp,
 * og en opplasting som ikke ryker på 80 %.
 *
 * Ligger lyden som egen rendisjon i HLS-manifesten, laster ffmpeg bare
 * lydsegmentene og rører aldri videobytene.
 */
export async function extractAudio(
  media: ResolvedMedia,
  outPath: string,
  bitrate: string,
): Promise<AudioArtifact> {
  const headers = media.referer ? ["-headers", `Referer: ${media.referer}\r\n`] : [];

  await run("ffmpeg", [
    "-hide_banner",
    "-loglevel", "warning",
    ...headers,
    "-i", media.url,
    "-vn", "-sn", "-dn",
    "-map", "0:a:0",
    "-ac", "1",
    "-ar", "16000",
    "-c:a", "libopus",
    "-b:a", bitrate,
    "-application", "voip",
    "-y", outPath,
  ]);

  const probe = await run("ffprobe", [
    "-v", "error",
    "-show_entries", "format=duration",
    "-of", "csv=p=0",
    outPath,
  ]);

  const { size } = await stat(outPath);
  return {
    path: outPath,
    codec: "opus",
    bitrateKbps: parseInt(bitrate, 10),
    sampleRate: 16000,
    channels: 1,
    durationSec: Math.round(parseFloat(probe.trim())),
    sizeBytes: size,
    sha256: await sha256(outPath),
  };
}

/**
 * Lyduttrekk bak et grensesnitt, slik at pipelinen kan kjøres uten ffmpeg.
 *
 * Samme begrunnelse som for ASR-mocken: hele veien fra video-ID til artikkel
 * skal kunne testes før noen tilgang er på plass.
 */
export interface AudioExtractor {
  readonly name: string;
  extract(
    media: ResolvedMedia,
    outPath: string,
    bitrate: string,
  ): Promise<AudioArtifact>;
}

export class FfmpegExtractor implements AudioExtractor {
  readonly name = "ffmpeg";
  extract = extractAudio;
}

/** Later som lyden er trukket ut. Brukes når ffmpeg ikke finnes. */
export class MockExtractor implements AudioExtractor {
  readonly name = "mock";
  constructor(private durationSec: number) {}

  async extract(
    _media: ResolvedMedia,
    outPath: string,
    bitrate: string,
  ): Promise<AudioArtifact> {
    const kbps = parseInt(bitrate, 10);
    return {
      path: outPath,
      codec: "opus",
      bitrateKbps: kbps,
      sampleRate: 16000,
      channels: 1,
      durationSec: this.durationSec,
      // Samme regnestykke som en ekte kjøring ville gitt.
      sizeBytes: Math.round((kbps * 1000 * this.durationSec) / 8),
      sha256: "mock",
    };
  }
}

/** ffmpeg der den finnes, mock ellers. */
export async function pickExtractor(
  fallbackDurationSec: number,
): Promise<AudioExtractor> {
  try {
    await run("ffmpeg", ["-version"]);
    return new FfmpegExtractor();
  } catch {
    return new MockExtractor(fallbackDurationSec);
  }
}

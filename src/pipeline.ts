import { randomUUID } from "node:crypto";
import type { Config } from "./config.js";
import { ClaudeClient } from "./providers/llm/claude.js";
import type { ASRProvider } from "./providers/asr/index.js";
import { MockASR } from "./providers/asr/mock.js";
import { GeminiASR } from "./providers/asr/gemini.js";
import { GoogleSTT } from "./providers/asr/google-stt.js";
import type { VideoSource } from "./providers/video/index.js";
import { MockVideoSource } from "./providers/video/mock.js";
import { FlowplayerSource } from "./providers/video/flowplayer.js";
import { ArtifactStore } from "./store/artifacts.js";
import { pickExtractor } from "./steps/audio.js";
import { parseRoster } from "./steps/roster.js";
import { transcribe } from "./steps/transcribe.js";
import { matchSpeakers } from "./steps/speakers.js";
import { placeAgenda } from "./steps/agenda.js";
import { writeArticle } from "./steps/article.js";
import type { Job } from "./types.js";

export function makeASR(cfg: Config): ASRProvider {
  switch (cfg.asrProvider) {
    case "mock":
      return new MockASR(cfg.mockTranscript);
    case "gemini":
      return new GeminiASR({
        projectId: req(cfg.vertex?.projectId, "GCP_PROJECT"),
        region: cfg.vertex!.region,
        model: cfg.geminiModel,
      });
    case "google-stt":
      return new GoogleSTT({
        projectId: req(cfg.vertex?.projectId, "GCP_PROJECT"),
        region: cfg.vertex!.region,
        recognizer: "_",
        gcsBucket: "",
      });
  }
}

export function makeVideo(cfg: Config): VideoSource {
  return cfg.videoProvider === "mock"
    ? new MockVideoSource(cfg.mockVideo)
    : new FlowplayerSource({
        workspaceId: req(cfg.flowplayer.workspaceId, "FLOWPLAYER_WORKSPACE_ID"),
        apiToken: req(cfg.flowplayer.apiToken, "FLOWPLAYER_API_TOKEN"),
      });
}

function req(v: string | undefined, navn: string): string {
  if (!v) throw new Error(`Mangler ${navn} i miljøet.`);
  return v;
}

export interface RunOptions {
  videoId: string;
  /** Innkalling eller protokoll. Utelates den, mister du ordlista,
   *  sakskartet og navnemappingen – altså det meste av verdien. */
  documentPath?: string;
  prompt?: string;
  log?: (s: string) => void;
}

/**
 * Kjører hele veien fra video-ID til artikkelutkast.
 *
 * Rekkefølgen er ikke tilfeldig: dokumentet leses FØR transkripsjonen, fordi
 * deltakerlista er ordlista talegjenkjenningen skal biases med.
 */
export async function run(cfg: Config, opts: RunOptions): Promise<Job> {
  const log = opts.log ?? (() => {});
  const store = new ArtifactStore(cfg.dataDir);
  const claude = await ClaudeClient.create({
    model: cfg.claudeModel,
    vertex: cfg.vertex,
  });

  const job: Job = {
    id: randomUUID().slice(0, 8),
    status: "opprettet",
    createdAt: new Date().toISOString(),
    video: {
      provider: cfg.videoProvider,
      videoId: opts.videoId,
      workspaceId: cfg.flowplayer.workspaceId || undefined,
    },
    articles: [],
  };
  await store.ensureDir(job.id);
  log(`Jobb ${job.id} opprettet for video ${opts.videoId}`);

  try {
    // 1. Deltakerliste og sakskart – først, fordi ordlista trengs i steg 3.
    if (opts.documentPath) {
      job.status = "leser-dokument";
      await store.save(job);
      log(`Leser ${opts.documentPath} ...`);
      job.roster = await parseRoster(claude, opts.documentPath);
      log(
        `  ${job.roster.people.length} personer, ${job.roster.agenda.length} saker ` +
          `(${job.roster.documentKind})`,
      );
    } else {
      log("Ingen deltakerliste oppgitt – hopper over ordliste og navnemapping.");
    }

    // 2. Lyd.
    job.status = "henter-lyd";
    await store.save(job);
    const media = await makeVideo(cfg).resolve(job.video);
    const extractor = await pickExtractor(media.durationSec ?? 420);
    log(
      `Henter lyd fra ${media.hasSeparateAudio ? "egen lydrendisjon" : "mediestrøm"} ` +
        `(${extractor.name}) ...`,
    );
    job.audio = await extractor.extract(
      media,
      store.audioPath(job.id),
      cfg.audioBitrate,
    );
    log(
      `  ${(job.audio.sizeBytes / 1048576).toFixed(1)} MB, ` +
        `${Math.round(job.audio.durationSec / 60)} min`,
    );

    // 3. Transkripsjon.
    job.status = "transkriberer";
    await store.save(job);
    const asr = makeASR(cfg);
    log(`Transkriberer med ${asr.name} ...`);
    job.transcript = await transcribe(asr, job.audio, job.roster);
    log(
      `  ${job.transcript.segments.length} segmenter, ` +
        `tidsstempler: ${job.transcript.timestampQuality}`,
    );

    // 4. Talere og saker.
    if (job.roster) {
      job.status = "matcher-talere";
      await store.save(job);
      job.speakers = await matchSpeakers(claude, job.transcript, job.roster);
      const sikre = job.speakers.filter((s) => s.confidence >= 0.8).length;
      log(`  ${job.speakers.length} talere, ${sikre} med høy konfidens`);

      job.roster.agenda = await placeAgenda(claude, job.transcript, job.roster);
      const plassert = job.roster.agenda.filter((a) => a.startSec !== undefined).length;
      log(`  ${plassert} av ${job.roster.agenda.length} saker plassert på tidslinja`);

      job.status = "venter-pa-bekreftelse";
      await store.save(job);
    }

    // 5. Artikkel.
    if (opts.prompt) {
      log("Skriver artikkelutkast ...");
      job.articles.push(
        await writeArticle(claude, {
          prompt: opts.prompt,
          transcript: job.transcript,
          roster: job.roster ?? {
            source: "upload",
            documentKind: "ukjent",
            documentName: "-",
            people: [],
            agenda: [],
          },
          speakers: job.speakers ?? [],
        }),
      );
    }

    job.status = "klar";
    await store.save(job);
    return job;
  } catch (e) {
    job.status = "feilet";
    job.error = e instanceof Error ? e.message : String(e);
    await store.save(job);
    throw e;
  }
}

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
import { transcriptFromSubtitleUrl } from "./steps/subtitles.js";
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
        apiKey: req(cfg.flowplayer.apiKey, "FLOWPLAYER_API_KEY"),
      });
}

/** Velger norsk undertekst. Bokmål og nynorsk er begge greit; "no" er
 *  det Flowplayer bruker når språket er satt til Norwegian. */
export function pickNorwegian(
  subs: { language: string; url: string }[] | undefined,
): { language: string; url: string } | undefined {
  if (!subs?.length) return undefined;
  return subs.find((s) => /^(no|nb|nn)\b/i.test(s.language));
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

    job.status = "henter-lyd";
    await store.save(job);
    const media = await makeVideo(cfg).resolve(job.video);
    if (media.title) log(`  "${media.title}"`);
    if (media.chapters?.length) {
      log(`  ${media.chapters.length} kapittelmarkører fra Flowplayer`);
    }
    if (media.existingSubtitles?.length) {
      log(
        `  Videoen har allerede undertekster: ` +
          `${media.existingSubtitles.map((s) => s.language).join(", ")}`,
      );
    }

    // Flowplayer transkriberer nye videoer automatisk på norsk, men
    // kvaliteten er erfaringsmessig for svak til å bygge journalistikk på.
    // Derfor AV som standard. Slå på med USE_EXISTING_SUBTITLES=true når du
    // vil ha en gratis målestokk å sammenligne en ekte ASR-motor mot på det
    // samme møtet.
    const ferdig = cfg.useExistingSubtitles
      ? pickNorwegian(media.existingSubtitles)
      : undefined;

    if (ferdig) {
      job.status = "transkriberer";
      await store.save(job);
      log(`Bruker Flowplayers ferdige transkript (${ferdig.language}) ...`);
      job.transcript = await transcriptFromSubtitleUrl(
        ferdig.url,
        ferdig.language,
        media.durationSec,
      );
      log(
        `  ${job.transcript.segments.length} replikker, ingen taleridentifikasjon, ` +
          `tidsstempler: omtrentlige`,
      );
      log("  NB: referansetranskript til sammenligning – ikke publiseringskvalitet.");
    } else {
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

      job.status = "transkriberer";
      await store.save(job);
      const asr = makeASR(cfg);
      log(`Transkriberer med ${asr.name} ...`);
      job.transcript = await transcribe(asr, job.audio, job.roster);
      log(
        `  ${job.transcript.segments.length} segmenter, ` +
          `tidsstempler: ${job.transcript.timestampQuality}`,
      );
    }

    // 4. Talere og saker.
    if (job.roster) {
      job.status = "matcher-talere";
      await store.save(job);
      job.speakers = await matchSpeakers(claude, job.transcript, job.roster);
      const sikre = job.speakers.filter((s) => s.confidence >= 0.8).length;
      log(`  ${job.speakers.length} talere, ${sikre} med høy konfidens`);

      job.roster.agenda = await placeAgenda(
        claude,
        job.transcript,
        job.roster,
        media.chapters,
      );
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

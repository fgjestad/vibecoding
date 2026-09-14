import { resolve } from "node:path";

export interface Config {
  claudeModel: string;
  vertex?: { projectId: string; region: string };
  asrProvider: "mock" | "assemblyai" | "gemini" | "google-stt";
  videoProvider: "mock" | "flowplayer" | "embed";
  flowplayer: { workspaceId: string; apiKey: string; publisherId: string };
  /** Bruk transkriptet Flowplayer allerede har laget. AV som standard –
   *  kvaliteten er erfaringsmessig for svak. Slå på for å hente et gratis
   *  referansetranskript å måle en ekte ASR-motor mot. */
  useExistingSubtitles: boolean;
  assemblyAiKey: string;
  geminiModel: string;
  /** Opus-bitrate. 24k er standardvalget: 4 timer blir ~43 MB, og de 14 MB
   *  ekstra over 16k er billig forsikring på gjenkjenningskvalitet. */
  audioBitrate: string;
  /** Cue-instansen tekstene eksporteres til. Per avis. */
  cueHost: string;
  cuePublication: string;
  dataDir: string;
  mockTranscript: string;
  mockVideo: string;
}

/**
 * Renser en verdi fra miljøet.
 *
 * Nøkler limes inn for hånd i Render-dashbordet, og da blir det fort med et
 * linjeskift eller et par anførselstegn. Headeren blir ugyldig, API-et svarer
 * 401, og verdien ser helt riktig ut i dashbordet — en ekkel feil å lete
 * etter. Billigere å fjerne søppelet enn å feilsøke det.
 */
function ren(v: string | undefined): string {
  return (v ?? "").trim().replace(/^["']|["']$/g, "").trim();
}

export function loadConfig(env = process.env): Config {
  const projectId = ren(env.GCP_PROJECT) || undefined;
  return {
    claudeModel: ren(env.CLAUDE_MODEL) || "claude-opus-5",
    vertex: projectId
      ? { projectId, region: ren(env.GCP_REGION) || "europe-north1" }
      : undefined,
    asrProvider: (env.ASR_PROVIDER ?? "mock") as Config["asrProvider"],
    videoProvider: (env.VIDEO_PROVIDER ?? "mock") as Config["videoProvider"],
    flowplayer: {
      workspaceId: ren(env.FLOWPLAYER_WORKSPACE_ID),
      apiKey: ren(env.FLOWPLAYER_API_KEY),
      // "pi" i embed-lenka. Konstant per utgiver, ikke per video.
      publisherId: ren(env.FLOWPLAYER_PUBLISHER_ID),
    },
    useExistingSubtitles: env.USE_EXISTING_SUBTITLES === "true",
    assemblyAiKey: ren(env.ASSEMBLYAI_API_KEY),
    geminiModel: env.GEMINI_MODEL ?? "gemini-2.5-pro",
    audioBitrate: env.AUDIO_BITRATE ?? "24k",
    cueHost: ren(env.CUE_HOST).replace(/\/$/, ""),
    cuePublication: ren(env.CUE_PUBLICATION),
    dataDir: resolve(env.DATA_DIR ?? "./data"),
    mockTranscript: resolve(env.MOCK_TRANSCRIPT ?? "./fixtures/transkript.json"),
    mockVideo: resolve(env.MOCK_VIDEO ?? "./fixtures/moete.mp4"),
  };
}

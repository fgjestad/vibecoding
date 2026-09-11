import { resolve } from "node:path";

export interface Config {
  claudeModel: string;
  vertex?: { projectId: string; region: string };
  asrProvider: "mock" | "gemini" | "google-stt";
  videoProvider: "mock" | "flowplayer";
  flowplayer: { workspaceId: string; apiToken: string };
  geminiModel: string;
  /** Opus-bitrate. 24k er standardvalget: 4 timer blir ~43 MB, og de 14 MB
   *  ekstra over 16k er billig forsikring på gjenkjenningskvalitet. */
  audioBitrate: string;
  dataDir: string;
  mockTranscript: string;
  mockVideo: string;
}

export function loadConfig(env = process.env): Config {
  const projectId = env.GCP_PROJECT;
  return {
    claudeModel: env.CLAUDE_MODEL ?? "claude-opus-5",
    vertex: projectId
      ? { projectId, region: env.GCP_REGION ?? "europe-north1" }
      : undefined,
    asrProvider: (env.ASR_PROVIDER ?? "mock") as Config["asrProvider"],
    videoProvider: (env.VIDEO_PROVIDER ?? "mock") as Config["videoProvider"],
    flowplayer: {
      workspaceId: env.FLOWPLAYER_WORKSPACE_ID ?? "",
      apiToken: env.FLOWPLAYER_API_TOKEN ?? "",
    },
    geminiModel: env.GEMINI_MODEL ?? "gemini-2.5-pro",
    audioBitrate: env.AUDIO_BITRATE ?? "24k",
    dataDir: resolve(env.DATA_DIR ?? "./data"),
    mockTranscript: resolve(env.MOCK_TRANSCRIPT ?? "./fixtures/transkript.json"),
    mockVideo: resolve(env.MOCK_VIDEO ?? "./fixtures/moete.mp4"),
  };
}

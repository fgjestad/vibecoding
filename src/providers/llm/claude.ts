import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import type { z } from "zod";

export type Effort = "low" | "medium" | "high" | "xhigh" | "max";

/**
 * Tynn innpakning rundt Claude, brukt til alt unntatt lyd-til-tekst:
 * dokumentparsing, saksinndeling, talermatching og artikkelskriving.
 *
 * Kjører mot Vertex AI når GCP_PROJECT er satt, ellers mot Claude API
 * direkte. Vertex-veien er den som passer Amedias oppsett: samme prosjekt,
 * samme ADC-autentisering, ingen ekstra nøkkel å forvalte.
 */
export class ClaudeClient {
  private constructor(
    private client: Anthropic,
    readonly model: string,
  ) {}

  static async create(cfg: {
    model: string;
    vertex?: { projectId: string; region: string };
  }): Promise<ClaudeClient> {
    if (cfg.vertex) {
      // Dynamisk import: pakken kreves bare når Vertex faktisk brukes.
      const { AnthropicVertex } = await import("@anthropic-ai/vertex-sdk");
      const c = new AnthropicVertex({
        projectId: cfg.vertex.projectId,
        region: cfg.vertex.region,
      });
      return new ClaudeClient(c as unknown as Anthropic, cfg.model);
    }
    return new ClaudeClient(new Anthropic(), cfg.model);
  }

  /**
   * Ber om et svar som validerer mot et zod-skjema.
   *
   * Bruker messages.create + output_config.format framfor messages.parse,
   * fordi create-flaten er identisk på Vertex-klienten og førstepartsklienten.
   * Valideringen gjør vi selv rett etterpå, så vi står igjen med samme garanti.
   */
  async structured<T extends z.ZodType>(args: {
    schema: T;
    system: string;
    content: Anthropic.ContentBlockParam[];
    effort?: Effort;
    maxTokens?: number;
  }): Promise<z.infer<T>> {
    const res = await this.client.messages.create({
      model: this.model,
      max_tokens: args.maxTokens ?? 16000,
      system: args.system,
      messages: [{ role: "user", content: args.content }],
      thinking: { type: "adaptive" },
      output_config: {
        effort: args.effort ?? "high",
        format: zodOutputFormat(args.schema),
      },
    });

    if (res.stop_reason === "refusal") {
      throw new Error(
        `Claude avslo forespørselen: ${res.stop_details?.explanation ?? "ukjent grunn"}`,
      );
    }

    const text = res.content
      .filter((b): b is Anthropic.TextBlock => b.type === "text")
      .map((b) => b.text)
      .join("");

    if (!text.trim()) throw new Error("Tomt svar fra modellen.");
    return args.schema.parse(JSON.parse(text));
  }
}

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { Job } from "../types.js";

/**
 * Lagrer jobbens artefakter som filer på disk.
 *
 * Hvert steg skriver sitt resultat før neste starter, slik at fire timer
 * aldri transkriberes to ganger fordi artikkelsteget feilet. I skya blir
 * dette GCS med samme layout – derfor er grensesnittet holdt så smalt.
 */
export class ArtifactStore {
  constructor(private root: string) {}

  private dir(jobId: string) {
    return join(this.root, jobId);
  }

  async save(job: Job): Promise<void> {
    const d = this.dir(job.id);
    await mkdir(d, { recursive: true });
    await writeFile(join(d, "job.json"), JSON.stringify(job, null, 2));
    // Transkriptet skrives også separat – det er fasiten alle senere steg
    // leser, og den vil man kunne åpne uten å lete i jobbobjektet.
    if (job.transcript) {
      await writeFile(
        join(d, "transkript.json"),
        JSON.stringify(job.transcript, null, 2),
      );
    }
  }

  async load(jobId: string): Promise<Job | null> {
    try {
      return JSON.parse(await readFile(join(this.dir(jobId), "job.json"), "utf8"));
    } catch {
      return null;
    }
  }

  audioPath(jobId: string): string {
    return join(this.dir(jobId), "lyd.opus");
  }

  async ensureDir(jobId: string): Promise<void> {
    await mkdir(this.dir(jobId), { recursive: true });
  }
}

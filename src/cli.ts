import { parseArgs } from "node:util";
import { loadConfig } from "./config.js";
import { run } from "./pipeline.js";

const HJELP = `
moteskriver – transkriberer kommunale møteopptak og skriver artikkelutkast

  npm run kjor -- <video-id> [valg]

Valg:
  --dokument <fil>   Møteinnkalling eller protokoll (PDF eller bilde).
                     Gir ordliste til talegjenkjenningen, sakskart og
                     navn på talerne. Utelates den, mister du det meste.
  --prompt <tekst>   Oppdrag til artikkelen. Utelates den, stopper vi
                     etter transkribering.
  --hjelp

Miljø (se .env.example). Uten noe satt kjører alt på mocks.
`;

async function main() {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      dokument: { type: "string" },
      prompt: { type: "string" },
      hjelp: { type: "boolean", short: "h" },
    },
  });

  if (values.hjelp || positionals.length === 0) {
    console.log(HJELP);
    process.exit(values.hjelp ? 0 : 1);
  }

  const cfg = loadConfig();
  console.log(
    `Motorer: video=${cfg.videoProvider}, asr=${cfg.asrProvider}, ` +
      `claude=${cfg.claudeModel}${cfg.vertex ? " (Vertex)" : ""}\n`,
  );

  const job = await run(cfg, {
    videoId: positionals[0]!,
    documentPath: values.dokument,
    prompt: values.prompt,
    log: (s) => console.log(s),
  });

  console.log(`\nFerdig. Artefakter i ${cfg.dataDir}/${job.id}/`);

  const art = job.articles[0];
  if (art) {
    console.log(`\n${"=".repeat(60)}\n${art.headline}\n${"=".repeat(60)}\n`);
    console.log(art.body);
    console.log(`\nKilder (${art.citations.length}):`);
    for (const c of art.citations) {
      console.log(`  [${fmt(c.startSec)}] "${c.quote.slice(0, 80)}..."`);
    }
  }

  if (job.speakers?.some((s) => !s.confirmed)) {
    console.log(
      `\nMerk: ${job.speakers.filter((s) => !s.confirmed).length} talere er ikke ` +
        `bekreftet. Navn skal bekreftes av et menneske før publisering.`,
    );
  }
}

function fmt(s: number): string {
  const t = Math.floor(s);
  return `${String(Math.floor(t / 3600)).padStart(2, "0")}:${String(
    Math.floor((t % 3600) / 60),
  ).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}

main().catch((e) => {
  console.error(`\nFeil: ${e instanceof Error ? e.message : e}`);
  process.exit(1);
});

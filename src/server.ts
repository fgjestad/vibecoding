import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { readFile, writeFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { loadConfig } from "./config.js";
import { makeVideo, run } from "./pipeline.js";
import { ArtifactStore, ryddAvbrutte } from "./store/artifacts.js";
import { RosterStore } from "./store/rosters.js";
import { parseRosterText } from "./steps/roster.js";
import { ClaudeClient } from "./providers/llm/claude.js";
import { writeArticle } from "./steps/article.js";
import type { Job } from "./types.js";

const cfg = loadConfig();
const store = new ArtifactStore(cfg.dataDir);
const lister = new RosterStore(cfg.dataDir);
const PORT = Number(process.env.PORT ?? 3000);

/** Jobber som kjører akkurat nå. Selve tilstanden ligger på disk. */
const kjorer = new Set<string>();

const json = (res: ServerResponse, kode: number, data: unknown) => {
  const kropp = JSON.stringify(data);
  res.writeHead(kode, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(kropp),
  });
  res.end(kropp);
};

async function lesKropp(req: IncomingMessage): Promise<any> {
  const biter: Buffer[] = [];
  let storrelse = 0;
  for await (const b of req) {
    storrelse += b.length;
    // Innkallinger er PDF-er på noen MB. 25 MB er rikelig, og hindrer at
    // noen fyller disken med én forespørsel.
    if (storrelse > 25 * 1024 * 1024) throw new Error("For stor forespørsel (maks 25 MB).");
    biter.push(b);
  }
  return biter.length ? JSON.parse(Buffer.concat(biter).toString("utf8")) : {};
}

/**
 * Starter jobben og svarer med én gang.
 *
 * Fire timer lyd tar minutter å transkribere. En HTTP-forespørsel kan ikke
 * stå og vente på det, så jobben kjører videre etter at svaret er sendt og
 * nettsiden spør om status.
 */
async function startJobb(body: any): Promise<{ id: string }> {
  const videoId = String(body.videoId ?? "").trim();
  if (!videoId) throw new Error("Mangler videoId.");

  // Dokumentet kommer som base64 i JSON – enklere enn multipart, og
  // dokumentleseren tar det formatet uansett.
  let documentPath: string | undefined;
  const id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
  await store.ensureDir(id);

  if (body.document?.base64) {
    const navn = String(body.document.name ?? "dokument.pdf").replace(/[^\w.-]/g, "_");
    documentPath = join(cfg.dataDir, id, navn);
    await writeFile(documentPath, Buffer.from(body.document.base64, "base64"));
  }

  kjorer.add(id);
  const rosterId = body.rosterId ? String(body.rosterId) : undefined;
  void run(cfg, { videoId, documentPath, rosterId, jobId: id })
    .catch(() => {})            // feilen lagres allerede i jobben
    .finally(() => kjorer.delete(id));

  return { id };
}

async function ruter(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const url = new URL(req.url ?? "/", `http://${req.headers.host}`);
  const sti = url.pathname;

  if (sti === "/healthz") return json(res, 200, { ok: true, kjorer: kjorer.size });

  if (sti === "/api/rosters" && req.method === "GET") {
    return json(res, 200, (await lister.list()).map((l) => ({
      id: l.id, navn: l.navn, antall: l.roster.people.length,
    })));
  }

  if (sti === "/api/rosters" && req.method === "POST") {
    const body = await lesKropp(req);
    const navn = String(body.navn ?? "").trim();
    const tekst = String(body.tekst ?? "").trim();
    if (!navn) throw new Error("Gi lista et navn, f.eks. «Nes kommunestyre».");
    if (tekst.length < 10) throw new Error("Lim inn navnelista først.");

    const claude = await ClaudeClient.create({ model: cfg.claudeModel, vertex: cfg.vertex });
    const roster = await parseRosterText(claude, tekst, navn);
    if (roster.people.length === 0) {
      throw new Error("Fant ingen navn i teksten. Sjekk at du limte inn riktig.");
    }
    const lagret = await lister.save(navn, roster);
    return json(res, 201, { id: lagret.id, navn: lagret.navn, roster: lagret.roster });
  }

  const rl = sti.match(/^\/api\/rosters\/([\w-]+)$/);
  if (rl && req.method === "GET") {
    const l = await lister.load(rl[1]!);
    return l ? json(res, 200, l) : json(res, 404, { error: "Ukjent navneliste." });
  }

  if (sti === "/api/jobs" && req.method === "POST") {
    return json(res, 202, await startJobb(await lesKropp(req)));
  }

  if (sti === "/api/jobs" && req.method === "GET") {
    const jobber = await store.list();
    // Listevisningen trenger ikke hele transkriptet.
    return json(res, 200, jobber.map(oppsummer));
  }

  const m = sti.match(/^\/api\/jobs\/([\w-]+)(\/\w+)?$/);
  if (m) {
    const jobb = await store.load(m[1]!);
    if (!jobb) return json(res, 404, { error: "Ukjent jobb." });

    if (!m[2] && req.method === "GET") return json(res, 200, jobb);

    if (m[2] === "/media" && req.method === "GET") {
      // Slås opp på nytt hver gang. Mediefil-URL-er fra Flowplayer har
      // kortlevde tokens, så de lagres aldri i jobben — vi har video-ID-en.
      const media = await makeVideo(cfg).resolve(jobb.video);
      return json(res, 200, {
        url: media.progressiveUrl ?? media.url,
        title: media.title,
        durationSec: media.durationSec,
      });
    }

    if (m[2] === "/speakers" && req.method === "POST") {
      return json(res, 200, await bekreftTalere(jobb, await lesKropp(req)));
    }
    if (m[2] === "/articles" && req.method === "POST") {
      return json(res, 200, await lagArtikkel(jobb, await lesKropp(req)));
    }
  }

  return statisk(sti, res);
}

/** Journalisten bekrefter hvem talerne er. Først da kan navn brukes. */
async function bekreftTalere(jobb: Job, body: any): Promise<Job> {
  const gyldige = new Set((jobb.roster?.people ?? []).map((p) => p.id));
  for (const inn of body.mappings ?? []) {
    const m = jobb.speakers?.find((s) => s.speaker === inn.speaker);
    if (!m) continue;
    m.personId = inn.personId && gyldige.has(inn.personId) ? inn.personId : null;
    m.label = inn.label || undefined;
    m.confirmed = true;
    // Et menneske har sagt ja. Da er det ikke lenger en gjetning.
    m.confidence = 1;
  }
  jobb.status = "klar";
  await store.save(jobb);
  return jobb;
}

async function lagArtikkel(jobb: Job, body: any): Promise<Job> {
  if (!jobb.transcript) throw new Error("Jobben har ikke noe transkript ennå.");
  const prompt = String(body.prompt ?? "").trim();
  if (!prompt) throw new Error("Mangler prompt.");

  const claude = await ClaudeClient.create({ model: cfg.claudeModel, vertex: cfg.vertex });
  const sak = jobb.roster?.agenda.find((a) => a.ref === body.agendaRef);

  jobb.articles.push(await writeArticle(claude, {
    prompt,
    transcript: jobb.transcript,
    roster: jobb.roster ?? {
      source: "upload", documentKind: "ukjent", documentName: "-",
      people: [], agenda: [],
    },
    speakers: jobb.speakers ?? [],
    agendaItem: sak,
  }));
  await store.save(jobb);
  return jobb;
}

const oppsummer = (j: Job) => ({
  id: j.id, status: j.status, createdAt: j.createdAt,
  videoId: j.video.videoId, error: j.error,
  antallSaker: j.roster?.agenda.length ?? 0,
  antallArtikler: j.articles.length,
});

const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
};

async function statisk(sti: string, res: ServerResponse): Promise<void> {
  // normalize + prefikssjekk hindrer at "../../etc/passwd" slipper ut.
  const fil = normalize(join(process.cwd(), "public", sti === "/" ? "index.html" : sti));
  if (!fil.startsWith(join(process.cwd(), "public"))) {
    return json(res, 403, { error: "Nei." });
  }
  try {
    const data = await readFile(fil);
    res.writeHead(200, { "content-type": MIME[extname(fil)] ?? "application/octet-stream" });
    res.end(data);
  } catch {
    json(res, 404, { error: "Ikke funnet." });
  }
}

const avbrutte = await ryddAvbrutte(store);
if (avbrutte) console.log(`Ryddet ${avbrutte} jobb(er) avbrutt av forrige omstart.`);

createServer((req, res) => {
  ruter(req, res).catch((e) => {
    json(res, 400, { error: e instanceof Error ? e.message : String(e) });
  });
}).listen(PORT, () => {
  console.log(`møteskriver lytter på :${PORT}`);
  console.log(`  data:  ${cfg.dataDir}`);
  console.log(`  asr:   ${cfg.asrProvider}    video: ${cfg.videoProvider}`);
});

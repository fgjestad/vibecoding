import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { vocabularyFrom, ambiguousSurnames } from "../src/steps/roster.js";
import { loadConfig } from "../src/config.js";
import { harLovligDomene, lesAuthConfig, lagSessionCookie, lesSession } from "../src/auth.js";
import { finnMediaUrler } from "../src/providers/video/embed.js";
import { finnNavnerettinger, anvendRettinger, likhet } from "../src/steps/navn.js";
import { MockASR } from "../src/providers/asr/mock.js";
import { transcribe } from "../src/steps/transcribe.js";
import { pickEncoding, pickProgressive } from "../src/providers/video/flowplayer.js";
import { parseVtt } from "../src/steps/subtitles.js";
import { tilSegmenter, kortSprak, boostliste } from "../src/providers/asr/assemblyai.js";
import { pickNorwegian } from "../src/pipeline.js";
import type { AudioArtifact, Roster } from "../src/types.js";

const roster: Roster = {
  source: "upload",
  documentKind: "protokoll",
  documentName: "protokoll.pdf",
  people: [
    { id: "p1", name: "Kari Nordmann", party: "Ap", role: "ordfører", group: "folkevalgt", present: true },
    { id: "p2", name: "Ola Berg", party: "H", role: "representant", group: "folkevalgt", present: true },
    { id: "p3", name: "Per Hansen", party: null, role: "kommunedirektør", group: "administrasjon", present: true },
  ],
  agenda: [{ ref: "PS 45/25", title: "Detaljregulering Storgata 4" }],
};

test("ordlista tar med fulle navn, etternavn, roller, parti og saksnumre", () => {
  const v = vocabularyFrom(roster);
  assert.ok(v.includes("Kari Nordmann"));
  assert.ok(v.includes("Nordmann"), "etternavn alene – slik folk omtales i debatten");
  assert.ok(v.includes("ordfører"));
  assert.ok(v.includes("Ap"));
  assert.ok(v.includes("PS 45/25"));
});

test("motorens egenskaper overstyrer det fixturen påstår", async () => {
  const asr = new MockASR("./fixtures/transkript.json");
  const audio = { durationSec: 420 } as AudioArtifact;
  const t = await transcribe(asr, audio, roster);

  assert.equal(t.engine, "mock");
  assert.equal(t.timestampQuality, asr.timestampQuality);
  assert.ok(t.segments.length > 0);
});

test("transkriptet har ord-nivå tidsstempler på hvert segment", async () => {
  const asr = new MockASR("./fixtures/transkript.json");
  const t = await transcribe(asr, {} as AudioArtifact, undefined);
  for (const s of t.segments) {
    assert.ok(s.words.length > 0, `segment ved ${s.start}s mangler ord-nivå data`);
    assert.ok(s.end > s.start);
  }
});

// --- Flowplayer: valg av mediefil -----------------------------------------

test("HLS velges framfor progressive filer", () => {
  const valgt = pickEncoding([
    { format: "1080p", bitrate: 4400, video_file_url: "https://x/1080.mp4" },
    { format: "hls", bitrate: 2600, video_file_url: "https://x/master.m3u8" },
    { format: "240p", bitrate: 400, video_file_url: "https://x/240.mp4" },
  ]);
  assert.equal(valgt?.format, "hls", "HLS lar ffmpeg hente kun lydrendisjonen");
});

test("uten HLS velges laveste bitrate", () => {
  const valgt = pickEncoding([
    { format: "1080p", bitrate: 4400, video_file_url: "https://x/1080.mp4" },
    { format: "240p", bitrate: 400, video_file_url: "https://x/240.mp4" },
    { format: "720p", bitrate: 2600, video_file_url: "https://x/720.mp4" },
  ]);
  assert.equal(valgt?.format, "240p", "for lyduttrekk er 240p like god som 1080p");
});

test("formater uten mediefil-URL hoppes over", () => {
  const valgt = pickEncoding([
    { format: "img", video_file_url: undefined },
    { format: "teaser" },
    { format: "360p", bitrate: 750, video_file_url: "https://x/360.mp4" },
  ]);
  assert.equal(valgt?.format, "360p");
});

test("tom liste gir undefined i stedet for å kaste", () => {
  assert.equal(pickEncoding([]), undefined);
});

// --- WebVTT-parsing --------------------------------------------------------

const VTT = `WEBVTT

1
00:00:12.400 --> 00:00:24.100
Da ønsker jeg velkommen til
formannskapsmøtet.

00:00:25.000 --> 00:00:58.300
<v Per Hansen>Takk, ordfører.

2
00:01:39.000 --> 00:02:08.400
Jeg vil advare mot å vedta dette.
`;

test("VTT-parsing tar tid, flerlinjet tekst og talermerking", () => {
  const s = parseVtt(VTT);
  assert.equal(s.length, 3);

  assert.equal(s[0]!.start, 12.4);
  assert.equal(s[0]!.end, 24.1);
  assert.equal(s[0]!.text, "Da ønsker jeg velkommen til formannskapsmøtet.",
    "flerlinjede replikker slås sammen til én");
  assert.equal(s[0]!.speaker, null);

  assert.equal(s[1]!.speaker, "Per Hansen", "<v ...> gir taler når den finnes");
  assert.equal(s[1]!.text, "Takk, ordfører.");

  assert.equal(s[2]!.start, 99, "1:39 blir 99 sekunder");
});

test("undertekster later ikke som de har ord-nivå tidsstempler", () => {
  for (const seg of parseVtt(VTT)) {
    assert.deepEqual(seg.words, [], "tomt er ærligere enn oppdiktede ordtider");
  }
});

test("komma som desimalskilletegn og kort millisekunddel tolkes riktig", () => {
  const s = parseVtt("WEBVTT\n\n00:00:01,5 --> 00:00:02,25\nHei\n");
  assert.equal(s[0]!.start, 1.5, '",5" er 500 ms, ikke 5 ms');
  assert.equal(s[0]!.end, 2.25);
});

test("norsk undertekst velges, andre språk ignoreres", () => {
  assert.equal(
    pickNorwegian([
      { language: "en", url: "https://x/en.vtt" },
      { language: "no", url: "https://x/no.vtt" },
    ])?.language,
    "no",
  );
  assert.equal(pickNorwegian([{ language: "en", url: "https://x/en.vtt" }]), undefined);
  assert.equal(pickNorwegian([]), undefined);
  assert.equal(pickNorwegian(undefined), undefined);
});

// --- Delte etternavn (ekte data fra Nes kommunestyre) ----------------------

test("delte etternavn fanges opp, også innad i samme parti", () => {
  const nes = JSON.parse(readFileSync("./rosters/nes.json", "utf8"));
  const delte = ambiguousSurnames(nes.people);

  // Verste tilfelle: samme etternavn OG samme parti – partitilhørighet
  // skiller dem ikke engang.
  assert.deepEqual(delte.get("Tømte")?.sort(),
    ["Anne Grethe Tømte", "Sondre Tømte"]);
  assert.deepEqual(delte.get("Roterud")?.sort(),
    ["Simen Roterud", "Tom Roterud"]);
  assert.deepEqual(delte.get("Aavik")?.sort(),
    ["Karoline Aavik", "Trond Aavik"]);

  // Tre personer, tre partier.
  assert.equal(delte.get("Johansen")?.length, 3);
  assert.equal(delte.get("Lunder")?.length, 3);
});

test("entydige etternavn markeres ikke som delte", () => {
  const nes = JSON.parse(readFileSync("./rosters/nes.json", "utf8"));
  const delte = ambiguousSurnames(nes.people);
  assert.equal(delte.has("Nyhus"), false, "ordføreren er den eneste Nyhus");
  assert.equal(delte.has("Rønoldtangen"), false);
});

test("partinavn lagres ordrett og normaliseres ikke bort", () => {
  const nes = JSON.parse(readFileSync("./rosters/nes.json", "utf8"));
  const geir = nes.people.find((p: any) => p.name === "Geir Antonsen");
  assert.equal(geir.party, "Uavhengig/Høyre",
    "en uavhengig utbryter er ikke Høyre – det ville vært feil i en artikkel");
  assert.equal(geir.partyShort, null, "ingen kort form der den ville villede");

  const ketil = nes.people.find((p: any) => p.name === "Ketil Rønneberg");
  assert.equal(ketil.party, "Fellesliste for SV og Rødt");
  assert.equal(ketil.partyShort, null);
});

test("ingen kontaktopplysninger lagres om folkevalgte", () => {
  const nes = JSON.parse(readFileSync("./rosters/nes.json", "utf8"));
  for (const p of nes.people) {
    assert.deepEqual(
      Object.keys(p).filter((k) => /phone|mail|tlf|epost/i.test(k)),
      [], `${p.name} har kontaktfelt – systemet trenger bare navn, parti og rolle`,
    );
  }
});

// --- AssemblyAI ------------------------------------------------------------

test("millisekunder fra AssemblyAI regnes om til sekunder", () => {
  const s = tilSegmenter({
    id: "x", status: "completed",
    utterances: [{
      text: "Takk, ordfører.", start: 25000, end: 58300, confidence: 0.9,
      speaker: "B",
      words: [{ text: "Takk", start: 25000, end: 25300, confidence: 0.98, speaker: "B" }],
    }],
  } as any);

  assert.equal(s[0]!.start, 25, "25000 ms er 25 sekunder, ikke 25000");
  assert.equal(s[0]!.end, 58.3);
  assert.equal(s[0]!.words[0]!.end, 25.3);
});

test("talere fra AssemblyAI får samme form som resten av systemet", () => {
  const s = tilSegmenter({
    id: "x", status: "completed",
    utterances: [
      { text: "En.", start: 0, end: 1000, confidence: 0.9, speaker: "A" },
      { text: "To.", start: 1000, end: 2000, confidence: 0.9, speaker: "B" },
    ],
  } as any);
  assert.equal(s[0]!.speaker, "SPEAKER_A");
  assert.equal(s[1]!.speaker, "SPEAKER_B");
});

test("uten diarisering deles ord opp på setningsslutt", () => {
  const ord = (text: string, start: number, end: number) =>
    ({ text, start, end, confidence: 0.9 });
  const s = tilSegmenter({
    id: "x", status: "completed",
    words: [
      ord("Første", 0, 500), ord("setning.", 500, 1000),
      ord("Andre", 1000, 1500), ord("setning.", 1500, 2000),
    ],
  } as any);
  assert.equal(s.length, 2);
  assert.equal(s[0]!.text, "Første setning.");
  assert.equal(s[0]!.speaker, null, "ingen diarisering – ikke lat som");
  assert.equal(s[1]!.start, 1);
});

test("språkkoden kortes ned slik AssemblyAI vil ha den", () => {
  assert.equal(kortSprak("no-NO"), "no");
  assert.equal(kortSprak("nb-NO"), "nb");
});

test("ordlista holdes innenfor AssemblyAIs grenser", () => {
  const nes = JSON.parse(readFileSync("./rosters/nes.json", "utf8"));
  const liste = boostliste(vocabularyFrom({
    source: "upload", documentKind: "protokoll", documentName: "x",
    people: nes.people, agenda: [],
  }));

  assert.ok(liste.length <= 1000);
  assert.ok(liste.includes("Rønoldtangen"), "vanskelige egennavn er hele poenget");
  assert.ok(liste.includes("Sørli-Sidselssønn"));
  for (const o of liste) {
    assert.ok(o.split(/\s+/).length <= 6, `"${o}" er for lang for word_boost`);
  }
});

// --- Miljøvariabler --------------------------------------------------------

test("nøkler renses for linjeskift og anførselstegn fra innliming", () => {
  const cfg = loadConfig({
    FLOWPLAYER_API_KEY: "  abc123\n",
    ASSEMBLYAI_API_KEY: '"def456"',
    GCP_PROJECT: "   ",
  } as any);

  assert.equal(cfg.flowplayer.apiKey, "abc123",
    "et linjeskift på slutten gjør headeren ugyldig og gir 401");
  assert.equal(cfg.assemblyAiKey, "def456");
  assert.equal(cfg.vertex, undefined,
    "bare mellomrom er ikke et prosjekt-ID");
});

// --- Embed-modulen ---------------------------------------------------------

test("medie-URL-er finnes også når JS-en har escapet skråstreker", () => {
  const js = `var c={src:"https:\\/\\/cdn.example.com\\/a\\/master.m3u8",t:1};`;
  assert.deepEqual(finnMediaUrler(js), ["https://cdn.example.com/a/master.m3u8"]);
});

test("unicode-escapede skråstreker håndteres også", () => {
  const js = `{"url":"https:\\u002F\\u002Fcdn.test\\u002Ffil.mp4"}`;
  assert.deepEqual(finnMediaUrler(js), ["https://cdn.test/fil.mp4"]);
});

test("URL-er plukkes ut uten hermetegn og komma på slutten", () => {
  const js = `sources:["https://a.com/x.m3u8","https://a.com/y.mp4"],poster:"https://a.com/p.jpg"`;
  const f = finnMediaUrler(js);
  assert.deepEqual(f, ["https://a.com/x.m3u8", "https://a.com/y.mp4"],
    "poster-bildet er ikke media og skal ikke med");
});

test("duplikater fjernes", () => {
  const js = `a="https://x/f.m3u8"; b="https://x/f.m3u8";`;
  assert.equal(finnMediaUrler(js).length, 1);
});

test("ingen media gir tom liste, ikke krasj", () => {
  assert.deepEqual(finnMediaUrler("console.log('hei')"), []);
});

// --- Spilleliste vs. ekte mediefil ----------------------------------------

test("spillelister velges bort når mottakeren skal laste ned fila selv", () => {
  const enc = [
    { format: "hls", bitrate: 2600, video_file_url: "https://x/master.m3u8" },
    { format: "mpeg-dash", bitrate: 2600, video_file_url: "https://x/m.mpd" },
    { format: "1080p", bitrate: 4400, video_file_url: "https://x/1080.mp4" },
    { format: "240p", bitrate: 400, video_file_url: "https://x/240.mp4" },
  ];
  // ffmpeg vil ha manifesten – den kan hente kun lydrendisjonen derfra.
  assert.equal(pickEncoding(enc)?.format, "hls");
  // AssemblyAI laster ned fila selv og kan ikke lese en spilleliste.
  assert.equal(pickProgressive(enc)?.format, "240p");
});

test("bilder og teasere er ikke mediefiler", () => {
  const valgt = pickProgressive([
    { format: "img", bitrate: 1, video_file_url: "https://x/p.jpg" },
    { format: "teaser", bitrate: 2, video_file_url: "https://x/t.mp4" },
    { format: "360p", bitrate: 750, video_file_url: "https://x/360.mp4" },
  ]);
  assert.equal(valgt?.format, "360p");
});

test("bare spillelister gir ingen progressiv variant", () => {
  assert.equal(pickProgressive([
    { format: "hls", bitrate: 2600, video_file_url: "https://x/m.m3u8" },
  ]), undefined, "da må ffmpeg til – og feilmeldingen må si det");
});

// --- Navnerettelser (ekte feil fra AssemblyAI) ----------------------------

const nesRoster = () => ({
  source: "upload" as const, documentKind: "ukjent" as const,
  documentName: "nes", agenda: [],
  people: JSON.parse(readFileSync("./rosters/nes.json", "utf8")).people,
});

const seg = (text: string) => [{ start: 0, end: 5, speaker: null, text, words: [] }];

test("retter det AssemblyAI faktisk hørte feil", () => {
  const f = finnNavnerettinger(
    seg("Da gir jeg ordet til Tone Rønnaug Tangen fra Arbeiderpartiet."),
    nesRoster(),
  );
  assert.equal(f.length, 1);
  assert.equal(f[0]!.from, "Tone Rønnaug Tangen");
  assert.equal(f[0]!.to, "Tone Rønoldtangen");
  assert.ok(f[0]!.score >= 0.7);
});

test("rettingen settes inn i teksten", () => {
  const s = seg("Takk til Tone Rønnaug Tangen for innlegget.");
  const ut = anvendRettinger(s, finnNavnerettinger(s, nesRoster()));
  assert.equal(ut[0]!.text, "Takk til Tone Rønoldtangen for innlegget.");
});

test("navn som allerede er riktige røres ikke", () => {
  assert.deepEqual(
    finnNavnerettinger(seg("Ordfører Tove Nyhus åpnet møtet."), nesRoster()),
    [],
  );
});

test("vanlige ord rettes ikke til navn selv om de likner litt", () => {
  const f = finnNavnerettinger(
    seg("Det Nye Forslaget Ble Vedtatt Med Fire Mot Tre Stemmer."),
    nesRoster(),
  );
  assert.deepEqual(f, [], "falske treff er verre enn å la noe stå urettet");
});

test("uten klar margin til nest beste rettes ingenting", () => {
  const roster = {
    ...nesRoster(),
    people: [
      { id: "a", name: "Jon Hansen", party: null, role: null, group: "folkevalgt" as const, present: null },
      { id: "b", name: "Jan Hansen", party: null, role: null, group: "folkevalgt" as const, present: null },
    ],
  };
  assert.deepEqual(
    finnNavnerettinger(seg("Representanten Jen Hansen tok ordet."), roster),
    [], "to like nære kandidater — da vet vi ikke hvem som ble sagt",
  );
});

test("likhet regner riktig på norske tegn", () => {
  assert.equal(likhet("Rønoldtangen", "Rønoldtangen"), 1);
  assert.ok(likhet("Rønnaug Tangen", "Rønoldtangen") > 0.6);
  assert.ok(likhet("Blådammen", "Blodammen") > 0.85);
});

// --- Adgangskontroll -------------------------------------------------------

test("bare det eksakte domenet slipper inn", () => {
  assert.equal(harLovligDomene("fred@amedia.no", "amedia.no"), true);
  assert.equal(harLovligDomene("Fred@Amedia.NO", "amedia.no"), true,
    "store bokstaver i e-post skal ikke sperre noen ute");

  // Den farlige varianten: et domene som SLUTTER på amedia.no.
  assert.equal(harLovligDomene("angriper@ikke-amedia.no", "amedia.no"), false);
  assert.equal(harLovligDomene("angriper@amedia.no.evil.com", "amedia.no"), false);
  assert.equal(harLovligDomene("fred@gmail.com", "amedia.no"), false);
  // To krøllalfa kan brukes til å lure enkle sjekker.
  assert.equal(harLovligDomene("a@b@amedia.no", "amedia.no"), false);
});

test("innlogging regnes som usatt når noe mangler", () => {
  const fullt = {
    GOOGLE_CLIENT_ID: "x", GOOGLE_CLIENT_SECRET: "y",
    SESSION_SECRET: "z", PUBLIC_URL: "https://a.no",
  };
  assert.ok(lesAuthConfig(fullt as any));
  for (const mangler of Object.keys(fullt)) {
    const delvis = { ...fullt, [mangler]: "" };
    assert.equal(lesAuthConfig(delvis as any), null,
      `uten ${mangler} skal oppsettet regnes som ufullstendig, ikke halvveis aktivt`);
  }
});

test("sesjonscookie kan ikke forfalskes", () => {
  const cfg = {
    clientId: "x", clientSecret: "y", sessionSecret: "hemmelig",
    allowedDomain: "amedia.no", publicUrl: "https://a.no",
  };
  const cookie = lagSessionCookie(
    { email: "fred@amedia.no", exp: Date.now() + 60000 }, cfg,
  );
  const verdi = cookie.split(";")[0]!.split("=").slice(1).join("=");

  const les = (c: string) =>
    lesSession({ headers: { cookie: `moteskriver_session=${c}` } } as any, cfg);

  assert.equal(les(verdi)?.email, "fred@amedia.no");

  // Bytt ut innholdet, behold signaturen.
  const falsk = Buffer.from(JSON.stringify(
    { email: "angriper@evil.com", exp: Date.now() + 60000 },
  )).toString("base64url");
  assert.equal(les(`${falsk}.${verdi.split(".")[1]}`), null);

  // Utløpt sesjon.
  const gammel = lagSessionCookie({ email: "fred@amedia.no", exp: Date.now() - 1 }, cfg);
  assert.equal(les(gammel.split(";")[0]!.split("=").slice(1).join("=")), null);
});

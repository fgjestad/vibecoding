import { test } from "node:test";
import assert from "node:assert/strict";
import { vocabularyFrom } from "../src/steps/roster.js";
import { MockASR } from "../src/providers/asr/mock.js";
import { transcribe } from "../src/steps/transcribe.js";
import { pickEncoding } from "../src/providers/video/flowplayer.js";
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

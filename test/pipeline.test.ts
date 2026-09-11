import { test } from "node:test";
import assert from "node:assert/strict";
import { vocabularyFrom } from "../src/steps/roster.js";
import { MockASR } from "../src/providers/asr/mock.js";
import { transcribe } from "../src/steps/transcribe.js";
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

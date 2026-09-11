import { z } from "zod";
import type { ClaudeClient } from "../providers/llm/claude.js";
import type { Roster, SpeakerMapping, Transcript } from "../types.js";
import { ambiguousSurnames } from "./roster.js";

const MatchSchema = z.object({
  matches: z.array(
    z.object({
      speaker: z.string(),
      personId: z.string().nullable(),
      label: z.string().nullable(),
      confidence: z.number().min(0).max(1),
      reasoning: z.string(),
    }),
  ),
});

const SYSTEM = `Du kobler anonyme talere fra et transkript til navngitte personer på en deltakerliste.

Møtet er et norsk kommunalt politisk møte. Debatten er full av holdepunkter:
møteledelse ("takk, ordfører", "da gir jeg ordet til"), tiltaleformer
("representanten Hansen"), selvpresentasjon, og roller som avslører seg gjennom
hva personen snakker om (kommunedirektøren svarer på administrative spørsmål).

Regler:
- personId MÅ være en id fra deltakerlista, eller null.
- Snakker noen som ikke står på lista – innledere, eksterne, publikum i
  spørretimen – sett personId=null og gi en beskrivende label.
- Er du i tvil, sett lav confidence. Et menneske bekrefter alt før det brukes.
  Et feil navn på et sitat er den dyreste feilen dette systemet kan gjøre,
  så gjett aldri for å fylle ut.
- Er det flere talere enn personer som var til stede, har diariseringen
  sannsynligvis splittet én person i to. Si fra om det i reasoning.

DELTE ETTERNAVN — les dette nøye:
Flere personer på lista kan dele etternavn, noen ganger i samme parti. Blir
en taler bare omtalt med et slikt etternavn, er det IKKE et entydig
holdepunkt. Da må du enten finne noe annet som skiller dem — fornavn, rolle,
saksfelt de har ordet i — eller sette confidence under 0.5 og si i reasoning
hvilke personer det står mellom. Gjett aldri på den ene for å slippe å være
usikker.`;

/**
 * Foreslår hvem hver SPEAKER_xx er.
 *
 * Fordi deltakerlista er et lukket sett, går dette fra «gjett et navn» til
 * «velg én av femten» – en helt annen feilrate. Men forslagene er og blir
 * forslag: ingenting med confirmed=false skal inn i en publisert artikkel.
 */
export async function matchSpeakers(
  claude: ClaudeClient,
  transcript: Transcript,
  roster: Roster,
): Promise<SpeakerMapping[]> {
  const speakers = [...new Set(transcript.segments.map((s) => s.speaker))].filter(
    (s): s is string => s !== null,
  );
  if (speakers.length === 0) return [];

  // Gi modellen de første ytringene per taler – det er der folk presenterer
  // seg og blir tiltalt ved navn.
  const proever = speakers.map((sp) => {
    const yt = transcript.segments
      .filter((s) => s.speaker === sp)
      .slice(0, 5)
      .map((s) => `  [${fmt(s.start)}] ${s.text}`)
      .join("\n");
    return `${sp}:\n${yt}`;
  });

  const tilstede = roster.people.filter((p) => p.present !== false);
  const liste = tilstede
    .map(
      (p) =>
        `  ${p.id}: ${p.name}${p.party ? ` (${p.party})` : ""}${p.role ? ` – ${p.role}` : ""} [${p.group}]`,
    )
    .join("\n");

  const delte = ambiguousSurnames(tilstede);
  const advarsel = delte.size
    ? `\n\nDelte etternavn på denne lista (etternavn alene skiller ikke):\n` +
      [...delte].map(([e, n]) => `  ${e}: ${n.join(", ")}`).join("\n")
    : "";

  const res = await claude.structured({
    schema: MatchSchema,
    system: SYSTEM,
    effort: "high",
    content: [
      {
        type: "text",
        text:
          `Deltakerliste (${roster.documentKind}):\n${liste}\n\n` +
          `Talere i transkriptet (${speakers.length} stk):\n\n${proever.join("\n\n")}\n\n` +
          advarsel +
          `\n\nKoble hver taler til en person, eller til null med en label.`,
      },
    ],
  });

  const gyldige = new Map(roster.people.map((p) => [p.id, p.name]));
  return res.matches.map((m) => {
    // Slipper aldri gjennom en id som ikke finnes i rosteret.
    const personId = m.personId && gyldige.has(m.personId) ? m.personId : null;
    const navn = personId ? gyldige.get(personId)! : "";
    const etternavn = navn.split(/\s+/).slice(-1)[0] ?? "";

    // Hviler treffet på et etternavn flere deler, settes det tak på
    // konfidensen uansett hvor sikker modellen sier den er. Mennesket
    // skal se at dette er et valg mellom flere, ikke et faktum.
    const usikkert = delte.has(etternavn);
    return {
      speaker: m.speaker,
      personId,
      label: m.label ?? undefined,
      confidence: usikkert ? Math.min(m.confidence, 0.49) : m.confidence,
      confirmed: false,
    };
  });
}

function fmt(s: number): string {
  const t = Math.floor(s);
  return `${String(Math.floor(t / 3600)).padStart(2, "0")}:${String(
    Math.floor((t % 3600) / 60),
  ).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}

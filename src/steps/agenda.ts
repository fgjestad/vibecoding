import { z } from "zod";
import type { ClaudeClient } from "../providers/llm/claude.js";
import type { AgendaItem, Roster, Transcript } from "../types.js";

const AgendaSchema = z.object({
  items: z.array(
    z.object({
      ref: z.string(),
      startSec: z.number(),
      endSec: z.number(),
      found: z.boolean(),
    }),
  ),
});

const SYSTEM = `Du plasserer saker fra et kommunalt sakskart på møtets tidslinje.

Møteledelsen annonserer som regel hver sak når den tas opp – saksnummeret leses
høyt, eller tittelen refereres. Bruk det til å finne start- og sluttidspunkt.

- Sett found=false for saker som ikke ble behandlet i dette opptaket. Ikke gjett
  på tidspunkter for dem.
- Saker kan behandles i annen rekkefølge enn sakskartet.
- Tidspunktene skal være sekunder fra start av opptaket.`;

/**
 * Kobler sakskartet til tidslinja, slik at artikler kan skrives per sak
 * i stedet for per møte – som er slik en journalist faktisk jobber.
 *
 * Fordi sakskartet kommer fra kommunens eget dokument, forankres inndelingen
 * i deres saksnumre i stedet for i modellens tolkning. Det er også
 * koblingsnøkkelen til v2.0, der hver sak skal hentes sammen med sitt saksdokument.
 */
export async function placeAgenda(
  claude: ClaudeClient,
  transcript: Transcript,
  roster: Roster,
): Promise<AgendaItem[]> {
  if (roster.agenda.length === 0) return [];

  const oversikt = transcript.segments
    .map((s) => `[${Math.round(s.start)}] ${s.text}`)
    .join("\n");

  const sakskart = roster.agenda.map((a) => `  ${a.ref}: ${a.title}`).join("\n");

  const res = await claude.structured({
    schema: AgendaSchema,
    system: SYSTEM,
    effort: "high",
    content: [
      {
        type: "text",
        text:
          `Sakskart:\n${sakskart}\n\n` +
          `Transkript (sekunder i klammer):\n${oversikt}\n\n` +
          `Plasser hver sak på tidslinja.`,
      },
    ],
  });

  const tider = new Map(res.items.filter((i) => i.found).map((i) => [i.ref, i]));
  return roster.agenda.map((a) => {
    const t = tider.get(a.ref);
    return t ? { ...a, startSec: t.startSec, endSec: t.endSec } : a;
  });
}

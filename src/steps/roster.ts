import { readFile } from "node:fs/promises";
import { basename, extname } from "node:path";
import { z } from "zod";
import type { ClaudeClient } from "../providers/llm/claude.js";
import type { Roster } from "../types.js";

const RosterSchema = z.object({
  documentKind: z.enum(["innkalling", "protokoll", "ukjent"]),
  people: z.array(
    z.object({
      name: z.string(),
      party: z.string().nullable(),
      role: z.string().nullable(),
      group: z.enum(["folkevalgt", "administrasjon", "ekstern"]),
      present: z.boolean().nullable(),
    }),
  ),
  agenda: z.array(z.object({ ref: z.string(), title: z.string() })),
});

const SYSTEM = `Du leser dokumenter fra norske kommunale politiske møter – møteinnkallinger og møteprotokoller.

Du skal trekke ut to ting: deltakerlista og sakskartet.

Om oppmøte, som er den vanligste feilkilden:
- En PROTOKOLL er skrevet etter møtet. Den vet hvem som faktisk møtte, hvem som
  hadde forfall og hvilke varamedlemmer som stilte. Sett present=true/false ut fra det.
- En INNKALLING er skrevet før møtet. Den lister hvem som er innkalt, ikke hvem
  som kom. Sett present=null for alle. Ikke gjett.

Norske møtedokumenter bruker ofte overskrifter som «Faste medlemmer som møtte»,
«Faste medlemmer som ikke møtte», «Varamedlemmer som møtte» og «Fra administrasjonen
møtte». Layouten varierer mye mellom kommuner, så bruk overskriftene som hint, ikke
som fasit.

Folkevalgte får gruppe "folkevalgt" og partitilhørighet der den står (Ap, H, Sp, FrP,
SV, V, KrF, MDG, R osv.). Kommunedirektør, rådmann og saksbehandlere får
"administrasjon" og party=null.

Sakskartet: ta med saksnummeret ordrett slik kommunen skriver det, f.eks. "PS 45/25",
og tittelen på saken.

Ta aldri med navn som ikke står i dokumentet.`;

/**
 * Leser deltakerliste og sakskart ut av en innkalling eller protokoll.
 *
 * Dette steget kommer FØR transkripsjonen, ikke etter. Grunnen er at
 * navnelista er ordlista talegjenkjenningen skal biases med – egennavn er
 * nettopp det ASR bommer på, og en deltakerliste er en ferdig kuratert liste
 * over akkurat de navnene som blir sagt hundre ganger i møtet.
 *
 * Samme dokument gir sakskartet gratis, som er det saksinndelingen forankres i.
 */
export async function parseRoster(
  claude: ClaudeClient,
  docPath: string,
): Promise<Roster> {
  const ext = extname(docPath).toLowerCase();
  const data = (await readFile(docPath)).toString("base64");

  const block =
    ext === ".pdf"
      ? ({
          type: "document" as const,
          source: {
            type: "base64" as const,
            media_type: "application/pdf" as const,
            data,
          },
        })
      : ({
          type: "image" as const,
          source: {
            type: "base64" as const,
            media_type: mediaTypeFor(ext),
            data,
          },
        });

  const parsed = await claude.structured({
    schema: RosterSchema,
    system: SYSTEM,
    effort: "high",
    content: [
      block,
      {
        type: "text",
        text: "Trekk ut deltakerlista og sakskartet fra dette møtedokumentet.",
      },
    ],
  });

  return {
    source: "upload",
    documentKind: parsed.documentKind,
    documentName: basename(docPath),
    people: parsed.people.map((p, i) => ({ ...p, id: `p${i + 1}` })),
    agenda: parsed.agenda,
  };
}

function mediaTypeFor(ext: string): "image/png" | "image/jpeg" | "image/webp" {
  if (ext === ".png") return "image/png";
  if (ext === ".webp") return "image/webp";
  return "image/jpeg";
}

/**
 * Ordlista som sendes til talegjenkjenningen.
 *
 * Bevisst begrenset til navn, parti og roller. Hele dokumentet ville gitt
 * modellen for mye å «gjenkjenne», og Whisper-baserte motorer skriver da inn
 * navn som aldri ble sagt.
 */
export function vocabularyFrom(roster: Roster): string[] {
  const ord = new Set<string>();
  for (const p of roster.people) {
    ord.add(p.name);
    // Etternavn alene – slik representanter omtales i debatten.
    const deler = p.name.split(/\s+/);
    if (deler.length > 1) ord.add(deler[deler.length - 1]!);
    if (p.role) ord.add(p.role);
    if (p.party) ord.add(p.party);
  }
  for (const s of roster.agenda) ord.add(s.ref);
  return [...ord].filter((o) => o.length > 1);
}

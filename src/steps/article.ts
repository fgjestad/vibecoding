import { z } from "zod";
import type { ClaudeClient } from "../providers/llm/claude.js";
import type {
  AgendaItem,
  Article,
  Roster,
  SpeakerMapping,
  Transcript,
} from "../types.js";

const ArticleSchema = z.object({
  headline: z.string(),
  body: z.string(),
  citations: z.array(
    z.object({
      claim: z.string(),
      startSec: z.number(),
      endSec: z.number(),
      quote: z.string(),
      speakerPersonId: z.string().nullable(),
    }),
  ),
});

const SYSTEM = `Du skriver artikkelutkast for en norsk lokalavisjournalist, basert på
transkript fra et kommunalt politisk møte.

Dette er et UTKAST til en journalist, ikke en ferdig publisert tekst. Journalisten
redigerer, verifiserer og står ansvarlig.

Ufravikelige regler:

1. Skriv bare det som er dekket av transkriptet. Ingen bakgrunn du «vet», ingen
   antakelser om motiver, ingen utfylling av det som ikke ble sagt.
2. Hver faktapåstand skal ha en citation med tidspunkt og ordrett sitat fra
   transkriptet. Dette er journalistens verifiseringsverktøy – uten det er
   teksten ubrukelig.
3. Sitater gjengis ordrett. Rydd gjerne i «eh» og gjentakelser, men aldri i
   meningsinnholdet.
4. Tilskriv aldri en uttalelse til en person med mindre taleren er bekreftet.
   Er taleren ubekreftet, skriv "en representant" eller liknende.
5. Er transkriptet uklart på et punkt du trenger, la det stå åpent i stedet for
   å fylle inn. Skriv heller kort enn usikkert.

Språk: norsk bokmål, nøktern lokaljournalistikk. Ingen adjektivbruk som tar
stilling. Navn med partitilhørighet første gang: «Kari Nordmann (Ap)».`;

/**
 * Skriver et artikkelutkast fra transkriptet, forankret i tidsstempler.
 *
 * Citations er ikke pynt: de er grunnen til at en journalist kan stole på
 * utkastet. Hver påstand skal kunne klikkes tilbake til sekundet i videoen.
 */
export async function writeArticle(
  claude: ClaudeClient,
  args: {
    prompt: string;
    transcript: Transcript;
    roster: Roster;
    speakers: SpeakerMapping[];
    agendaItem?: AgendaItem;
  },
): Promise<Article> {
  const { prompt, transcript, roster, speakers, agendaItem } = args;

  // Avgrens til én sak når vi har tidsintervallet for den.
  const fra = agendaItem?.startSec ?? 0;
  const til = agendaItem?.endSec ?? Number.POSITIVE_INFINITY;
  const segmenter = transcript.segments.filter(
    (s) => s.end >= fra && s.start <= til,
  );

  const navn = new Map(roster.people.map((p) => [p.id, p]));
  const talere = new Map(speakers.map((s) => [s.speaker, s]));

  const tekst = segmenter
    .map((s) => {
      const m = s.speaker ? talere.get(s.speaker) : undefined;
      const p = m?.personId ? navn.get(m.personId) : undefined;
      // Ubekreftede match vises som usikre, slik at modellen ikke tilskriver
      // uttalelser til navn ingen har godkjent.
      const hvem = p
        ? m!.confirmed
          ? `${p.name}${p.party ? ` (${p.party})` : ""}`
          : `USIKKER: kanskje ${p.name}`
        : (m?.label ?? s.speaker ?? "ukjent taler");
      return `[${Math.round(s.start)}s] ${hvem}: ${s.text}`;
    })
    .join("\n");

  const advarsel =
    transcript.timestampQuality === "approximate"
      ? "\n\nMERK: tidsstemplene i dette transkriptet er omtrentlige (ikke målt). " +
        "Bruk dem likevel i citations, men de må verifiseres manuelt."
      : "";

  const res = await claude.structured({
    schema: ArticleSchema,
    system: SYSTEM,
    effort: "high",
    maxTokens: 16000,
    content: [
      {
        type: "text",
        text:
          `Oppdrag fra journalisten:\n${prompt}\n\n` +
          (agendaItem ? `Sak: ${agendaItem.ref} – ${agendaItem.title}\n\n` : "") +
          `Transkript:\n${tekst}${advarsel}`,
      },
    ],
  });

  return {
    agendaRef: agendaItem?.ref ?? null,
    prompt,
    headline: res.headline,
    body: res.body,
    citations: res.citations,
    generatedAt: new Date().toISOString(),
  };
}

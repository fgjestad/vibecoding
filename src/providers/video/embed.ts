import type { VideoRef } from "../../types.js";
import type { ResolvedMedia, VideoSource } from "./index.js";

const EMBED = "https://embed.flowplayer.com";

/**
 * Henter mediefila fra Flowplayers offentlige embed-modul.
 *
 * En avspiller må vite hvor mediefila ligger, og embed-modulen er offentlig
 * tilgjengelig uten autentisering — den lastes jo av hver eneste leser som
 * åpner en artikkel. Så URL-ene står der.
 *
 * Dette er en RESERVEVEI, ikke hovedveien. Platform-API-et er dokumentert og
 * gir tittel, varighet, kapittelmarkører og undertekster i tillegg. Denne gir
 * bare mediefila, og den kan slutte å virke hvis Flowplayer endrer formatet
 * på embed-modulen.
 *
 * Men den krever ingen nøkkel og ingen arbeidsområde-tilgang, så den er verdt
 * å ha når tilgangene henger.
 *
 * IKKE VERIFISERT mot ekte data — parsingen er skrevet formatuavhengig
 * nettopp derfor: den leter etter medie-URL-er uansett hvordan modulen er
 * bygget opp, i stedet for å anta en bestemt struktur.
 */
export class EmbedSource implements VideoSource {
  readonly name = "embed";

  constructor(private cfg: { publisherId: string }) {}

  async resolve(ref: VideoRef): Promise<ResolvedMedia> {
    const url = `${EMBED}/${this.cfg.publisherId}/${ref.videoId}.js`;
    const res = await fetch(url, {
      headers: { "user-agent": "Mozilla/5.0", accept: "*/*" },
    });
    if (!res.ok) {
      throw new Error(
        `Embed-modulen svarte ${res.status} for ${ref.videoId}. ` +
          "Sjekk at video-ID og publisher-ID (pi) hører sammen.",
      );
    }

    const kilder = finnMediaUrler(await res.text());
    if (kilder.length === 0) {
      throw new Error(
        `Fant ingen mediefil i embed-modulen for ${ref.videoId}. ` +
          "Formatet kan ha endret seg – bruk Platform-API-et " +
          "(VIDEO_PROVIDER=flowplayer) i stedet.",
      );
    }

    const hls = kilder.find((k) => k.includes(".m3u8"));
    // En spilleliste er ikke en mediefil. Tjenester som laster ned selv
    // trenger den progressive varianten.
    const progressiv = kilder.find((k) => /\.(mp4|m4a)(\?|$)/i.test(k));
    return {
      url: hls ?? progressiv ?? kilder[0]!,
      progressiveUrl: progressiv,
      hasSeparateAudio: Boolean(hls),
      durationSec: null,   // embed-modulen gir oss ikke dette pålitelig
      title: null,
    };
  }
}

/**
 * Plukker medie-URL-er ut av en JS-modul.
 *
 * Bevisst formatuavhengig: vi vet ikke hvordan modulen er bygget opp, og den
 * kan endres når som helst. Å lete etter URL-er som ser ut som media tåler
 * omskrivinger som en strukturell parsing ikke ville gjort.
 */
export function finnMediaUrler(js: string): string[] {
  // URL-er i JS er ofte escapet: "https:\/\/cdn\/fil.m3u8"
  const tekst = js.replace(/\\\//g, "/").replace(/\\u002[fF]/g, "/");
  const treff = tekst.match(/https?:\/\/[^\s"'`,)\]}\\]+\.(m3u8|mpd|mp4|m4a)(\?[^\s"'`,)\]}\\]*)?/gi);
  return [...new Set(treff ?? [])];
}

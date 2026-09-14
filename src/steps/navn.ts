import type { NameFix, Roster, Segment } from "../types.js";
import { ambiguousSurnames } from "./roster.js";

/**
 * Retter opp navn talegjenkjenningen har hørt feil.
 *
 * ASR bommer systematisk på egennavn — «Tone Rønoldtangen» ble til
 * «Tone Rønnaug Tangen». Ordlista vi sender inn hjelper, men fanger ikke alt,
 * og navnet må uansett bli riktig før det havner i en publisert sak.
 *
 * To regler holder dette trygt:
 *
 *   1. Ingenting rettes uten klar margin til nest beste kandidat. Er to
 *      personer omtrent like nær, er det ikke åpenbart hvem som ble sagt, og
 *      da lar vi det stå.
 *   2. Ingen retting er usynlig. Hver eneste endring registreres med original
 *      og score, og vises i transkriptet. Journalisten skal kunne se hva
 *      systemet har gjort med kildematerialet.
 */

const MIN_SCORE = 0.7;      // under dette er det ikke samme navn
const MIN_MARGIN = 0.15;    // avstand til nest beste, ellers er det for nære

export function levenshtein(a: string, b: string): number {
  if (a.length < b.length) [a, b] = [b, a];
  let forrige = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 0; i < a.length; i++) {
    const rad = [i + 1];
    for (let j = 0; j < b.length; j++) {
      rad.push(Math.min(
        forrige[j + 1]! + 1,
        rad[j]! + 1,
        forrige[j]! + (a[i] === b[j] ? 0 : 1),
      ));
    }
    forrige = rad;
  }
  return forrige[b.length]!;
}

/** Normaliserer bort tegnsetting og store bokstaver, men beholder æøå. */
const norm = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");

export function likhet(a: string, b: string): number {
  const x = norm(a), y = norm(b);
  if (!x || !y) return 0;
  return 1 - levenshtein(x, y) / Math.max(x.length, y.length);
}

/**
 * Leter etter feilhørte navn i transkriptet.
 *
 * Ser bare på ordgrupper som begynner med stor forbokstav — det luker bort
 * det aller meste av falske treff uten å miste navn.
 */
export function finnNavnerettinger(
  segmenter: Segment[],
  roster: Roster,
): NameFix[] {
  if (roster.people.length === 0) return [];

  const delteEtternavn = ambiguousSurnames(roster.people);
  const kandidater = roster.people.map((p) => ({
    person: p,
    navn: p.name,
    // Etternavn alene er bare et trygt holdepunkt når ingen deler det.
    etternavn: p.name.split(/\s+/).slice(-1)[0]!,
  }));

  const fikser: NameFix[] = [];

  segmenter.forEach((seg, i) => {
    const ord = seg.text.split(/\s+/);
    for (let start = 0; start < ord.length; start++) {
      if (!/^[A-ZÆØÅ]/.test(ord[start] ?? "")) continue;

      for (let lengde = 3; lengde >= 2; lengde--) {
        const bit = ord.slice(start, start + lengde).join(" ");
        if (bit.split(/\s+/).length < lengde) continue;

        const rent = bit.replace(/[.,:;!?]+$/, "");
        const treff = rangerTreff(rent, kandidater);
        if (!treff) continue;

        // Er hele treffet bare et delt etternavn, vet vi ikke hvem det er.
        if (delteEtternavn.has(treff.kandidat.etternavn) &&
            likhet(rent, treff.kandidat.etternavn) > likhet(rent, treff.kandidat.navn)) {
          continue;
        }

        if (rent !== treff.kandidat.navn) {
          fikser.push({
            segment: i,
            from: rent,
            to: treff.kandidat.navn,
            personId: treff.kandidat.person.id,
            score: Number(treff.score.toFixed(3)),
          });
        }
        start += lengde - 1;   // ikke overlapp med samme navn igjen
        break;
      }
    }
  });

  return fikser;
}

function rangerTreff(
  bit: string,
  kandidater: { person: { id: string }; navn: string; etternavn: string }[],
) {
  const lengde = norm(bit).length;
  const scorer = kandidater
    // Billig forhåndsfilter: navn med helt annen lengde kan ikke være samme navn.
    .filter((k) => {
      const l = norm(k.navn).length;
      return l >= lengde * 0.6 && l <= lengde * 1.6;
    })
    .map((k) => ({ kandidat: k, score: likhet(bit, k.navn) }))
    .sort((a, b) => b.score - a.score);

  const beste = scorer[0];
  if (!beste || beste.score < MIN_SCORE) return null;
  // Er nest beste omtrent like nær, er det ikke åpenbart hvem som ble sagt.
  if ((scorer[1]?.score ?? 0) > beste.score - MIN_MARGIN) return null;
  return beste;
}

/** Bruker rettingene på teksten. Originalen ligger igjen i NameFix. */
export function anvendRettinger(segmenter: Segment[], fikser: NameFix[]): Segment[] {
  const perSegment = new Map<number, NameFix[]>();
  for (const f of fikser) {
    perSegment.set(f.segment, [...(perSegment.get(f.segment) ?? []), f]);
  }

  return segmenter.map((seg, i) => {
    const mine = perSegment.get(i);
    if (!mine?.length) return seg;
    let tekst = seg.text;
    // Lengste først, så «Tone Rønnaug Tangen» ikke ødelegges av en kortere treff.
    for (const f of [...mine].sort((a, b) => b.from.length - a.from.length)) {
      tekst = tekst.split(f.from).join(f.to);
    }
    return { ...seg, text: tekst };
  });
}

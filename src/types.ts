/**
 * Datamodellen for ett møte.
 *
 * Bærende prinsipp: `Transcript` er eneste sannhet. Den produseres én gang og
 * gjenbrukes av alle senere steg, slik at fire timer aldri transkriberes to
 * ganger. Alt som utledes av den (saksinndeling, talernavn, artikler) lagres
 * ved siden av, aldri inni.
 *
 * Andre prinsipp: fakta og slutninger holdes fra hverandre. `Roster` er et
 * faktum om møtet, hentet fra innkalling eller protokoll. `SpeakerMapping` er
 * en slutning om hvem SPEAKER_03 er. Da kan slutningen gjøres om igjen uten å
 * røre faktaene, og faktakilden byttes (v2.0: hentet automatisk) uten at noe
 * annet endres.
 */

// ---------------------------------------------------------------- Kilde

/** Peker på møtevideoen. Lagres aldri som ferdig oppløst manifest-URL:
 *  de inneholder tidsbegrensede tokens og går ut på dato. ID-en gjør ikke det. */
export interface VideoRef {
  provider: "flowplayer" | "mock";
  /** Flowplayer-videoens ID – det journalisten limer inn. */
  videoId: string;
  /** Flowplayer workspace/publisher-ID. Konstant for hele Amedia. */
  workspaceId?: string;
}

export interface AudioArtifact {
  path: string;
  codec: string;
  bitrateKbps: number;
  sampleRate: number;
  channels: number;
  durationSec: number;
  sizeBytes: number;
  /** For idempotens: samme lyd gir samme hash gir ingen ny transkripsjon. */
  sha256: string;
}

// ---------------------------------------------------------------- Deltakere

export type PersonGroup = "folkevalgt" | "administrasjon" | "ekstern";

export interface Person {
  id: string;
  name: string;
  /** Partitilhørighet, f.eks. "Ap", "H", "Sp". Null for administrasjonen. */
  party: string | null;
  /** "ordfører", "varaordfører", "kommunedirektør", "representant" ... */
  role: string | null;
  group: PersonGroup;
  /**
   * true  = møtte (står i protokollen)
   * false = forfall
   * null  = ukjent – typisk når kilden er en innkalling, som er skrevet før
   *         møtet og derfor ikke vet om forfall og vara.
   */
  present: boolean | null;
}

/** Én sak fra sakskartet, f.eks. "PS 45/25 – Detaljregulering Storgata 4". */
export interface AgendaItem {
  /** Saksnummeret slik kommunen skriver det: "PS 45/25". */
  ref: string;
  title: string;
  /** Fylles i steg 6 når saken er plassert på møtets tidslinje. */
  startSec?: number;
  endSec?: number;
}

export interface Roster {
  source: "upload" | "fetched";
  /** "innkalling" har ikke oppmøtedata; "protokoll" har det. */
  documentKind: "innkalling" | "protokoll" | "ukjent";
  documentName: string;
  people: Person[];
  agenda: AgendaItem[];
}

// ---------------------------------------------------------------- Transkript

export interface Word {
  w: string;
  start: number;
  end: number;
  /** 0–1. Lav konfidens markeres i grensesnittet: «her er modellen i tvil». */
  conf?: number;
}

export interface Segment {
  start: number;
  end: number;
  /** Diariseringsetikett fra ASR – "SPEAKER_03". Aldri et personnavn. */
  speaker: string | null;
  text: string;
  words: Word[];
}

export interface Transcript {
  language: string;
  /** Hvilken motor som lagde dette. Runde 1: "gemini". Runde 2: "google-stt". */
  engine: string;
  engineVersion: string;
  /** Ord-nivå tidsstempler er presise hos ekte ASR, omtrentlige hos Gemini.
   *  Grensesnittet må kunne si fra om hvilken det er. */
  timestampQuality: "exact" | "approximate";
  durationSec: number;
  segments: Segment[];
}

// ---------------------------------------------------------------- Slutninger

export interface SpeakerMapping {
  /** Diariseringsetiketten, "SPEAKER_03". */
  speaker: string;
  /** Peker inn i Roster.people. Null når taleren ikke står på deltakerlista. */
  personId: string | null;
  /** Fritekst for talere utenfor lista: "ekstern innleder", "publikum". */
  label?: string;
  /** Systemets forslag, 0–1. Vises, men avgjør aldri alene. */
  confidence: number;
  /** Ingen navn havner i en artikkel før et menneske har bekreftet det. */
  confirmed: boolean;
}

export interface Article {
  agendaRef: string | null;
  prompt: string;
  headline: string;
  body: string;
  /** Hver påstand peker tilbake til et tidspunkt i videoen. Uten dette kan
   *  ikke journalisten verifisere, og da er teksten ubrukelig. */
  citations: Citation[];
  generatedAt: string;
}

export interface Citation {
  /** Tekstutdraget i artikkelen som denne kilden dekker. */
  claim: string;
  startSec: number;
  endSec: number;
  /** Ordrett fra transkriptet – det journalisten sjekker mot lyden. */
  quote: string;
  speakerPersonId: string | null;
}

// ---------------------------------------------------------------- Jobb

export type JobStatus =
  | "opprettet"
  | "henter-lyd"
  | "leser-dokument"
  | "transkriberer"
  | "matcher-talere"
  | "venter-pa-bekreftelse"
  | "klar"
  | "feilet";

export interface Job {
  id: string;
  status: JobStatus;
  createdAt: string;
  video: VideoRef;
  audio?: AudioArtifact;
  roster?: Roster;
  transcript?: Transcript;
  speakers?: SpeakerMapping[];
  articles: Article[];
  error?: string;
}

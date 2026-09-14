import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { Roster } from "../types.js";

export interface NavngittListe {
  id: string;
  navn: string;
  opprettet: string;
  roster: Roster;
}

/**
 * Lagrer navnelister per kommune.
 *
 * Et kommunestyre er det samme fra møte til møte, så lista skrives inn én
 * gang og gjenbrukes. Lister som følger med i repoet (rosters/) leses som
 * utgangspunkt; nye lagres på datadisken og overlever en ny deploy.
 */
export class RosterStore {
  constructor(private dataDir: string, private repoDir = "rosters") {}

  private get skriveDir() {
    return join(this.dataDir, "rosters");
  }

  async list(): Promise<NavngittListe[]> {
    const alle = [
      ...(await this.lesMappe(this.repoDir)),
      ...(await this.lesMappe(this.skriveDir)),
    ];
    // Egne lister vinner over de som følger med, ved samme id.
    const unike = new Map(alle.map((l) => [l.id, l]));
    return [...unike.values()].sort((a, b) => a.navn.localeCompare(b.navn, "nb"));
  }

  async load(id: string): Promise<NavngittListe | null> {
    return (await this.list()).find((l) => l.id === id) ?? null;
  }

  async save(navn: string, roster: Roster): Promise<NavngittListe> {
    const id = navn.toLowerCase().replace(/[^a-z0-9æøå]+/g, "-").replace(/^-|-$/g, "")
      || `liste-${Date.now().toString(36)}`;
    const liste: NavngittListe = {
      id, navn, opprettet: new Date().toISOString(), roster,
    };
    await mkdir(this.skriveDir, { recursive: true });
    await writeFile(join(this.skriveDir, `${id}.json`), JSON.stringify(liste, null, 2));
    return liste;
  }

  private async lesMappe(dir: string): Promise<NavngittListe[]> {
    let filer: string[];
    try {
      filer = (await readdir(dir)).filter((f) => f.endsWith(".json"));
    } catch {
      return [];
    }
    const ut: NavngittListe[] = [];
    for (const f of filer) {
      try {
        const data = JSON.parse(await readFile(join(dir, f), "utf8"));
        // Listene i repoet er lagret i et enklere format enn NavngittListe.
        ut.push(
          data.roster
            ? (data as NavngittListe)
            : {
                id: f.replace(/\.json$/, ""),
                navn: `${data.kommune ?? f} ${data.organ ?? ""}`.trim(),
                opprettet: data.retrieved ?? "",
                roster: {
                  source: "upload", documentKind: "ukjent",
                  documentName: data.source ?? f,
                  people: data.people ?? [], agenda: [],
                },
              },
        );
      } catch { /* hopp over ødelagte filer */ }
    }
    return ut;
  }
}

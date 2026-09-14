import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";

export interface AuthConfig {
  clientId: string;
  clientSecret: string;
  sessionSecret: string;
  /** Bare e-poster på dette domenet slipper inn. */
  allowedDomain: string;
  /** Appens egen adresse, for tilbakekallet fra Google. */
  publicUrl: string;
}

export interface Session {
  email: string;
  exp: number;
}

const COOKIE = "moteskriver_session";
const VARIGHET = 12 * 60 * 60 * 1000;   // en arbeidsdag

export function lesAuthConfig(env = process.env): AuthConfig | null {
  const clientId = (env.GOOGLE_CLIENT_ID ?? "").trim();
  const clientSecret = (env.GOOGLE_CLIENT_SECRET ?? "").trim();
  const sessionSecret = (env.SESSION_SECRET ?? "").trim();
  const publicUrl = (env.PUBLIC_URL ?? "").trim().replace(/\/$/, "");
  if (!clientId || !clientSecret || !sessionSecret || !publicUrl) return null;
  return {
    clientId, clientSecret, sessionSecret, publicUrl,
    allowedDomain: (env.ALLOWED_DOMAIN ?? "amedia.no").trim().toLowerCase(),
  };
}

// ---------------------------------------------------------------- Sesjon

function signer(data: string, hemmelighet: string): string {
  return createHmac("sha256", hemmelighet).update(data).digest("base64url");
}

export function lagSessionCookie(s: Session, cfg: AuthConfig): string {
  const kropp = Buffer.from(JSON.stringify(s)).toString("base64url");
  const verdi = `${kropp}.${signer(kropp, cfg.sessionSecret)}`;
  const sikker = cfg.publicUrl.startsWith("https://") ? " Secure;" : "";
  return `${COOKIE}=${verdi}; Path=/; HttpOnly;${sikker} SameSite=Lax; Max-Age=${VARIGHET / 1000}`;
}

export function lesSession(req: IncomingMessage, cfg: AuthConfig): Session | null {
  const raa = lesCookie(req, COOKIE);
  if (!raa) return null;
  const [kropp, signatur] = raa.split(".");
  if (!kropp || !signatur) return null;

  // timingSafeEqual, ikke ===, så signaturen ikke kan gjettes tegn for tegn.
  const ventet = Buffer.from(signer(kropp, cfg.sessionSecret));
  const gitt = Buffer.from(signatur);
  if (ventet.length !== gitt.length || !timingSafeEqual(ventet, gitt)) return null;

  try {
    const s = JSON.parse(Buffer.from(kropp, "base64url").toString()) as Session;
    if (s.exp < Date.now()) return null;
    // Domenet sjekkes også her, ikke bare ved innlogging: endres ALLOWED_DOMAIN,
    // skal gamle sesjoner slutte å gjelde umiddelbart.
    if (!harLovligDomene(s.email, cfg.allowedDomain)) return null;
    return s;
  } catch {
    return null;
  }
}

export function harLovligDomene(email: string, domene: string): boolean {
  const e = email.trim().toLowerCase();
  // Sjekker på @domene, ikke "slutter på domene" – ellers ville
  // "noen@ikke-amedia.no" sluppet inn.
  return e.endsWith(`@${domene}`) && e.indexOf("@") === e.lastIndexOf("@");
}

function lesCookie(req: IncomingMessage, navn: string): string | null {
  for (const bit of (req.headers.cookie ?? "").split(";")) {
    const [k, ...v] = bit.trim().split("=");
    if (k === navn) return v.join("=");
  }
  return null;
}

// ---------------------------------------------------------------- Google

export function loginUrl(cfg: AuthConfig, state: string): string {
  const p = new URLSearchParams({
    client_id: cfg.clientId,
    redirect_uri: `${cfg.publicUrl}/auth/callback`,
    response_type: "code",
    scope: "openid email profile",
    // hd er bare et hint til Googles kontovelger. Det er IKKE en
    // sikkerhetsgrense – domenet må sjekkes på nytt når svaret kommer.
    hd: cfg.allowedDomain,
    state,
    prompt: "select_account",
  });
  return `https://accounts.google.com/o/oauth2/v2/auth?${p}`;
}

/**
 * Bytter inn koden fra Google mot brukerens e-post.
 *
 * Tokenet hentes med vår client_secret over TLS direkte fra Google, så
 * innholdet kan leses uten å verifisere signaturen selv — det er ingen
 * tredjepart i veien.
 */
export async function hentEpost(kode: string, cfg: AuthConfig): Promise<string> {
  const res = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      code: kode,
      client_id: cfg.clientId,
      client_secret: cfg.clientSecret,
      redirect_uri: `${cfg.publicUrl}/auth/callback`,
      grant_type: "authorization_code",
    }),
  });
  if (!res.ok) {
    throw new Error(`Google avviste innloggingen (${res.status}).`);
  }

  const { id_token } = (await res.json()) as { id_token?: string };
  if (!id_token) throw new Error("Google svarte uten id_token.");

  const del = id_token.split(".")[1];
  if (!del) throw new Error("Ugyldig id_token fra Google.");
  const krav = JSON.parse(Buffer.from(del, "base64url").toString()) as {
    email?: string;
    email_verified?: boolean;
  };

  if (!krav.email) throw new Error("Google oppga ingen e-postadresse.");
  if (krav.email_verified === false) {
    throw new Error("E-postadressen er ikke bekreftet hos Google.");
  }
  return krav.email;
}

export const nyState = () => randomBytes(16).toString("hex");

export function settStateCookie(state: string, cfg: AuthConfig): string {
  const sikker = cfg.publicUrl.startsWith("https://") ? " Secure;" : "";
  return `oauth_state=${state}; Path=/; HttpOnly;${sikker} SameSite=Lax; Max-Age=600`;
}

export const lesState = (req: IncomingMessage) => lesCookie(req, "oauth_state");

export function slettCookies(res: ServerResponse): void {
  res.setHeader("Set-Cookie", [
    `${COOKIE}=; Path=/; HttpOnly; Max-Age=0`,
    `oauth_state=; Path=/; HttpOnly; Max-Age=0`,
  ]);
}

export const sesjonsvarighet = VARIGHET;

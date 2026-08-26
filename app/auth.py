"""To brukernivåer: lesere slipper rett inn, mens redigering krever passord.

Innlogging holdes i en signert informasjonskapsel i stedet for HTTP basic-auth,
slik at leseren aldri møter en passordboks — de skal bare kunne se oversikten."""

import hashlib
import hmac
import os
import secrets
import time

from fastapi import HTTPException, Request, status

COOKIE_NAVN = "faglig_tirsdag_admin"
VARIGHET = 30 * 24 * 3600  # 30 dager


def _nokkel() -> bytes:
    """Nøkkelen som signerer innloggingen. Uten SECRET_KEY satt utledes den fra
    passordet, slik at appen virker lokalt uten ekstra oppsett — men da logges alle
    ut hvis passordet byttes, som uansett er ønsket oppførsel."""
    return (os.environ.get("SECRET_KEY") or f"avledet:{admin_passord()}").encode("utf-8")


def admin_passord() -> str:
    return os.environ.get("ADMIN_PASSORD", "")


def passord_er_satt() -> bool:
    return bool(admin_passord())


def sjekk_passord(forsoek: str) -> bool:
    return passord_er_satt() and hmac.compare_digest(forsoek, admin_passord())


def lag_token() -> str:
    utloeper = str(int(time.time()) + VARIGHET)
    signatur = hmac.new(_nokkel(), utloeper.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{utloeper}.{signatur}"


def _token_er_gyldig(token: str) -> bool:
    try:
        utloeper, signatur = token.split(".", 1)
    except ValueError:
        return False
    forventet = hmac.new(_nokkel(), utloeper.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signatur, forventet):
        return False
    try:
        return int(utloeper) > time.time()
    except ValueError:
        return False


def er_admin(request: Request) -> bool:
    """Sier om den som ber om siden er innlogget. Brukes til å bestemme hva som
    vises, ikke til å blokkere — blokkeringen skjer i krev_admin()."""
    if not passord_er_satt():
        return False
    token = request.cookies.get(COOKIE_NAVN, "")
    return bool(token) and _token_er_gyldig(token)


def krev_admin(request: Request) -> None:
    """Vaktpost på alle ruter som endrer noe."""
    if not er_admin(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Du må logge inn for å endre noe. Gå til /logg-inn.",
        )


def ny_hemmelig_nokkel() -> str:
    return secrets.token_urlsafe(32)

import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic()


def _stemmer(verdi: str, forventet: str) -> bool:
    return bool(forventet) and secrets.compare_digest(verdi, forventet)


def sjekk_passord(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Godtar innlogging som ENTEN admin ELLER journalist. Returnerer rollen ('admin' eller
    'journalist'), som resten av appen bruker til å styre hva brukeren får se og gjøre.

    Admin-innlogging er satt opp som før (APP_BRUKER/APP_PASSORD, med en beskyttelse mot at
    appen starter uten passord satt). Journalist-innlogging (APP_BRUKER_JOURNALIST/
    APP_PASSORD_JOURNALIST) er valgfri — hvis disse ikke er satt i miljøvariablene, finnes
    det rett og slett ingen journalist-pålogging ennå."""
    admin_bruker = os.environ.get("APP_BRUKER", "raumnes")
    admin_passord = os.environ.get("APP_PASSORD")
    if not admin_passord:
        raise HTTPException(
            status_code=500,
            detail="APP_PASSORD er ikke satt i miljøvariablene. Sett den før du starter appen.",
        )
    if _stemmer(credentials.username, admin_bruker) and _stemmer(credentials.password, admin_passord):
        return "admin"

    journalist_bruker = os.environ.get("APP_BRUKER_JOURNALIST", "")
    journalist_passord = os.environ.get("APP_PASSORD_JOURNALIST", "")
    if _stemmer(credentials.username, journalist_bruker) and _stemmer(credentials.password, journalist_passord):
        return "journalist"

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Feil brukernavn eller passord",
        headers={"WWW-Authenticate": "Basic"},
    )


def sjekk_admin(rolle: str = Depends(sjekk_passord)) -> str:
    """Krever admin-rollen spesifikt — brukes på kildeliste- og undersøkelsesruter, som
    journalist-brukeren ikke skal ha tilgang til."""
    if rolle != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Krever admin-tilgang")
    return rolle

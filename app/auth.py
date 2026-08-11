import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic()


def sjekk_passord(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    riktig_bruker = os.environ.get("APP_BRUKER", "raumnes")
    riktig_passord = os.environ.get("APP_PASSORD")
    if not riktig_passord:
        raise HTTPException(
            status_code=500,
            detail="APP_PASSORD er ikke satt i miljøvariablene. Sett den før du starter appen.",
        )
    bruker_ok = secrets.compare_digest(credentials.username, riktig_bruker)
    passord_ok = secrets.compare_digest(credentials.password, riktig_passord)
    if not (bruker_ok and passord_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Feil brukernavn eller passord",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

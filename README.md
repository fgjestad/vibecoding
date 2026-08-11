# vibecoding

## Raumnes arrangementer — jobb 1: Kildeliste

Kjør lokalt:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fyll inn APP_PASSORD (og ANTHROPIC_API_KEY for "oppdag nye kilder")
export $(cat .env | grep -v '^#' | xargs)
.venv/bin/uvicorn app.main:app --reload
```

Åpne http://127.0.0.1:8000 (brukernavn: verdien av `APP_BRUKER`, standard `raumnes`; passord: `APP_PASSORD`).

# Categorisation System Backend

FastAPI and MongoDB API for Airport Categorisation. People sign in with SelfBrief CMS. This service does not store passwords.

Behaviour, auth cases, and the data we do and do not update are in [docs/SYSTEM.md](docs/SYSTEM.md).

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env` (it is not committed) and run:

```bash
python3 main.py
```

The API is at `http://localhost:8000`, with docs at `http://localhost:8000/docs`.

## Environment

| Variable | Description |
|---|---|
| `ENV` | `local`, `dev`, or `prod` |
| `MONGO_URI` | MongoDB connection string |
| `MONGO_DB_NAME` | Database name |
| `WEBSITE_DOMAIN` | Frontend origin, used for CORS |
| `SELFBRIEF_ISSUER` | CMS OIDC issuer |
| `SELFBRIEF_BASE_URL` | CMS API origin, used for `/api/v1/sso/me/` |

AWS, Resend, and the job worker settings are in `config/settings.py`.

## Deployment

GitHub Actions builds and deploys on push:

- `main` deploys the prod ECS service.
- `dev` deploys the dev ECS service.

See `.github/workflows/`.

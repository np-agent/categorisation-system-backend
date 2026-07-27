# Categorisation System Backend

A simple FastAPI backend using MongoDB for data storage and SuperTokens for authentication.

## Tech Stack

- **[FastAPI](https://fastapi.tiangolo.com/)** — web framework
- **[MongoDB](https://www.mongodb.com/)** (via [Motor](https://motor.readthedocs.io/)) — database
- **[SuperTokens](https://supertokens.com/)** — authentication / session management
- **[slowapi](https://github.com/laurentS/slowapi)** — rate limiting
- **Docker** — containerization
- **GitHub Actions** — CI/CD to AWS ECS

## Project Structure

```
backend/
├── api/
│   ├── health.py          # health check endpoint
│   └── v1/
│       └── users.py       # test users endpoints (Mongo + SuperTokens)
├── config/
│   ├── settings.py        # environment variables / app settings
│   ├── cors.py            # CORS origin config
│   ├── limiter.py         # rate limiter instance
│   └── supertoken_config.py # SuperTokens init
├── database/
│   ├── database.py        # Mongo client/connection
│   └── session.py         # connect/close/get_database helpers
├── middleware/
│   ├── rate_limit.py       # tags each request with a user_id/IP for rate limiting
│   └── verify_session.py   # SuperTokens session verification dependency
├── models/
│   └── user.py             # Pydantic models
├── main.py                 # app entrypoint
├── requirements.txt
└── Dockerfile
```

## Local Setup

1. **Create a virtual environment and install dependencies**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure environment variables**

   Create a `.env` file in `backend/` (see [Environment Variables](#environment-variables) below).

3. **Run the app**

   ```bash
   uvicorn main:app --reload
   ```

   The API will be available at `http://localhost:8000`, with interactive docs at `http://localhost:8000/docs`.

## Environment Variables

| Variable | Description |
|---|---|
| `ENV` | `local`, `dev`, or `prod` |
| `MONGO_URI` | MongoDB connection string |
| `MONGO_DB_NAME` | Database name to use |
| `API_DOMAIN` | Base URL of this API (required by SuperTokens) |
| `WEBSITE_DOMAIN` | Base URL of the frontend (required by SuperTokens, also used for CORS) |
| `SUPERTOKENS_CONNECTION_URI` | URL of your SuperTokens core |
| `SUPERTOKENS_API_KEY` | API key for your SuperTokens core |

## Running with Docker

```bash
docker build -t categorisation-system-backend .
docker run -p 8000:8000 --env-file .env categorisation-system-backend
```

## Deployment

Deployments run automatically via GitHub Actions:

- Push to `main` → builds and pushes the `latest` image to ECR → deploys to the `categorisation-system-backend-prod` ECS service.
- Push to `dev` → builds and pushes the `dev` image to ECR → deploys to the `categorisation-system-backend-dev` ECS service.

See `.github/workflows/` for the pipeline definitions.

# Document-to-record pipeline

A pipeline for converting invoice PDFs into validated PostgreSQL records.

## Step 1

The repository skeleton and PostgreSQL 16 database are implemented.
Files reserved for later steps are empty placeholders.

### Start and verify

Start Docker Desktop with Linux containers, then run these commands in
PowerShell from the repository root. Choose a password on the first run and
reuse it on later runs. To keep it across sessions, set `POSTGRES_PASSWORD`
in `.env`.

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
$env:POSTGRES_PASSWORD = Read-Host 'Postgres password'
docker compose config --quiet
docker compose up -d --wait db
docker compose exec db psql -v ON_ERROR_STOP=1 -U postgres -d invoices -c 'SELECT current_database(), version();' -c '\dt public.*'
```

The database defaults to `invoices`, the user defaults to `postgres`, and the
table listing should contain `documents`, `line_items`, and `processing_log`.
`POSTGRES_DB` and `POSTGRES_USER` can be set in `.env`. If you override them,
use those values for `-d` and `-U` in the verification command.
The host connection is available at `127.0.0.1:5433`. Port 5433 avoids a
Windows bind failure on port 5432 in the development environment.

`sql/schema.sql` runs automatically when the database volume is first
initialized. The named `postgres_data` volume persists across container
restarts. Changes to the schema file or initialization credentials take effect
only when initializing a fresh database volume.

Stop the containers while retaining the database with
`docker compose down`.

# IPO Put Research Prototype

A local research prototype for discovering recent U.S. IPO candidates from SEC EDGAR registration filings, storing them in PostgreSQL, and browsing them through a FastAPI dashboard.

## Current capabilities

- Downloads SEC EDGAR quarterly `master.idx` files.
- Filters `S-1`, `S-1/A`, `F-1`, and `F-1/A` registration filings.
- Stores issuer CIKs, companies, filings, and IPO-candidate records in PostgreSQL.
- Exposes a FastAPI JSON API.
- Serves a searchable browser dashboard.
- Uses CIK and filing accession numbers for deduplication.
- Supports idempotent ingestion: rerunning the same date range does not duplicate existing rows.

> **Prototype status:** this is candidate discovery, not yet a definitive list of priced IPOs. Registration statements can include offerings that never price, secondary registrations, uplistings, SPAC-related activity, funds, and other cases. A later enrichment stage will classify candidates and locate final `424B4` prospectuses.

## Recommended environment

The easiest supported development environments are:

- Ubuntu or another Debian-based Linux distribution.
- Ubuntu under WSL2 on Windows.

The commands below are the same for native Ubuntu and WSL2 unless otherwise noted.

## 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y \
    python3 \
    python3-venv \
    python3-pip \
    postgresql \
    postgresql-contrib \
    git
```

Start PostgreSQL:

```bash
sudo service postgresql start
```

Check its status:

```bash
sudo service postgresql status
```

## 2. Create the PostgreSQL user and database

Open the PostgreSQL administration shell:

```bash
sudo -u postgres psql
```

Create the prototype role and database:

```sql
CREATE USER ipo_app WITH PASSWORD 'ipo_dev_password';
CREATE DATABASE ipo_research OWNER ipo_app;
\q
```

The default development credentials used by this prototype are:

```text
Database: ipo_research
User:     ipo_app
Password: ipo_dev_password
Host:     localhost
Port:     5432
```

These credentials are intended only for a local development environment. Change them before exposing PostgreSQL to another machine or using the project in a production environment.

### Test the database login

```bash
psql -h localhost -U ipo_app -d ipo_research
```

When prompted, enter:

```text
ipo_dev_password
```

A successful connection gives a prompt similar to:

```text
ipo_research=>
```

Exit with:

```sql
\q
```

## 3. Clone and enter the project

```bash
git clone https://github.com/BurnCan/ipo-put-research.git
cd ipo-put-research
```

If you are using WSL2, keep the project in the Linux filesystem (for example `~/projects/ipo-put-research`) rather than `/mnt/c/...` for the smoothest Python/Git experience.

## 4. Create the Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

You will need to reactivate the environment in each new terminal session:

```bash
source .venv/bin/activate
```

## 5. Configure the application

Copy the example configuration:

```bash
cp .env.example .env
```

The default `.env.example` contains:

```env
DATABASE_URL=postgresql+psycopg://ipo_app:ipo_dev_password@localhost:5432/ipo_research
SEC_USER_AGENT=IPO Research Prototype your-email@example.com
```

### Change the SEC contact email

SEC automated clients should identify themselves with contact information. Edit `.env` and replace `your-email@example.com` with your real email address:

```bash
nano .env
```

For example:

```env
SEC_USER_AGENT=IPO Research Prototype jane@example.com
```

Do **not** commit `.env`; it is excluded by `.gitignore`.

## 6. Ingest recent IPO candidates

With the virtual environment active:

```bash
python scripts/ingest_recent_ipos.py --days 365
```

A successful first run prints counts similar to:

```text
{'candidate_filings_seen': ..., 'companies_created': ..., 'filings_created': ..., 'ipo_candidates_created': ...}
```

Running the same command again should produce zero newly created records for data already ingested.

## 7. Start the web application

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8007
```

Open:

```text
http://localhost:8007
```

WSL2 normally forwards this localhost port automatically to the Windows browser.

FastAPI documentation is available at:

```text
http://localhost:8007/docs
```

## API endpoints

- `GET /api/health`
- `GET /api/ipos`
- `GET /api/ipos/{id}`
- `POST /api/ingest/sec?days=365`

## Database administration

### Log in as `ipo_app`

```bash
psql -h localhost -U ipo_app -d ipo_research
```

### Change the `ipo_app` password

Open PostgreSQL as the administrator:

```bash
sudo -u postgres psql
```

Then run:

```sql
ALTER USER ipo_app WITH PASSWORD 'your_new_password';
\q
```

Update the password in your local `.env` to match:

```env
DATABASE_URL=postgresql+psycopg://ipo_app:your_new_password@localhost:5432/ipo_research
```

If the new password contains URL-reserved characters such as `@`, `:`, `/`, `?`, or `#`, URL-encode the password before placing it in `DATABASE_URL`.

### Reset the development database

If you want to test a completely fresh ingest or discard all local prototype data, recreate the database and application role.

**Warning:** this permanently deletes all data currently stored in the local `ipo_research` database.

Open PostgreSQL as the administrator:

```bash
sudo -u postgres psql```

Then run:
```sql
DROP DATABASE IF EXISTS ipo_research;
DROP ROLE IF EXISTS ipo_app;

CREATE USER ipo_app WITH PASSWORD 'ipo_dev_password';
CREATE DATABASE ipo_research OWNER ipo_app;
\q
```

If PostgreSQL reports that the database is being accessed by other users, stop the FastAPI server and any open psql sessions, then try again.

After recreating the database, make sure your .env uses the matching credentials and run the ingest again:

```bash
python scripts/ingest_recent_ipos.py --days 365
```

### Start PostgreSQL after a reboot

On Ubuntu/WSL:

```bash
sudo service postgresql start
```

## Optional Docker database

If Docker is already installed, `docker-compose.yml` can start a PostgreSQL development database using the same default credentials:

```bash
docker compose up -d db
```

Docker is optional; the normal Linux/WSL installation above does not require it.

## Project layout

```text
app/
  api/                 FastAPI routes
  services/            SEC ingestion logic
  static/              Dashboard CSS
  templates/           Dashboard HTML
  config.py             Environment settings
  db.py                 SQLAlchemy engine/session
  models.py             Database models
  schemas.py            API schemas
scripts/
  ingest_recent_ipos.py Command-line ingest entry point
tests/
requirements.txt
.env.example
docker-compose.yml
```

## Immediate next milestones

1. Fetch each issuer's SEC submissions history and attach `424B4`, `EFFECT`, `8-A12B`, and subsequent filings.
2. Classify genuine operating-company IPOs versus secondary offerings, SPACs, funds, and uplistings.
3. Parse final prospectus fields including ticker, exchange, IPO price, shares offered, post-offering shares outstanding, primary/secondary shares, underwriters, and lockup language.
4. Store source and confidence metadata for parsed fields.
5. Add market-price and options-data providers after the IPO dataset is reliable.
6. Build scoring and backtesting before considering any automated trade execution.

## SEC fair-access note

The SEC asks automated clients to identify themselves and comply with its fair-access policies. Keep request rates modest and cache source data rather than repeatedly downloading the same material.

## Disclaimer

This project is a research prototype. It does not provide investment advice and does not currently submit trades.

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
- Conservatively classifies candidates from stored SEC filing chronology and associates a plausible final `424B4`.
- Caches associated final prospectuses and extracts conservative offering facts with provenance.

> **Prototype status:** classifications are heuristic research labels based only on SEC filing metadata, not authoritative legal determinations. Registration statements can represent many transaction types, so uncertain or contradictory cases intentionally remain `unknown` / `needs_review`.

## Research pipeline

```text
SEC master.idx → IPO candidate discovery → SEC submissions enrichment
  → candidate classification → final prospectus association → prospectus cache
  → deterministic offering extraction → fact provenance → canonical IPO fields
```

Milestone 3 implements this displayed pipeline. Market, options, scoring, and trading stages remain out of scope.

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

For development and tests, install the separate development requirements instead:

```bash
pip install -r requirements-dev.txt
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
FILING_CACHE_DIR=./data/filings
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

### Enrich discovered issuers

After candidate ingestion, fetch each discovered issuer's SEC submissions metadata:

```bash
python scripts/enrich_sec_submissions.py
```

This fills company names, tickers, and exchanges where SEC data is available and attaches later
filings such as `424B4`, `EFFECT`, `8-A`, `10-Q`, `10-K`, and `8-K` (plus foreign-issuer
`20-F` and `6-K` filings). The command is idempotent: accession numbers already stored are not
created again. For a small development run, use:

```bash
python scripts/enrich_sec_submissions.py --limit 25
```

### Upgrade an existing database

`create_all()` cannot add columns to an existing table. After pulling Milestone 2, preserve the
existing data and apply the narrowly scoped, idempotent upgrade (it is safe to rerun):

```bash
python scripts/upgrade_schema.py
```

It adds the five classification/prospectus columns and the PostgreSQL prospectus foreign-key
index; it does not delete or rewrite candidate data. Fresh databases receive the same schema from
SQLAlchemy metadata.

### Classify candidates

Classification uses only filings already stored by ingest/enrichment and makes no live SEC calls:

```bash
python scripts/classify_ipo_candidates.py
python scripts/classify_ipo_candidates.py --limit 25
```

`--company-id ID` and `--ipo-id ID` can target one record. Runs are idempotent: derived values are
recomputed deterministically, and the summary reports unchanged rows.

The IPO fields are:

- `candidate_type`: `operating_company_ipo`, `spac`, `fund`, or conservative `unknown` (the model
  also reserves `secondary_offering`, `uplist`, and `other` for future evidence rules).
- `classification_status`: `unclassified` before processing, `classified` when evidence is
  sufficient, or `needs_review` for weak, conflicting, or ambiguous evidence.
- `offering_status`: `filed` for registration alone, `effective` for a relevant `EFFECT`, `priced`
  for a confidently linked `424B4`, `withdrawn` for a relevant `RW`, or `unknown` for insufficient
  or conflicting evidence.
- `classification_reason`: a concise explanation of the signals used.

Candidate typing requires multiple signals. SPAC and fund name patterns require subsequent offering
evidence; an ordinary operating-company label requires a nearby final prospectus. Any periodic
filing (`10-K`, `10-Q`, `20-F`, or `6-K`) predating registration causes review rather than a guess
between a secondary offering and uplisting.

Final-prospectus association anchors on the first S-1/F-1 date, considers only post-registration
`424B4` filings within 180 days, and chooses the nearest. If the two nearest candidates are within
three days, no prospectus is linked and the case is marked `needs_review`. `RW` and `EFFECT` use the
same window. A confidently linked prospectus remains `priced` despite a later `RW`, because that
withdrawal may relate to another sequence. This chronology-based association defines the source filing
eligible for Milestone 3 parsing.

## Prospectus processing (Milestone 3)

First apply the data-preserving, idempotent schema upgrade, then process associated final prospectuses:

```bash
python scripts/upgrade_schema.py
python scripts/process_final_prospectuses.py
python scripts/process_final_prospectuses.py --limit 25
python scripts/process_final_prospectuses.py --ipo-id 123 --reparse
python scripts/process_final_prospectuses.py --ipo-id 123 --refetch --reparse
```

The upgrade adds `primary_shares`, `secondary_shares`, and `shares_outstanding_post_ipo`, plus the
`filing_documents` and `ipo_facts` tables; it preserves existing rows and is safe to rerun. The default
processor reuses successful downloads and facts. `--reparse` reruns the current parser against cached
text without downloading; `--refetch` explicitly downloads again and refreshes normalized text. A failed
document is recorded without aborting the batch.

### Prospectus cache

`FILING_CACHE_DIR` defaults to `./data/filings`. Each filing uses
`<CIK>/<ACCESSION>/raw.html` and `<CIK>/<ACCESSION>/text.txt`. Raw bytes are retained, their SHA-256 and
byte size are stored with queryable HTTP/source/error/UTC timestamp metadata, and normalized UTF-8 text
removes scripts and styles, decodes entities, preserves useful line separation, and collapses excessive
whitespace. Cache files are gitignored. Normalization uses the `beautifulsoup4` dependency; there is no
OCR, AI, or LLM dependency.

### Provenance, confidence, and promotion

`ipo_facts` records the IPO and source filing, typed value, unit, confidence, stable parser name/version,
short excerpt/location, and direct or derived status. Exact duplicate identity is IPO + filing + field +
parser name/version + an evidence identity key (value, source, confidence, and derivation), so reruns do not duplicate facts while distinct provenance and earlier parser versions
remain available.

The single canonical promotion threshold is **0.90**. Facts below it remain available without changing
canonical IPO data. Only facts from `final_prospectus_filing_id` qualify. Distinct high-confidence values
for the same field are reported as ambiguous and clear the canonical field rather than retaining stale data. Unchanged values
are not rewritten. When canonical price and offered shares exist, deal size is recorded as a derived fact
(`ipo_price * shares_offered`) using the lower input confidence.

### Parsed fields and limitations

Parser `final_prospectus_offering` version `1` attempts only `ipo_price`, `shares_offered`,
`primary_shares`, `secondary_shares`, `shares_outstanding_post_ipo`, and derived `deal_size`. It prioritizes
explicit cover/summary language and avoids authorized, option-plan, over-allotment, historical-financing,
pre-offering, fully diluted, option, and warrant counts. Ambiguous language is intentionally unpromoted.

This milestone does not parse lockups, underwriters, financial statements, use of proceeds, market data,
or options, and does not reconstruct complex tables or perform OCR.

The list API exposes canonical offering values and compact cache/fact counts. The detail API also exposes
concise fact provenance. The dashboard shows price, offered shares, derived deal size, and parsed status.

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

IPO list/detail responses include classification and canonical offering fields, a `final_prospectus`
object, cache metadata, and fact counts. Detail responses add compact provenance facts.

## Testing

Tests use in-memory SQLite and do not call live SEC endpoints:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

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
  enrich_sec_submissions.py SEC metadata enrichment
  classify_ipo_candidates.py Offline candidate classification
  upgrade_schema.py      Idempotent Milestone 2 schema upgrade
tests/
requirements.txt
requirements-dev.txt
.env.example
docker-compose.yml
```

## Later milestones

Market/options providers, scoring, and backtesting remain future work after the provenance-backed
prospectus dataset is evaluated. Trading execution is not implemented.

## SEC fair-access note

The SEC asks automated clients to identify themselves and comply with its fair-access policies. Keep request rates modest and cache source data rather than repeatedly downloading the same material.

## Disclaimer

This project is a research prototype. It does not provide investment advice and does not currently submit trades.

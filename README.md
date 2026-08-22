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
- Caches associated final prospectuses and extracts conservative offering facts and agreement-level lockups with provenance.

> **Prototype status:** classifications are heuristic research labels based only on SEC filing metadata, not authoritative legal determinations. Registration statements can represent many transaction types, so uncertain or contradictory cases intentionally remain `unknown` / `needs_review`.

## Research pipeline

```text
SEC candidate discovery
  → SEC enrichment
  → classification
  → prospectus extraction
  → lockup extraction
  → market-history ingestion
  → pre-event signal snapshots
  → event/post-event outcomes
  → future backtesting / signal research
```

Milestone 6 adds a descriptive, backtest-ready lockup-event layer. Options, composite scoring,
recommendations, and trading remain out of scope.

## Lockup event analysis (Milestone 6)

Analysis is stored as recomputable derived state; raw `daily_prices` remain authoritative. The default
target is each IPO's `primary_lockup_id`, while `--lockup-id` permits research on any extracted
agreement. Snapshot and outcome formulas have independent explicit version `1` identifiers.

### Sessions, dates, and point-in-time integrity

The source `event_date` prefers the stated expiration and otherwise uses the calculated expiration.
It is preserved separately from `event_trade_date`, the **first stored trading session on or after**
that date. Thus a weekend or holiday moves to the following available session, never the preceding
one. If no on-or-after bar exists, `event_trade_date` remains null.

Snapshots are row-oriented observations at `-60, -40, -20, -10, -5, -1` trading sessions. Outcomes
use exact session offsets `0, +1, +5, +10, +20, +40`. For every snapshot,
`data_cutoff_date = observation_date`, and its price-history query requires that cutoff and filters
out every later bar. This is an enforced guardrail against look-ahead bias, not merely metadata.
An observation row is still created with short history, but an exact unavailable window remains
null rather than being relabeled (for example, 17 sessions never becomes a 20-session return).

For prospective events without a stored event session, snapshots remain ungenerated because future
exchange holidays make their trading-session offsets ambiguous. Once the event session is present,
all offsets are resolved exclusively against the authoritative stored `daily_prices` sequence.

### Snapshot measurements

Snapshot rows copy stable offering structure (IPO price, primary/secondary/total offered shares and
deal size) and derive the secondary fraction without greenshoe shares. They include calendar/session
age, exact trailing returns, as-of high and low, drawdown, range position, and IPO-gain retention.
Liquidity features include exact 5/20/40-session volume averages, 5-to-20 volume ratio, and average
dollar volume using `close * volume`. Up/down days compare close with the preceding session; flat
days are excluded and a missing side produces a null ratio.

Realized volatility is the **sample** standard deviation of exactly N close-to-close returns,
annualized as `stdev * sqrt(252)`. Daily range is `(high - low) / previous_close`, averaged across
exactly 20 sessions. Long-window values remain null until their complete input history exists.

### Event outcomes and incomplete windows

Event measurements preserve the previous close and event OHLCV. Gap, intraday, and close returns
are respectively `open / previous_close - 1`, `close / open - 1`, and
`close / previous_close - 1`. Pre-event convenience returns compare event close with exact negative
offset closes; post-event returns compare exact positive-offset closes with event close.

Bearish MFE is `(event_close - minimum post-event low) / event_close`; bearish MAE is
`(maximum post-event high - event_close) / event_close`. Both are non-negative excursions from a
bearish/short perspective and use daily lows/highs, not closes. Volume response uses the complete
15-session baseline `-20` through `-6`, deliberately excluding the immediate pre-event week.

Upcoming events have null future outcomes. An observed event with no `+1` is `event_today`, partial
future history is `post_event_incomplete`, and at least 40 post-event sessions is `complete`.
`max_post_event_session_available` explicitly describes completeness. Idempotent reruns update the
same versioned rows, so newly ingested bars progressively fill outcomes without duplicating data.

Run the schema upgrade and entirely offline analysis from the repository root:

```bash
python scripts/upgrade_schema.py
python scripts/analyze_lockup_events.py --limit 25
python scripts/analyze_lockup_events.py --ticker ALH
python scripts/analyze_lockup_events.py --ipo-id 14 --recompute
python scripts/analyze_lockup_events.py --lockup-id 11
```

IPO JSON includes compact `primary_lockup_event` data, and
`GET /api/ipos/{id}/lockup-snapshots` returns its ordered trajectory. The dashboard shows the lockup
status and selected pre/post measurements. This milestone intentionally defers benchmark/sector
adjustment, ownership and insider data, fundamentals and valuation, options and implied volatility,
put profitability, simulations, scoring/rankings, causal labels, alerts, and execution.

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
MARKET_DATA_PROVIDER=massive
MASSIVE_API_KEY=your-api-key-here
MARKET_INITIAL_LOOKBACK_DAYS=730
MARKET_REFRESH_DAYS=30
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

### Massive Stocks Basic setup

Get an API key from the developer's Massive account, then edit the local `.env` (not
`.env.example`) and set:

```env
MARKET_DATA_PROVIDER=massive
MASSIVE_API_KEY=your-api-key-here
```

Never commit the real key. `.env` is gitignored, while `.env.example` contains placeholders only.
Verify the configuration without printing the secret itself:

```bash
python -c "from app.config import settings; print(settings.market_data_provider); print(bool(settings.massive_api_key))"
```

The expected output is:

```text
massive
True
```

The application, SEC pipeline, API, and dashboard work without this key; only the market-ingestion
command requires it and reports a clear configuration error when it is absent.

Massive is isolated behind `MarketDataProvider`. The ingestion and summary layers consume normalized
`DailyBar` values, so a future provider can replace Massive without changing the database or research
logic. The adapter calls the daily aggregate endpoint in ascending order with `adjusted=false`.
Consequently, `open`, `high`, `low`, `close`, and `volume` are authoritative **raw** observations;
raw close is never silently replaced by an adjusted value.

### Security identity and incremental history

`securities` preserves the symbol actually used for a fetch separately from the convenient current
`companies.ticker`. This permits multiple or time-bounded symbols later and ensures a ticker change
does not delete old observations. Initialization from SEC company metadata is deterministic and
idempotent. `daily_prices` retains the provider and provider symbol on every normalized observation.

The first fetch starts at the IPO date when known. When it is unknown, the fetch uses the bounded
`MARKET_INITIAL_LOOKBACK_DAYS` provider lookback (two years by default); the SEC registration's
`first_filing_date` is never treated as evidence that trading began. The earliest returned bar
establishes the observed first trading day; it does not modify `IPO.ipo_date`. Later runs start at
`latest_trade_date + 1 day`, skip requests when already current, and enforce database uniqueness by
security/date/provider. `--refresh` re-fetches and upserts only the most recent
`MARKET_REFRESH_DAYS` calendar days (30 by default), preserving older bars while allowing recent
provider corrections. Use `--refresh-days N` to override that window explicitly. `--sleep` defaults
to a conservative 12 seconds between symbols; transient 408/429/5xx responses use bounded
exponential retry/backoff.

Run the idempotent schema upgrade and an ingestion batch from the repository root:

```bash
python scripts/upgrade_schema.py
python scripts/ingest_market_history.py --limit 5
python scripts/ingest_market_history.py --ipo-id 14
python scripts/ingest_market_history.py --ticker ALH
python scripts/ingest_market_history.py --ticker ALH --refresh --sleep 12
python scripts/ingest_market_history.py --ticker ALH --refresh --refresh-days 90
```

### Market summary metrics

`ipo_market_summary` is a rerunnable derived cache, not raw evidence. It records the earliest bar's
open/close, latest close/date, and maximum raw daily high. Returns are decimal fractions (`0.25`
means +25%):

* `first_day_close_return_vs_ipo_price = (first_day_close - ipo_price) / ipo_price`
* `return_from_ipo_price = (latest_close - ipo_price) / ipo_price`
* `drawdown_from_post_ipo_high = (latest_close - post_ipo_high) / post_ipo_high`

Drawdown is normally zero or negative. When IPO price is missing, both IPO-relative returns remain
null while all observed-price fields and drawdown are still calculated. Compact summaries appear in
IPO list/detail responses and the dashboard; bounded raw history is available at
`GET /api/ipos/{id}/prices?limit=500`.

Rebuild this derived cache after changing IPO facts without fetching market history:

```bash
python scripts/recompute_market_summaries.py
python scripts/recompute_market_summaries.py --limit 25
python scripts/recompute_market_summaries.py --ipo-id 14
python scripts/recompute_market_summaries.py --ticker ALH
```

The command is offline and idempotent: it reads only stored `daily_prices` for the configured market
data provider and **does not contact Massive**. A normal incremental ingestion run also rebuilds a
summary from stored bars when that security is already current, without making a provider request.

### Market-data limitations

Massive Basic may return only the history covered by the current plan. Partial/no data is reported
separately rather than treated as fatal. This milestone does not implement options, intraday bars,
full corporate-action normalization, complete historical ticker-change resolution, delisted-symbol
completeness, lockup-event backtesting, or composite bearish scoring.

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

`create_all()` cannot add columns to an existing table. After pulling a new milestone, preserve the
existing data and apply the narrowly scoped, idempotent upgrade (it is safe to rerun):

```bash
python scripts/upgrade_schema.py
```

It adds missing classification/offering/lockup columns, provenance tables, foreign keys, and indexes;
it does not delete or rewrite candidate data and is safe to run repeatedly. Fresh databases receive
the same schema from SQLAlchemy metadata.

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
canonical IPO data. Only current-version facts from `final_prospectus_filing_id` qualify; prior-version
facts remain as provenance but do not override corrected parser semantics. Distinct high-confidence values
for the same field are reported as ambiguous and clear the canonical field rather than retaining stale data. Unchanged values
are not rewritten. `primary_shares` means shares sold by the issuer/company, while
`secondary_shares` means shares sold by existing/selling stockholders. `shares_offered` is the total base
offering sold to the public: `primary_shares + secondary_shares` when both are explicitly present.
Optional underwriter over-allotment/greenshoe shares are not included yet.

When canonical price and offered shares exist, `deal_size` is recorded as a derived fact
(`ipo_price * shares_offered`) using the lower input confidence. It therefore represents total base
offering value, not issuer net proceeds.

### Parsed fields and limitations

Parser `final_prospectus_offering` version `2` attempts only `ipo_price`, `shares_offered`,
`primary_shares`, `secondary_shares`, `shares_outstanding_post_ipo`, and derived `deal_size`. It prioritizes
explicit cover/summary language and avoids authorized, option-plan, over-allotment, historical-financing,
pre-offering, fully diluted, option, and warrant counts. Ambiguous language is intentionally unpromoted.

This milestone does not parse lockups, underwriters, financial statements, use of proceeds, market data,
or options, and does not reconstruct complex tables or perform OCR.

Prospectus reprocessing does not trigger market-history work. After a newly extracted IPO price is
promoted, market history can be refreshed/recomputed separately to update price-based returns.

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

## Lockup extraction (Milestone 4)

### What lockup extraction does

The deterministic parser reuses the normalized `text.txt` in the Milestone 3 cache; it never starts
a second download path. It first discovers bounded `UNDERWRITING`, `SHARES ELIGIBLE FOR (FUTURE)
SALE`, `LOCK-UP AGREEMENTS/ARRANGEMENTS`, and `RESTRICTIONS ON SALE` regions, then looks for sale
restriction concepts inside those regions. An unrelated day count elsewhere in a prospectus is not
considered. Each match becomes an agreement-level `ipo_lockups` row with its source excerpt and
locator, filing, parser name/version, and confidence.

Within those bounded regions, a sale or transfer restriction requiring prior written consent or a
waiver from specifically named securities firms is treated as an underwriter lockup even when the
operative sentence identifies the offering banks by name rather than by the words “underwriter” or
“representative.” Generic written-consent language without a sale restriction does not qualify.

An IPO may have multiple rows because company issuance restrictions, shareholder restrictions,
market-standoff terms, or distinct holder groups can coexist. Controlled lockup types are
`underwriter_lockup`, `company_lockup`, `market_standoff`, `contractual_restriction`, `other`, and
`unknown`; controlled holders are `directors_officers`, `existing_stockholders`,
`selling_stockholders`, `company`, `employees`, `pre_ipo_investors`, `sponsor`, `other`, and
`unknown`. The original holder wording is retained rather than replaced by the label.

Confidence has one interpretation: 0.95–1.00 is very high, 0.90–0.949 high, 0.75–0.899 plausible,
and below 0.75 weak/informational. Exact evidence identity includes the IPO and filing, agreement
attributes, parser identity/version, excerpt, and locator. Consequently exact reruns are idempotent
while two agreements with the same duration but distinct holder or source provenance survive.

### Primary lockup

`primary_lockup_expiration_date` means **the highest-confidence estimated expiration date of the
principal underwriter-style lockup affecting existing shareholders or comparable pre-IPO holders**.
It is a research convenience signal, not a legal determination. Promotion requires a high-confidence,
dated underwriter lockup for a principal holder group. Company-only restrictions, employee market
standoffs, undated or weak matches are excluded. Compatible evidence with one date may be promoted;
materially conflicting dates clear both `primary_lockup_id` and the canonical date, including a stale
selection from an earlier run.

### Explicit versus calculated dates

`stated_expiration_date` records a calendar date actually stated by the prospectus and is not
recalculated. `calculated_expiration_date` is populated only when a reliable duration is explicitly
anchored to the date of the prospectus and the filing date is known. The convention is ordinary
calendar arithmetic, `prospectus_date + timedelta(days=duration_days)`, without exchange-calendar or
business-day adjustment. Unclear anchors produce no calculated date. An exact “six months” can be
represented conservatively as 180 days; “approximately six months” is rejected.

### Early release and locked shares

Waiver, partial/staggered release, early-release, and blackout-adjustment language is flagged and a
concise source fragment is preserved. Complex conditional release dates are not predicted. Shares are
captured only when grammar ties a count directly to the lockup. A percentage may be derived only when
both that count and the canonical post-offering shares outstanding are available, and derived values
are marked as such.

Apply the schema upgrade and parse the existing cache:

```bash
python scripts/upgrade_schema.py
python scripts/extract_lockups.py
python scripts/extract_lockups.py --limit 25
python scripts/extract_lockups.py --ipo-id 123 --reparse
```

`--reparse` runs current parser logic again but its evidence key preserves exact-rerun idempotency.
IPOs without a successful normalized cached document are skipped and reported; this command does not
fetch them.

### Milestone 4 limitations

This layer does not model actual exchange trading-day adjustments, every legal exception,
underwriter discretionary waiver outcomes, future unlock-event probabilities, supply-shock scoring,
or put-trade timing. It also does not use an LLM or OCR and does not make trading recommendations.

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
  process_final_prospectuses.py Cache and parse final prospectuses
  extract_lockups.py      Parse cached text into agreement-level lockups
  upgrade_schema.py      Idempotent schema upgrades through Milestone 4
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

# IPO Put Research Prototype

A local research prototype for discovering recent U.S. IPO candidates from SEC EDGAR registration filings, storing them in PostgreSQL, and browsing them through a FastAPI dashboard.

## Hybrid US-equities calendar model

M8 separates expected session identity from observed market data. The offline
`exchange_calendars` **XNYS** calendar determines when a canonical US-equities
session (including the exact T-5 session) should occur; `DailyPrice` rows
independently determine whether market data actually exists for that session.
A calendar-derived date therefore never implies that a bar or M6 snapshot is
available.

This lets M8 resolve T-5 before a future lockup occurs. Future lockups whose
T-5 is on or before the frozen prospective cutoff are immediately recorded as
prospectively unavailable. For eligible events, M8 distinguishes a T-5 that
has not yet arrived from a T-5 whose market data is still missing. Existing M6
snapshots and genuine frozen M8 signals remain authoritative and are not
rewritten; disagreements with XNYS are reported for review. XNYS is the v1
canonical US-equities calendar. Exchange-specific and foreign calendars are a
future extension.

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

## Lockup research dashboard

Run the application and open the dashboard at <http://127.0.0.1:8000/>:

```bash
uvicorn app.main:app --reload
```

The root page presents the immutable frozen hypothesis, clean-cohort upcoming
lockups, frozen prospective signals and their lifecycle, prospective group
evaluation, and a separately identified historical discovery reference.
Historical discovery results are **not out-of-sample**; M8 prospective
observations are kept separate.

The upcoming-events table presents each observation's read-only research
lifecycle. **Pre-event 20d return** is the 20-session return ending at T-5, and
**Pre-event 20d realized vol** is realized volatility over that same lookback.
**Frozen group** is the immutable classification produced by the frozen
thresholds. **Outcome status** reports the stored lifecycle after signal lock;
**Post-event 20d return** is the frozen realized +20-session outcome; and
**Result** interprets that outcome relative to the frozen hypothesis. A bearish
outcome in a non-target group is identified as non-target and is never counted
as a hypothesis hit.

### Strict prospective

`strict_prospective` requires canonical XNYS T-5 to be strictly later than the
hypothesis version's freeze date. It remains the only primary M8 evidence.

### Shadow prospective

`shadow_prospective` is secondary validation evidence for mechanically admitted
events whose canonical T-5 is on/before that version's freeze while the event is
after it and still unseen when the signal is locked. It is not strict
prospective and is never included in primary M8 statistics. The updater requires
the exact T-5 DailyPrice and a complete canonical 21-session feature window;
absence of an old M6 snapshot is therefore distinct from market-data absence.
It never rewrites M6 snapshots, never admits historical outcomes retroactively,
and attaches outcomes only after maturity using the frozen M8 definition.
`lockup_prospective_signals.created_at` is the immutable signal-lock timestamp
(also exposed as `signal_locked_at` by the dashboard API). Under the current
date-level admission guard, a shadow lock's UTC date must be strictly before
the canonical event session; reruns never replace this timestamp.
Each future hypothesis version uses its own freeze metadata.

```bash
python scripts/update_prospective_signals.py --hypothesis-id m7_return20_vol20_minus5_post20 --evaluation-mode strict_prospective --dry-run
python scripts/update_prospective_signals.py --hypothesis-id m7_return20_vol20_minus5_post20 --evaluation-mode shadow_prospective --dry-run
```

The dashboard uses these deterministic, read-only research endpoints:

- `GET /api/research/hypothesis`
- `GET /api/research/summary`
- `GET /api/research/pipeline-status` (actual execution/stage provenance and independent market date)
- `GET /api/research/upcoming-lockups`
- `GET /api/research/prospective-signals` (optional `status`,
  `interaction_group`, and `ticker` filters)
- `GET /api/research/prospective-evaluation`
- `GET /api/research/historical-reference`

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

## Daily research pipeline

The daily orchestration command keeps the prospective M8 dataset current as new market data
arrives. It reuses the existing stage services and always runs the clean cohort (classified,
priced, operating-company IPOs with a selected dated primary lockup) in this order:

```text
market-history ingestion
        ↓
M6 snapshots/outcomes
        ↓
M8 prospective signals/outcomes
```

This is how future T-5 signals and, once they mature, +20 outcomes become available for the frozen
`m7_return20_vol20_minus5_post20` hypothesis. A failure stops the pipeline before any dependent
stage. The command prints one JSON report and exits zero only after complete success:

```bash
python scripts/update_research_pipeline.py
python scripts/update_research_pipeline.py \
  --log-file logs/daily_pipeline.log
```

The log file is append-only and its parent directories are created automatically. A Linux/WSL
`fcntl` lock at `data/update_research_pipeline.lock` prevents overlapping runs; an overlap reports
`status: already_running` and exits non-zero. The optional `--skip-market-history`, `--skip-m6`, and
`--skip-m8` flags explicitly mark stages as skipped and are intended only for debugging.

`--dry-run` is **not** a full-pipeline no-write mode. Market ingestion and M6 have no native dry-run,
so they execute their normal idempotent refreshes and may write database rows; only M8 receives
`dry_run=True` and rolls back its prospective changes. This distinction is also stated in the
command's help text.

### Schedule daily with cron (Linux/WSL)

The portable `scripts/run_daily_pipeline.sh` wrapper discovers its repository root, changes to it,
activates `.venv`, and writes the structured report to `logs/daily_pipeline.log`. Make it executable:

```bash
chmod +x scripts/run_daily_pipeline.sh
```

Cron supplies a minimal environment, so use absolute paths in the crontab. For a checkout at
`/home/weird/projects/ipo-put-research`, edit the schedule with `crontab -e` and add:

```cron
30 18 * * 1-5 PIPELINE_TRIGGER=cron /home/weird/projects/ipo-put-research/scripts/run_daily_pipeline.sh >> /home/weird/projects/ipo-put-research/logs/cron.log 2>&1
```

The five schedule fields are `minute hour day-of-month month day-of-week`; therefore this runs
Monday-Friday at 18:30 in the cron daemon's **local timezone**. `PIPELINE_TRIGGER=cron` records
explicit trigger provenance; direct wrapper invocations default to `manual`. Running after the
US market close, such as 6:30 PM Eastern when the cron environment is configured for Eastern time,
is recommended. The Python command does not impose a timezone.

Each invocation now stores its actual UTC start/finish, host, result, and the real
`market_history`, `m6_analysis`, and `m8_prospective` stage results. The dashboard's **Last pipeline
run** is this execution provenance; **Latest market date** remains the independent
`MAX(DailyPrice.trade_date)`. A successful holiday run need not advance that date. Runs before this
feature was deployed are intentionally not reconstructed, so a new deployment initially reports
"No recorded runs yet." Database provenance supplements, rather than replaces, the cron and daily
pipeline logs.

Verify and troubleshoot without changing pipeline data:

```bash
crontab -l
service cron status
sudo service cron start
tail -f logs/daily_pipeline.log
tail -f logs/cron.log
```

Exact cron service management varies with the WSL/systemd setup. Cron inside WSL runs only while
the WSL environment and its services are available. If Windows is shut down or sleeping, or WSL is
not running in a way that keeps cron active, the job may not execute. Windows Task Scheduler is
more reliable when Windows itself must start WSL; it can invoke (automation is not included here):

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc \
  "/home/weird/projects/ipo-put-research/scripts/run_daily_pipeline.sh"
```

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

To keep Milestone 6 aligned with the research cohort used by the upstream pipeline, run:

```bash
python scripts/analyze_lockup_events.py \
  --classification-status classified \
  --candidate-type operating_company_ipo \
  --offering-status priced \
  --primary-lockup-only
```

For staged execution, apply a limit after the cohort filters:

```bash
python scripts/analyze_lockup_events.py \
  --classification-status classified \
  --candidate-type operating_company_ipo \
  --offering-status priced \
  --primary-lockup-only \
  --limit 25
```

This preserves one consistent downstream research universe:

```text
classified priced operating IPO
        ↓
final prospectus
        ↓
selected dated primary lockup
        ↓
market history
        ↓
M6 pre-event snapshots + event outcomes
```

The value filters compose, and `--primary-lockup-only` requires both a selected primary lockup and
its stored primary expiration date. Filters are applied before deterministic ordering and `--limit`.
An explicit `--lockup-id` overrides cohort filtering and primary selection so any extracted lockup,
including a non-primary one, remains directly analyzable. M6 reads only stored database data and
remains entirely offline; it does not call Massive, the SEC, or another external service.

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

After processing final prospectuses and extracting primary lockups, restrict provider requests to
the lockup research cohort with:

```bash
python scripts/ingest_market_history.py \
  --classification-status classified \
  --candidate-type operating_company_ipo \
  --offering-status priced \
  --primary-lockup-only
```

`--primary-lockup-only` requires both a selected `primary_lockup_id` and a
`primary_lockup_expiration_date`; arbitrary lockup rows alone do not qualify. All cohort filters are
composable and are applied before `--limit`. For a staged provider batch, run:

```bash
python scripts/ingest_market_history.py \
  --classification-status classified \
  --candidate-type operating_company_ipo \
  --offering-status priced \
  --primary-lockup-only \
  --limit 25
```

The operational sequence remains intentionally manual: process final prospectuses, extract primary
lockups, ingest market history with `--primary-lockup-only`, and then analyze lockup events. Keep the
real Massive API key only in `.env`; the setup remains `MARKET_DATA_PROVIDER=massive` and
`MASSIVE_API_KEY=...`.

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

Bulk processing can be restricted to the confidently classified research universe. Filters are
composable and are applied **before** `--limit`:

```bash
python scripts/process_final_prospectuses.py \
  --classification-status classified \
  --candidate-type operating_company_ipo \
  --offering-status priced \
  --limit 25
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
are not rewritten.

Offering-field semantics are:

- `primary_shares`: issuer/company shares sold in the base offering.
- `secondary_shares`: existing/selling-stockholder shares sold in the base offering.
- `shares_offered`: total base offering shares, excluding optional underwriter over-allotment shares.
- `shares_outstanding_post_ipo`: base post-offering shares outstanding; for a multi-class issuer an
  explicit total across classes takes precedence over class-specific counts.
- `deal_size`: `ipo_price * shares_offered`.

For the primary and secondary components, **`0` means the prospectus explicitly states none**, while
**`NULL` means unknown/not extracted**. The parser does not infer zero merely because it did not find one
side. Optional underwriter over-allotment/greenshoe shares are excluded from base offering shares,
post-offering shares outstanding, and deal size.

When canonical price and offered shares exist, `deal_size` is recorded as a derived fact
(`ipo_price * shares_offered`) using the lower input confidence. It therefore represents total base
offering value, not issuer net proceeds.

### Parsed fields and limitations

Parser `final_prospectus_offering` version `4` attempts only `ipo_price`, `shares_offered`,
`primary_shares`, `secondary_shares`, `shares_outstanding_post_ipo`, and derived `deal_size`. It prioritizes
explicit cover and bounded contexts around offering-summary labels found throughout the document, and avoids authorized, option-plan, over-allotment, historical-financing,
pre-offering, fully diluted, option, and warrant counts. It recognizes bounded final-price sentences and
simple cover pricing tables, explicit issuer/selling-holder allocations (including `None`), and common
post-offering outstanding labels. Ambiguous language is intentionally unpromoted.

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

For building the operating-company lockup research cohort, the recommended command is:

```bash
python scripts/extract_lockups.py \
  --classification-status classified \
  --candidate-type operating_company_ipo \
  --offering-status priced \
  --limit 25
```

Omit `--limit 25` to process the entire cohort. These optional filters are composable and are applied
before `--limit`; with no filters, extraction retains its existing behavior of considering every IPO
with a linked final prospectus.

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

## Milestone 7: point-in-time lockup backtesting

M7 provides **point-in-time exploratory backtesting of pre-lockup signals** using only
the stored M6 snapshots and outcomes.  The canonical dataset unit is one selected
primary lockup × observation offset. Multiple offsets from the same lockup are
repeated observations, not independent IPO events; row counts must never be read as
independent-event counts.

The default clean research cohort is classified, priced operating-company IPOs with
a selected primary lockup and expiration date. Export it deterministically with:

```bash
python scripts/export_lockup_backtest.py
```

The default file is `data/backtests/lockup_signal_outcomes.csv`; `--output`, cohort
filters, ticker/IPO filters, and `--limit` are available. Filters are applied before
the limit. Missing values remain missing, and each outcome horizon uses its maximum
valid sample (for example, +5 does not require a mature +40 outcome).

Compare a feature separately across the standard offsets:

```bash
python scripts/analyze_lockup_backtest.py \
  --feature return_20d \
  --outcome post_20d_return
```

Or inspect a single offset:

```bash
python scripts/analyze_lockup_backtest.py \
  --feature return_20d \
  --outcome post_20d_return \
  --offset -10
```

JSON reports contain per-offset descriptive returns, fixed bearish thresholds,
Spearman rank correlation, and median-split groups (including stored M6 bearish
MFE/MAE where the chosen horizon exists). `--persistence` adds a deterministic sign
path summary at -20/-10/-5/-1. Feature names are restricted to an explicit pre-event
allowlist, separate from the retrospective outcome allowlist. No offsets are pooled
for a naïve significance test.

### Controlled two-feature interaction

M7 can also test one explicitly preselected feature pair at one explicit observation
offset. For the predefined momentum/volatility question, run:

```bash
python scripts/analyze_lockup_backtest.py \
  --feature return_20d \
  --second-feature realized_vol_20d \
  --outcome post_20d_return \
  --offset -5 \
  --interaction
```

### Frozen M7 robustness analysis

The hypothesis `m7_return20_vol20_minus5_post20` was selected before this
robustness analysis and is frozen as `return_20d` plus
`realized_vol_20d`, observed at offset `-5`, against `post_20d_return`, with a
`median_split` grouping rule and analysis version `m7_robustness_v1`. Run it
explicitly with:

```bash
python scripts/analyze_lockup_backtest.py \
  --feature return_20d \
  --second-feature realized_vol_20d \
  --outcome post_20d_return \
  --offset -5 \
  --interaction \
  --robustness
```

Robustness mode reruns the same intercept-plus-two-feature OLS once per event,
leaving exactly that event out. It reports coefficient ranges and sign flips.
It also rebuilds all median-split cells from each reduced sample: the medians
and therefore `high_high` membership are recomputed after every exclusion.
Full-sample leverage, residual, standardized-residual, and Cook's-distance
diagnostics identify observations that strongly affect the fit; the tool never
automatically excludes influential observations.

This remains small-sample, exploratory sensitivity analysis. Leave-one-event-
out stability is **not** true out-of-sample validation and does not establish a
trading edge. It does not search new features, pairs, offsets, thresholds, or
models, and it applies no multiple-testing correction. The frozen identity is
only a stable specification for later evaluation on future matured events; no
future result is recorded or evaluated by this command.

These two features were chosen before running this analysis. Complete cases are split
into four groups using each feature's analysis-sample median: low is less than or
equal to the median and high is greater than the median. No threshold optimization,
feature-pair search, or offset search is performed. The same complete cases feed an
exploratory OLS regression with an intercept, `outcome ~ feature1 + feature2`; singular,
degenerate, and too-small samples are reported without fitting unstable coefficients.

The current event sample is small, and the regression does not establish an
independent trading edge. It is only intended to test whether one signal remains
informative after accounting for another. Its group summaries and regression use
stored M6 features and outcomes only and do not create a score or recommendation.

P-values are exploratory: the historical event sample is small and many features,
horizons, and offsets may be inspected. M7 does not prove a trading edge, account for
options pricing, benchmark-adjust raw stock returns, optimize thresholds, or provide
trade recommendations. It also does not require incomplete unlock-supply fields;
benchmark adjustment and better locked-share coverage are possible future work.

```text
SEC discovery/enrichment
        ↓
classification
        ↓
prospectus extraction
        ↓
primary lockup extraction
        ↓
market history
        ↓
M6 point-in-time snapshots/outcomes
        ↓
M7 backtest dataset + signal analysis
```

## Milestone 8: prospective validation

M7 is historical discovery; M8 is prospective validation. The sole frozen
hypothesis is `m7_return20_vol20_minus5_post20`: `return_20d` and
`realized_vol_20d` at trading-session offset `-5`, evaluated against
`post_20d_return` with median grouping thresholds frozen from the discovery
cohort. Those thresholds are stored in the specification and prospective
observations can never update them.

Advance the lifecycle from stored, point-in-time M6 snapshots and outcomes:

```bash
python scripts/update_prospective_signals.py \
  --hypothesis-id m7_return20_vol20_minus5_post20

python scripts/update_prospective_signals.py \
  --hypothesis-id m7_return20_vol20_minus5_post20 \
  --dry-run
```

Dry-run reports pending work without database writes. Normal runs permanently
copy the first eligible M6 `-5` snapshot, classify equality as low, and never
refresh those signal fields. A later run may attach the stored M6 +20 outcome
without changing the original classification. The field named
`prospective_start_date` is the hypothesis freeze/cutoff date, not the first
date admitted to evaluation. Observations on or before the 2026-08-23 cutoff
are historical and excluded rather than backfilled; only observations strictly
after the cutoff are eligible for prospective evaluation.

A future lockup can nevertheless be ineligible when its exact required T-5
session occurred on or before that cutoff. M8 records this expected lifecycle
outcome separately from events genuinely waiting for T-5; it is neither an
error nor a prospective signal. The date is accepted only from an existing M6
snapshot or M6's stored-market-session alignment—M8 never guesses it with
calendar or weekday subtraction.

Evaluate genuine prospective rows only:

```bash
python scripts/evaluate_prospective_signals.py \
  --hypothesis-id m7_return20_vol20_minus5_post20
```

Historical M7 rows are not out-of-sample, and leave-one-out robustness is not
out-of-sample validation. M8 rows are the first genuine out-of-sample test.
The report is descriptive and does not retrain, search thresholds, produce a
recommendation, or evaluate an options strategy.

## Later milestones

Options backtesting, benchmark adjustment, scoring, and trading execution are not implemented.

## SEC fair-access note

The SEC asks automated clients to identify themselves and comply with its fair-access policies. Keep request rates modest and cache source data rather than repeatedly downloading the same material.

## Disclaimer

This project is a research prototype. It does not provide investment advice and does not currently submit trades.

## M6 canonical-session parity audit

Historical M6 snapshots treated the available `DailyPrice` rows as trading-session
identity. Consequently, sparse stored history can shift an offset such as T-5 away
from the exact fifth exchange session. The read-only parity audit compares the
stored event and observation dates with the expected sessions from the canonical
XNYS `exchange_calendars` service, reports missing bars and whether the old
stored-bar offset is reproducible, and measures exposure in the frozen M7
discovery cohort.

Each detail row distinguishes exact matches, event-only mismatches,
sparse-history observation mismatches, unexplained observation mismatches,
combined event/observation mismatches, and missing required fields. Summary
counters retain that separation: `observation_sparse_history_cases` counts only
observation-session mismatches reproduced from sparse stored-bar history;
event-only and combined mismatches have their own counters. The deprecated
`sparse_market_history_cases` name remains as an observation-only compatibility
alias. `total_session_mismatches` is the sum of event-only, observation-only
(both sparse and unexplained), and combined session-identity mismatches; it
excludes exact matches and rows with missing required fields.

`sparse_data_related_mismatches` is a separate, evidence-based aggregate. It
requires both missing expected sessions and reproduction of the legacy stored-bar
offset; an event mismatch additionally requires that its canonical event session
is missing. It never infers sparse history from a date mismatch alone. Thus a
live result can be read as distinct exact matches, observation mismatches, event
mismatches, and combined mismatches, while the total reconciles the three
mismatch categories without obscuring their causes.

The M7 impact summary reports the mismatch rate and includes each affected
discovery event's actual T-5 mismatch type. M7 canonical features are reported
as recomputable only when all 21 exact XNYS sessions needed by the existing M6
20-session return and realized-volatility formulas are stored through T-5.

The audit does **not** rewrite M6 snapshots, M7 evidence or thresholds, or M8
prospective signals. A later, explicitly versioned recalculation can be considered
only after this impact has been measured.

```bash
python scripts/audit_m6_session_parity.py \
  --classification-status classified \
  --candidate-type operating_company_ipo \
  --offering-status priced \
  --primary-lockup-only \
  --mismatches-only
```

Use `--details` for all rows, `--ticker`, `--ipo-id`, or `--lockup-id` to narrow
the cohort, and optional `--output path.csv` to export the selected detail rows.

## Canonical market-data coverage and targeted repair

Market-data completeness uses a deliberately hybrid model: the canonical XNYS
calendar defines the sessions that **should** exist, while `DailyPrice` records
the bars actually stored.  The coverage layer reports the deterministic
difference (and separately reports stored rows on non-sessions); a provider's
empty response or error never redefines an exchange session as a holiday.

Audit a date range, or the full range required by a lockup's earliest M6
snapshot and exact 21-session feature window, without writing anything:

```bash
python scripts/audit_market_data_coverage.py --ticker NBRG --lockup-required-range
python scripts/audit_market_data_coverage.py --ticker NBRG \
  --start-date 2026-07-22 --end-date 2026-07-29 --details
```

Lockup-required audits report ancillary lockup rows that have no known event
date as `no_known_event_date` and continue with every plannable row. Use
`--primary-lockup-only` for the standard research-cohort validation commands;
omit it when intentionally auditing non-primary lockups. An explicit
`--start-date`/`--end-date` range does not require a lockup event date and
deduplicates identical security/date-range work selected through multiple
lockup rows.

Targeted repair is a separate, explicitly actioned command.  Dry-run makes no
provider calls and performs no writes; it displays missing sessions and the
batched request ranges.  Replace `--dry-run` with `--execute` to reuse the
configured market-history provider and idempotent `DailyPrice` upserts.

```bash
python scripts/backfill_market_data_gaps.py --ticker NBRG \
  --primary-lockup-only --lockup-required-range --dry-run
```

Backfill uses the same skip reporting, explicit-range behavior, and task
deduplication as the read-only audit.

Future canonical sessions remain visible to planning, but repair requests are
capped at the current (or injected) as-of date and no placeholder rows are
created.  These concepts remain independent: a stored bar does not imply an M6
snapshot, and an M6 snapshot does not imply M8 prospective eligibility.  This
layer neither materializes nor rewrites snapshots; historical M6 v1 and all M7
and M8 evidence remain frozen.

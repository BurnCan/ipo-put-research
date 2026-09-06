# M9B — historical options-data feasibility

## Question and scope

M9B asks one gating question: **can the configured provider identify put
contracts which existed on the canonical T-5 XNYS session and return their
historical bid and ask on that session?** This is a read-only data-capability
audit. It adds no option schema, signal, score, entry rule, valuation rule, or
backtest, and it does not alter M8 or M9A evidence.

A historically valid backtest would require all of the following, without
substitution or interpolation:

1. canonical event and T-5 sessions from the project's XNYS calendar;
2. contract discovery explicitly **as of T-5**, including option symbol,
   underlying, put/call, expiration, and strike;
3. a provider-stamped historical T-5 quote containing both bid and ask; and
4. historical quotes for that same symbol on the event, +1, +5, +10, and +20
   sessions (where those sessions have occurred).

Last trade alone is inadequate. A current chain, current snapshot, or latest
quote is not evidence for any historical date.

## Facts observed in this repository

Before M9B, `app/config.py` configured Massive with one API key and the existing
adapter supported only equity daily aggregates. There was no options-provider
abstraction or options retrieval path. Securities already provide a durable
provider symbol, clean-cohort fields already exist on IPOs, and canonical T-5
is resolved by the existing `exchange_calendars` XNYS helper. The M9B probe
reuses those pieces and does not persist results.

The implementation now probes three capabilities separately:

| Capability | Massive request used | What counts as evidence |
| --- | --- | --- |
| Historical contract discovery | `GET /v3/reference/options/contracts` with `as_of`, `expired=true`, `contract_type=put`, and expiration no earlier than event +20 | A returned historical contract with a contract ticker |
| Historical bid/ask | `GET /v3/quotes/{optionsTicker}` bounded to the requested date | A returned quote whose provider timestamp resolves to that date; bid and ask are reported independently |
| Current chain | `GET /v3/snapshot/options/{underlyingAsset}` | Current availability only; never historical evidence |

These paths and parameters correspond to Massive's published
[options contracts](https://massive.com/docs/rest/options/contracts/all-contracts),
[options quotes](https://massive.com/docs/rest/options/quotes), and
[option-chain snapshot](https://massive.com/docs/rest/options/snapshots/option-chain-snapshot)
interfaces. Documentation exposure establishes that an interface exists; it
does **not** establish the configured account's entitlement or historical depth.

## Account entitlement and sample result

No Massive API key is present in the repository environment, so no live request
was made while implementing M9B and there is no defensible live sample result
to record here. In particular, historical T-5 put bid/ask is **not yet observed
as obtainable** and account entitlement remains untested. This is deliberately
reported as unknown rather than inferred from documentation or from a current
chain.

At runtime the script selects up to 12 reached-T-5 names from the existing
classified, priced operating-company cohort with a selected primary lockup. It
prefers a naturally available persisted M8 name, then alternates older and
newer events. Explicit `--tickers` remain restricted to that cohort. Every name
is retained in the report even when a request fails.

HTTP 401 is classified as `authentication_failure`; HTTP 403 as
`entitlement_plan_restriction`; 404/405 as `unsupported_endpoint`; and
transient HTTP/network, malformed response, no-contract, no-quote, and
wrong-date response cases remain distinct. The key is sent only as a request
parameter and is never included in console output, notes, or report URLs.

## Running the audit

With the configured database and `MASSIVE_API_KEY` available:

```bash
python scripts/audit_options_data_feasibility.py --limit 12 \
  --as-of-date 2026-09-06 --output data/m9b_options_feasibility.json
```

Or select cohort names explicitly:

```bash
python scripts/audit_options_data_feasibility.py --tickers TICKER1 TICKER2 --limit 12
```

The console gives a compact per-name summary. The optional JSON contains the
contract metadata, T-5 field availability, each later-session result, explicit
failure categories, tri-state aggregate capability fields, and one of the five
required feasibility classifications. The output file is the only write made
by the probe and is created only when `--output` is supplied.

## Conclusion and next step

**Present conclusion: `provider_entitlement_unknown` operationally, pending a
credentialed run.** Massive publishes interfaces relevant to all three checks,
and this repository can now test them without confusing current with
historical observations. However, published endpoints alone do not answer
whether this account can retrieve T-5 bid/ask or whether the necessary history
exists across the cohort.

The next step is to run the bounded 10–15-name audit with the deployed database
and current Massive key, retain the JSON result, and review both field coverage
and denial categories. Design an options schema only if that evidence shows
historical contract discovery, T-5 bid/ask, and same-contract follow-up
valuation with adequate sample coverage.

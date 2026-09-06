# M9B — historical options-data feasibility

## Question and scope

M9B asks one gating question: **can the configured provider identify put
contracts which existed on the canonical T-5 XNYS session and return their
historical bid and ask on that session?** This is a read-only data-capability
audit. It adds no option schema, signal, score, entry rule, valuation rule, or
backtest, and it does not alter M8 or M9A evidence.

A provider demonstrates historical **entry reconstruction** capability with
the following, without substitution or interpolation:

1. canonical event and T-5 sessions from the project's XNYS calendar;
2. contract discovery explicitly **as of T-5**, including option symbol,
   underlying, put/call, expiration, and strike;
3. a provider-stamped historical T-5 quote containing both bid and ask; and

Same-contract **follow-up valuation** is a separate capability check. The audit
requests historical quotes for the same symbol on the event, +1, +5, +10, and
+20 sessions only when the target session has occurred and is on or before the
contract's validated expiration date. A target after expiration is reported as
`contract_expired`; it is a normal contract-lifecycle outcome, not missing data
or a provider failure. The expiration date itself remains eligible for probing.

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
| Historical contract discovery | `GET /v3/reference/options/contracts` with `as_of=T-5`, `expired=true`, `contract_type=put`, and expiration no earlier than T-5 | A returned historical contract with a contract ticker and valid expiration showing that it was live on T-5 |
| Historical bid/ask | `GET /v3/quotes/{optionsTicker}` bounded to the requested date | A returned quote whose provider timestamp resolves to that date; bid and ask are reported independently |
| Current chain | `GET /v3/snapshot/options/{underlyingAsset}` | Current availability only; never historical evidence |

These paths and parameters correspond to Massive's published
[options contracts](https://massive.com/docs/rest/options/contracts/all-contracts),
[options quotes](https://massive.com/docs/rest/options/quotes), and
[option-chain snapshot](https://massive.com/docs/rest/options/snapshots/option-chain-snapshot)
interfaces. Documentation exposure establishes that an interface exists; it
does **not** establish the configured account's entitlement or historical depth.

## Live capability and optionability findings

Historical Massive contract-reference control queries succeeded for SPY and
AAPL as of 2026-09-04 and returned historical puts. This establishes that the
provider can perform the required historical contract-reference lookup. It is
separate from whether a particular cohort company had listed puts.

The first three prospective names to reach canonical T-5 had no historical put
contracts at T-5: TRGS on 2026-08-18, GENB on 2026-08-19, and SWMR on
2026-09-04. Each lookup completed successfully with an empty result, so each is
reported as `optionable_at_t5=false`. This is an **optionability constraint**,
not a provider-capability failure. Three observations are far too few to infer
or present a stable optionability rate for the cohort.

Because none of those names supplied a real historical contract on which to
request quotes, historical T-5 bid/ask and later-valuation capabilities remain
untested and are reported as `null`, not `false`.

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
failure categories, per-name tri-state `optionable_at_t5`, optionability summary
counts, tri-state aggregate capability fields, and an explicit feasibility
classification. The representative contract is selected
deterministically by earliest expiration, then strike and ticker; selection
does not consider survival to +20. The report also identifies contracts that
expire before +20. The output file is the only write made by the probe and is
created only when `--output` is supplied.

## Conclusion and next step

**Present cohort conclusion: `cohort_not_optionable_at_t5`.** Historical
contract-reference capability has been demonstrated by the SPY and AAPL
controls, while all three reached-T-5 prospective names returned no puts. Quote
capability remains unknown until an optionable cohort name appears; an empty
contract result cannot test it.

The next step is to continue the bounded audit as more prospective names reach
T-5. Once a name has a valid historical put, its T-5 bid/ask and same-contract
follow-up quotes can test the still-unresolved quote capabilities. Follow-up
coverage should be reviewed separately for each reached session during that
contract's lifetime; natural expiration before +20 does not make the provider
historically incapable.

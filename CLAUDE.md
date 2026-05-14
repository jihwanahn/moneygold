# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Korean (KOSPI/KOSDAQ) equity screening + LLM advisory pipeline. The code is Python scripts with no package metadata — there's no `requirements.txt`, `pyproject.toml`, or virtualenv config. Comments and prompts are in Korean.

## Running

There are two entry points; both must be invoked from the repo root (paths in `constants.py` and cache dirs are relative):

- `python screen.py` — broad screening across KRX. Pulls `data/screening_history.json`, decides which previously-seen tickers are stale enough to re-advise (refresh cadence depends on last `action`: BUY=1d → SELL=30d), explores up to 5 new candidates, then runs Screener → Analyzer → Advisor and writes back to the history file.
- `python track.py` — focused pass over `data/user_interest.json` only (no exploration, no history mutation). Prints the advice JSON for each ticker.

Both scripts call `login_krx()` first, which logs into `data.krx.co.kr` using credentials from `config.json` and monkey-patches `pykrx.website.comm.webio` so all subsequent pykrx calls reuse the authenticated `requests.Session`. Without a valid login, pykrx fetches will fail or be rate-limited.

## Required secrets (not in repo)

- `config.json` — `{"id": ..., "password": ...}` for KRX login. The committed file holds Korean placeholders (`"아이디"`, `"비밀번호"`).
- `DART_API_KEY` in `analyzer.py:26` — hardcoded constant currently set to the placeholder `"다트 키"`. Replace with a real OpenDART key before running anything that touches the Analyzer stage.
- `OPENAI_API_KEY` (or `GOOGLE_API_KEY` if switching providers) — read from environment by langchain.

## Pipeline architecture

Three stages, each implemented as a class in its own module and chained by `screen.py` / `track.py`:

1. **Screener** (`screener.py`) — KRX market data via `pykrx`.
   - Stage 1 (`build_universe`): fetch fundamentals + market cap for the full market, filter by MCAP ≥ 50B KRW and trading value ≥ 300M KRW, keep top 400 by a rough PBR/PER/DIV composite.
   - Stage 2 (`add_mom_vol`): pull cached OHLCV per ticker to compute 120-day momentum and 60-day return volatility.
   - Scoring (`score_and_rank`): winsorize + percentile-rank PBR/PER/DIV/MOM/VOL, weighted-sum into `SCORE`. The ranked DF is then split into `tracking_tickers` (forced in) + top-`exploration` from the unvisited pool. `build_user_interest_df` is the parallel path used by `track.py` — same column shape, no scoring.
   - Output: `result/screener/{biz_date}/{ticker}.json` (one file per ticker, the per-item dict from `build_batch`).

2. **Analyzer** (`analyzer.py`, ~1.3k lines, the heaviest module) — DART (OpenDART) financial-statement enrichment via `dart-fss` + raw HTTP.
   - Maps KRX ticker → DART `corp_code`, then `fetch_latest_n_fnltt_by_rcept_dt` walks disclosures by `rcept_dt` (filing date, not fiscal date) and de-dupes restatements per `(bsns_year, reprt_code)`.
   - Statements are pulled via `fnlttSinglAcntAll.json` with CFS-then-OFS fallback (`_fetch_fnltt_best_effort_with_fsdiv_cached`).
   - Reports come as **cumulative** values; `_periodize_metrics_from_cumulative` subtracts prior quarters within the same fiscal year to produce stand-alone quarterly metrics. To do this it needs the Q1/H1/Q3/Annual reports for that year — missing ones are back-filled by `_ensure_year_reports_for_periodize`.
   - Account matching is keyword-based against `account_nm` (see `REVENUE_KEYS`, `OP_KEYS`, `NI_KEYS`, `CAPEX_KEYS` near the top), with normalization in `_normalize_fnltt_df` (NBSP/space stripping, fullwidth parens).
   - `decorate_dart_to_quant` is the public entry called by both runners; it returns the screener payload plus `financials_dart` and `events_dart` (recent CB/BW/EB issuances).
   - Output: `result/analyzer/{biz_date}/{ticker}.json`.
   - Debug: setting `DEBUG_TICKER` (currently `"0004V0"`) dumps raw DART rows, IS/CF blocks, and keyword hits to `./debug_dart/` whenever extraction misses a field. Set to `None` to disable.

3. **Advisor** (`advisor.py`) — wraps a langchain chat model (`build_llm` supports OpenAI with `web_search_preview` tool binding, or Gemini without tool binding) with a Korean system prompt that demands a single-JSON-object response (no markdown, no code fences). Output is written verbatim as `result/advisor/{asof}/{ticker}{postfix}.txt`. `screen.py` then `json.loads`-es it to extract `action` for the history update.

## Caching layout

All caches are parquet/JSON on disk; cache hits skip the network. None of these are checked in (`.gitignore` covers them).

- `pykrx_cache/` — market-wide fundamentals/cap (per `biz_date` × `market`), per-ticker OHLCV (`ohlcv_{ticker}_{start}_{end}.parquet`), and investor trading-value/volume timeseries. Wrappers: `_cached_df`, `get_ohlcv_cached`.
- `dart_cache/` — `fnltt_{corp_code}_{year}_{reprt_code}_{fs_div}.parquet` plus a `.meta.json` sidecar containing `rcept_no`. On read, if the caller passes `expected_rcept_no` and it disagrees with the sidecar, the cache is invalidated (handles DART restatements).
- `debug_dart/` — only populated when `DEBUG_TICKER` matches.
- `result/` — final outputs, organized as `result/{screener|analyzer|advisor}/{biz_date}/{ticker}.{json|txt}`. Both runners check `os.path.exists` on these and skip stages whose output is already cached, so deleting a single file is the way to force a re-run for one ticker.

## Things to know before editing

- `screener.py` and `analyzer.py` both define `get_biz_date()` and `_load_parquet/_save_parquet` — they are intentionally separate copies (different cache atomicity behavior in analyzer).
- `screen.py` and `track.py` duplicate the KRX login + `webio` monkey-patch block verbatim; changes to login flow must be made in both.
- `screen.py:237-240` has a known bug: the loop assigns `each["action"] = action` using the *last* `action` from the prior loop's local scope, not the per-ticker value from `history_update`. Don't propagate this pattern.
- The Analyzer's REQ_SLEEP (0.25s) gates every DART call — lowering it triggers rate limits; raising it makes a fresh run on a large screen take a long time. Cache reuse is the real speedup.
- The Advisor's expected output is strict JSON; the prompt explicitly forbids backticks/markdown. If you change the prompt template (`USER_PROMPT_TEMPLATE` in `advisor.py`), make sure `screen.py`'s `json.loads(advice)` still works or it will silently fall into the `Manual Adjustment Required` branch and skip the history update.

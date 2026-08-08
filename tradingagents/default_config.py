import os

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

# Single source of truth for env-var → config-key overrides. To expose
# a new config key for environment-based override, add a row here — no
# entry-point script changes required. Coercion is driven by the type
# of the existing default, so users can keep writing plain strings in
# their .env file.
_ENV_OVERRIDES = {
    "TRADINGAGENTS_LLM_PROVIDER":         "llm_provider",
    "TRADINGAGENTS_DEEP_THINK_LLM":       "deep_think_llm",
    "TRADINGAGENTS_QUICK_THINK_LLM":      "quick_think_llm",
    "TRADINGAGENTS_LLM_BACKEND_URL":      "backend_url",
    "TRADINGAGENTS_OUTPUT_LANGUAGE":      "output_language",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS":    "max_debate_rounds",
    "TRADINGAGENTS_MAX_RISK_ROUNDS":      "max_risk_discuss_rounds",
    "TRADINGAGENTS_CHECKPOINT_ENABLED":   "checkpoint_enabled",
    "TRADINGAGENTS_BENCHMARK_TICKER":     "benchmark_ticker",
    "TRADINGAGENTS_TEMPERATURE":          "temperature",
    "TRADINGAGENTS_LLM_MAX_RETRIES":      "llm_max_retries",
    # Provider-specific reasoning/thinking knobs (None = each provider's own
    # default). Settable here for non-interactive runs; the CLI also offers an
    # interactive choice, which is skipped when the matching var is set.
    "TRADINGAGENTS_GOOGLE_THINKING_LEVEL":   "google_thinking_level",
    "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "openai_reasoning_effort",
    "TRADINGAGENTS_ANTHROPIC_EFFORT":        "anthropic_effort",
    # --- Crypto execution layer (optional, off by default) ---
    # All execution knobs are env-overridable so a live run can be configured
    # purely via .env without editing code. Default to paper/testnet.
    "TRADINGAGENTS_EXECUTION_ENABLED":       "execution_enabled",
    "TRADINGAGENTS_EXECUTION_MODE":          "execution_mode",
    "TRADINGAGENTS_CRYPTO_EXCHANGE":         "crypto_exchange",
    "TRADINGAGENTS_CRYPTO_API_KEY":          "crypto_api_key",
    "TRADINGAGENTS_CRYPTO_SECRET":           "crypto_secret",
    "TRADINGAGENTS_CRYPTO_PASSPHRASE":       "crypto_passphrase",
    "TRADINGAGENTS_CRYPTO_HTTPS_PROXY":      "crypto_https_proxy",
    "TRADINGAGENTS_CRYPTO_QUOTE_BUDGET":     "crypto_quote_budget",
    "TRADINGAGENTS_CRYPTO_MAX_POSITION":     "crypto_max_position",
    "TRADINGAGENTS_CRYPTO_COOLDOWN_SECONDS": "crypto_cooldown_seconds",
    "TRADINGAGENTS_CRYPTO_BUY_COOLDOWN_SECONDS":  "crypto_buy_cooldown_seconds",
    "TRADINGAGENTS_CRYPTO_SELL_COOLDOWN_SECONDS": "crypto_sell_cooldown_seconds",
    "TRADINGAGENTS_CRYPTO_STOP_LOSS_PCT":    "crypto_stop_loss_pct",
    # --- Autonomous runner (optional, off by default) ---
    # The runner wraps propagate() in a loop. Disabled unless explicitly turned
    # on; when off the framework behaves as a single-shot CLI/script.
    "TRADINGAGENTS_RUNNER_ENABLED":          "runner_enabled",
    "TRADINGAGENTS_RUNNER_INTERVAL_SECONDS": "runner_interval_seconds",
    "TRADINGAGENTS_RUNNER_MAX_CYCLES":       "runner_max_cycles",
    "TRADINGAGENTS_RUNNER_TICKERS":          "runner_tickers",
    "TRADINGAGENTS_RUNNER_DB_PATH":          "runner_db_path",
    "TRADINGAGENTS_RUNNER_REFLECT_EVERY_N_CYCLES": "runner_reflect_every_n_cycles",
    "TRADINGAGENTS_RUNNER_REFLECT_MIN_AGE_HOURS":  "runner_reflect_min_age_hours",
    "TRADINGAGENTS_RUNNER_REFLECT_HOLD_EVERY_N_CYCLES": "runner_reflect_hold_every_n_cycles",
    "TRADINGAGENTS_RUNNER_DAILY_LOSS_LIMIT":   "runner_daily_loss_limit",
    "TRADINGAGENTS_RUNNER_MAX_DRAWDOWN":       "runner_max_drawdown",
}


_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")


def _coerce(value: str, reference):
    """Coerce env-var string to the type of the existing default value.

    Invalid values raise ``ValueError`` rather than silently falling back to a
    default — a misspelled boolean (e.g. ``treu``) or non-numeric int should fail
    loudly at startup, not quietly misconfigure an unattended run.
    """
    if isinstance(reference, bool):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE:
            return True
        if normalized in _BOOL_FALSE:
            return False
        raise ValueError(
            f"expected a boolean ({'/'.join(_BOOL_TRUE + _BOOL_FALSE)}), got {value!r}"
        )
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    return value


def _apply_env_overrides(config: dict) -> dict:
    """Apply TRADINGAGENTS_* env vars to the config dict in-place."""
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        try:
            config[key] = _coerce(raw, config.get(key))
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_var}: {exc}") from exc

    # Legacy cooldown fallback: ``TRADINGAGENTS_CRYPTO_COOLDOWN_SECONDS`` (the
    # pre-direction single-value knob) must keep working for existing
    # deployments. If it is set and neither direction-aware env var is, seed
    # both direction keys from it. Direction-aware vars always win when
    # explicitly set.
    legacy_cooldown = os.environ.get("TRADINGAGENTS_CRYPTO_COOLDOWN_SECONDS")
    if (
        legacy_cooldown
        and not os.environ.get("TRADINGAGENTS_CRYPTO_BUY_COOLDOWN_SECONDS")
        and not os.environ.get("TRADINGAGENTS_CRYPTO_SELL_COOLDOWN_SECONDS")
    ):
        legacy_value = config.get("crypto_cooldown_seconds")
        if legacy_value is not None:
            config["crypto_buy_cooldown_seconds"] = legacy_value
            config["crypto_sell_cooldown_seconds"] = legacy_value

    return config


DEFAULT_CONFIG = _apply_env_overrides({
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
    # Feishu webhook for daily report push (optional; used by daily_report.py)
    "feishu_webhook": os.getenv("TRADINGAGENTS_FEISHU_WEBHOOK", ""),
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    "memory_log_max_entries": None,
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.5",
    "quick_think_llm": "gpt-5.4-mini",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Sampling temperature, forwarded to every provider when set. None leaves
    # each provider at its own default. Lower values reduce run-to-run
    # variation on models that honor it; reasoning models largely ignore it
    # and no setting makes LLM output bit-identical across runs (see README).
    "temperature": None,
    # SDK retry budget forwarded to every provider chat client. None leaves each
    # provider/SDK at its own default (usually 2). Raise it to ride out bursty
    # 429 throttling on rate-limited deployments instead of aborting a run (#1091).
    "llm_max_retries": None,
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # News / data fetching parameters
    # Increase for longer lookback strategies or to broaden macro coverage;
    # decrease to reduce token usage in agent prompts.
    "news_article_limit": 20,             # max articles per ticker (ticker-news)
    "global_news_article_limit": 10,      # max articles for global/macro news
    "global_news_lookback_days": 7,       # macro news lookback window
    # Search queries used by get_global_news for macro headlines. Extend or
    # replace to broaden geographic / sector coverage.
    "global_news_queries": [
        "Federal Reserve interest rates inflation",
        "S&P 500 earnings GDP economic outlook",
        "geopolitical risk trade war sanctions",
        "ECB Bank of England BOJ central bank policy",
        "oil commodities supply chain energy",
    ],
    # Data vendor configuration
    # Category-level configuration (default for all tools in category).
    # The configured value is the exact vendor chain — requests are NOT silently
    # routed to vendors you didn't choose. For ordered fallback, list several,
    # e.g. "yfinance,alpha_vantage". "default" uses all available vendors.
    "data_vendors": {
        "core_stock_apis": "okx,yfinance",           # Options: okx, yfinance, alpha_vantage
        "technical_indicators": "okx,yfinance",      # Options: okx, yfinance, alpha_vantage
        "fundamental_data": "okx,yfinance",          # Options: okx, yfinance, alpha_vantage
        "news_data": "okx,yfinance",                 # Options: okx, yfinance, alpha_vantage
        "macro_data": "fred",                        # Options: fred (needs FRED_API_KEY)
        "prediction_markets": "polymarket",          # Options: polymarket (keyless)
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
    # Benchmark for alpha calculation in the reflection layer.
    # ``benchmark_ticker`` (when set) overrides the suffix map for all
    # tickers; leave it None to use ``benchmark_map`` for auto-detection
    # based on the ticker's exchange suffix. SPY remains the US default
    # so the reflection label keeps reading "Alpha vs SPY" for US tickers
    # while non-US tickers get their regional index automatically.
    "benchmark_ticker": None,
    "benchmark_map": {
        ".NS":  "^NSEI",       # NSE India (Nifty 50)
        ".BO":  "^BSESN",      # BSE India (Sensex)
        ".T":   "^N225",       # Tokyo (Nikkei 225)
        ".HK":  "^HSI",        # Hong Kong (Hang Seng)
        ".L":   "^FTSE",       # London (FTSE 100)
        ".TO":  "^GSPTSE",     # Toronto (TSX Composite)
        ".AX":  "^AXJO",       # Australia (ASX 200)
        ".SS":  "000001.SS",   # Shanghai (SSE Composite)
        ".SZ":  "399001.SZ",   # Shenzhen (SZSE Component)
        "":     "SPY",         # default for US-listed tickers (no suffix)
    },
    # --- Crypto execution layer (optional) ---
    # Off by default: when False, runs only produce analysis reports and never
    # place orders. Enable only after reading the risk notes in the README and
    # tradingagents/execution/crypto_broker.py.
    "execution_enabled": False,
    # "paper" routes orders to the exchange testnet (e.g. Binance testnet /
    # OKX sandbox). "live" uses real funds — never start here.
    "execution_mode": "paper",
    # ccxt exchange id. Spot only; futures are intentionally not supported.
    # Common: binance, okx, bybit, kraken, coinbase.
    "crypto_exchange": "binance",
    "crypto_api_key": None,
    "crypto_secret": None,
    # OKX requires a passphrase (the 3rd credential set when creating the API
    # key); Binance/Bybit/Kraken ignore it. Mapped to ccxt's `password` field.
    "crypto_passphrase": None,
    # Optional HTTPS proxy for the ccxt session. Needed in networks where the
    # exchange domain is unreachable directly (e.g. OKX from mainland China).
    # Example: "http://127.0.0.1:7897". None = direct connection.
    "crypto_https_proxy": None,
    # Max quote currency (USDT) to spend on a single BUY. Caps risk per signal.
    "crypto_quote_budget": 5000.0,
    # Max fraction of total account equity to hold in a single base asset
    # (e.g. 0.2 = 20% in BTC). Sell orders are unaffected.
    "crypto_max_position": 0.2,
    # Minimum seconds between orders on the same symbol. Guards against the
    # non-deterministic LLM re-issuing the same signal within a session.
    # ``crypto_cooldown_seconds`` is the legacy single-value knob (seeds both
    # directions for backward compatibility); prefer the direction-aware pair
    # below. Buy cooldown stays long (avoid doubling up on a signal); sell
    # cooldown is short so exits (take-profit / stop-loss) are never blocked
    # by a stale "just bought" stamp.
    "crypto_cooldown_seconds": 14400,  # 4 hours (legacy fallback)
    "crypto_buy_cooldown_seconds": 14400,   # 4 hours between buys on a symbol
    "crypto_sell_cooldown_seconds": 3600,   # 1 hour between sells on a symbol
    # Code-level hard stop-loss: when a held position drops this many percent
    # below its last filled BUY price, the runner force-sells at market
    # regardless of what the LLM decides. 0 disables the check. This is a
    # deterministic safety net that cannot be talked out of by the PM.
    "crypto_stop_loss_pct": 10.0,

    # --- Autonomous runner (optional, OFF by default) ---
    # Wraps propagate() in a scheduled loop. When enabled, the runner takes a
    # list of tickers, runs the full analysis -> decision -> broker order
    # pipeline for each on a fixed interval, and persists positions + orders to
    # a SQLite DB so the agent knows what it holds between cycles. Disabled
    # unless the user opts in — the default framework behavior is single-shot.
    "runner_enabled": False,
    # Seconds between the start of consecutive cycles. The LLM analysis itself
    # can take several minutes, so the effective cadence is max(interval,
    # analysis_runtime). The PM's decision horizon is multi-day (daily OHLCV
    # confirmation), so 4 hours per cycle is enough to catch signals without
    # re-running the same daily setup every hour. 14400s = 4 hours.
    "runner_interval_seconds": 14400,
    # Hard cap on total cycles across all tickers. 0 = run forever (until
    # interrupted). Useful for "run 3 cycles then stop" smoke tests.
    "runner_max_cycles": 0,
    # Comma-separated tickers to analyze each cycle, e.g. "BTC-USD,ETH-USD".
    # Each ticker is analyzed sequentially within a cycle.
    "runner_tickers": "BTC-USD,ETH-USD,SOL-USD",
    # SQLite path for the runner state store. None = default under
    # data_cache_dir/runner_state.db.
    "runner_db_path": None,
    # Reflect on filled trades every N cycles (0 = never auto-reflect).
    # Reflection pulls unreflected filled orders, fetches current prices,
    # computes PnL, calls the LLM for a 3-5 sentence lesson, and appends it
    # to the memory log so the next analysis run can learn from it.
    "runner_reflect_every_n_cycles": 3,
    # Minimum age (hours) a trade must reach before it's eligible for
    # reflection. Prevents reflecting on a trade seconds after it fills.
    "runner_reflect_min_age_hours": 1.0,
    # Reflect on HOLD (no-trade) decisions every N cycles (0 = never).
    # Unlike trade reflection, this reviews decisions that did NOT result in
    # an order — "did my HOLD age well?" — so the system keeps learning even
    # during idle stretches where nothing fills. cycle_count increments per
    # ticker (3 tickers × 6 outer rounds/day at 4h cadence = 18 cycle-runs/day),
    # so 18 ≈ once per day.
    "runner_reflect_hold_every_n_cycles": 18,
    # Guardrails halt: skip a cycle (no LLM, no new orders, but stop-loss still
    # runs) when the account equity drops below these thresholds. Both are
    # NEGATIVE numbers (loss percentages). daily_loss_limit compares against
    # the first non-NULL equity_before of the UTC day; max_drawdown compares
    # against the all-time peak equity_after. Set to 0 or a positive number to
    # disable (but _check_guardrails validates < 0 at runtime and raises).
    "runner_daily_loss_limit": -0.10,   # -10% intraday floating loss
    "runner_max_drawdown":     -0.15,   # -15% historical drawdown from peak
})

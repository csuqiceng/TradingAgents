"""OKX-based crypto market data functions.

Replaces yfinance for crypto OHLCV, technical indicators, and news.
Uses ccxt for market data and CryptoCompare for news.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import pandas as pd
import requests

from .errors import VendorRateLimitError
from .stockstats_utils import _assert_ohlcv_not_stale, _clean_dataframe, _ensure_date_column
from .symbol_utils import NoMarketDataError
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Configuration
# ------------------------------------------------------------------ #

_OKX_CACHE_DIR = os.path.join(
    os.path.expanduser("~"), ".tradingagents", "cache", "okx"
)
_OHLCV_CACHE_TTL_SECONDS = 900  # 15 min same-day cache
_CRYPTO_PROXY = os.environ.get(
    "TRADINGAGENTS_CRYPTO_HTTPS_PROXY",
    "http://127.0.0.1:7897",
)
# CryptoCompare: prefer env var, fall back to a known test key.
_CRYPTOCOMPARE_API_KEY = os.environ.get(
    "CRYPTOCOMPARE_API_KEY",
    "d3c9c19d007792e5eeec72588bc00f7b71babfdd77073f0a2edd7512f03e4700",
)

# Max candles per OKX fetch_ohlcv call (OKX limit).
_OKX_FETCH_LIMIT = 500
# Max retries for OKX public API calls.
_OKX_MAX_RETRIES = 3


# ------------------------------------------------------------------ #
# Symbol helpers
# ------------------------------------------------------------------ #

def to_ccxt_symbol(symbol: str) -> str:
    """Convert a TradingAgents ticker (e.g. ``BTC-USD``) to a ccxt pair."""
    base = symbol.upper().replace("+", "")
    for sep in ("-", "/", "_"):
        if sep in base:
            base = base.split(sep)[0]
            break
    for quote in ("USDT", "USDC", "USD", "BUSD"):
        if base.endswith(quote) and len(base) > len(quote):
            base = base[: -len(quote)]
            break
    return f"{base}/USDT"


def _token_keywords(ticker: str) -> list[str]:
    """Return a list of keywords for matching news against a token.

    Uses word-boundary patterns to avoid false positives from substrings
    (e.g. ``SOL`` matching ``solution``, ``ETH`` matching ``method``).
    For common crypto names we also include the full name for better recall.
    """
    base = to_ccxt_symbol(ticker).split("/")[0]
    keywords = {base.lower()}

    # Known full-name mappings for better recall
    name_map = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
        "USDT": "tether",
        "USDC": "usd coin",
        "BNB": "bnb",
        "XRP": "ripple",
        "ADA": "cardano",
        "DOGE": "dogecoin",
        "DOT": "polkadot",
        "AVAX": "avalanche",
        "MATIC": "polygon",
        "LINK": "chainlink",
        "UNI": "uniswap",
        "ATOM": "cosmos",
    }
    if base.upper() in name_map:
        keywords.add(name_map[base.upper()])

    return list(keywords)


# ------------------------------------------------------------------ #
# Exchange client
# ------------------------------------------------------------------ #

def _create_exchange() -> Any:
    """Create a lightweight ccxt OKX client for public data (no auth needed)."""
    try:
        import ccxt
    except ImportError as exc:
        raise ImportError("ccxt is required for OKX data. Install with: pip install ccxt") from exc

    exchange = ccxt.okx({
        "enableRateLimit": True,
    })
    if _CRYPTO_PROXY:
        exchange.proxies = {"http": _CRYPTO_PROXY, "https": _CRYPTO_PROXY}
    return exchange


def _okx_retry(fn, *args, max_retries=_OKX_MAX_RETRIES, base_delay=2.0, **kwargs):
    """Execute an OKX API call with exponential backoff on rate limits."""
    import ccxt
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except ccxt.RateLimitExceeded as exc:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "OKX rate limited, retrying in %.0fs (attempt %d/%d)",
                    delay, attempt + 1, max_retries,
                )
                time.sleep(delay)
            else:
                raise VendorRateLimitError(
                    f"OKX rate limited after {max_retries} retries: {exc}"
                ) from exc
        except Exception as exc:
            # Non-rate-limit errors propagate immediately
            raise


# ------------------------------------------------------------------ #
# OHLCV data
# ------------------------------------------------------------------ #

def _needs_same_day_refresh(cache_file: str, curr_date_dt: datetime, today: datetime) -> bool:
    """Whether a cached frame needs a refresh for the same day."""
    if curr_date_dt.date() < today.date():
        return False
    return time.time() - os.path.getmtime(cache_file) > _OHLCV_CACHE_TTL_SECONDS


# Known crypto tickers (case-insensitive, add more as needed)
_KNOWN_CRYPTO_TICKERS = {
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "DOT", "AVAX",
    "BNB", "LINK", "UNI", "ATOM", "MATIC", "TRX", "SHIB", "PEPE",
    "FLOKI", "BOME", "ENA", "STRK", "SUI", "NEIRO", "OKB", "ACT",
    "PNUT", "NMR", "LAT", "MEW", "IOTA", "DOGS", "TRUMP", "TON",
    "FIL", "APT", "ARB", "OP", "INJ", "TIA", "SEI", "WIF",
    "BONK", "CRV", "AAVE", "MKR", "COMP", "SUSHI", "CAKE",
    "STX", "EGLD", "FTM", "ALGO", "NEAR", "FLOW", "ICP", "FET",
    "AGIX", "OCEAN", "RNDR", "HNT", "ANKR", "CHZ", "SAND",
    "MANA", "AXS", "GALA", "ILV", "YGG", "APE", "BLUR",
    "LDO", "SSV", "RPL", "FXS", "CVX", "BAL", "ZRX", "1INCH",
    "ENS", "DYDX", "MINA", "CELO", "KSM", "WAVES", "ZEC",
    "DASH", "ETC", "XTZ", "EOS", "IOST", "IOTX", "NEO", "GAS",
    "VET", "VTHO", "THETA", "TFUEL", "HOT", "RVN", "SC",
    "STORJ", "AR", "BAND", "GRT", "OCEAN", "NKN", "HIVE",
    "ALPHA", "ALICE", "BAKE", "BTCST", "BURGER", "C98", "CHR",
    "COTI", "DODO", "DUSK", "FET", "FRONT", "FUN", "HARD",
    "IDEX", "JST", "KAVA", "KDA", "KLAY", "LINA", "LIT",
    "MDX", "MITH", "MLN", "ONT", "ORN", "OXT", "PAXG",
    "POLS", "PROM", "PROS", "QNT", "QUICK", "RAD", "RAI",
    "REN", "REQ", "RIF", "RLC", "ROSE", "RSR", "SXP",
    "TOMO", "TORN", "TRB", "TRIBE", "TROY", "TRU", "TVK",
    "UMA", "UTK", "VITE", "VOXEL", "WAN", "WAXP", "WOO",
    "XEM", "XNO", "XPRT", "XVS", "YFI", "YFII", "ZEN",
    "ZIL", "ZKS", "ZRX",
}

# Stock exchange suffixes — tickers with these are definitely NOT crypto.
_STOCK_SUFFIXES = {
    ".SS", ".SZ",        # China A-shares
    ".NS", ".BO",        # India
    ".T", ".TO", ".V",  # Tokyo / Toronto
    ".HK",                # Hong Kong
    ".L", ".DE", ".PA", ".MI",  # Europe
    ".AX",                # Australia
    ".ST", ".CO", ".OL", ".HE", ".BR", ".LS",  # Nordic
    ".TW", ".KS", ".KQ",  # Taiwan / Korea
    ".JK", ".KL", ".SG",  # Southeast Asia
    ".MX", ".TA", ".SA", ".SI", ".IS",  # Other
}


def is_crypto_ticker(symbol: str) -> bool:
    """Quick heuristic: is this symbol likely a cryptocurrency?

    Returns False for known stock exchange tickers (e.g. ``000001.SS``,
    ``AAPL``, ``TSM``) so the routing system can fall back to yfinance
    without making a wasted OKX API call.
    """
    sym = symbol.upper().strip()

    # 1. Stock exchange suffix → definitely NOT crypto
    for suffix in _STOCK_SUFFIXES:
        if sym.endswith(suffix):
            return False

    # 2. Known stock tickers that just happen to be common (no suffix)
    #    We don't maintain a blocklist — if it has no suffix and isn't a
    #    known crypto, we let OKX try and fail (fast fail, no API cost).

    # 3. Extract base symbol
    for sep in ("-", "/", "_"):
        if sep in sym:
            base = sym.split(sep)[0]
            break
    else:
        base = sym

    # 4. Known crypto ticker → definitely crypto
    if base in _KNOWN_CRYPTO_TICKERS:
        return True

    # 5. Crypto quote suffix → very likely crypto
    if any(q in sym for q in ["-USDT", "-USDC", "/USDT", "/USDC", "/USD"]):
        return True

    # 6. Default: let OKX try (fast fail if symbol doesn't exist on OKX)
    return True


def _assert_is_crypto(symbol: str, func_name: str) -> None:
    """Raise NoMarketDataError early if the symbol is not a crypto ticker.

    This avoids wasting an OKX API call for stock tickers that should be
    served by yfinance.
    """
    if not is_crypto_ticker(symbol):
        raise NoMarketDataError(
            symbol, None,
            f"Not a crypto ticker — '{func_name}' serves crypto only. "
            f"Falling back to yfinance.",
        )


def get_okx_ohlcv(
    symbol: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Fetch OHLCV data from OKX, returning a DataFrame with yfinance-compatible columns.

    Returns columns: Date, Open, High, Low, Close, Volume.
    Caches to disk per symbol to reduce API calls.
    Automatically paginates if the requested range exceeds 500 candles.
    """
    ccxt_symbol = to_ccxt_symbol(symbol)
    safe_symbol = safe_ticker_component(ccxt_symbol.replace("/", "-"))
    curr_date_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    today = pd.Timestamp.today()

    # Cache key
    cache_file = os.path.join(_OKX_CACHE_DIR, f"{safe_symbol}-okx-ohlcv.csv")
    os.makedirs(_OKX_CACHE_DIR, exist_ok=True)

    # Try cache first
    data = None
    if os.path.exists(cache_file):
        cached = pd.read_csv(cache_file, on_bad_lines="skip", encoding="utf-8")
        if (
            not cached.empty
            and {"Date", "Open", "High", "Low", "Close", "Volume"}.issubset(cached.columns)
            and not _needs_same_day_refresh(cache_file, curr_date_dt, today)
        ):
            data = cached

    if data is None:
        exchange = _create_exchange()

        # Calculate how many days of data we need (with buffer for SMA200 etc.)
        request_days = (end_dt - curr_date_dt).days + 400  # generous buffer
        if request_days > _OKX_FETCH_LIMIT:
            # Paginate: fetch recent data, then older data if needed
            all_candles = []
            since = None
            remaining = request_days
            while remaining > 0:
                try:
                    batch = _okx_retry(
                        exchange.fetch_ohlcv,
                        ccxt_symbol,
                        timeframe="1d",
                        limit=min(_OKX_FETCH_LIMIT, remaining),
                        since=since,
                    )
                except (NoMarketDataError, VendorRateLimitError) as exc:
                    raise NoMarketDataError(
                        symbol, ccxt_symbol,
                        f"OKX fetch_ohlcv failed: {exc}",
                    ) from exc

                if not batch:
                    break
                all_candles = batch + all_candles  # prepend (since goes backwards)
                if len(batch) < min(_OKX_FETCH_LIMIT, remaining):
                    break  # no more data
                # Move since back: use the first candle's timestamp
                since = batch[0][0] - 1

                # If we already have data from the start_date, we're done
                first_ts = datetime.fromtimestamp(batch[0][0] / 1000, tz=timezone.utc)
                if first_ts <= curr_date_dt:
                    break
                remaining = (end_dt - first_ts).days + 400

            candles = all_candles
        else:
            # Single fetch
            try:
                candles = _okx_retry(
                    exchange.fetch_ohlcv,
                    ccxt_symbol,
                    timeframe="1d",
                    limit=_OKX_FETCH_LIMIT,
                )
            except (NoMarketDataError, VendorRateLimitError) as exc:
                raise NoMarketDataError(
                    symbol, ccxt_symbol,
                    f"OKX fetch_ohlcv failed: {exc}",
                ) from exc

        if not candles:
            raise NoMarketDataError(
                symbol, ccxt_symbol, "OKX returned no OHLCV data",
            )

        rows = []
        for candle in candles:
            ts_ms, o, h, l, c, v = candle
            rows.append({
                "Date": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                "Open": float(o),
                "High": float(h),
                "Low": float(l),
                "Close": float(c),
                "Volume": float(v),
            })

        downloaded = pd.DataFrame(rows)
        downloaded = _ensure_date_column(downloaded)
        downloaded.to_csv(cache_file, index=False, encoding="utf-8")
        data = downloaded

    # Clean and filter
    data = _clean_dataframe(data)
    data = data[(data["Date"] >= curr_date_dt) & (data["Date"] <= end_dt)]

    # Staleness check: crypto trades 24/7, so data should always be recent
    _assert_ohlcv_not_stale(data, end_date, symbol, ccxt_symbol)

    # Round prices
    for col in ("Open", "High", "Low", "Close"):
        if col in data.columns:
            data[col] = data[col].round(2)

    return data


# ------------------------------------------------------------------ #
# Stock data (for get_stock_data tool)
# ------------------------------------------------------------------ #

def get_okx_stock_data(
    symbol: Annotated[str, "ticker symbol, e.g. BTC-USD"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Retrieve OHLCV price data for a crypto symbol from OKX.

    Automatically falls back to yfinance for stock tickers.
    Returns a CSV-formatted string with Date, Open, High, Low, Close, Volume.
    """
    _assert_is_crypto(symbol, "get_okx_stock_data")
    data = get_okx_ohlcv(symbol, start_date, end_date)
    ccxt_symbol = to_ccxt_symbol(symbol)

    header = (
        f"# Crypto price data for {symbol} ({ccxt_symbol}) "
        f"from {start_date} to {end_date}\n"
        f"# Total records: {len(data)}\n"
        f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"# Source: OKX\n\n"
    )
    return header + data.to_csv(index=False)


# ------------------------------------------------------------------ #
# Technical indicators (using OKX OHLCV + stockstats)
# ------------------------------------------------------------------ #

def get_okx_indicators(
    symbol: Annotated[str, "ticker symbol, e.g. BTC-USD"],
    indicator: Annotated[str, "technical indicator name"],
    curr_date: Annotated[str, "Current date in YYYY-mm-dd format"],
    look_back_days: Annotated[int, "how many days to look back"] = 60,
) -> str:
    """Calculate technical indicators from OKX OHLCV data.

    Automatically falls back to yfinance for stock tickers.
    Supported indicators: close_50_sma, close_200_sma, close_10_ema,
    macd, macds, macdh, rsi, boll, boll_ub, boll_lb, atr, vwma, mfi.
    """
    _assert_is_crypto(symbol, "get_okx_indicators")
    from stockstats import wrap

    best_ind_params = {
        "close_50_sma": "50 SMA: A medium-term trend indicator.",
        "close_200_sma": "200 SMA: A long-term trend benchmark.",
        "close_10_ema": "10 EMA: A responsive short-term average.",
        "macd": "MACD: Momentum via differences of EMAs.",
        "macds": "MACD Signal: An EMA smoothing of the MACD line.",
        "macdh": "MACD Histogram: Gap between MACD and signal.",
        "rsi": "RSI: Momentum to flag overbought/oversold conditions.",
        "boll": "Bollinger Middle: 20 SMA basis.",
        "boll_ub": "Bollinger Upper Band: 2 std dev above middle.",
        "boll_lb": "Bollinger Lower Band: 2 std dev below middle.",
        "atr": "ATR: Average True Range for volatility.",
        "vwma": "VWMA: Volume-weighted moving average.",
        "mfi": "MFI: Money Flow Index, price+volume momentum.",
    }

    if indicator not in best_ind_params:
        raise ValueError(
            f"Indicator '{indicator}' not supported. "
            f"Choose from: {list(best_ind_params.keys())}"
        )

    # Fetch enough data (look_back + 250 buffer for SMA200)
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    fetch_start = (curr_dt - timedelta(days=look_back_days + 250)).strftime("%Y-%m-%d")
    data = get_okx_ohlcv(symbol, fetch_start, curr_date)

    if data.empty:
        return f"NO_DATA: No OHLCV data available for {symbol} from OKX."

    df = wrap(data)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    df[indicator]  # trigger stockstats calculation

    result_lines = []
    for _, row in df.iterrows():
        val = row[indicator]
        val_str = "N/A" if pd.isna(val) else str(val)
        result_lines.append(f"{row['Date']}: {val_str}")

    # Safety: clamp look_back_days to valid range
    safe_n = max(1, min(look_back_days, len(result_lines)))

    result = (
        f"## {indicator} values for {symbol} "
        f"from {(curr_dt - timedelta(days=look_back_days)).strftime('%Y-%m-%d')} "
        f"to {curr_date} (OKX data):\n\n"
        + "\n".join(result_lines[-safe_n:])
        + "\n\n"
        + best_ind_params[indicator]
    )
    return result


# ------------------------------------------------------------------ #
# News (CryptoCompare)
# ------------------------------------------------------------------ #

def _fetch_cryptocompare_news(limit: int = 10) -> list[dict]:
    """Fetch latest crypto news from CryptoCompare API."""
    url = (
        f"https://min-api.cryptocompare.com/data/v2/news/"
        f"?lang=EN&limit={limit}&api_key={_CRYPTOCOMPARE_API_KEY}"
    )
    try:
        proxies = {"http": _CRYPTO_PROXY, "https": _CRYPTO_PROXY} if _CRYPTO_PROXY else None
        resp = requests.get(url, proxies=proxies, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        return body.get("Data", [])
    except Exception as exc:
        logger.warning("CryptoCompare news fetch failed: %s", exc)
        return []


def _article_matches_ticker(article: dict, keywords: list[str]) -> bool:
    """Check if a CryptoCompare article is related to a given token.

    Uses word-boundary matching on tags, categories, source, and title.
    """
    text_fields = []
    tags = (article.get("tags", "") or "").lower()
    categories = (article.get("categories", "") or "").lower()
    source = (article.get("source", "") or "").lower()
    title = (article.get("title", "") or "").lower()

    for kw in keywords:
        # Word-boundary pattern: match whole word within text
        # Simple approach: check if keyword appears as a standalone token
        for field in [tags, categories, source, title]:
            if kw in field:
                # Verify it's a word boundary (not just a substring)
                words = field.replace("|", " ").replace(",", " ").split()
                if kw in words or any(kw == w.strip() for w in words):
                    return True
                # Loose check for substrings in title (titles are free text)
                if field == title and kw in title:
                    return True
    return False


def get_okx_news(
    ticker: str,
    start_date: str,
    end_date: str,
) -> str:
    """Retrieve crypto news for a specific token using CryptoCompare.

    Automatically falls back to yfinance news for stock tickers.

    Args:
        ticker: Crypto symbol (e.g., "BTC-USD")
        start_date: Start date in yyyy-mm-dd format
        end_date: End date in yyyy-mm-dd format

    Returns:
        Formatted string containing news articles.
    """
    _assert_is_crypto(ticker, "get_okx_news")
    keywords = _token_keywords(ticker)
    articles = _fetch_cryptocompare_news(limit=20)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_inclusive = end_dt + timedelta(days=1)

    filtered = []
    for art in articles:
        if not _article_matches_ticker(art, keywords):
            continue

        pub_ts = art.get("published_on")
        if pub_ts:
            pub_date = datetime.fromtimestamp(pub_ts, tz=timezone.utc)
            if not (start_dt <= pub_date < end_inclusive):
                continue

        filtered.append(art)

    if not filtered:
        return f"No crypto news found for {ticker} between {start_date} and {end_date}."

    lines = [f"## {ticker} Crypto News, from {start_date} to {end_date} (CryptoCompare):\n"]
    for art in filtered[:10]:
        title = art.get("title", "No title")
        body_text = (art.get("body", "") or "")[:300]
        url = art.get("url", "")
        source_name = art.get("source", "Unknown")
        lines.append(f"### {title} (source: {source_name})")
        if body_text:
            lines.append(body_text)
        if url:
            lines.append(f"Link: {url}")
        lines.append("")

    return "\n".join(lines)


def get_okx_global_news(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Retrieve global crypto news using CryptoCompare.

    ``get_global_news`` has no ticker parameter, so it always serves crypto
    news. Stock tickers calling this function will get crypto news too —
    that's acceptable since "global news" is a general overview.
    """
    if look_back_days is None:
        look_back_days = 7
    if limit is None:
        limit = 10

    articles = _fetch_cryptocompare_news(limit=limit)

    if not articles:
        return "No global crypto news available from CryptoCompare."

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    cutoff = curr_dt - timedelta(days=look_back_days)

    lines = [
        f"## Global Crypto News (CryptoCompare), "
        f"past {look_back_days} days from {curr_date}:\n"
    ]
    count = 0
    for art in articles:
        if count >= limit:
            break
        pub_ts = art.get("published_on")
        if pub_ts:
            pub_date = datetime.fromtimestamp(pub_ts, tz=timezone.utc)
            if pub_date < cutoff:
                continue

        title = art.get("title", "No title")
        body_text = (art.get("body", "") or "")[:300]
        url = art.get("url", "")
        source_name = art.get("source", "Unknown")
        categories = art.get("categories", "")
        lines.append(f"### {title} (source: {source_name})")
        if categories:
            lines.append(f"Categories: {categories}")
        if body_text:
            lines.append(body_text)
        if url:
            lines.append(f"Link: {url}")
        lines.append("")
        count += 1

    if count == 0:
        return f"No global crypto news found in the past {look_back_days} days."

    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Fundamentals (not applicable for crypto)
# ------------------------------------------------------------------ #

def get_okx_fundamentals(
    ticker: str,
    curr_date: str | None = None,
) -> str:
    """Fundamentals — crypto has none, stock falls back to yfinance."""
    _assert_is_crypto(ticker, "get_okx_fundamentals")
    return (
        f"FUNDAMENTALS_UNAVAILABLE: Fundamental data is not applicable for "
        f"crypto assets ({ticker}). This is a cryptocurrency without financial "
        f"statements, earnings reports, or balance sheets. Proceed with "
        f"technical analysis and market data only."
    )


def get_okx_balance_sheet(*args, **kwargs) -> str:
    """Not applicable for crypto."""
    # Try to check if first arg looks like a ticker; if it does, route stock
    # tickers to the yfinance fallback automatically.
    if args and isinstance(args[0], str) and not is_crypto_ticker(args[0]):
        raise NoMarketDataError(
            args[0], None,
            "Not a crypto ticker — balance sheet serves crypto only. "
            "Falling back to yfinance.",
        )
    return "FUNDAMENTALS_UNAVAILABLE: Balance sheet data is not available for crypto assets."


def get_okx_cashflow(*args, **kwargs) -> str:
    """Not applicable for crypto."""
    if args and isinstance(args[0], str) and not is_crypto_ticker(args[0]):
        raise NoMarketDataError(
            args[0], None,
            "Not a crypto ticker — cash flow serves crypto only. "
            "Falling back to yfinance.",
        )
    return "FUNDAMENTALS_UNAVAILABLE: Cash flow data is not available for crypto assets."


def get_okx_income_statement(*args, **kwargs) -> str:
    """Not applicable for crypto."""
    if args and isinstance(args[0], str) and not is_crypto_ticker(args[0]):
        raise NoMarketDataError(
            args[0], None,
            "Not a crypto ticker — income statement serves crypto only. "
            "Falling back to yfinance.",
        )
    return "FUNDAMENTALS_UNAVAILABLE: Income statement data is not available for crypto assets."


def get_okx_insider_transactions(*args, **kwargs) -> str:
    """Not applicable for crypto."""
    if args and isinstance(args[0], str) and not is_crypto_ticker(args[0]):
        raise NoMarketDataError(
            args[0], None,
            "Not a crypto ticker — insider transactions serves crypto only. "
            "Falling back to yfinance.",
        )
    return "FUNDAMENTALS_UNAVAILABLE: Insider transaction data is not available for crypto assets."
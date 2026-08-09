"""Guard the cache-friendly prompt layout: system messages must be static.

DeepSeek context caching is prefix-based — the system prompt must be
byte-identical across calls for the cache to hit. This regression suite
guards the layout we standardized on (commit 1447946):

- system message: 100% static (role + rules), no dynamic interpolation
- dynamic data (dates, tickers, news, reports, debate history): user message

Two classes of bugs are caught here that functional tests miss:

1. **Dynamic interpolation in a system prompt** (e.g. ``f\"...{asset_label}...\"``
   or ``f\"...{current_date}...\"``) — breaks the cached prefix whenever the
   value changes (ticker, cycle, asset type).
2. **Literal ``{get_language_instruction()}`` leftovers** — when a prompt is
   converted from f-string to plain string, the call expression stops being
   evaluated and the literal text is sent to the model (i18n regression).

The checks are source-level (inspect) so they run fast and need no LLM.
"""
from __future__ import annotations

import inspect
import re

import pytest

import tradingagents.agents.analysts.fundamentals_analyst as fundamentals_analyst
import tradingagents.agents.analysts.market_analyst as market_analyst
import tradingagents.agents.analysts.news_analyst as news_analyst
import tradingagents.agents.analysts.sentiment_analyst as sentiment_analyst
import tradingagents.agents.managers.portfolio_manager as portfolio_manager
import tradingagents.agents.managers.research_manager as research_manager
import tradingagents.agents.researchers.bear_researcher as bear_researcher
import tradingagents.agents.researchers.bull_researcher as bull_researcher
import tradingagents.agents.risk_mgmt.aggressive_debator as aggressive_debator
import tradingagents.agents.risk_mgmt.conservative_debator as conservative_debator
import tradingagents.agents.risk_mgmt.neutral_debator as neutral_debator
import tradingagents.agents.trader.trader as trader

# Modules whose agent prompts must keep a static system message.
_CACHE_FRIENDLY_MODULES = [
    market_analyst,
    news_analyst,
    fundamentals_analyst,
    sentiment_analyst,
    bull_researcher,
    bear_researcher,
    aggressive_debator,
    conservative_debator,
    neutral_debator,
    research_manager,
    portfolio_manager,
    trader,
]

# Dynamic values that must never be interpolated into a system prompt.
# Each pattern is a fragment that would appear in an f-string system
# template if someone regressed the layout.
_FORBIDDEN_IN_SYSTEM = [
    r"\{ticker\}",
    r"\{current_date\}",
    r"\{trade_date\}",
    r"\{start_date\}",
    r"\{end_date\}",
    r"\{asset_label\}",
    r"\{company_of_interest\}",
    r"\{news_block\}",
    r"\{history\}",
    r"\{instrument_context\}",
    r"\{data_block\}",
    r"\{trader_decision\}",
    r"\{market_research_report\}",
    r"\{sentiment_report\}",
    r"\{news_report\}",
    r"\{fundamentals_report\}",
    r"\{investment_debate_state",
    r"\{research_plan\}",
    r"\{trader_plan\}",
    r"\{lessons_line\}",
    r"\{past_context\}",
]

# Literal (un-evaluated) function-call leftovers from f-string → plain-string
# conversions. These render as literal text sent to the model.
_LITERAL_LEFT_OVERS = [
    "{get_language_instruction()}",
    "{get_instrument_context_from_state(state)}",
    "{NO_EXTERNAL_TOOLS}",
]


def _system_templates(src: str) -> list[str]:
    """Extract f-string/plain-string literals assigned to system-prompt vars."""
    # Look for `system_message = (...)` / `system_prompt = f"..."` blocks and
    # any `"system", "..."` template entries inside from_messages(...).
    chunks: list[str] = []
    for pattern in (
        r"system_message\s*=\s*(?:\(|f?\"\"\"|f?\"|f?')(.*?)(?:\"\"\"|\"|'|\))",
        r"system_prompt\s*=\s*(?:\(|f?\"\"\"|f?\"|f?')(.*?)(?:\"\"\"|\"|'|\))",
        r'\("system",\s*(.*?)\)',
    ):
        for m in re.finditer(pattern, src, re.DOTALL):
            chunks.append(m.group(1))
    return chunks


def _plain_strings(src: str) -> list[str]:
    """Return string literals in the source that are NOT f-strings.

    Only plain (non-f) strings can carry a literal ``{expr}`` leftover from an
    f-string → plain-string conversion. Inside a real f-string the braces are
    evaluated, so they must not be flagged.
    """
    import ast

    tree = ast.parse(src)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            # f-string: collect the literal text parts only (FormattedValue
            # children are evaluated expressions, not literal leftovers).
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    out.append(value.value)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


def _system_strings(src: str) -> list[str]:
    """Return string literal contents that are part of a system-prompt
    construction, covering all idioms used in the codebase:

    - ``system_message = "..."`` / ``system_prompt = "..."`` assignments
    - ``("system", "...")`` tuples inside ``from_messages`` templates
    - ``{"role": "system", "content": "..."}`` dict entries

    Only these strings are the system prompt; checking the whole source file
    would flag legitimate dynamic placeholders in user messages.
    """
    import ast

    tree = ast.parse(src)
    out: list[str] = []
    for node in ast.walk(tree):
        # Direct assignment: system_message = "..." / system_prompt = "..."
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in ("system_message", "system_prompt")
            for t in node.targets
        ):
            out.extend(_string_parts(node.value))
        # Tuple inside from_messages: ("system", "...")
        if isinstance(node, ast.Tuple) and len(node.elts) >= 2:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and first.value == "system":
                out.extend(_string_parts(node.elts[1]))
        # Dict entry: {"role": "system", "content": "..."}
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if isinstance(key, ast.Constant) and key.value == "role":
                    # Look ahead: this dict may be the messages entry; grab
                    # the content sibling directly.
                    continue
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "role"
                    and isinstance(value, ast.Constant)
                    and value.value == "system"
                ):
                    for k2, v2 in zip(node.keys, node.values, strict=False):
                        if isinstance(k2, ast.Constant) and k2.value == "content":
                            out.extend(_string_parts(v2))
    return out


def _string_parts(node) -> list[str]:
    """Extract literal text from a string/f-string/concatenation node."""
    import ast

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                # A formatted value in the system prompt is exactly what we
                # guard against — record its *name* (variable or called
                # function) so the caller can allow config-level exceptions.
                expr = value.value
                if isinstance(expr, ast.Name):
                    parts.append(f"{{FORMATTED:{expr.id}}}")
                elif isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
                    parts.append(f"{{FORMATTED:{expr.func.id}}}")
                elif isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
                    parts.append(f"{{FORMATTED:{expr.func.attr}}}")
                else:
                    parts.append(f"{{FORMATTED:?}}")
        return ["".join(parts)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _string_parts(node.left) + _string_parts(node.right)
    return []


@pytest.mark.unit
@pytest.mark.parametrize("mod", _CACHE_FRIENDLY_MODULES, ids=lambda m: m.__name__.split(".")[-1])
def test_system_prompt_has_no_dynamic_interpolation(mod):
    """System prompts must not contain dynamic value placeholders.

    Scoped to the actual system-message construction (not the whole source
    file): a ``{instrument_context}`` placeholder in a *user* message is
    legitimate; the same placeholder in the *system* message is a caching
    regression.
    """
    src = inspect.getsource(mod)
    system_texts = _system_strings(src)
    assert system_texts, f"{mod.__name__}: could not locate any system-message construction"
    # Config-level static expressions that are allowed inside the system
    # prompt: they never change between cycles/tickers within a run
    # (NO_EXTERNAL_TOOLS is a module constant; get_language_instruction()
    # reads config which is fixed for the process lifetime). They do not
    # disturb DeepSeek prefix caching.
    ALLOWED_FORMATTED = ("NO_EXTERNAL_TOOLS", "get_language_instruction")
    for text in system_texts:
        for formatted in re.findall(r"\{FORMATTED:(\w+)\}", text):
            assert formatted in ALLOWED_FORMATTED, (
                f"{mod.__name__}: system prompt f-string formats {formatted} — "
                f"dynamic interpolation breaks DeepSeek prefix caching"
            )
        for fragment in _FORBIDDEN_IN_SYSTEM:
            assert re.search(fragment, text) is None, (
                f"{mod.__name__}: dynamic placeholder {fragment} in system "
                f"prompt — breaks DeepSeek prefix caching"
            )


def _plain_strings(src: str) -> list[str]:
    """Return string literals in the source that are NOT f-strings.

    Only plain (non-f) strings can carry a literal ``{expr}`` leftover from an
    f-string → plain-string conversion. Inside a real f-string the braces are
    evaluated, so they must not be flagged.
    """
    import ast

    tree = ast.parse(src)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            # f-string: collect the literal text parts only (FormattedValue
            # children are evaluated expressions, not literal leftovers).
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    out.append(value.value)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


@pytest.mark.unit
@pytest.mark.parametrize("mod", _CACHE_FRIENDLY_MODULES, ids=lambda m: m.__name__.split(".")[-1])
def test_no_literal_function_call_leftovers(mod):
    """f-string → plain-string conversions must not leave literal calls."""
    src = inspect.getsource(mod)
    for literal in _LITERAL_LEFT_OVERS:
        for text in _plain_strings(src):
            assert literal not in text, (
                f"{mod.__name__}: literal {literal} found in a plain string — "
                f"the expression is no longer evaluated and would be sent "
                f"verbatim to the model"
            )


@pytest.mark.unit
def test_language_instruction_is_evaluated_in_sentiment_system():
    """The sentiment system message must call get_language_instruction() (not
    contain its literal form) so non-English runs receive the instruction."""
    src = inspect.getsource(sentiment_analyst)
    # The call must appear as a real call expression...
    assert re.search(r"get_language_instruction\(\)", src) is not None
    # ...and never as a literal placeholder inside the string.
    assert "{get_language_instruction()}" not in src


@pytest.mark.unit
def test_news_analyst_system_has_no_asset_label():
    """news_analyst previously interpolated {asset_label} (stock→company /
    crypto→asset) into its system prompt, breaking the shared prefix across
    asset types. The system must use neutral wording."""
    src = inspect.getsource(news_analyst)
    assert "{asset_label}" not in src
    # The dynamic label may still exist as a local var, but it must not be
    # used inside the system-message construction.
    assert re.search(r"system_message\s*=.*asset_label", src, re.DOTALL) is None

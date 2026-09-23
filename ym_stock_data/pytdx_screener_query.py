"""Pure compiler for the deliberately small zero-auth PyTDX screen grammar."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

from .a_share_codes import is_supported_a_share_code


_UNIVERSE = re.compile(
    r"沪深A股|沪市A股|上交所A股|深市A股|深交所A股"
)
_NON_ST = re.compile(r"非\s*ST")
_NON_SUSPENDED = re.compile(r"非停牌")
_CODE = re.compile(r"股票代码\s*(?:为|是|=)\s*(?P<code>\d{6})")
_PRICE_NUMBER = r"(?:0|[1-9]\d{0,5})(?:\.\d{1,4})?"
_PCT_NUMBER = rf"[+-]?{_PRICE_NUMBER}"
_OPERATOR = (
    r"大于等于|不低于|小于等于|不高于|大于|高于|超过|小于|低于|"
    r">=|<=|≥|≤|>|<"
)
_PRICE_RANGE = re.compile(
    rf"最新价\s*(?P<low>{_PRICE_NUMBER})\s*(?:元\s*)?(?:到|至|~)\s*"
    rf"(?P<high>{_PRICE_NUMBER})\s*(?:元)?"
)
_PRICE_COMPARE = re.compile(
    rf"最新价\s*(?P<operator>{_OPERATOR})\s*(?P<value>{_PRICE_NUMBER})\s*(?:元)?"
)
_PCT_RANGE = re.compile(
    rf"涨幅\s*(?P<low>{_PCT_NUMBER})\s*%\s*(?:到|至|~)\s*"
    rf"(?P<high>{_PCT_NUMBER})\s*%"
)
_PCT_COMPARE = re.compile(
    rf"涨幅\s*(?P<operator>{_OPERATOR})\s*(?P<value>{_PCT_NUMBER})\s*%"
)
_SEPARATOR = re.compile(r"(?:\s|,|、|;|且|并且)*\Z")
_OPERATOR_MAP = {
    "大于等于": ">=",
    "不低于": ">=",
    "≥": ">=",
    "大于": ">",
    "高于": ">",
    "超过": ">",
    "小于等于": "<=",
    "不高于": "<=",
    "≤": "<=",
    "小于": "<",
    "低于": "<",
}


@dataclass(frozen=True)
class NumericPredicate:
    field: str
    operator: str
    first: float
    second: float | None = None

    def matches(self, value: float) -> bool:
        if not math.isfinite(value):
            return False
        if self.operator == "range":
            return self.first <= value <= float(self.second)
        if self.operator == ">=":
            return value >= self.first
        if self.operator == "<=":
            return value <= self.first
        if self.operator == ">":
            return value > self.first
        if self.operator == "<":
            return value < self.first
        return False


@dataclass(frozen=True)
class CompiledPytdxScreenerQuery:
    markets: tuple[int, ...]
    code: str | None
    exclude_st: bool
    exclude_suspended: bool
    numeric_predicates: tuple[NumericPredicate, ...]

    def matches(self, *, price: float, pct_change: float) -> bool:
        values = {"price": price, "pct_change": pct_change}
        return all(item.matches(values[item.field]) for item in self.numeric_predicates)


def _normalized(query: object) -> str | None:
    if not isinstance(query, str):
        return None
    value = unicodedata.normalize("NFKC", query)
    value = "".join(
        character.upper() if character.isascii() else character
        for character in value
    )
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def _mark(occupied: list[bool], match: re.Match[str]) -> bool:
    start, end = match.span()
    if any(occupied[start:end]):
        return False
    occupied[start:end] = [True] * (end - start)
    return True


def _float(value: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _predicate(
    *, field: str, match: re.Match[str], is_range: bool
) -> NumericPredicate | None:
    first = _float(match.group("low" if is_range else "value"))
    second = _float(match.group("high")) if is_range else None
    if first is None or (is_range and (second is None or first > second)):
        return None
    if field == "price" and (first < 0 or (second is not None and second < 0)):
        return None
    operator = "range" if is_range else match.group("operator")
    return NumericPredicate(
        field=field,
        operator=_OPERATOR_MAP.get(operator, operator),
        first=first,
        second=second,
    )


def _market_tuple(universe: str) -> tuple[int, ...]:
    if universe == "沪深A股":
        return (0, 1)
    if universe in {"沪市A股", "上交所A股"}:
        return (1,)
    return (0,)


def _code_market(code: str) -> int | None:
    if is_supported_a_share_code(code, "SH"):
        return 1
    if is_supported_a_share_code(code, "SZ"):
        return 0
    return None


def compile_pytdx_screener_query(
    query: object,
) -> CompiledPytdxScreenerQuery | None:
    """Compile only a fully consumed, AND-only沪深 structured query."""

    value = _normalized(query)
    if value is None:
        return None
    occupied = [False] * len(value)
    universes = list(_UNIVERSE.finditer(value))
    if len(universes) != 1 or not _mark(occupied, universes[0]):
        return None
    markets = _market_tuple(universes[0].group(0))

    singleton_patterns = (
        ("exclude_st", _NON_ST),
        ("exclude_suspended", _NON_SUSPENDED),
        ("code", _CODE),
    )
    values: dict[str, object] = {
        "exclude_st": False,
        "exclude_suspended": False,
        "code": None,
    }
    filter_count = 0
    for key, pattern in singleton_patterns:
        matches = list(pattern.finditer(value))
        if len(matches) > 1:
            return None
        if matches:
            if not _mark(occupied, matches[0]):
                return None
            filter_count += 1
            values[key] = (
                matches[0].group("code") if key == "code" else True
            )

    predicates: list[NumericPredicate] = []
    numeric_patterns = (
        ("price", _PRICE_RANGE, True),
        ("price", _PRICE_COMPARE, False),
        ("pct_change", _PCT_RANGE, True),
        ("pct_change", _PCT_COMPARE, False),
    )
    for field, pattern, is_range in numeric_patterns:
        for match in pattern.finditer(value):
            if any(occupied[match.start() : match.end()]):
                continue
            item = _predicate(field=field, match=match, is_range=is_range)
            if item is None or not _mark(occupied, match):
                return None
            predicates.append(item)
            filter_count += 1

    if any(
        sum(item.field == field for item in predicates) > 1
        for field in ("price", "pct_change")
    ):
        return None

    if filter_count == 0:
        return None
    if predicates and not values["exclude_suspended"]:
        return None
    code = values["code"]
    if isinstance(code, str):
        market = _code_market(code)
        if market is None or market not in markets:
            return None

    residual = "".join(
        character if not occupied[index] else " "
        for index, character in enumerate(value)
    )
    if not _SEPARATOR.fullmatch(residual):
        return None
    return CompiledPytdxScreenerQuery(
        markets=markets,
        code=code if isinstance(code, str) else None,
        exclude_st=bool(values["exclude_st"]),
        exclude_suspended=bool(values["exclude_suspended"]),
        numeric_predicates=tuple(predicates),
    )


def is_pytdx_screener_compatible(params: object) -> bool:
    """Return whether request metadata and query are within the fifth-source scope."""

    if not isinstance(params, dict):
        return False
    if params.get("date") is not None or params.get("version") is not None:
        return False
    if params.get("lang") == "English":
        return False
    if params.get("expected_row_shape") not in {None, "stock_rows"}:
        return False
    return compile_pytdx_screener_query(params.get("query")) is not None

from __future__ import annotations

import ast
import math
import re
from collections import Counter
from typing import Any


SCHEMA_VERSION = "factor_region_semantics_v1"


FIELD_ALIASES = {
    "cap": "total_mv",
    "market_cap": "total_mv",
    "margin_balance": "borrow_money_bal",
    "margin_buy_amount": "purch_borrow_money",
    "short_balance": "sec_lending_bal",
}


FIELD_MEANINGS = {
    "open": "开盘价格",
    "high": "最高价格",
    "low": "最低价格",
    "close": "收盘价格",
    "pre_close": "前收盘价格",
    "pct_change": "收益变化",
    "returns": "收益变化",
    "volume": "成交量",
    "amount": "成交金额",
    "turnover_rate": "换手活跃度",
    "amp": "日内振幅",
    "vwap": "成交均价",
    "total_mv": "总市值",
    "float_mv": "流通市值",
    "tot_share": "总股本",
    "free_share": "自由流通股本",
    "float_a_share": "流通A股本",
    "holder_num": "股东户数",
    "net_mf_amount": "主力资金净流入",
    "net_mf_vol": "主力资金净流量",
    "lg_net_amount": "大单资金净流入",
    "lg_net_vol": "大单资金净流量",
    "sm_net_amount": "小单资金净流入",
    "sm_net_vol": "小单资金净流量",
    "borrow_money_bal": "融资余额",
    "purch_borrow_money": "融资买入",
    "sec_lending_bal": "融券余额",
    "margin_trade_bal": "融资融券余额",
    "pe": "市盈率估值",
    "pb": "市净率估值",
    "ps_ttm": "市销率估值",
    "dv_ttm": "股息率",
    "roe": "净资产收益率",
    "roa": "资产收益率",
    "eps": "每股收益",
    "net_profit": "净利润",
    "net_asset_ps": "每股净资产",
    "tot_equity": "股东权益",
    "total_assets": "总资产",
    "cost_15pct": "低位筹码成本",
    "cost_85pct": "高位筹码成本",
}


FUNCTION_ALIASES = {
    "delta": "ts_delta",
    "delay": "ts_shift",
    "correlation": "ts_corr",
    "covariance": "ts_cov",
    "stddev": "ts_std",
    "ts_std_dev": "ts_std",
    "ts_delay": "ts_shift",
    "ts_covariance": "ts_cov",
    "ts_arg_max": "ts_argmax",
    "ts_arg_min": "ts_argmin",
    "av_diff": "ts_av_diff",
}


MONOTONIC_WRAPPERS = {
    "rank",
    "zscore",
    "scale",
    "group_rank",
    "group_zscore",
    "tanh",
    "sigmoid",
}


WINDOW_FUNCTIONS = {
    "ts_mean",
    "ts_std",
    "ts_max",
    "ts_min",
    "ts_sum",
    "ts_shift",
    "ts_delta",
    "ts_rank",
    "ts_argmax",
    "ts_argmin",
    "ts_corr",
    "ts_cov",
    "decay_linear",
    "product",
    "ts_av_diff",
    "ts_zscore",
    "ema",
    "sma",
    "wma",
    "rsi",
    "macd",
    "obv",
    "boll_upper",
    "boll_lower",
    "boll_mid",
}


def _field_name(name: str) -> str:
    raw = str(name or "").strip().lower()
    return FIELD_ALIASES.get(raw, raw)


def _function_name(name: str) -> str:
    raw = str(name or "").strip()
    return FUNCTION_ALIASES.get(raw, FUNCTION_ALIASES.get(raw.lower(), raw.lower()))


def _meaning(field: str) -> str:
    field = _field_name(field)
    return FIELD_MEANINGS.get(field, field)


def _number(node: ast.AST) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _number(node.operand)
        return -value if value is not None else None
    return None


def _window_bucket(value: float | None) -> str | None:
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    if value <= 10:
        return "短期"
    if value <= 30:
        return "中期"
    return "长期"


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return _function_name(node.func.id)
    return ""


def _node_fields(node: ast.AST) -> set[str]:
    fields: set[str] = set()
    for item in ast.walk(node):
        if not isinstance(item, ast.Name):
            continue
        parent_is_call_name = any(
            isinstance(parent, ast.Call) and parent.func is item
            for parent in ast.walk(node)
        )
        if parent_is_call_name:
            continue
        fields.add(_field_name(item.id))
    return fields


def _canonical(node: ast.AST) -> str:
    if isinstance(node, ast.Expression):
        return _canonical(node.body)
    if isinstance(node, ast.Name):
        return f"field:{_field_name(node.id)}"
    if isinstance(node, ast.Constant):
        return "#"
    if isinstance(node, ast.UnaryOp):
        op = "neg" if isinstance(node.op, ast.USub) else "pos"
        return f"{op}({_canonical(node.operand)})"
    if isinstance(node, ast.BinOp):
        op = {
            ast.Add: "add",
            ast.Sub: "sub",
            ast.Mult: "mul",
            ast.Div: "div",
            ast.Pow: "pow",
            ast.BitXor: "pow",
        }.get(type(node.op), type(node.op).__name__.lower())
        if op == "mul" and _number(node.left) is not None:
            return _canonical(node.right)
        if op == "mul" and _number(node.right) is not None:
            return _canonical(node.left)
        if op == "div" and _number(node.right) is not None:
            return _canonical(node.left)
        children = [_canonical(node.left), _canonical(node.right)]
        if op in {"add", "mul"}:
            children.sort()
        return f"{op}({','.join(children)})"
    if isinstance(node, ast.BoolOp):
        op = "and" if isinstance(node.op, ast.And) else "or"
        children = sorted(_canonical(item) for item in node.values)
        return f"{op}({','.join(children)})"
    if isinstance(node, ast.Compare):
        ops = ",".join(type(item).__name__.lower() for item in node.ops)
        return f"compare:{ops}({_canonical(node.left)},{','.join(_canonical(item) for item in node.comparators)})"
    if isinstance(node, ast.IfExp):
        return f"where({_canonical(node.test)},{_canonical(node.body)},{_canonical(node.orelse)})"
    if isinstance(node, ast.Call):
        name = _call_name(node)
        args = list(node.args)
        if name in MONOTONIC_WRAPPERS and args:
            return _canonical(args[0])
        kept: list[ast.AST] = []
        for idx, arg in enumerate(args):
            if name in WINDOW_FUNCTIONS and idx == len(args) - 1 and _number(arg) is not None:
                continue
            kept.append(arg)
        return f"{name}({','.join(_canonical(item) for item in kept)})"
    return type(node).__name__.lower()


def _short(text: str, limit: int = 48) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip(" ，、")
    return normalized if len(normalized) <= limit else normalized[: max(1, limit - 1)] + "…"


def _is_intraday_price_location(node: ast.AST) -> bool:
    fields = _node_fields(node)
    return fields == {"close", "high", "low"} and any(
        isinstance(item, ast.BinOp) and isinstance(item.op, ast.Div)
        for item in ast.walk(node)
    )


def _describe(node: ast.AST, *, negative: bool = False) -> str:
    if isinstance(node, ast.Expression):
        return _describe(node.body, negative=negative)
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return _describe(node.operand, negative=not negative)
        return _describe(node.operand, negative=negative)
    if isinstance(node, ast.Name):
        base = _meaning(node.id)
        return _short(f"低{base}" if negative else base)
    if isinstance(node, ast.Compare):
        left = _describe(node.left)
        right = _describe(node.comparators[0]) if node.comparators else "阈值"
        operator = node.ops[0] if node.ops else None
        relation = {
            ast.Lt: "低于",
            ast.LtE: "不高于",
            ast.Gt: "高于",
            ast.GtE: "不低于",
            ast.Eq: "等于",
            ast.NotEq: "不等于",
        }.get(type(operator), "相对")
        return _short(f"{left}{relation}{right}")
    if isinstance(node, ast.Call):
        name = _call_name(node)
        args = list(node.args)
        if name in MONOTONIC_WRAPPERS and args:
            return _describe(args[0], negative=negative)
        first = _describe(args[0]) if args else "信号"
        second = _describe(args[1]) if len(args) > 1 else ""
        if name in {"ts_mean", "ts_sum", "ema", "sma", "wma", "decay_linear", "product"}:
            return _short(f"{first}{'低' if negative else ''}持续性")
        if name == "ts_delta":
            return _short(f"{first}{'下降' if negative else '变化'}")
        if name in {"ts_std", "atr"}:
            return _short(f"{'低' if negative else ''}{first}波动")
        if name in {"ts_corr", "ts_cov"}:
            relation = "反向联动" if negative else "联动"
            return _short(f"{first}与{second}{relation}")
        if name in {"ts_zscore", "ts_av_diff"}:
            return _short(f"{first}{'反向' if negative else ''}历史偏离")
        if name == "ts_rank":
            return _short(f"{first}{'低' if negative else ''}历史位置")
        if name in {"ts_max", "ts_argmax"}:
            return _short(f"{first}{'低' if negative else ''}高位特征")
        if name in {"ts_min", "ts_argmin"}:
            return _short(f"{first}{'低' if negative else ''}低位特征")
        if name in {"where", "trade_when"}:
            condition = _describe(args[0]) if args else "条件"
            positive = _describe(args[1]) if len(args) > 1 else "主信号"
            negative_branch = _describe(args[2]) if len(args) > 2 else ""
            branch = f"{positive}，否则{negative_branch}" if negative_branch else positive
            return _short(f"{condition}时使用{branch}")
        if name == "abs":
            return _short(f"{first}绝对强度")
        if name in {"max", "min"} and len(args) >= 2:
            return _short(f"{first}的{'下限' if name == 'max' else '上限'}约束")
        prefix = "反向" if negative else ""
        return _short(f"{prefix}{first}{name}")
    if isinstance(node, ast.BinOp):
        if _is_intraday_price_location(node):
            return "收盘价在日内高低区间的位置"
        left_number = _number(node.left)
        right_number = _number(node.right)
        if isinstance(node.op, ast.Mult) and left_number is not None:
            return _describe(node.right, negative=negative ^ (left_number < 0))
        if isinstance(node.op, ast.Mult) and right_number is not None:
            return _describe(node.left, negative=negative ^ (right_number < 0))
        if isinstance(node.op, ast.Div) and right_number is not None:
            return _describe(node.left, negative=negative ^ (right_number < 0))
        left = _describe(node.left)
        right = _describe(node.right)
        if isinstance(node.op, ast.Mult):
            return _short(f"{left} × {right}")
        if isinstance(node.op, ast.Add):
            return _short(f"{left} + {right}")
        if isinstance(node.op, ast.Sub):
            return _short(f"{left}减去{right}")
        if isinstance(node.op, ast.Div):
            left_fields = _node_fields(node.left)
            right_fields = _node_fields(node.right)
            if left_fields and left_fields <= right_fields:
                return _short(f"{left}相对自身历史水平")
            return _short(f"{left}相对{right}")
        return _short(f"{left}与{right}复合")
    if isinstance(node, ast.IfExp):
        return _short(f"条件触发的{_describe(node.body)}")
    return "复合信息关系"


def _top_legs(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.Expression):
        return _top_legs(node.body)
    if isinstance(node, ast.Call) and _call_name(node) in MONOTONIC_WRAPPERS and node.args:
        return _top_legs(node.args[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        if _number(node.left) is not None:
            return _top_legs(node.right)
        if _number(node.right) is not None:
            return _top_legs(node.left)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add)):
        return [*_top_legs(node.left), *_top_legs(node.right)]
    return [node]


def _combination_form(node: ast.AST) -> str:
    if isinstance(node, ast.Expression):
        return _combination_form(node.body)
    if isinstance(node, ast.Call) and _call_name(node) in MONOTONIC_WRAPPERS and node.args:
        return _combination_form(node.args[0])
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Mult):
            return "乘法联合确认"
        if isinstance(node.op, ast.Add):
            return "加性复合"
        if isinstance(node.op, ast.Sub):
            return "主信号减去惩罚项"
        if isinstance(node.op, ast.Div):
            return "相对强度或归一化"
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name in {"where", "trade_when"}:
            return "条件触发"
        if name in {"ts_corr", "ts_cov"}:
            return "字段联动"
    return "单一信息腿"


def analyze_expression(expression: str) -> dict[str, Any]:
    text = str(expression or "").strip()
    if not text:
        return {"available": False, "reason": "empty_expression"}
    try:
        tree = ast.parse(text, mode="eval")
    except Exception as exc:
        return {
            "available": False,
            "reason": "semantic_parse_error",
            "error": str(exc)[:160],
        }
    fields = sorted(_node_fields(tree))
    legs = [_short(_describe(item), 56) for item in _top_legs(tree)]
    legs = list(dict.fromkeys(item for item in legs if item and item != "复合信息关系"))
    if not legs:
        legs = [_short(_describe(tree), 56)]
    windows: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in WINDOW_FUNCTIONS or not node.args:
            continue
        bucket = _window_bucket(_number(node.args[-1]))
        if bucket:
            windows.append(bucket)
    return {
        "available": True,
        "schema_version": SCHEMA_VERSION,
        "fields": fields,
        "field_meanings": {field: _meaning(field) for field in fields},
        "legs": legs,
        "combination_form": _combination_form(tree),
        "canonical_signature": _canonical(tree),
        "window_buckets": sorted(set(windows)),
        "summary": _short(_describe(tree), 72),
    }


def _member_expressions(region: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    members = list(region.get("members") or [])
    representative = region.get("representative")
    if isinstance(representative, dict):
        members.append(representative)
    for idx, member in enumerate(members):
        if not isinstance(member, dict):
            continue
        factor_id = str(member.get("factor_id") or f"member_{idx}")
        expression = str(member.get("expression") or "").strip()
        if not expression or factor_id in seen:
            continue
        seen.add(factor_id)
        rows.append((factor_id, expression))
    return rows


def build_region_profile(region: dict[str, Any]) -> dict[str, Any]:
    analyses: list[dict[str, Any]] = []
    for factor_id, expression in _member_expressions(region):
        analysis = analyze_expression(expression)
        if analysis.get("available"):
            analyses.append({**analysis, "factor_id": factor_id, "expression": expression})
    analyses.sort(key=lambda item: (str(item.get("factor_id") or ""), str(item.get("expression") or "")))
    member_count = int(region.get("size") or len(analyses) or 0)
    if not analyses:
        return {
            "schema_version": SCHEMA_VERSION,
            "region_uid": region.get("region_uid"),
            "name": "未解析信息区域",
            "core_fields": [],
            "core_structures": [],
            "combination_form": "未知",
            "active_factor_count": member_count,
            "semantic_status": "unavailable",
        }

    field_counts: Counter[str] = Counter()
    leg_counts: Counter[str] = Counter()
    combination_counts: Counter[str] = Counter()
    field_usage: dict[str, Counter[str]] = {}
    for analysis in analyses:
        unique_fields = set(analysis.get("fields") or [])
        field_counts.update(sorted(unique_fields))
        unique_legs = set(analysis.get("legs") or [])
        leg_counts.update(sorted(unique_legs))
        combination_counts.update([str(analysis.get("combination_form") or "")])
        for field in sorted(unique_fields):
            usage = field_usage.setdefault(field, Counter())
            for leg in sorted(unique_legs):
                if _meaning(field) in leg or field in leg:
                    usage[leg] += 1

    n = len(analyses)
    if n == 1:
        core_field_names = list(analyses[0].get("fields") or [])
        core_legs = list(analyses[0].get("legs") or [])[:2]
        semantic_status = "single"
    elif n == 2:
        shared_fields = [field for field, count in field_counts.items() if count == 2]
        core_field_names = shared_fields or [field for field, _ in field_counts.most_common(3)]
        shared_legs = [leg for leg, count in leg_counts.items() if count == 2]
        core_legs = shared_legs or [leg for leg, _ in leg_counts.most_common(2)]
        semantic_status = "coherent" if shared_fields or shared_legs else "mixed"
    else:
        core_field_names = [
            field
            for field, count in field_counts.most_common()
            if count / n >= 0.60
        ]
        core_legs = [
            leg
            for leg, count in leg_counts.most_common()
            if count / n >= 0.50
        ][:2]
        semantic_status = "coherent" if core_field_names and core_legs else "mixed"
        if not core_field_names:
            core_field_names = [field for field, _ in field_counts.most_common(3)]
        if not core_legs:
            core_legs = [leg for leg, _ in leg_counts.most_common(2)]

    if semantic_status == "mixed":
        name = "混合区域：" + " / ".join(core_legs[:2])
    else:
        name = " × ".join(core_legs[:2]) if len(core_legs) > 1 else (core_legs[0] if core_legs else analyses[0]["summary"])
    combination_form = combination_counts.most_common(1)[0][0] or "单一信息腿"
    core_fields = []
    for field in core_field_names[:4]:
        usage_counts = field_usage.get(field) or Counter()
        usage = usage_counts.most_common(1)[0][0] if usage_counts else _meaning(field)
        if len(usage) > 36:
            usage = f"{_meaning(field)}用于{combination_form}"
        core_fields.append(
            {
                "field": field,
                "meaning": _meaning(field),
                "usage": usage,
            }
        )
    representative = region.get("representative") if isinstance(region.get("representative"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "region_uid": region.get("region_uid"),
        "name": _short(name, 64),
        "core_fields": core_fields,
        "core_structures": core_legs[:2],
        "combination_form": combination_form,
        "active_factor_count": member_count,
        "semantic_status": semantic_status,
        "representative_factor_id": representative.get("factor_id"),
        "representative_expression": representative.get("expression"),
        "parsed_factor_count": n,
    }


def semantic_signature(expression: str) -> str:
    analysis = analyze_expression(expression)
    return str(analysis.get("canonical_signature") or "")

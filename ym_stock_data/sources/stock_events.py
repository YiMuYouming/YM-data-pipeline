"""东方财富低频股票事件旁路。"""

from __future__ import annotations

from .eastmoney_http import query_datacenter as eastmoney_datacenter


EVENTS = {
    "lockup": {
        "report": "RPT_LIFT_STAGE",
        "code_field": "SECURITY_CODE",
        "sort": "FREE_DATE",
    },
    "margin": {
        "report": "RPTA_WEB_RZRQ_GGMX",
        "code_field": "SCODE",
        "sort": "DATE",
    },
    "block_trade": {
        "report": "RPT_DATA_BLOCKTRADE",
        "code_field": "SECURITY_CODE",
        "sort": "TRADE_DATE",
    },
    "holder_num": {
        "report": "RPT_HOLDERNUMLATEST",
        "code_field": "SECURITY_CODE",
        "sort": "END_DATE",
    },
    "dividend": {
        "report": "RPT_SHAREBONUS_DET",
        "code_field": "SECURITY_CODE",
        "sort": "EX_DIVIDEND_DATE",
    },
}


NORMALIZERS = {
    "lockup": lambda rows: [
        {
            "date": str(row.get("FREE_DATE") or "")[:10],
            "type": row.get("FREE_SHARES_TYPE") or "",
            "shares": row.get("FREE_SHARES") or 0,
            "able_shares": row.get("ABLE_FREE_SHARES") or 0,
        }
        for row in rows
    ],
    "margin": lambda rows: [
        {
            "date": str(row.get("DATE") or "")[:10],
            "rzye": row.get("RZYE") or 0,
            "rzmre": row.get("RZMRE") or 0,
            "rqye": row.get("RQYE") or 0,
            "rzrqye": row.get("RZRQYE") or 0,
        }
        for row in rows
    ],
    "block_trade": lambda rows: [
        {
            "date": str(row.get("TRADE_DATE") or "")[:10],
            "price": row.get("DEAL_PRICE") or 0,
            "close": row.get("CLOSE_PRICE") or 0,
            "volume": row.get("DEAL_VOLUME") or 0,
            "amount": row.get("DEAL_AMT") or 0,
            "buyer": row.get("BUYER_NAME") or "",
            "seller": row.get("SELLER_NAME") or "",
        }
        for row in rows
    ],
    "holder_num": lambda rows: [
        {
            "date": str(row.get("END_DATE") or "")[:10],
            "holder_num": row.get("HOLDER_NUM") or 0,
            "change_num": row.get("HOLDER_NUM_CHANGE") or 0,
            "change_ratio": row.get("HOLDER_NUM_RATIO") or 0,
            "avg_shares": row.get("AVG_FREE_SHARES") or 0,
        }
        for row in rows
    ],
    "dividend": lambda rows: [
        {
            "date": str(row.get("EX_DIVIDEND_DATE") or "")[:10],
            "bonus_rmb": row.get("PRETAX_BONUS_RMB") or 0,
            "transfer_ratio": row.get("TRANSFER_RATIO") or 0,
            "bonus_ratio": row.get("BONUS_RATIO") or 0,
            "plan": row.get("ASSIGN_PROGRESS") or "",
        }
        for row in rows
    ],
}


def fetch_stock_event(event: str, code: str, page_size: int = 30) -> dict:
    if event not in EVENTS:
        raise ValueError(f"不支持的股票事件: {event}")
    config = EVENTS[event]
    try:
        rows = eastmoney_datacenter(
            report_name=config["report"],
            filter_str=f'({config["code_field"]}="{code}")',
            page_size=page_size,
            sort_columns=config["sort"],
            sort_types="-1",
        )
    except Exception as exc:
        return {
            "event": event,
            "code": code,
            "total": 0,
            "items": [],
            "error": str(exc),
            "error_type": type(exc).__name__,
            "source": "eastmoney_datacenter",
        }
    items = NORMALIZERS[event](rows)
    return {
        "event": event,
        "code": code,
        "total": len(items),
        "items": items,
        "source": "eastmoney_datacenter",
    }

#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime

from .client import get_tushare_client


def main() -> int:
    started_at = datetime.now().isoformat(timespec="seconds")
    pro = get_tushare_client()
    try:
        df = pro.trade_cal(
            exchange="SSE",
            start_date="20260525",
            end_date="20260605",
            fields="exchange,cal_date,is_open,pretrade_date",
        )
        payload = {
            "status": "ok",
            "started_at": started_at,
            "rows": int(len(df)),
            "columns": list(df.columns),
            "sample": df.head(5).to_dict(orient="records"),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        message = str(exc)
        if "频率超限" in message:
            payload = {
                "status": "rate_limited_but_authenticated",
                "started_at": started_at,
                "message": message,
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            return 0
        payload = {
            "status": "failed",
            "started_at": started_at,
            "message": message,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

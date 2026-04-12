#!/usr/bin/env python3
"""
kr-stock-resolver.py — 한국 종목명/코드를 야후 종목코드로 변환

사용법:
  python3 kr-stock-resolver.py 삼성전자
  python3 kr-stock-resolver.py 이노인스트루먼트
  python3 kr-stock-resolver.py 005930
  python3 kr-stock-resolver.py 삼성전자,카카오,이노인스트루먼트
"""

import json
import sys

try:
    import yfinance as yf
except ImportError:
    print(json.dumps({"error": "yfinance required"}))
    sys.exit(1)


def resolve(query: str) -> dict:
    """종목명 또는 코드를 야후 코드로 변환"""
    query = query.strip()

    # 이미 야후 코드인 경우
    if ".KS" in query or ".KQ" in query:
        return {"input": query, "yahoo": query, "found": True}

    # 6자리 숫자 코드인 경우 → .KS와 .KQ 모두 시도
    if query.isdigit() and len(query) == 6:
        for suffix in [".KS", ".KQ"]:
            code = query + suffix
            try:
                t = yf.Ticker(code)
                h = t.history(period="5d")
                if len(h) > 0:
                    name = t.info.get("shortName", "")
                    return {"input": query, "yahoo": code, "name": name, "found": True}
            except Exception:
                continue
        return {"input": query, "yahoo": None, "found": False, "error": "종목코드를 찾을 수 없습니다"}

    # 한글 종목명인 경우 → yfinance search
    try:
        results = yf.Search(query, max_results=5)
        quotes = results.quotes if hasattr(results, 'quotes') else []
        for q in quotes:
            sym = q.get("symbol", "")
            if sym.endswith(".KS") or sym.endswith(".KQ"):
                return {
                    "input": query,
                    "yahoo": sym,
                    "name": q.get("shortname", q.get("longname", "")),
                    "exchange": q.get("exchange", ""),
                    "found": True,
                }
    except Exception:
        pass

    # search 실패 시 브루트포스 시도하지 않음
    return {"input": query, "yahoo": None, "found": False, "error": f"'{query}'를 찾을 수 없습니다. 종목코드(예: 215790)로 입력해보세요."}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: kr-stock-resolver.py <종목명 또는 코드>"}))
        sys.exit(1)

    queries = sys.argv[1].split(",")
    results = []
    for q in queries:
        q = q.strip()
        if not q:
            continue
        results.append(resolve(q))

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

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
import urllib.request


# 토스증권 public API (인증 불필요)
TOSS_STOCK_INFO_URL = "https://wts-info-api.tossinvest.com/api/v1/stock-infos?codes={}"


def fetch_toss_stock_info(product_code: str) -> dict | None:
    """토스증권 public API로 종목 정보 조회"""
    try:
        url = TOSS_STOCK_INFO_URL.format(product_code)
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            results = data.get("result", [])
            if results:
                return results[0]
    except Exception:
        pass
    return None


def resolve(query: str) -> dict:
    """종목명 또는 코드를 야후 코드로 변환 (토스증권 API 우선)"""
    query = query.strip()

    # 이미 야후 코드인 경우
    if ".KS" in query or ".KQ" in query:
        return {"input": query, "yahoo": query, "found": True}

    # 6자리 숫자 코드인 경우 → 토스증권 API로 시장 확인
    if query.isdigit() and len(query) == 6:
        info = fetch_toss_stock_info(f"A{query}")
        if info:
            market_code = info.get("market", {}).get("code", "")
            name = info.get("name", "")
            # KSQ=코스닥→.KQ, KSP=코스피→.KS
            suffix = ".KQ" if market_code == "KSQ" else ".KS"
            return {
                "input": query,
                "yahoo": f"{query}{suffix}",
                "name": name,
                "market": market_code,
                "found": True,
            }
        # 토스 API 실패 시 yfinance 폴백
        try:
            import yfinance as yf
            for suffix in [".KS", ".KQ"]:
                t = yf.Ticker(query + suffix)
                h = t.history(period="5d")
                if len(h) > 0:
                    return {"input": query, "yahoo": query + suffix, "found": True}
        except Exception:
            pass
        return {"input": query, "yahoo": None, "found": False, "error": "종목코드를 찾을 수 없습니다"}

    # 한글 종목명 → yfinance search (토스 API에 검색 기능 없음)
    try:
        import yfinance as yf
        results = yf.Search(query, max_results=5)
        quotes = results.quotes if hasattr(results, 'quotes') else []
        for q in quotes:
            sym = q.get("symbol", "")
            if sym.endswith(".KS") or sym.endswith(".KQ"):
                return {
                    "input": query, "yahoo": sym,
                    "name": q.get("shortname", q.get("longname", "")),
                    "found": True,
                }
    except Exception:
        pass

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

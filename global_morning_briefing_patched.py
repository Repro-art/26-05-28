import os
import re
import time
import html
import json
import math
import textwrap
from datetime import datetime, timedelta, timezone

import requests
import feedparser
import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup
from dateutil import tz

# =========================
# 환경변수
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

USE_OPENAI = bool(OPENAI_API_KEY)
KST = tz.gettz("Asia/Seoul")
UTC = timezone.utc

# =========================
# 설정
# =========================
INDEX_MAP = {
    # 미국
    "S&P500": "^GSPC",
    "NASDAQ": "^IXIC",
    "DOW": "^DJI",
    "필라델피아 반도체": "^SOX",
    "러셀2000": "^RUT",

    # 아시아
    "일본": "^N225",
    "홍콩": "^HSI",
    "중국": "000001.SS",
    "대만": "^TWII",

    # 유럽
    "영국": "^FTSE",
    "독일": "^GDAXI",
    "프랑스": "^FCHI",
    "유럽": "^STOXX50E",
}

MACRO_MAP = {
    "미국 2년물": "^IRX",
    "미국 10년물": "^TNX",
    "달러 인덱스": "DX-Y.NYB",
    "VIX": "^VIX",

    "WTI": "CL=F",
    "브렌트유": "BZ=F",
    "천연가스": "NG=F",

    "금": "GC=F",
    "은": "SI=F",
    "구리": "HG=F",
}

CRYPTO_MAP = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
}

MACRO_DISPLAY_KIND = {
    "미국 2년물": "bp",
    "미국 10년물": "bp",
}


def format_change(name: str, item: dict | None) -> str:
    if not item:
        return f"{name} N/A"
    pct = item.get("pct")
    delta = item.get("delta")
    last = item.get("last_close")
    kind = MACRO_DISPLAY_KIND.get(name, "pct")

    if kind == "bp":
        if delta is None:
            return f"{name} N/A"
        # ^TNX / ^IRX are yield*10 on Yahoo. 1.0 move == 0.10%p == 10bp
        bp = delta * 10.0
        sign = "+" if bp > 0 else ""
        level = f" ({last / 10.0:.2f}%)" if isinstance(last, (int, float)) else ""
        return f"{name} {sign}{bp:.1f}bp{level}"

    if pct is None:
        return f"{name} N/A"
    sign = "+" if pct > 0 else ""
    return f"{name} {sign}{pct:.2f}%"

# 뉴스는 속도/사실확인 우선:
# Reuters / AP / 공식발표 위주.
NEWS_QUERIES = [
    '("Federal Reserve" OR Fed OR Treasury OR inflation OR jobs OR tariff OR sanctions) site:reuters.com',
    '("Nvidia" OR Microsoft OR Apple OR Amazon OR Meta OR Alphabet OR Broadcom OR Tesla) site:reuters.com',
    '("Federal Reserve" OR Treasury OR inflation OR jobs OR tariff OR sanctions) site:apnews.com',
    '("earnings" OR guidance OR acquisition OR merger) (site:sec.gov OR site:investor.apple.com OR site:investor.nvidia.com OR site:microsoft.com OR site:aboutamazon.com OR site:about.fb.com)',
]

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

# =========================
# 유틸
# =========================
def now_kst():
    return datetime.now(tz=KST)

def pct_str(v):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "N/A"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}%"

def safe_get(d, key, default=None):
    try:
        return d.get(key, default)
    except Exception:
        return default

def html_escape(s: str) -> str:
    return html.escape(s or "", quote=False)

def telegram_send_html(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 비어있습니다.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, data=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram send 실패: {data}")

def telegram_send_chunked_html(message: str, limit: int = 3500):
    lines = message.splitlines()
    buf = ""
    for line in lines:
        candidate = buf + ("\n" if buf else "") + line
        if len(candidate) > limit:
            if buf:
                telegram_send_html(buf)
                time.sleep(1.0)
                buf = line
            else:
                # 한 줄이 너무 긴 경우 강제 분할
                for i in range(0, len(line), limit):
                    part = line[i:i+limit]
                    telegram_send_html(part)
                    time.sleep(1.0)
                buf = ""
        else:
            buf = candidate
    if buf:
        telegram_send_html(buf)

# =========================
# 1) 해외 주요 지수
# =========================
def fetch_index_snapshot(symbol: str):
    """
    최근 유효 가격 2개를 기준으로 변화율을 계산.
    - pct: 일반 자산용 % 변화율
    - delta: 절대 변화량 (채권수익률 bp 계산 등에 사용)
    """
    try:
        df = yf.download(
            symbol,
            period="10d",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            if ("Close", symbol) in df.columns:
                closes = df[("Close", symbol)].dropna()
            else:
                closes = df.xs("Close", axis=1, level=0).iloc[:, 0].dropna()
        else:
            closes = df["Close"].dropna()

        if len(closes) < 2:
            return None

        last_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])
        delta = last_close - prev_close
        pct = (last_close / prev_close - 1.0) * 100.0 if prev_close else None

        return {
            "symbol": symbol,
            "last_close": last_close,
            "prev_close": prev_close,
            "delta": delta,
            "pct": pct,
            "date": closes.index[-1].strftime("%Y-%m-%d"),
        }
    except Exception:
        return None

def fetch_all_indices():
    out = []
    for name, symbol in INDEX_MAP.items():
        snap = fetch_index_snapshot(symbol)
        out.append({
            "name": name,
            "symbol": symbol,
            "last_close": safe_get(snap, "last_close"),
            "prev_close": safe_get(snap, "prev_close"),
            "delta": safe_get(snap, "delta"),
            "pct": safe_get(snap, "pct"),
            "date": safe_get(snap, "date"),
        })
        time.sleep(0.15)
    return out

def fetch_macro_indicators():
    out = []
    for name, symbol in MACRO_MAP.items():
        snap = fetch_index_snapshot(symbol)
        out.append({
            "name": name,
            "symbol": symbol,
            "last_close": safe_get(snap, "last_close"),
            "prev_close": safe_get(snap, "prev_close"),
            "delta": safe_get(snap, "delta"),
            "pct": safe_get(snap, "pct"),
            "date": safe_get(snap, "date"),
        })
        time.sleep(0.15)
    return out

def fetch_symbol_group(name_map: dict):
    out = []
    for name, symbol in name_map.items():
        snap = fetch_index_snapshot(symbol)
        out.append({
            "name": name,
            "symbol": symbol,
            "last_close": safe_get(snap, "last_close"),
            "prev_close": safe_get(snap, "prev_close"),
            "delta": safe_get(snap, "delta"),
            "pct": safe_get(snap, "pct"),
            "date": safe_get(snap, "date"),
        })
        time.sleep(0.15)
    return out

def fetch_crypto_snapshot():
    return fetch_symbol_group(CRYPTO_MAP)

# =========================
# 2) 미국 시총 상위 15개
# =========================
def fetch_top15_us_companies():
    """
    CompaniesMarketCap 미국 시총 순위 상위 15개 파싱.
    종목코드가 페이지 구조상 직접 안 잡히는 경우를 대비해
    기업명 -> 티커 매핑 보조 테이블 사용.
    """
    url = "https://companiesmarketcap.com/usa/largest-companies-in-the-usa-by-market-cap/"
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "lxml")

    rows = soup.select("table tbody tr")
    results = []

    fallback_ticker_map = {
        "NVIDIA": "NVDA",
        "Microsoft": "MSFT",
        "Apple": "AAPL",
        "Alphabet (Google)": "GOOGL",
        "Amazon": "AMZN",
        "Meta Platforms": "META",
        "Broadcom": "AVGO",
        "Tesla": "TSLA",
        "Berkshire Hathaway": "BRK-B",
        "Taiwan Semiconductor Manufacturing": "TSM",
        "Eli Lilly": "LLY",
        "Walmart": "WMT",
        "JPMorgan Chase": "JPM",
        "Visa": "V",
        "Exxon Mobil": "XOM",
        "Mastercard": "MA",
        "Oracle": "ORCL",
        "Costco": "COST",
        "Netflix": "NFLX",
    }

    for tr in rows:
        name_tag = tr.select_one("div.company-name")
        if not name_tag:
            continue

        name = name_tag.get_text(" ", strip=True)

        # 링크에서 티커 힌트 시도
        ticker = None
        a_tag = tr.select_one("a")
        if a_tag and a_tag.get("href"):
            href = a_tag["href"]
            # 예: /apple/marketcap/ -> 종목코드는 안 나오는 경우가 많음
            # 여기서는 fallback 사용

        if not ticker:
            ticker = fallback_ticker_map.get(name)

        if ticker:
            results.append({
                "company": name,
                "ticker": ticker,
            })

        if len(results) >= 15:
            break

    if len(results) < 15:
        raise RuntimeError("시총 상위 15개 파싱 실패: 결과가 부족합니다.")

    return results[:15]

def fetch_stock_pct(ticker: str):
    try:
        df = yf.download(
            ticker,
            period="7d",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            if ("Close", ticker) in df.columns:
                closes = df[("Close", ticker)].dropna()
            else:
                closes = df.xs("Close", axis=1, level=0).iloc[:, 0].dropna()
        else:
            closes = df["Close"].dropna()

        if len(closes) < 2:
            return None

        last_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])
        pct = (last_close / prev_close - 1.0) * 100.0

        return {
            "ticker": ticker,
            "last_close": last_close,
            "prev_close": prev_close,
            "pct": pct,
            "date": closes.index[-1].strftime("%Y-%m-%d"),
        }
    except Exception:
        return None

def fetch_top15_moves():
    companies = fetch_top15_us_companies()
    out = []
    for i, item in enumerate(companies, start=1):
        snap = fetch_stock_pct(item["ticker"])
        out.append({
            "rank": i,
            "company": item["company"],
            "ticker": item["ticker"],
            "pct": safe_get(snap, "pct"),
            "date": safe_get(snap, "date"),
        })
        time.sleep(0.15)
    return out

# =========================
# 3) 뉴스 수집
# =========================
def clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s

def normalize_url(url: str) -> str:
    if not url:
        return url
    url = re.sub(r"[?&](utm_[^=&]+|oc=5|hl=en-US|gl=US|ceid=US:en)=[^&]*", "", url)
    url = url.replace("http://", "https://")
    return url

def fetch_google_news_rss(query: str, max_items: int = 10):
    rss_url = GOOGLE_NEWS_RSS.format(query=requests.utils.quote(query))
    feed = feedparser.parse(rss_url)
    items = []
    for entry in feed.entries[:max_items]:
        title = clean_text(entry.get("title", ""))
        link = normalize_url(entry.get("link", ""))
        published = entry.get("published", "")
        summary = clean_text(BeautifulSoup(entry.get("summary", ""), "lxml").get_text(" ", strip=True))
        items.append({
            "title": title,
            "link": link,
            "published": published,
            "summary": summary,
        })
    return items

def domain_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""

def score_news_item(item: dict) -> int:
    title = (item.get("title") or "").lower()
    domain = domain_of(item.get("link") or "")

    score = 0
    if "reuters.com" in domain:
        score += 5
    elif "apnews.com" in domain:
        score += 4
    elif domain.endswith("sec.gov"):
        score += 4
    elif "investor." in domain or "aboutamazon.com" in domain or "microsoft.com" in domain:
        score += 3

    important_kw = [
        "fed", "federal reserve", "treasury", "inflation", "jobs",
        "tariff", "sanction", "guidance", "earnings", "forecast",
        "acquisition", "merger", "chip", "ai", "data center", "cloud"
    ]
    for kw in important_kw:
        if kw in title:
            score += 1

    return score

def dedupe_news(items):
    seen = set()
    out = []
    for item in sorted(items, key=score_news_item, reverse=True):
        key = re.sub(r"[^a-z0-9가-힣]+", " ", (item["title"] or "").lower()).strip()
        key = " ".join(key.split()[:10])
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out

def fetch_news_pool():
    all_items = []
    for q in NEWS_QUERIES:
        try:
            items = fetch_google_news_rss(q, max_items=8)
            all_items.extend(items)
            time.sleep(0.6)
        except Exception:
            continue
    all_items = dedupe_news(all_items)
    all_items = sorted(all_items, key=score_news_item, reverse=True)
    return all_items[:12]

# =========================
# 4) 뉴스 요약
# =========================
def summarize_news_fallback(news_items):
    """
    OpenAI 없을 때:
    헤드라인 기반으로 최대한 짧게 정리.
    """
    lines = []
    for item in news_items[:5]:
        title = item["title"]
        domain = domain_of(item["link"])
        if "reuters.com" in domain:
            src = "Reuters"
        elif "apnews.com" in domain:
            src = "AP"
        elif domain.endswith("sec.gov"):
            src = "SEC"
        else:
            src = domain.replace("www.", "")

        lines.append(f"- {title} ({src})")
    return "\n".join(lines)

def summarize_news_openai(news_items):
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)

    compressed = []
    for i, item in enumerate(news_items[:10], start=1):
        compressed.append(
            {
                "id": i,
                "title": item["title"],
                "summary": item["summary"][:400],
                "link": item["link"],
            }
        )

    prompt = f"""
너는 '해외 모닝브리핑 편집자'다.

목표:
- 한국 사용자가 아침에 20~40초 안에 읽을 수 있게 정리
- 속도와 사실확인이 중요
- 루머/해석 과잉 금지
- 확인 가능한 사실 중심
- 뉴스 4~6개만 뽑기
- 각 줄은 짧고 보기 쉽게
- 시장 영향이 뚜렷하면 한 줄 안에 아주 짧게 포함
- 출력은 순수 텍스트만

- 사실(Fact)과 해석(Interpretation)을 분리
- 시장 영향이 약한 정보는 제거
- ETF 비용률/언급량/SNS 잡음 제외
- 실제 가격/금리/수급/실적 중심
- 같은 사건은 하나로 통합
- "주목", "관심", "기대" 같은 표현 최소화
- 단정적 표현보다 가능성 중심 표현 사용
- 한국 투자자가 오늘 무엇을 봐야 하는지 포함


입력 뉴스:
{json.dumps(compressed, ensure_ascii=False, indent=2)}

출력 형식:

[FACT]
- 실제 발생한 핵심 뉴스만

[INTERPRETATION]
- 시장이 어떻게 반응할 가능성이 있는지

[WATCHPOINT]
- 한국장에서 체크할 포인트

예시:
- [매크로] 미국 고용/물가 관련 뉴스로 금리 경로 재평가
- [반도체] AI·칩 관련 이슈로 대형 기술주 변동성 확대
- [빅테크] 실적/가이던스/규제 관련 핵심 포인트
- [정책] 관세·제재·규제 변화 여부 체크
- [체크포인트] 한국장에서는 반도체/환율/미국선물 확인 필요

중요:
- 사실이 불명확하면 빼라
- 같은 사건은 하나로 묶어라
- 5줄 내외
"""

    resp = client.responses.create(
        model="gpt-5-mini",
        input=prompt,
    )
    text = (resp.output_text or "").strip()
    return text

def summarize_news(news_items):
    if USE_OPENAI:
        try:
            return summarize_news_openai(news_items)
        except Exception:
            pass
    return summarize_news_fallback(news_items)

# =========================
# 4.1) 시장요약
# =========================

def get_item_from_groups(name, *groups):
    for group in groups:
        for item in group:
            if item["name"] == name:
                return item
    return None


def get_pct_from_groups(name, *groups):
    item = get_item_from_groups(name, *groups)
    return item.get("pct") if item else None


def format_evidence(name, value):
    if value is None:
        return None
    return f"{name} {pct_str(value)}"


def build_us_market_tone(macro, indices):
    spx = get_pct_from_groups("S&P500", indices)
    ndx = get_pct_from_groups("NASDAQ", indices)
    sox = get_pct_from_groups("필라델피아 반도체", indices)
    rut = get_pct_from_groups("러셀2000", indices)
    vix = get_pct_from_groups("VIX", macro)

    evidence = [
        format_evidence("S&P500", spx),
        format_evidence("NASDAQ", ndx),
        format_evidence("러셀2000", rut),
        format_evidence("SOX", sox),
        format_evidence("VIX", vix),
    ]
    evidence = [x for x in evidence if x]

    if spx is None or ndx is None:
        return "미국장 분위기: 데이터 부족", evidence

    tags = []

    if sox is not None:
        if sox >= 1.0:
            tags.append("반도체 강세")
        elif sox <= -1.0:
            tags.append("반도체 약세")

    if rut is not None:
        if rut >= 1.0:
            tags.append("소형주 강세")
        elif rut <= -1.0:
            tags.append("소형주 약세")

    if vix is not None:
        if vix >= 5.0:
            tags.append("변동성 확대")
        elif vix <= -5.0:
            tags.append("변동성 완화")

    if spx >= 0.8 and ndx >= 1.0:
        tone = "Risk-on"
    elif spx <= -0.8 and ndx <= -1.0:
        tone = "Risk-off"
    elif ndx > spx and ndx > 0:
        tone = "기술주 중심 강세"
    elif ndx < spx and ndx < 0:
        tone = "기술주 중심 약세"
    else:
        tone = "혼조"

    if tags:
        return {"title": "미국장 분위기", "tone": f"{tone} ({', '.join(tags[:3])})", "evidence": evidence}
    return {"title": "미국장 분위기", "tone": tone, "evidence": evidence}


def build_world_market_tone(indices):
    us = []
    eu = []
    asia = []

    spx = get_pct_from_groups("S&P500", indices)
    ndx = get_pct_from_groups("NASDAQ", indices)
    uk = get_pct_from_groups("영국", indices)
    de = get_pct_from_groups("독일", indices)
    fr = get_pct_from_groups("프랑스", indices)
    eu50 = get_pct_from_groups("유럽", indices)
    jp = get_pct_from_groups("일본", indices)
    hk = get_pct_from_groups("홍콩", indices)
    cn = get_pct_from_groups("중국", indices)
    tw = get_pct_from_groups("대만", indices)

    for v in [spx, ndx]:
        if v is not None:
            us.append(v)
    for v in [uk, de, fr, eu50]:
        if v is not None:
            eu.append(v)
    for v in [jp, hk, cn, tw]:
        if v is not None:
            asia.append(v)

    def avg(xs):
        return sum(xs) / len(xs) if xs else None

    us_avg = avg(us)
    eu_avg = avg(eu)
    asia_avg = avg(asia)

    tags = []

    if us_avg is not None:
        if us_avg <= -0.7:
            tags.append("미국 약세")
        elif us_avg >= 0.7:
            tags.append("미국 강세")

    if eu_avg is not None:
        if eu_avg <= -0.7:
            tags.append("유럽 약세")
        elif eu_avg >= 0.7:
            tags.append("유럽 강세")

    if asia_avg is not None:
        if asia_avg <= -0.5:
            tags.append("아시아 약세")
        elif asia_avg >= 0.5:
            tags.append("아시아 강세")
        else:
            tags.append("아시아 혼조")

    negatives = sum(1 for x in [us_avg, eu_avg, asia_avg] if x is not None and x <= -0.5)
    positives = sum(1 for x in [us_avg, eu_avg, asia_avg] if x is not None and x >= 0.5)

    if negatives >= 2:
        tone = "전반 약세"
    elif positives >= 2:
        tone = "전반 강세"
    else:
        tone = "혼조"

    evidence = [
        format_evidence("S&P500", spx),
        format_evidence("NASDAQ", ndx),
        format_evidence("영국", uk),
        format_evidence("독일", de),
        format_evidence("프랑스", fr),
        format_evidence("유럽", eu50),
        format_evidence("일본", jp),
        format_evidence("홍콩", hk),
        format_evidence("중국", cn),
        format_evidence("대만", tw),
    ]
    evidence = [x for x in evidence if x]

    if tags:
        return {"title": "세계증시 분위기", "tone": f"{tone} ({', '.join(tags[:3])})", "evidence": evidence}
    return {"title": "세계증시 분위기", "tone": tone, "evidence": evidence}


def build_bond_fx_tone(macro):
    y2 = get_pct_from_groups("미국 2년물", macro)
    y10 = get_pct_from_groups("미국 10년물", macro)
    dxy = get_pct_from_groups("달러 인덱스", macro)

    tags = []

    if y10 is not None:
        if y10 >= 1.0:
            tags.append("장기금리 상승")
        elif y10 <= -1.0:
            tags.append("장기금리 하락")

    if y2 is not None:
        if y2 >= 1.0:
            tags.append("단기금리 상승")
        elif y2 <= -1.0:
            tags.append("단기금리 하락")

    if dxy is not None:
        if dxy >= 0.4:
            tags.append("달러 강세")
        elif dxy <= -0.4:
            tags.append("달러 약세")
        else:
            tags.append("달러 혼조")

    if y10 is not None and y2 is not None:
        if y10 > 0 and y2 > 0:
            tone = "금리 상승 압력"
        elif y10 < 0 and y2 < 0:
            tone = "채권 강세"
        else:
            tone = "금리 혼조"
    else:
        tone = "데이터 부족"

    if dxy is not None:
        tone = f"{tone} · {'달러 강세' if dxy >= 0.4 else '달러 약세' if dxy <= -0.4 else '달러 혼조'}"

    evidence = [
        format_evidence("미국 2년물", y2),
        format_evidence("미국 10년물", y10),
        format_evidence("달러 인덱스", dxy),
    ]
    evidence = [x for x in evidence if x]

    return {"title": "채권/환율 분위기", "tone": tone, "evidence": evidence}


def build_commodity_tone(macro):
    oil = get_pct_from_groups("WTI", macro)
    brent = get_pct_from_groups("브렌트유", macro)
    gas = get_pct_from_groups("천연가스", macro)
    gold = get_pct_from_groups("금", macro)
    silver = get_pct_from_groups("은", macro)
    copper = get_pct_from_groups("구리", macro)

    tags = []

    oil_ref = None
    if oil is not None and brent is not None:
        oil_ref = (oil + brent) / 2
    elif oil is not None:
        oil_ref = oil
    elif brent is not None:
        oil_ref = brent

    if oil_ref is not None:
        if oil_ref >= 1.5:
            tags.append("유가 강세")
        elif oil_ref <= -1.5:
            tags.append("유가 약세")

    if gold is not None:
        if gold >= 0.7:
            tags.append("금 강세")
        elif gold <= -0.7:
            tags.append("금 약세")

    if copper is not None:
        if copper >= 0.8:
            tags.append("구리 강세")
        elif copper <= -0.8:
            tags.append("구리 약세")

    tone = "혼조" if not tags else ", ".join(tags[:3])

    evidence = [
        format_evidence("WTI", oil),
        format_evidence("브렌트유", brent),
        format_evidence("천연가스", gas),
        format_evidence("금", gold),
        format_evidence("은", silver),
        format_evidence("구리", copper),
    ]
    evidence = [x for x in evidence if x]

    return {"title": "원자재 분위기", "tone": tone, "evidence": evidence}


def build_crypto_tone(crypto):
    btc = get_pct_from_groups("BTC", crypto)
    eth = get_pct_from_groups("ETH", crypto)
    sol = get_pct_from_groups("SOL", crypto)

    rises = sum(1 for x in [btc, eth, sol] if x is not None and x >= 1.0)
    falls = sum(1 for x in [btc, eth, sol] if x is not None and x <= -1.0)

    tags = []

    if btc is not None:
        if btc >= 1.0:
            tags.append("BTC 강세")
        elif btc <= -1.0:
            tags.append("BTC 약세")

    if eth is not None:
        if eth >= 1.0:
            tags.append("ETH 강세")
        elif eth <= -1.0:
            tags.append("ETH 약세")

    if falls >= 2:
        tone = "위험자산 약세"
    elif rises >= 2:
        tone = "위험선호"
    else:
        tone = "혼조"

    evidence = [
        format_evidence("BTC", btc),
        format_evidence("ETH", eth),
        format_evidence("SOL", sol),
    ]
    evidence = [x for x in evidence if x]

    if tags:
        return {"title": "크립토 분위기", "tone": f"{tone} ({', '.join(tags[:2])})", "evidence": evidence}
    return {"title": "크립토 분위기", "tone": tone, "evidence": evidence}


def build_most_active_tone(top15):
    positives = [x for x in top15 if x["pct"] is not None and x["pct"] > 0]
    negatives = [x for x in top15 if x["pct"] is not None and x["pct"] < 0]

    msft = next((x for x in top15 if x["ticker"] == "MSFT"), None)
    aapl = next((x for x in top15 if x["ticker"] == "AAPL"), None)

    tags = []
    for mega in [msft, aapl]:
        if mega and mega["pct"] is not None and mega["pct"] >= 1.0:
            tags.append(f"{mega['ticker']} 강세")

    if len(positives) >= 10:
        tone = "대형주 전반 강세"
    elif len(negatives) >= 10:
        tone = "대형주 전반 약세"
    else:
        tone = "대형주 혼조"

    evidence = []
    for item in top15[:5]:
        evidence.append(f"{item['ticker']} {pct_str(item['pct'])}")

    if tags:
        return {"title": "주도주 분위기", "tone": f"{tone} ({', '.join(tags[:2])})", "evidence": evidence}
    return {"title": "주도주 분위기", "tone": tone, "evidence": evidence}


# =========================
# 4.4) 핵심축 / 관전포인트
# =========================

def build_key_drivers(macro, indices, news_items):
    drivers = []

    sox = get_pct_from_groups("필라델피아 반도체", indices)
    y10 = get_pct_from_groups("미국 10년물", macro)
    dxy = get_pct_from_groups("달러 인덱스", macro)
    oil = get_pct_from_groups("WTI", macro)
    vix = get_pct_from_groups("VIX", macro)

    if sox is not None:
        if sox >= 1.5:
            drivers.append("AI 반도체 강세 지속 가능성")
        elif sox <= -1.5:
            drivers.append("반도체 차익실현 압력 확대 가능성")

    if y10 is not None:
        if y10 >= 1.0:
            drivers.append("장기금리 상승 부담 지속")
        elif y10 <= -1.0:
            drivers.append("금리 완화 기대 확대")

    if dxy is not None:
        if dxy >= 0.5:
            drivers.append("달러 강세로 위험자산 부담 가능성")
        elif dxy <= -0.5:
            drivers.append("달러 약세 기반 위험선호 유지 가능성")

    if oil is not None and abs(oil) >= 2:
        drivers.append("유가 변동성 확대 여부 주시")

    if vix is not None and vix >= 5:
        drivers.append("변동성 확대 가능성")

    drivers = list(dict.fromkeys(drivers))
    return drivers[:4]


def build_watchpoints(macro, indices):
    points = []

    sox = get_pct_from_groups("필라델피아 반도체", indices)
    y10 = get_pct_from_groups("미국 10년물", macro)
    dxy = get_pct_from_groups("달러 인덱스", macro)
    vix = get_pct_from_groups("VIX", macro)

    if sox is not None:
        if sox >= 1:
            points.append("삼성전자·SK하이닉스 시초 강세 이후 외국인 수급 유지 여부")
        elif sox <= -1:
            points.append("반도체 갭하락 이후 낙폭 축소 여부")

    if y10 is not None:
        if y10 >= 1:
            points.append("장기금리 상승으로 성장주 차익실현 여부")
        elif y10 <= -1:
            points.append("금리 하락 기반 기술주 반등 지속 여부")

    if dxy is not None:
        if dxy >= 0.5:
            points.append("원달러 환율 상승 여부 체크")
        elif dxy <= -0.5:
            points.append("외국인 수급 개선 여부 확인")

    if vix is not None and vix >= 5:
        points.append("장초반 변동성 확대 가능성 주의")

    return points[:5]


# =========================
# 4.5) 한국장 체크포인트
# =========================

def build_korea_facts(macro, indices):
    lines = []

    sox = get_pct_from_groups("필라델피아 반도체", indices)
    yield10 = get_pct_from_groups("미국 10년물", macro)
    dollar = get_pct_from_groups("달러 인덱스", macro)
    oil = get_pct_from_groups("WTI", macro)
    vix = get_pct_from_groups("VIX", macro)

    if sox is not None:
        item = get_item_from_groups("필라델피아 반도체", indices)
    if item is not None:
        lines.append(format_change("SOX", item))

    if yield10 is not None:
        item = get_item_from_groups("미국 10년물", macro)
    if item is not None:
        lines.append(format_change("미국 10년물", item).replace("미국 10년물 ", "미국10년물 "))

    if dollar is not None:
        item = get_item_from_groups("달러 인덱스", macro)
    if item is not None:
        lines.append(format_change("달러 인덱스", item).replace("달러 인덱스 ", "달러인덱스 "))

    if vix is not None:
        item = get_item_from_groups("VIX", macro)
    if item is not None:
        lines.append(format_change("VIX", item))

    if oil is not None:
        item = get_item_from_groups("WTI", macro)
    if item is not None:
        lines.append(format_change("WTI", item))

    return lines

# =========================
# 5) 텔레그램 메시지 조립
# =========================
def build_message(
    macro,
    indices,
    top15,
    crypto,
    news_summary,
    checkpoints,
    key_drivers,
    watchpoints,
):
    today = now_kst().strftime("%Y-%m-%d (%a)")

    global_data = build_world_market_tone(indices)
    us_data = build_us_market_tone(macro, indices)
    bond_data = build_bond_fx_tone(macro)
    commodity_data = build_commodity_tone(macro)

    msg = []
    msg.append(f"<b>해외 모닝 브리핑</b>  {html_escape(today)}")
    msg.append("")

    
    msg.append("<b>오늘 시장 핵심축</b>")
    for item in key_drivers:
        msg.append(f"- {html_escape(item)}")

    msg.append("")
    msg.append("<b>글로벌 FACT</b>")
    for line in global_data["evidence"]:
        msg.append(f"- {html_escape(line)}")

    msg.append("")
    msg.append("<b>글로벌 해석</b>")
    msg.append(f"- {html_escape(global_data['tone'])}")

    msg.append("")
    msg.append("<b>미국장 FACT</b>")
    for line in us_data["evidence"]:
        msg.append(f"- {html_escape(line)}")

    msg.append("")
    msg.append("<b>미국장 해석</b>")
    msg.append(f"- {html_escape(us_data['tone'])}")

    msg.append("")
    msg.append("<b>채권/환율 FACT</b>")
    for line in bond_data["evidence"]:
        msg.append(f"- {html_escape(line)}")

    msg.append("")
    msg.append("<b>채권/환율 해석</b>")
    msg.append(f"- {html_escape(bond_data['tone'])}")

    msg.append("")
    msg.append("<b>원자재 FACT</b>")
    for line in commodity_data["evidence"]:
        msg.append(f"- {html_escape(line)}")

    msg.append("")
    msg.append("<b>원자재 해석</b>")
    msg.append(f"- {html_escape(commodity_data['tone'])}")

    msg.append("")
    msg.append("<b>오늘 관전 포인트</b>")
    for item in watchpoints:
        msg.append(f"- {html_escape(item)}")

    msg.append("")
    msg.append("<b>한국장 체크포인트</b>")
    for line in checkpoints:
        msg.append(f"- {html_escape(line)}")

# optionally append news summary at end if available
    if news_summary:
        msg.append("")
        msg.append("<b>참고 뉴스</b>")
        for line in (news_summary or "").splitlines():
            if line.strip():
                msg.append(f"- {html_escape(line.lstrip('- ').strip())}")

    return "\n".join(msg)

# =========================
# 6) 실행
# =========================
def main():
    print("[INFO] fetching macro indicators...")
    macro = fetch_macro_indicators()

    print("[INFO] fetching indices...")
    indices = fetch_all_indices()

    print("[INFO] fetching crypto...")
    crypto = fetch_crypto_snapshot()

    print("[INFO] fetching top15 market cap companies...")
    top15 = fetch_top15_moves()

    print("[INFO] fetching news...")
    news_items = fetch_news_pool()

    print("[INFO] summarizing news...")
    news_summary = summarize_news(news_items)

    print("[INFO] building checkpoints...")
    checkpoints = build_korea_facts(macro, indices)

    message = build_message(macro, indices, top15, crypto, news_summary, checkpoints)

    print("[INFO] sending telegram...")
    telegram_send_chunked_html(message)

    print("[DONE] sent successfully")

if __name__ == "__main__":
    main()
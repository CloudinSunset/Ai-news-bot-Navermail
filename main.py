# ─────────────────────────────────────────────
# 일일 AI/디지털 정책 뉴스 리포트 - 네이버 메일
# (보안/안정성 개선 반영 버전)
# ─────────────────────────────────────────────

import os
import sys
import time
import re
import html
import urllib.parse
import json
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
import requests
from google import genai
from difflib import SequenceMatcher
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─────────────────────────────────────────────
# 환경변수 (GitHub Secrets에서 주입)
# ─────────────────────────────────────────────
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()
NAVER_ID = os.environ["NAVER_ID"].strip()
NAVER_APP_PW = os.environ["NAVER_APP_PW"].strip()
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"].strip()

# ─────────────────────────────────────────────
# 키워드 정의
# ─────────────────────────────────────────────
CENTRAL_KEYWORDS = ["AI 정책", "데이터센터", "양자컴퓨팅", "디지털전환", "인공지능 전략"]
REGIONS = ["서울", "경기", "인천", "강원", "충북", "충남", "전북", "전남",
           "경북", "경남", "부산", "대구", "광주", "대전", "제주"]
FILTER_KEYWORDS = ["AI", "인공지능", "AX", "DX", "로봇", "데이터산업", "산업", "사업", "MOU", "디지털전환"]

ECONOMY_KEYWORDS = ["증시", "주가", "상한가", "호황", "성장", "매수", "나스닥", "코스피",
                    "GDP", "금리", "환율", "실적", "수혜", "전망", "분석", "리포트",
                    "목표가", "강세", "약세", "투자권고", "증권", "외인", "기관"]

CORPORATE_KEYWORDS = ["출시", "선보여", "이벤트", "할인", "사전예약", "공개채용", "업데이트",
                      "이용권", "구독", "신제품", "출장 서비스", "솔루션 공급", "B2B", "CSP",
                      "공모", "기술력", "플랫폼", "서비스"]

EXCLUDE_ORGANIZATIONS = ["대학", "대학교", "학교", "학원", "교육", "캠프", "졸업", "입학", "수강", "수료"]
POLITICS_KEYWORDS = ["후보", "공약", "출마", "선거", "의원", "당선", "유세", "국회", "총선", "지선", "대선", "국힘", "민주당", "사설", "트럼프", "백악관", "총리"]
GOV_KEYWORDS = ["정부", "부처", "시청", "도청", "지자체", "공공", "국가",
                "과학기술정보통신부", "중기부", "산업부"] + REGIONS


# ─────────────────────────────────────────────
# 공통 유틸 - HTML/URL 안전 처리
# ─────────────────────────────────────────────
def safe_url(u: str) -> str:
    """http(s) 스킴만 허용. 그 외(javascript: 등)는 무력화."""
    try:
        scheme = urlparse(u).scheme.lower()
    except Exception:
        return "#"
    return u if scheme in ("http", "https") else "#"


def esc(s: str) -> str:
    """HTML 특수문자 이스케이프 (속성/본문 공용)."""
    return html.escape(s or "", quote=True)


# ─────────────────────────────────────────────
# 1) 뉴스 수집 - Google News RSS (한국)
# ─────────────────────────────────────────────
def fetch_news(query: str, limit: int = 20, retries: int = 2):
    encoded_query = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"

    content = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            content = resp.content
            break
        except Exception as e:
            print(f"[WARN] RSS 요청 실패 ({query}) 시도 {attempt + 1}: {e}", flush=True)
            time.sleep(1.0)
    if content is None:
        return []

    # XML 파싱도 예외 보호 (구글이 비정상/HTML 응답을 줄 수 있음)
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        print(f"[WARN] RSS 파싱 실패 ({query}): {e}", flush=True)
        return []

    items = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = item.find("source")
        source_name = source.text if source is not None else ""

        # 구글 뉴스가 강제로 붙인 ' - 언론사명'을 제목에서 안전하게 제거
        if source_name:
            title = re.sub(rf'\s*-\s*{re.escape(source_name)}$', '', title)

        if title and link:
            items.append({
                "title": title,
                "link": link,
                "source": source_name or "",
            })
    return items


# ─────────────────────────────────────────────
# 2) 키워드 필터링 및 중복 제거
# ─────────────────────────────────────────────
def is_relevant(title: str) -> bool:
    if any(k in title for k in POLITICS_KEYWORDS):
        return False
    if any(k in title for k in ECONOMY_KEYWORDS):
        return False

    has_gov = any(k in title for k in GOV_KEYWORDS)
    has_filter = any(k in title for k in FILTER_KEYWORDS) or \
                 any(k in title for k in CENTRAL_KEYWORDS)

    if any(k in title for k in EXCLUDE_ORGANIZATIONS) and not has_gov:
        return False
    if any(k in title for k in CORPORATE_KEYWORDS) and not has_gov:
        return False

    return has_filter


def clean_title(title: str) -> str:
    title = re.sub(r'\[.*?\]|\(.*?\)|【.*?】', ' ', title)
    title = re.sub(r'[^가-힣a-zA-Z0-9]', '', title)
    return title.lower()


def calculate_title_similarity(title1: str, title2: str) -> float:
    t1 = clean_title(title1)
    t2 = clean_title(title2)
    if not t1 or not t2:
        return 0.0
    return SequenceMatcher(None, t1, t2).ratio()


def collect_filtered_articles(max_total: int = 8):
    all_articles = []
    queries = CENTRAL_KEYWORDS + ["AI 정부 정책", "디지털전환 사업", "AI 지자체 MOU"]

    for q in queries:
        for art in fetch_news(q, limit=15):
            t = art["title"]
            is_duplicate = False
            for existing_art in all_articles:
                similarity = calculate_title_similarity(t, existing_art["title"])
                if similarity >= 0.6:
                    is_duplicate = True
                    break

            if is_duplicate:
                continue
            if not is_relevant(t):
                continue

            all_articles.append(art)
        time.sleep(0.3)

    return all_articles[:max_total]


# ─────────────────────────────────────────────
# 3) Gemini 요약 (JSON 데이터 파이프라인)
# ─────────────────────────────────────────────
def _strip_code_fence(text: str) -> str:
    """앞뒤 설명/공백이 섞여 있어도 ```...``` 블록 내부 JSON을 안전하게 추출."""
    text = text.strip()
    m = re.search(r"

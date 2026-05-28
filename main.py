"""
일일 AI/디지털 정책 뉴스 리포트 - 네이버 메일 전송 버전
- Google News RSS로 기사 수집 → 정규화 후 SequenceMatcher 중복 제거(60%) → 키워드 필터링 → Gemini 2.5 Flash 요약 → 네이버 메일 전송
"""

import os
import sys
import time
import re
import urllib.parse
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
import requests
import google.generativeai as genai
from difflib import SequenceMatcher
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─────────────────────────────────────────────
# 환경변수 (GitHub Secrets에서 주입)
# ─────────────────────────────────────────────
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
NAVER_ID = os.environ["rla7735"]              # 보내는 사람 네이버 아이디 (예: 'myid', '@naver.com' 제외)
NAVER_APP_PW = os.environ["NAVER_APP_PW"]      # 네이버 2단계 인증 애플리케이션 비밀번호
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"] # 받는 사람 이메일 주소

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
POLITICS_KEYWORDS = ["후보", "공약", "출마", "선거", "의원", "당선", "유세", "국회", "총선", "지선", "대선"]
GOV_KEYWORDS = ["정부", "부처", "시청", "도청", "지자체", "공공", "국가",
               "과학기술정보통신부", "중기부", "산업부"] + REGIONS

# ─────────────────────────────────────────────
# 1) 뉴스 수집 - Google News RSS (한국)
# ─────────────────────────────────────────────
def fetch_news(query: str, limit: int = 20):
    encoded_query = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] RSS 요청 실패 ({query}): {e}", flush=True)
        return []

    items = []
    root = ET.fromstring(resp.content)
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        source = item.find("source")
        source_name = source.text if source is not None else ""
        if title and link:
            items.append({
                "title": title,
                "link": link,
                "pub_date": pub_date,
                "source": source_name,
            })
    return items

# ─────────────────────────────────────────────
# 2) 키워드 필터링 및 중복 제거
# ─────────────────────────────────────────────
def is_relevant(title: str) -> bool:
    if any(k in title for k in POLITICS_KEYWORDS): return False
    if any(k in title for k in ECONOMY_KEYWORDS): return False

    has_gov = any(k in title for k in GOV_KEYWORDS)
    has_filter = any(k in title for k in FILTER_KEYWORDS) or \
                 any(k in title for k in CENTRAL_KEYWORDS)

    if any(k in title for k in EXCLUDE_ORGANIZATIONS) and not has_gov: return False
    if any(k in title for k in CORPORATE_KEYWORDS) and not has_gov: return False

    return has_filter

def clean_title(title: str) -> str:
    title = re.sub(r'\[.*?\]|\(.*?\)|【.*?】', ' ', title)
    title = re.sub(r'[^가-힣a-zA-Z0-9]', '', title)
    return title.lower()

def calculate_title_similarity(title1: str, title2: str) -> float:
    t1 = clean_title(title1)
    t2 = clean_title(title2)
    if not t1 or not t2: return 0.0
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
            
            if is_duplicate: continue
            if not is_relevant(t): continue
                
            all_articles.append(art)
        time.sleep(0.3)

    return all_articles[:max_total]

# ─────────────────────────────────────────────
# 3) Gemini 요약
# ─────────────────────────────────────────────
def summarize_with_gemini(articles: list) -> str:
    if not articles:
        return "오늘 조건에 맞는 기사가 없었습니다."

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash")

    titles_block = "\n".join([f"- {a['title']} ({a['source']})" for a in articles])
    prompt = (
        "다음은 오늘의 AI/디지털 정책 관련 뉴스 제목 목록이다.\n"
        "각 항목을 한국어로 1~2줄로 핵심만 요약해라.\n"
        "가독성을 위해 반드시 각 요약 항목 앞에 '□ 번호.' 기호를 붙여라. (예: □ 1. 제목 및 요약...)\n"
        "정부/지자체 정책 동향과 사업 추진 내용에 초점을 맞춰라.\n"
        "기사 검색 안될 시 추측 금지, 기사에 없는 내용은 만들지 마라.\n\n"
        f"{titles_block}"
    )

    try:
        resp = model.generate_content(prompt)
        return resp.text.strip()
    except Exception as e:
        print(f"[WARN] Gemini 요약 실패: {e}", flush=True)
        return "\n".join([f"□ {i+1}. {a['title']}" for i, a in enumerate(articles)])

# ─────────────────────────────────────────────
# 4) 네이버 메일 전송 로직
# ─────────────────────────────────────────────
def send_naver_mail(subject: str, body: str):
    """네이버 SMTP 서버를 이용해 이메일 전송"""
    smtp_server = "smtp.naver.com"
    smtp_port = 465 # 네이버 SMTP SSL 포트

    # 메일 객체 생성
    msg = MIMEMultipart()
    msg['Subject'] = subject
    msg['From'] = f"{NAVER_ID}@naver.com"
    msg['To'] = RECIPIENT_EMAIL

    # 메일 본문 첨부
    msg.attach(MIMEText(body, 'plain'))

    try:
        # SMTP 서버 연결 및 전송
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(NAVER_ID, NAVER_APP_PW)
            server.send_message(msg)
        print("[OK] 네이버 메일 전송 성공", flush=True)
    except Exception as e:
        print(f"[ERROR] 메일 전송 실패: {e}", flush=True)
        raise

# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    kst = timezone(timedelta(hours=9))
    today = datetime.now(kst).strftime("%Y-%m-%d (%a)")

    print(f"[1/3] 뉴스 수집 시작 - {today}", flush=True)
    articles = collect_filtered_articles(max_total=8)
    print(f"      → 필터 통과 기사 {len(articles)}건", flush=True)

    print("[2/3] Gemini 요약", flush=True)
    summary = summarize_with_gemini(articles)

    print("[3/3] 네이버 메일 전송", flush=True)
    subject = f"📰 {today} AI/디지털 정책 뉴스 리포트"
    
    if articles:
        body = summary + "\n\n[원문 링크]\n" + "\n".join(
            [f"□ {i+1}. {a['link']}" for i, a in enumerate(articles)]
        )
    else:
        body = summary

    send_naver_mail(subject, body)
    print("[DONE] 모든 작업 완료", flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FATAL] {e}", flush=True)
        sys.exit(1)

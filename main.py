# ─────────────────────────────────────────────
# 일일 AI/디지털 정책 뉴스 리포트 - 네이버 메일
# (서버 혼잡 대비 10초 재시도 로직 + 전국 세부 지역명 완벽 적용 버전)
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

# 1. 광역 지자체 (세종 포함)
REGIONS_MAIN = ["서울", "경기", "인천", "강원", "충북", "충남", "전북", "전남",
                "경북", "경남", "부산", "대구", "광주", "대전", "제주", "세종"]

# 2. 전국 세부 시/군 지역명 (160여 개)
REGIONS_SUB = [
    "수원", "성남", "의정부", "안양", "부천", "광명", "평택", "동두천", "안산", "고양", "과천", "구리", "남양주", "오산", "시흥", "군포", "의왕", "하남", "용인", "파주", "이천", "안성", "김포", "화성", "광주", "양주", "포천", "여주", "연천", "가평", "양평", 
    "춘천", "원주", "강릉", "동해", "태백", "속초", "삼척", "홍천", "횡성", "영월", "평창", "정선", "철원", "화천", "양구", "인제", "고성", "양양", 
    "청주", "충주", "제천", "보은", "옥천", "영동", "증평", "진천", "괴산", "음성", "단양", 
    "천안", "공주", "보령", "아산", "서산", "논산", "계룡", "당진", "금산", "부여", "서천", "청양", "홍성", "예산", "태안", 
    "전주", "군산", "익산", "정읍", "남원", "김제", "완주", "진안", "무주", "장수", "임실", "순창", "고창", "부안", 
    "목포", "여수", "순천", "나주", "광양", "담양", "곡성", "구례", "고흥", "보성", "화순", "장흥", "강진", "해남", "영암", "무안", "함평", "영광", "장성", "완도", "진도", "신안", 
    "포항", "경주", "김천", "안동", "구미", "영주", "영천", "상주", "문경", "경산", "군위", "의성", "청송", "영양", "영덕", "청도", "고령", "성주", "칠곡", "예천", "봉화", "울진", "울릉", 
    "창원", "진주", "통영", "사천", "김해", "밀양", "거제", "양산", "의령", "함안", "창녕", "남해", "하동", "산청", "함양", "거창", "합천"
]

# 두 리스트를 합쳐서 최종 REGIONS 생성
REGIONS = REGIONS_MAIN + REGIONS_SUB

FILTER_KEYWORDS = ["AI", "인공지능", "AX", "DX", "로봇", "데이터산업", "산업", "사업", "MOU", "디지털전환"]

ECONOMY_KEYWORDS = ["증시", "주가", "상한가", "호황", "성장", "매수", "나스닥", "코스피",
                    "GDP", "금리", "환율", "실적", "수혜", "전망", "분석", "리포트",
                    "목표가", "강세", "약세", "투자권고", "증권", "외인", "기관"]

CORPORATE_KEYWORDS = ["출시", "선보여", "이벤트", "할인", "사전예약", "공개채용", "업데이트",
                      "이용권", "구독", "신제품", "출장 서비스", "솔루션 공급", "B2B", "CSP",
                      "기술력", "플랫폼", "서비스", "ET톡"]

EXCLUDE_ORGANIZATIONS = ["대학", "대학교", "학교", "학원", "교육", "캠프", "졸업", "입학", "수강", "수료"]

POLITICS_KEYWORDS = ["후보", "공약", "출마", "선거", "의원", "당선", "유세", "국회", "총선", "지선", "대선", "국힘", "민주당", "사설", "트럼프", "백악관", "총리", "성과",
                     "양향자", "하정우", "배경훈", "이재명", "머스크", "업스테이지"]

# 시청, 군청, 구청 등 포괄적 지자체 단어와 확장된 REGIONS 결합
GOV_KEYWORDS = ["정부", "부처", "시청", "도청", "군청", "구청", "지자체", "공공", "국가", "공고",
                "과학기술정보통신부", "과기정통부", "중기부", "산업부"] + REGIONS


# ─────────────────────────────────────────────
# 공통 유틸 - HTML/URL 안전 처리
# ─────────────────────────────────────────────
def safe_url(u: str) -> str:
    try:
        scheme = urlparse(u).scheme.lower()
    except Exception:
        return "#"
    return u if scheme in ("http", "https") else "#"


def esc(s: str) -> str:
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
    queries = CENTRAL_KEYWORDS + ["AI 정부 정책", "디지털전환 사업", "AI 지자체 MOU", "AI 지자체", "지역 인공지능", "AI 도입 시청", "디지털전환 지자체", "스마트시티 AI"]

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
# 3) Gemini 요약 (JSON 데이터 파이프라인)
# ─────────────────────────────────────────────
def _strip_code_fence(text: str) -> str:
    text = text.strip()
    fence = chr(96) * 3 
    pattern = rf"{fence}(?:json)?\s*(.*?)\s*{fence}"
    
    m = re.search(pattern, text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _render_article_div(region, title, source, link, summary):
    return (
        '<div style="margin-bottom: 25px; line-height: 1.6; font-family: \'Malgun Gothic\', sans-serif;">\n'
        f'    <a href="{esc(safe_url(link))}" style="text-decoration: none; font-size: 15px; font-weight: bold; color: #03c75a;">📍 {esc(region)}</a><br>\n'
        f'    <span style="font-weight: bold; font-size: 15px; color: #333;">□ {esc(title)}</span> '
        f'<span style="font-size: 13px; color: #888;">- {esc(source)}</span><br>\n'
        f'    <span style="font-size: 14px; color: #555;">✓ {esc(summary)}</span>\n'
        '</div>\n'
    )


def summarize_with_gemini_to_html(articles: list, today_str: str) -> str:
    header = (
        f"<h2 style='color: #2c3e50;'>📰 {esc(today_str)} AI 언론 동향 뉴스</h2>"
        "<hr style='border: 1px solid #eee; margin-bottom: 20px;'>"
    )

    if not articles:
        return header + "<p>오늘 조건에 맞는 기사가 없었습니다.</p>"

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt_data = [{"index": i, "title": a['title']} for i, a in enumerate(articles)]

    prompt = (
        "다음은 오늘의 뉴스 기사 데이터이다. 각 기사를 분석하여 반드시 아래 지시사항에 따라 **JSON 배열(Array) 형태**로만 답변해라.\n\n"
        "[지시사항]\n"
        "1. \"summary\": 단순히 제목을 반복하지 마라. 기사의 핵심 내용(누가, 무엇을, 어떻게)과 목적(왜 이 사업/정책을 하는지)을 1~2줄로 명확하게 요약해라.\n"
        "2. \"region\": 기사 내용과 관련된 지역명(예: 서울, 충남, 전남 등)을 추출해라. 지자체가 아니거나 명확하지 않으면 \"정부/종합\" 또는 \"전국\"으로 표기해라.\n"
        "3. 다른 말은 절대 덧붙이지 말고 오직 JSON 포맷만 출력해라.\n\n"
        "[입력 데이터]\n"
        f"{json.dumps(prompt_data, ensure_ascii=False)}\n\n"
        "[출력 포맷 예시]\n"
        "[\n"
        "  {\"index\": 0, \"region\": \"충남\", \"summary\": \"산업통상부 주도로 충남 예산군이 AI 로봇 기술 현장 실증을 통해 스마트팜 확산 기반을 다지는 사업이다.\"},\n"
        "  {\"index\": 1, \"region\": \"종합\", \"summary\": \"...\"}\n"
        "]"
    )

    summary_data = None
    max_retries = 3 # 최대 3번까지 재시도

    # ⭐ 10초 대기 재시도 로직 적용
    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
            )
            raw_text = _strip_code_fence(resp.text)
            summary_data = json.loads(raw_text)
            break # 성공하면 즉시 루프 탈출!
            
        except Exception as e:
            print(f"[WARN] Gemini 요약 실패 (시도 {attempt + 1}/{max_retries}): {e}", flush=True)
            if attempt < max_retries - 1:
                print("⏳ 구글 서버 혼잡! 10초 후 다시 시도합니다...", flush=True)
                time.sleep(10) # 10초 대기 후 다시 시도
            else:
                print("[ERROR] 최대 재시도 횟수를 초과했습니다. 요약 없이 원문만 전송합니다.", flush=True)

    # 3번 다 실패해서 summary_data가 여전히 None일 경우 (기존의 Fallback 로직)
    if not summary_data:
        body = ""
        for art in articles:
            body += (
                '<div style="margin-bottom: 20px;">\n'
                f'    <a href="{esc(safe_url(art["link"]))}" style="text-decoration: none; font-weight: bold; color: #03c75a;">📍 원문보기</a><br>\n'
                f'    <span style="font-weight: bold;">□ {esc(art["title"])}</span> - {esc(art["source"])}<br>\n'
                '    <span>✓ 일시적인 AI 서버 혼잡으로 요약을 제공할 수 없습니다.</span>\n'
                '</div>\n'
            )
        return header + body

    # 성공적으로 요약을 받아왔을 경우
    summary_by_idx = {}
    for item in summary_data:
        idx = item.get("index")
        if isinstance(idx, int) and 0 <= idx < len(articles):
            summary_by_idx[idx] = {
                "region": item.get("region", "종합"),
                "summary": item.get("summary", "요약 없음"),
            }

    body = ""
    for i, art in enumerate(articles):
        info = summary_by_idx.get(i, {"region": "종합", "summary": "요약을 생성하지 못했습니다."})
        body += _render_article_div(
            info["region"], art["title"], art["source"], art["link"], info["summary"]
        )
    return header + body


# ─────────────────────────────────────────────
# 4) 네이버 메일 전송 로직 (HTML 적용)
# ─────────────────────────────────────────────
def send_naver_mail(subject: str, html_body: str):
    smtp_server = "smtp.naver.com"
    smtp_port = 465

    msg = MIMEMultipart("alternative")
    msg['Subject'] = subject
    msg['From'] = f"{NAVER_ID}@naver.com"
    msg['To'] = RECIPIENT_EMAIL

    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    try:
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
    today_str = datetime.now(kst).strftime("%Y-%m-%d (%a)")

    print(f"[1/3] 뉴스 수집 시작 - {today_str}", flush=True)
    articles = collect_filtered_articles(max_total=8)
    print(f"      → 필터 통과 기사 {len(articles)}건", flush=True)

    print("[2/3] Gemini 요약 및 HTML 렌더링", flush=True)
    html_output = summarize_with_gemini_to_html(articles, today_str)

    print("[3/3] 네이버 메일 전송", flush=True)
    subject = f"📰 {today_str} AI 언론 동향 뉴스 리포트"
    send_naver_mail(subject, html_output)

    print("[DONE] 모든 작업 완료", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FATAL] {e}", flush=True)
        sys.exit(1)

# ─────────────────────────────────────────────
# 일일 관심 종목 및 산업 동향 뉴스 리포트 (stock_main.py)
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
NAVER_ID = os.environ.get("NAVER_ID", "").strip()
NAVER_APP_PW = os.environ.get("NAVER_APP_PW", "").strip()
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "").strip()

# ─────────────────────────────────────────────
# 검색 키워드 정의
# ─────────────────────────────────────────────
# 1. 1순위: 관심 주식 종목 및 기업
TARGET_COMPANIES = [
    "브로드컴", "TSMC", "솔리드파워", "현대차", 
    "크래프톤", "DSC인베스트먼트", "알파벳", "구글", "퓨리오사AI"
]

# 2. 2순위: 종목 뉴스가 부족할 때 채울 관심 산업 분야
INDUSTRY_KEYWORDS = [
    "전력 효율 반도체", "데이터센터 전력", "AI 전력 인프라", "저전력 AI 반도체"
]

# 차단 키워드 (정치 스캔들, 범죄 등 주식과 무관한 노이즈 기사 차단용)
NOISE_KEYWORDS = ["살인", "폭행", "구속", "마약", "선거법", "음주운전"]

# ─────────────────────────────────────────────
# 공통 유틸
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
def fetch_news(query: str, limit: int = 5, retries: int = 2):
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
# 2) 키워드 필터링 및 중복 제거 로직
# ─────────────────────────────────────────────
def is_noise(title: str) -> bool:
    # 주식/산업 동향과 무관한 사건사고 뉴스 필터링
    if any(k in title for k in NOISE_KEYWORDS): return True
    return False

def clean_title(title: str) -> str:
    title = re.sub(r'\[.*?\]|\(.*?\)|【.*?】', ' ', title)
    title = re.sub(r'[^가-힣a-zA-Z0-9]', '', title)
    return title.lower()

def calculate_title_similarity(title1: str, title2: str) -> float:
    t1 = clean_title(title1)
    t2 = clean_title(title2)
    if not t1 or not t2: return 0.0
    return SequenceMatcher(None, t1, t2).ratio()

def collect_filtered_articles(target_total: int = 10):
    all_articles = []
    
    # [Step 1] 관심 주식 종목 뉴스 우선 수집
    print("👉 관심 기업 뉴스 수집 중...")
    for company in TARGET_COMPANIES:
        # 각 종목당 최신 3개씩 가져와서 검사
        for art in fetch_news(company, limit=3):
            if is_noise(art["title"]): continue
            
            # 중복 검사
            is_duplicate = any(calculate_title_similarity(art["title"], existing["title"]) >= 0.6 for existing in all_articles)
            if not is_duplicate:
                all_articles.append(art)
        time.sleep(0.3)

    # [Step 2] 종목 뉴스가 목표치(target_total)보다 적을 경우, 산업 뉴스 추가 수집
    if len(all_articles) < target_total:
        print(f"👉 종목 뉴스가 {len(all_articles)}건으로 부족하여 산업 동향 뉴스 추가 수집...")
        for keyword in INDUSTRY_KEYWORDS:
            for art in fetch_news(keyword, limit=4):
                if is_noise(art["title"]): continue
                
                # 중복 검사
                is_duplicate = any(calculate_title_similarity(art["title"], existing["title"]) >= 0.6 for existing in all_articles)
                if not is_duplicate:
                    all_articles.append(art)
            time.sleep(0.3)
            
            if len(all_articles) >= target_total:
                break

    # 최신성을 위해 상위 target_total 개수만 자름
    return all_articles[:target_total]

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

def _render_article_div(topic, title, source, link, summary):
    return (
        '<div style="margin-bottom: 25px; line-height: 1.6; font-family: \'Malgun Gothic\', sans-serif;">\n'
        f'    <a href="{esc(safe_url(link))}" style="text-decoration: none; font-size: 14px; font-weight: bold; color: #3b5998; background-color: #eef2f5; padding: 3px 8px; border-radius: 4px;">📈 {esc(topic)}</a><br>\n'
        f'    <span style="font-weight: bold; font-size: 16px; color: #333; display: inline-block; margin-top: 8px;">📌 {esc(title)}</span> '
        f'<span style="font-size: 13px; color: #888;">- {esc(source)}</span><br>\n'
        f'    <span style="font-size: 14px; color: #555;">💡 {esc(summary)}</span>\n'
        '</div>\n'
    )

def summarize_with_gemini_to_html(articles: list, today_str: str) -> str:
    header = (
        f"<h2 style='color: #2c3e50;'>💼 {esc(today_str)} 관심 주식 및 산업 동향 리포트</h2>"
        "<hr style='border: 1px solid #eee; margin-bottom: 20px;'>"
    )

    if not articles:
        return header + "<p>오늘 수집된 관련 기사가 없습니다.</p>"

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt_data = [{"index": i, "title": a['title']} for i, a in enumerate(articles)]

    prompt = (
        "다음은 오늘의 주식 및 비즈니스 관련 뉴스 기사 제목들이다. 각 기사를 분석하여 반드시 아래 지시사항에 따라 **JSON 배열(Array) 형태**로만 답변해라.\n\n"
        "[지시사항]\n"
        "1. \"summary\": 기사의 핵심 내용(기업의 실적, 신제품, M&A, 기술 개발, 호재 및 악재 등 비즈니스 임팩트)을 1~2줄로 명확하게 요약해라.\n"
        "2. \"topic\": 기사가 다루는 '핵심 기업명' 또는 '핵심 산업/테마'(예: 브로드컴, 데이터센터 전력, 현대차 등)를 추출해라.\n"
        "3. 다른 말은 절대 덧붙이지 말고 오직 JSON 포맷만 출력해라.\n\n"
        "[입력 데이터]\n"
        f"{json.dumps(prompt_data, ensure_ascii=False)}\n\n"
        "[출력 포맷 예시]\n"
        "[\n"
        "  {\"index\": 0, \"topic\": \"브로드컴\", \"summary\": \"브로드컴이 AI 반도체 수요 증가에 힘입어 1분기 어닝 서프라이즈를 기록하며 주가가 상승세를 보임.\"},\n"
        "  {\"index\": 1, \"topic\": \"데이터센터 전력\", \"summary\": \"...\"}\n"
        "]"
    )

    summary_data = None
    max_retries = 3

    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
            )
            raw_text = _strip_code_fence(resp.text)
            summary_data = json.loads(raw_text)
            break 
            
        except Exception as e:
            print(f"[WARN] Gemini 요약 실패 (시도 {attempt + 1}/{max_retries}): {e}", flush=True)
            if attempt < max_retries - 1:
                print("⏳ 구글 서버 혼잡! 10초 후 다시 시도합니다...", flush=True)
                time.sleep(10)
            else:
                print("[ERROR] 최대 재시도 횟수를 초과했습니다.", flush=True)

    if not summary_data:
        body = ""
        for art in articles:
            body += (
                '<div style="margin-bottom: 20px;">\n'
                f'    <a href="{esc(safe_url(art["link"]))}" style="text-decoration: none; font-weight: bold; color: #3b5998;">📍 원문보기</a><br>\n'
                f'    <span style="font-weight: bold;">□ {esc(art["title"])}</span> - {esc(art["source"])}<br>\n'
                '    <span>✓ 일시적인 AI 서버 혼잡으로 요약을 제공할 수 없습니다.</span>\n'
                '</div>\n'
            )
        return header + body

    summary_by_idx = {}
    for item in summary_data:
        idx = item.get("index")
        if isinstance(idx, int) and 0 <= idx < len(articles):
            summary_by_idx[idx] = {
                "topic": item.get("topic", "종합 증시"),
                "summary": item.get("summary", "요약 없음"),
            }

    body = ""
    for i, art in enumerate(articles):
        info = summary_by_idx.get(i, {"topic": "종합", "summary": "요약을 생성하지 못했습니다."})
        body += _render_article_div(
            info["topic"], art["title"], art["source"], art["link"], info["summary"]
        )
    return header + body

# ─────────────────────────────────────────────
# 4) 네이버 메일 전송 로직
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
# 메인 함수
# ─────────────────────────────────────────────
def main():
    kst = timezone(timedelta(hours=9))
    today_str = datetime.now(kst).strftime("%Y-%m-%d (%a)")

    print(f"[1/3] 주식/산업 뉴스 수집 시작 - {today_str}", flush=True)
    # 목표 기사 수를 10개로 설정 (필요에 따라 조절 가능)
    articles = collect_filtered_articles(target_total=10)
    print(f"      → 최종 수집 기사 {len(articles)}건", flush=True)

    print("[2/3] Gemini 요약 및 HTML 렌더링", flush=True)
    html_output = summarize_with_gemini_to_html(articles, today_str)

    print("[3/3] 네이버 메일 전송", flush=True)
    subject = f"💼 {today_str} 관심 종목 및 산업 동향 리포트"
    send_naver_mail(subject, html_output)

    print("[DONE] 모든 작업 완료", flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FATAL] {e}", flush=True)
        sys.exit(1)

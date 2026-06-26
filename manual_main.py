# ─────────────────────────────────────────────
# 수동 큐레이션 AI 정책 뉴스 리포트 - 네이버 메일
# (작성란 8개 기본 제공 및 빈칸 자동 무시 기능 추가)
# ─────────────────────────────────────────────

import os
import sys
import time
import html
import json
from datetime import datetime, timezone, timedelta
from google import genai
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
# 📝 매일 이곳에 원하는 기사를 입력하세요! (최대 8개)
# ─────────────────────────────────────────────
# 주의: 따옴표("") 안의 [글자]만 지우고 내용을 붙여넣으세요.
# 남는 칸은 지울 필요 없이 그대로 두면 코드가 알아서 무시합니다!

MY_NEWS_LIST = [
    {
        "region": "서울",
        "title": "카카오, 4대 과기원과 청소년 유니콘 창업가 발굴한다",
        "link": "http://www.asiae.co.kr/news/view.htm?idxno=2026062609392002381",
        "source": "아시아경제"
    },
    {
        "region": "인천",
        "title": "인천공항공사, AI-PORT 조성 본격 시동",
        "link": "https://www.sisaone.kr/news/article.html?no=2262469",
        "source": "시사1"
    },
    {
        "region": "충청남도",
        "title": "충남TP, ‘충남 AX혁신위원회’ 출범…1조원 규모 AI 대전환 사업 본격 시동",
        "link": "https://www.dnews.co.kr/uhtml/view.jsp?idxno=202606251023549940726",
        "source": "대한경제"
    },
    {
        "region": "전라북도",
        "title": "전북교육청, AI·디지털 과학탐구 환경 조성…121개교 선정·지원",
        "link": "https://www.news1.kr/local/jeonbuk/6208458",
        "source": "뉴스1"
    },
    {
        "region": "경상북도",
        "title": "구미시, 산업AI 에이전트 공모사업 선정…제조현장 물류혁신 본격화",
        "link": "https://www.thebestnews.kr/news/articleView.html?idxno=448454",
        "source": "더최고신문"
    },
    {
        "region": "[지역]",
        "title": "[제목]",
        "link": "[링크]",
        "source": "[언론사]"
    },
    {
        "region": "[지역]",
        "title": "[제목]",
        "link": "[링크]",
        "source": "[언론사]"
    },
    {
        "region": "[지역]",
        "title": "[제목]",
        "link": "[링크]",
        "source": "[언론사]"
    }
]

# ─────────────────────────────────────────────
# 공통 유틸 - HTML 안전 처리
# ─────────────────────────────────────────────
def esc(s: str) -> str:
    return html.escape(s or "", quote=True)

# ─────────────────────────────────────────────
# Gemini 요약 및 HTML 렌더링
# ─────────────────────────────────────────────
def _strip_code_fence(text: str) -> str:
    text = text.strip()
    fence = chr(96) * 3 
    import re
    pattern = rf"{fence}(?:json)?\s*(.*?)\s*{fence}"
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else text

def _render_article_div(region, title, source, link, summary):
    return (
        '<div style="margin-bottom: 25px; line-height: 1.6; font-family: \'Malgun Gothic\', sans-serif;">\n'
        f'    <a href="{esc(link)}" style="text-decoration: none; font-size: 15px; font-weight: bold; color: #03c75a;">📍 {esc(region)}</a><br>\n'
        f'    <span style="font-weight: bold; font-size: 15px; color: #333;">📌 {esc(title)}</span> '
        f'<span style="font-size: 13px; color: #888;">- {esc(source)}</span><br>\n'
        f'    <span style="font-size: 14px; color: #555;">✓ {esc(summary)}</span>\n'
        '</div>\n'
    )

def summarize_and_build_html(articles: list, today_str: str) -> str:
    header = (
        f"<h2 style='color: #2c3e50;'>📰 {esc(today_str)} AI 언론 동향 뉴스</h2>"
        "<hr style='border: 1px solid #eee; margin-bottom: 20px;'>"
    )

    if not articles:
        return header + "<p>오늘 입력된 기사가 없습니다.</p>"

    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt_data = [{"index": i, "title": a['title']} for i, a in enumerate(articles)]
    
    prompt = (
        "다음은 오늘의 뉴스 기사 제목이다. 각 기사 제목을 검색하여 반드시 아래 지시사항에 따라 **JSON 배열(Array) 형태**로만 답변해라.\n\n"
        "[지시사항]\n"
        "1. \"summary\": 기사 검색 내용을 바탕으로 핵심 내용(누가, 무엇을, 어떻게)과 목적을 1~2줄로 명확하게 요약해라.\n"
        "2. 다른 말은 절대 덧붙이지 말고 오직 JSON 포맷만 출력해라.\n\n"
        "[입력 데이터]\n"
        f"{json.dumps(prompt_data, ensure_ascii=False)}\n\n"
        "[출력 포맷 예시]\n"
        "[\n"
        "  {\"index\": 0, \"summary\": \"충남도가 삼성전자와 협력하여 지역 내 AI 전문 인재를 양성하기 위한 업무협약을 맺었다.\"},\n"
        "  {\"index\": 1, \"summary\": \"...\"}\n"
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
                time.sleep(30) 
            else:
                print("[ERROR] 요약 실패. 원문만 전송합니다.", flush=True)

    if not summary_data:
        body = ""
        for art in articles:
            body += _render_article_div(
                art["region"], art["title"], art["source"], art["link"], 
                "일시적인 AI 서버 혼잡으로 요약을 제공할 수 없습니다."
            )
        return header + body

    summary_by_idx = {}
    for item in summary_data:
        idx = item.get("index")
        if isinstance(idx, int) and 0 <= idx < len(articles):
            summary_by_idx[idx] = item.get("summary", "요약 없음")

    body = ""
    for i, art in enumerate(articles):
        summary_text = summary_by_idx.get(i, "요약을 생성하지 못했습니다.")
        body += _render_article_div(
            art["region"], art["title"], art["source"], art["link"], summary_text
        )
    return header + body

# ─────────────────────────────────────────────
# 네이버 메일 전송 로직 
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

    # ⭐ 사용자가 입력한 기사 중 '[제목]' 상태인 빈칸은 자동으로 무시하는 필터링 로직!
    valid_articles = []
    for art in MY_NEWS_LIST:
        title = art.get("title", "").strip()
        if title and title != "[제목]":
            valid_articles.append(art)

    print(f"[1/2] 수동 입력 기사 확인 - 총 {len(valid_articles)}건 (빈칸 제외)", flush=True)

    if not valid_articles:
        print("[INFO] 입력된 기사가 없어 메일 전송을 생략합니다.")
        return

    print("[2/2] Gemini 요약 및 메일 전송", flush=True)
    html_output = summarize_and_build_html(valid_articles, today_str)

    subject = f"📰 {today_str} AI 언론 동향 뉴스 리포트"
    send_naver_mail(subject, html_output)
    print("[DONE] 모든 작업 완료", flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FATAL] {e}", flush=True)
        sys.exit(1)

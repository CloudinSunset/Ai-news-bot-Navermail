"""
일일 AI/디지털 정책 뉴스 리포트 - 카카오톡 "나에게 보내기" 버전
- Google News RSS로 기사 수집 → 키워드 필터링 → Gemini 2.5 Flash 요약 → 카카오톡 전송
- 카카오 access_token은 6시간 만료라 매 실행마다 refresh_token으로 재발급
- refresh_token이 갱신되면 GitHub Secret(KAKAO_REFRESH_TOKEN)에 자동 업데이트
"""

import os
import sys
import json
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
import requests
import google.generativeai as genai

# ─────────────────────────────────────────────
# 환경변수 (GitHub Secrets에서 주입)
# ─────────────────────────────────────────────
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
KAKAO_REST_API_KEY = os.environ["KAKAO_REST_API_KEY"]
KAKAO_REFRESH_TOKEN = os.environ["KAKAO_REFRESH_TOKEN"]

# refresh_token 갱신 시 GitHub Secret 자동 업데이트용 (선택, 권장)
GH_PAT = os.environ.get("GH_PAT", "")          # repo 권한의 PAT
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "")  # owner/repo (Actions가 자동 주입)

# ─────────────────────────────────────────────
# 키워드 정의 (사용자 요청 그대로 유지)
# ─────────────────────────────────────────────
CENTRAL_KEYWORDS = ["AI 정책", "데이터센터", "양자컴퓨팅", "디지털전환", "인공지능 전략"]
REGIONS = ["서울", "경기", "인천", "강원", "충북", "충남", "전북", "전남",
           "경북", "경남", "부산", "대구", "광주", "대전", "제주"]
FILTER_KEYWORDS = ["AI", "인공지능", "AX", "DX", "로봇", "데이터산업", "산업", "사업", "MOU", "디지털전환"]

# 절대 제외: 경제/주식
ECONOMY_KEYWORDS = ["증시", "주가", "상한가", "호황", "성장", "매수", "나스닥", "코스피",
                    "GDP", "금리", "환율", "실적", "수혜", "전망", "분석", "리포트",
                    "목표가", "강세", "약세", "투자권고", "증권", "외인", "기관"]

# 정부/지자체 키워드 없으면 제외: 기업 홍보성
CORPORATE_KEYWORDS = ["출시", "선보여", "이벤트", "할인", "사전예약", "공개채용", "업데이트",
                     "이용권", "구독", "신제품", "출장 서비스", "솔루션 공급", "B2B", "CSP",
                     "공모", "기술력", "플랫폼", "서비스"]

# 정부/지자체 키워드 없으면 제외: 교육/기관
EXCLUDE_ORGANIZATIONS = ["대학", "대학교", "학교", "학원", "교육", "캠프", "졸업", "입학", "수강", "수료"]

# 절대 제외: 정치/선거
POLITICS_KEYWORDS = ["후보", "공약", "출마", "선거", "의원", "당선", "유세", "국회", "총선", "지선", "대선"]

# 공공성 판단용
GOV_KEYWORDS = ["정부", "부처", "시청", "도청", "지자체", "공공", "국가",
               "과학기술정보통신부", "중기부", "산업부"] + REGIONS


# ─────────────────────────────────────────────
# 1) 뉴스 수집 - Google News RSS (한국)
# ─────────────────────────────────────────────
def fetch_news(query: str, limit: int = 20):
    """Google News RSS에서 한국어 기사 수집."""
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
# 2) 키워드 필터링
# ─────────────────────────────────────────────
def is_relevant(title: str) -> bool:
    """제목 기준으로 관련 기사인지 판정."""

    # 절대 제외 1순위: 정치/선거
    if any(k in title for k in POLITICS_KEYWORDS):
        return False

    # 절대 제외 2순위: 경제/주식
    if any(k in title for k in ECONOMY_KEYWORDS):
        return False

    has_gov = any(k in title for k in GOV_KEYWORDS)
    has_filter = any(k in title for k in FILTER_KEYWORDS) or \
                 any(k in title for k in CENTRAL_KEYWORDS)

    # 교육/기관 - 정부/지자체 단어 없으면 제외
    if any(k in title for k in EXCLUDE_ORGANIZATIONS) and not has_gov:
        return False

    # 기업 홍보성 - 정부/지자체 단어 없으면 제외
    if any(k in title for k in CORPORATE_KEYWORDS) and not has_gov:
        return False

    # 최소한 필터 키워드 또는 중앙 키워드 1개 이상 포함되어야 함
    return has_filter


def collect_filtered_articles(max_total: int = 8):
    """모든 키워드로 검색 → 제목 중복 제거 → 필터링 → 상위 N개."""
    all_articles = []
    seen_titles = set()

    queries = CENTRAL_KEYWORDS + ["AI 정부 정책", "디지털전환 사업", "AI 지자체 MOU"]

    for q in queries:
        for art in fetch_news(q, limit=15):
            t = art["title"]
            # 단순 중복 제거 (제목 앞 30자 기준)
            key = t[:30]
            if key in seen_titles:
                continue
            if not is_relevant(t):
                continue
            seen_titles.add(key)
            all_articles.append(art)
        time.sleep(0.3)  # RSS 과다요청 방지

    return all_articles[:max_total]


# ─────────────────────────────────────────────
# 3) Gemini 요약
# ─────────────────────────────────────────────
def summarize_with_gemini(articles: list) -> str:
    """기사 목록을 Gemini 2.5 Flash로 요약."""
    if not articles:
        return "오늘 조건에 맞는 기사가 없었습니다."

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash")

    titles_block = "\n".join([f"- {a['title']} ({a['source']})" for a in articles])
    prompt = (
        "다음은 오늘의 AI/디지털 정책 관련 뉴스 제목 목록이다.\n"
        "각 항목을 한국어로 1~2줄로 핵심만 요약하고, 번호를 매겨라.\n"
        "정부/지자체 정책 동향과 사업 추진 내용에 초점을 맞춰라.\n"
        "추측 금지, 제목에 없는 내용은 만들지 마라.\n\n"
        f"{titles_block}"
    )

    try:
        resp = model.generate_content(prompt)
        return resp.text.strip()
    except Exception as e:
        print(f"[WARN] Gemini 요약 실패: {e}", flush=True)
        # 실패 시 제목만이라도 전달
        return "\n".join([f"{i+1}. {a['title']}" for i, a in enumerate(articles)])


# ─────────────────────────────────────────────
# 4) 카카오톡 토큰 갱신
# ─────────────────────────────────────────────
def refresh_kakao_token():
    """refresh_token으로 access_token 재발급. 새 refresh_token이 오면 함께 반환."""
    url = "https://kauth.kakao.com/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": KAKAO_REST_API_KEY,
        "refresh_token": KAKAO_REFRESH_TOKEN,
    }
    resp = requests.post(url, data=data, timeout=10)
    resp.raise_for_status()
    j = resp.json()

    access_token = j["access_token"]
    # 카카오는 refresh_token 만료 1개월 이내일 때만 새 refresh_token을 함께 발급
    new_refresh = j.get("refresh_token")
    return access_token, new_refresh


# ─────────────────────────────────────────────
# 5) 카카오톡 "나에게 보내기"
# ─────────────────────────────────────────────
def send_kakao_message(access_token: str, text: str, link_url: str = "https://news.google.com"):
    """카카오톡 메시지 API. 텍스트 1,000자 제한이라 길면 분할 전송."""
    url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
    headers = {"Authorization": f"Bearer {access_token}"}

    # 1,000자 제한 → 안전하게 900자 단위로 분할
    chunks = [text[i:i+900] for i in range(0, len(text), 900)] or [""]

    for idx, chunk in enumerate(chunks, 1):
        prefix = f"[{idx}/{len(chunks)}]\n" if len(chunks) > 1 else ""
        template = {
            "object_type": "text",
            "text": prefix + chunk,
            "link": {"web_url": link_url, "mobile_web_url": link_url},
            "button_title": "뉴스 더보기",
        }
        data = {"template_object": json.dumps(template, ensure_ascii=False)}
        resp = requests.post(url, headers=headers, data=data, timeout=10)
        if resp.status_code != 200:
            print(f"[ERROR] 카카오 전송 실패: {resp.status_code} {resp.text}", flush=True)
            resp.raise_for_status()
        time.sleep(0.5)


# ─────────────────────────────────────────────
# 6) refresh_token 자동 업데이트 (GitHub Secret)
# ─────────────────────────────────────────────
def update_github_secret(new_refresh_token: str):
    """
    카카오가 새 refresh_token을 발급하면 GitHub Secret을 자동 갱신.
    GH_PAT(repo 권한)와 GITHUB_REPOSITORY 환경변수가 있을 때만 동작.
    """
    if not GH_PAT or not GH_REPO:
        print("[INFO] GH_PAT 미설정 - refresh_token 수동 갱신 필요", flush=True)
        return

    try:
        from nacl import encoding, public  # PyNaCl
    except ImportError:
        print("[WARN] pynacl 미설치 - refresh_token 자동갱신 건너뜀", flush=True)
        return

    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
    }

    # 1) 공개키 조회
    pk_url = f"https://api.github.com/repos/{GH_REPO}/actions/secrets/public-key"
    pk = requests.get(pk_url, headers=headers, timeout=10).json()

    # 2) 암호화
    pkey = public.PublicKey(pk["key"].encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pkey).encrypt(new_refresh_token.encode("utf-8"))
    encrypted_value = encoding.Base64Encoder().encode(sealed).decode("utf-8")

    # 3) Secret 업데이트
    put_url = f"https://api.github.com/repos/{GH_REPO}/actions/secrets/KAKAO_REFRESH_TOKEN"
    body = {"encrypted_value": encrypted_value, "key_id": pk["key_id"]}
    r = requests.put(put_url, headers=headers, json=body, timeout=10)
    if r.status_code in (201, 204):
        print("[OK] KAKAO_REFRESH_TOKEN 갱신됨", flush=True)
    else:
        print(f"[WARN] Secret 업데이트 실패: {r.status_code} {r.text}", flush=True)


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    kst = timezone(timedelta(hours=9))
    today = datetime.now(kst).strftime("%Y-%m-%d (%a)")

    print(f"[1/4] 뉴스 수집 시작 - {today}", flush=True)
    articles = collect_filtered_articles(max_total=8)
    print(f"      → 필터 통과 기사 {len(articles)}건", flush=True)

    print("[2/4] Gemini 요약", flush=True)
    summary = summarize_with_gemini(articles)

    print("[3/4] 카카오 토큰 갱신", flush=True)
    access_token, new_refresh = refresh_kakao_token()
    if new_refresh and new_refresh != KAKAO_REFRESH_TOKEN:
        update_github_secret(new_refresh)

    print("[4/4] 카카오톡 전송", flush=True)
    header = f"📰 {today} AI/디지털 정책 뉴스\n" + "─" * 20 + "\n"
    if articles:
        body = summary + "\n\n[원문]\n" + "\n".join(
            [f"{i+1}. {a['link']}" for i, a in enumerate(articles)]
        )
    else:
        body = summary

    send_kakao_message(access_token, header + body)
    print("[DONE] 전송 완료", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FATAL] {e}", flush=True)
        sys.exit(1)

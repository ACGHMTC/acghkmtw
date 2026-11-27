import html as html_lib
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()

DATA_PATH = "data/events.json"
HKT = timezone(timedelta(hours=8))

# 1. Google News 關鍵字搜尋
RSS_QUERIES = [
    ("香港", "香港 (同人 OR Rainbow Gala OR RG OR 動漫節) -股價 -財報"),
    ("香港", "香港 (快閃店 OR 期間限定店 OR 展覽) (動漫 OR 遊戲) -股價 -財報"),
    ("澳門", "澳門 (動漫 OR 遊戲 OR 同人 OR 漫展) -股價 -財報"),
    ("台灣", "台灣 (CWT OR FF OR 開拓動漫祭 OR 漫博) -股價 -財報"),
    ("深圳", "深圳 (漫展 OR 動漫展 OR 遊戲展 OR 同人展) -股價 -財報"),
]

# 2. 官方 RSS 來源
DIRECT_RSS_FEEDS = [
    ("台灣", "https://gnn.gamer.com.tw/rss.xml?k=16"),
]


def get_web_sources():
    """動態產生包含當前月份與下個月的網頁活動來源清單"""
    now = datetime.now(HKT)
    ym_curr = now.strftime("%Y%m")
    # 計算下個月的 YYYYMM
    next_month_date = (now.replace(day=28) + timedelta(days=4))
    ym_next = next_month_date.strftime("%Y%m")

    sources = [
        ("香港", "DoujinArk", "https://doujinark.hk/event"),
        ("香港", "AniHK", "https://anihk.com/events/region/hk"),
        ("台灣", "AniHK", "https://anihk.com/events/region/tw"),
        ("澳門", "HappyMacao", "https://event.happymacao.com/cms62/e/action/ListInfo/?classid=76"),
        ("澳門", "澳門旅遊局", f"https://www.macaotourism.gov.mo/zh-hant/events/whatson?month={ym_curr}"),
    ]
    if ym_next != ym_curr:
        sources.append(
            ("澳門", "澳門旅遊局", f"https://www.macaotourism.gov.mo/zh-hant/events/whatson?month={ym_next}")
        )
    return sources


class EventLinkParser(HTMLParser):
    """從活動列表 HTML 中擷取包含活動標題與連結的候選區塊"""

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.in_script_or_style = False
        self.current_href = None
        self.current_text = []
        self.candidates = []

    def handle_starttag(self, tag, attrs):
        tag_l = tag.lower()
        if tag_l in ("script", "style", "noscript", "svg"):
            self.in_script_or_style = True
            return
        if tag_l == "a":
            attr_dict = dict(attrs)
            href = (attr_dict.get("href") or "").strip()
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self.current_href = urllib.parse.urljoin(self.base_url, href)
                self.current_text = []

    def handle_endtag(self, tag):
        tag_l = tag.lower()
        if tag_l in ("script", "style", "noscript", "svg"):
            self.in_script_or_style = False
            return
        if tag_l == "a" and self.current_href:
            raw_text = " ".join(self.current_text)
            clean_text = re.sub(r"\s+", " ", html_lib.unescape(raw_text)).strip()
            if len(clean_text) >= 6:
                self.candidates.append((self.current_href, clean_text))
            self.current_href = None
            self.current_text = []

    def handle_data(self, data):
        if self.in_script_or_style:
            return
        if self.current_href is not None:
            self.current_text.append(data)


def fetch_url_bytes(url: str, timeout: int = 18) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "zh-TW,zh-HK;q=0.9,zh;q=0.8,en;q=0.7",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def decode_html_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "big5", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def parse_web_events(region_hint: str, source_name: str, url: str, limit: int = 8):
    items = []
    try:
        raw_bytes = fetch_url_bytes(url)
        html_text = decode_html_bytes(raw_bytes)
        parser = EventLinkParser(url)
        parser.feed(html_text)

        # 過濾常見導覽列/頁尾無關字眼
        ignore_words = (
            "登入", "註冊", "私隱", "隱私", "條款", "聯絡我們", "關於我們",
            "首頁", "下一頁", "上一頁", "更多", "語言", "繁體", "简体", "English",
            "Facebook", "Instagram", "Twitter", "YouTube", "Cookie"
        )
        seen_local = set()
        for link, text in parser.candidates:
            if link == url or link in seen_local:
                continue
            if any(w.lower() == text.lower() for w in ignore_words) or len(text) > 160:
                continue
            seen_local.add(link)
            items.append(
                {
                    "region_hint": region_hint,
                    "source": source_name,
                    "title": text[:100],
                    "link": link,
                    "pub_date": datetime.now(HKT).strftime("%Y-%m-%d"),
                    "desc": f"來源網站：{source_name}｜擷取文字：{text}",
                }
            )
            if len(items) >= limit:
                break
    except Exception as e:
        print(f"[Web Warn] {source_name} ({url}): {e}")
    return items


def parse_rss_url(url: str, region_hint: str, source_name: str = "RSS", limit: int = 5):
    items = []
    try:
        xml_data = fetch_url_bytes(url, timeout=15)
        root = ET.fromstring(xml_data)
        for item in root.findall(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            desc = (item.findtext("description") or "").strip()
            clean_desc = re.sub(r"<[^>]+>", " ", html_lib.unescape(desc))
            clean_desc = re.sub(r"\s+", " ", clean_desc).strip()[:200]
            if title and link:
                items.append(
                    {
                        "region_hint": region_hint,
                        "source": source_name,
                        "title": title,
                        "link": link,
                        "pub_date": pub_date,
                        "desc": clean_desc,
                    }
                )
    except Exception as e:
        print(f"[RSS Warn] {url}: {e}")
    return items


def fetch_rss_items(region_hint: str, query: str, limit: int = 3):
    encoded_q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_q}&hl=zh-TW&gl=HK&ceid=HK:zh-Hant"
    return parse_rss_url(url, region_hint, source_name="GoogleNews", limit=limit)


def call_cloudflare_ai(prompt: str) -> str:
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        raise RuntimeError("缺少 CF_ACCOUNT_ID 或 CF_API_TOKEN 環境變數")

    model = "@cf/qwen/qwen2.5-7b-instruct"
    endpoint = (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{model}"
    )
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是香港、澳門、台灣、深圳動漫、遊戲、同人與ACG文化活動情報整理助手。"
                    "請判斷輸入資訊是否為明確的『香港、澳門、台灣或深圳』線下活動、漫展、同人場、遊戲活動、IP快閃店或大型流行文化展覽。"
                    "若為日本/歐美活動、純網頁導覽按鈕、純線上新聞或資訊過於模糊，請只回覆 null。"
                    "若是明確的四地線下活動，請只輸出一個合法 JSON 物件（不要 markdown 包裹，不要多餘解釋），欄位如下："
                    '{"region":"香港|澳門|台灣|深圳",'
                    '"category":"同人即賣會|綜合漫展|遊戲與快閃",'
                    '"title":"乾淨的活動名稱（繁體中文，去掉多餘年份前綴或網站名）",'
                    '"start_date":"YYYY-MM-DD（若無明確日期請留空字串）",'
                    '"end_date":"YYYY-MM-DD",'
                    '"venue":"場地或地區",'
                    '"deadline_note":"重要提醒、售票或報名資訊",'
                    '"tags":["標籤1","標籤2"]}'
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 400,
        "temperature": 0.15,
    }

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    if not result.get("success"):
        raise RuntimeError(f"Cloudflare AI API 失敗: {result}")

    ai_out = result.get("result", {})
    if isinstance(ai_out, dict):
        return (ai_out.get("response") or "").strip()
    return str(ai_out).strip()


def extract_json_object(text: str):
    cleaned = text.strip()
    if not cleaned or cleaned.lower() == "null":
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re

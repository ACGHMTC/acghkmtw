import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()

DATA_PATH = "data/events.json"

# 可自由增減的公開 RSS 來源（初期先用 Google News RSS 做四地關鍵字搜尋，穩定且免登入）
RSS_QUERIES = [
    ("香港", "香港 同人 動漫 活動"),
    ("香港", "香港 遊戲 快閃 展覽"),
    ("澳門", "澳門 動漫 遊戲 展覽"),
    ("台灣", "台灣 同人場 CWT FF 動漫 活動"),
    ("深圳", "深圳 漫展 遊戲 動漫"),
]


def fetch_rss_items(region_hint: str, query: str, limit: int = 5):
    encoded_q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_q}&hl=zh-TW&gl=HK&ceid=HK:zh-Hant"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ACGHMTBot/1.0)"},
    )
    items = []
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)
        for item in root.findall(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            if title and link:
                items.append(
                    {
                        "region_hint": region_hint,
                        "title": title,
                        "link": link,
                        "pub_date": pub_date,
                    }
                )
    except Exception as e:
        print(f"[RSS Warn] {query}: {e}")
    return items


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
                    "你是香港、澳門、台灣、深圳動漫、遊戲與同人活動情報整理助手。"
                    "請根據輸入的資訊判斷是否為明確的『線下活動、漫展、同人場、遊戲活動或快閃店』。"
                    "若不是明確的線下活動或資訊過於模糊，請只回覆 null。"
                    "若是明確活動，請只輸出一個合法 JSON 物件（不要用 markdown 包裹，不要多餘解釋），欄位如下："
                    '{"region":"香港|澳門|台灣|深圳",'
                    '"category":"同人即賣會|綜合漫展|遊戲與快閃",'
                    '"title":"活動名稱（繁體中文）",'
                    '"start_date":"YYYY-MM-DD（若無明確日期請填未來大致日期或留空字串）",'
                    '"end_date":"YYYY-MM-DD",'
                    '"venue":"場地或地區",'
                    '"deadline_note":"重要提醒或售票/報名資訊",'
                    '"tags":["標籤1","標籤2"]}'
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 400,
        "temperature": 0.2,
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
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def main():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"last_updated": "", "events": []}

    existing_events = data.get("events", [])
    seen_titles = {re.sub(r"\s+", "", e.get("title", "")).lower() for e in existing_events}
    seen_urls = {e.get("url", "") for e in existing_events}

    raw_items = []
    for region_hint, query in RSS_QUERIES:
        raw_items.extend(fetch_rss_items(region_hint, query, limit=3))

    added_count = 0
    for item in raw_items:
        if item["link"] in seen_urls:
            continue

        prompt = (
            f"地區參考：{item['region_hint']}\n"
            f"標題：{item['title']}\n"
            f"發布時間：{item['pub_date']}"
        )
        try:
            raw_ai = call_cloudflare_ai(prompt)
            parsed = extract_json_object(raw_ai)
        except Exception as e:
            print(f"[AI Warn] {item['title'][:30]}: {e}")
            time.sleep(2)
            continue

        if not parsed or not parsed.get("title"):
            time.sleep(1.5)
            continue

        norm_title = re.sub(r"\s+", "", parsed["title"]).lower()
        if norm_title in seen_titles:
            continue

        region = parsed.get("region") if parsed.get("region") in {"香港", "澳門", "台灣", "深圳"} else item["region_hint"]
        category = parsed.get("category") if parsed.get("category") in {"同人即賣會", "綜合漫展", "遊戲與快閃"} else "遊戲與快閃"
        start_date = (parsed.get("start_date") or "").strip()
        end_date = (parsed.get("end_date") or start_date).strip()

        new_event = {
            "id": f"auto-{int(time.time())}-{added_count}",
            "region": region,
            "category": category,
            "title": parsed["title"].strip(),
            "start_date": start_date or "近期公布",
            "end_date": end_date or start_date or "近期公布",
            "venue": (parsed.get("venue") or region).strip(),
            "deadline_note": (parsed.get("deadline_note") or "").strip(),
            "tags": parsed.get("tags") if isinstance(parsed.get("tags"), list) else [region],
            "url": item["link"],
        }

        existing_events.append(new_event)
        seen_titles.add(norm_title)
        seen_urls.add(item["link"])
        added_count += 1
        print(f"[Added] [{region}] {new_event['title']}")

        time.sleep(2)

    hkt = timezone(timedelta(hours=8))
    data["last_updated"] = datetime.now(hkt).strftime("%Y-%m-%d %H:%M HKT")
    data["events"] = existing_events

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"完成更新，共新增 {added_count} 筆活動。")


if __name__ == "__main__":
    main()

import os
import json
import requests
from bs4 import BeautifulSoup

# セカストオンラインのG-SHOCK検索URL
TARGET_URL = "https://www.2ndstreet.jp/search?category=900001&keyword=G-SHOCK"

# 重複チェック用の記録ファイル名
SEEN_FILE = "seen_items_2ndstreet.json"

# Discord Webhook URL (GitHub Secretsから取得)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL_2NDSTREET")

# 特典PDFに基づいた狙い目キーワード群
KEYWORDS = [
    "TWIN SENSOR", "TRIPLE SENSOR", "ALTI", "SURF",
    "オレンジ", "ピンク", "ネイビー", "グリーン", "イエロー",
    "迷彩", "カモフラ", "マーブル", "グラデーション",
    "フロッグマン", "FROGMAN", "マッドマン", "ガルフマン", "レイズマン",
    "スカイフォース", "DW-6700", "DW-002", "DW-003", "DW-004", "DW-8800", "DW-9000",
    "ラバーズコレクション", "ラバコレ", "イルクジ", "コラボ",
    "タフソーラー", "電波ソーラー"
]

def load_seen_items():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"Error loading seen items: {e}")
            return set()
    return set()

def save_seen_items(seen):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving seen items: {e}")

def send_discord_notification(item):
    if not DISCORD_WEBHOOK_URL:
        print("Discord Webhook URL is not set.")
        return

    payload = {
        "content": f"🚨 **【セカスト】利益候補 G-SHOCK 発見！** 🚨\n\n"
                   f"**商品名**: {item['title']}\n"
                   f"**価格**: {item['price']:,}円\n"
                   f"**キーワード**: {item['matched_keyword']}\n"
                   f"**URL**: {item['url']}"
    }

    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        if res.status_code == 204:
            print(f"Successfully notified: {item['title']}")
        else:
            print(f"Failed to send Discord notification: {res.status_code}, {res.text}")
    except Exception as e:
        print(f"Error sending Discord notification: {e}")

def main():
    seen_items = load_seen_items()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        res = requests.get(TARGET_URL, headers=headers, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"Error fetching URL: {e}")
        return

    soup = BeautifulSoup(res.text, "html.parser")
    items = soup.find_all("li", class_="item")

    new_matches = []

    for item in items:
        title_tag = item.find("p", class_="itemCard_name")
        price_tag = item.find("p", class_="itemCard_price")
        link_tag = item.find("a")

        if not (title_tag and price_tag and link_tag):
            continue

        title = title_tag.get_text(strip=True)
        price_str = price_tag.get_text(strip=True).replace("￥", "").replace(",", "").replace("（税込）", "")
        
        try:
            price = int(price_str)
        except ValueError:
            continue

        url = "https://www.2ndstreet.jp" + link_tag.get("href", "")
        item_id = link_tag.get("href", "").split("/")[-1]

        if item_id in seen_items:
            continue

        # 判定条件：8,000円以下かつキーワードにヒット
        matched = None
        for kw in KEYWORDS:
            if kw.lower() in title.lower():
                matched = kw
                break

        if price <= 8000 and matched:
            new_matches.append({
                "id": item_id,
                "title": title,
                "price": price,
                "matched_keyword": matched,
                "url": url
            })

    for match in new_matches:
        send_discord_notification(match)
        seen_items.add(match["id"])

    save_seen_items(seen_items)

if __name__ == "__main__":
    main()

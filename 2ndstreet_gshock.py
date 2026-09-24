import os
import json
import requests
from bs4 import BeautifulSoup

# 設定
TARGET_URL = "https://www.2ndstreet.jp/search?category=900001&keyword=G-SHOCK"
SEEN_FILE = "seen_items.json"
LINE_NOTIFY_TOKEN = os.environ.get("LINE_NOTIFY_TOKEN")

def load_seen_items():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"Error loading {SEEN_FILE}: {e}")
            return set()
    return set()

def save_seen_items(seen_items):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen_items), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving {SEEN_FILE}: {e}")

def send_line_notification(message):
    if not LINE_NOTIFY_TOKEN:
        print("LINE_NOTIFY_TOKEN is not set.")
        return
    url = "https://notify-api.line.me/api/notify"
    headers = {"Authorization": f"Bearer {LINE_NOTIFY_TOKEN}"}
    data = {"message": message}
    try:
        res = requests.post(url, headers=headers, data=data)
        res.raise_for_status()
        print("Notification sent successfully.")
    except Exception as e:
        print(f"Error sending LINE notification: {e}")

def main():
    seen_items = load_seen_items()
    
    # ブロック（403エラー）回避用のヘッダー設定
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://www.2ndstreet.jp/"
    }

    try:
        res = requests.get(TARGET_URL, headers=headers, timeout=15)
        res.raise_for_status()
    except Exception as e:
        print(f"Error fetching URL: {e}")
        return

    soup = BeautifulSoup(res.text, "html.parser")
    # 商品リストを取得（※2ndstreetのHTML構造に合わせたセレクタ）
    items = soup.select("li.itemCard") or soup.select(".item")

    new_items_found = False
    for item in items:
        link_tag = item.find("a")
        if not link_tag or "href" not in link_tag.attrs:
            continue
        
        item_url = "https://www.2ndstreet.jp" + link_tag["href"]
        item_id = item_url.split("/")[-1]

        if item_id not in seen_items:
            title_tag = item.find("p", class_="itemCard_name") or item.find("h3") or item.find("p")
            price_tag = item.find("p", class_="itemCard_price") or item.find("span", class_="price")
            
            title = title_tag.get_text(strip=True) if title_tag else "G-SHOCK"
            price = price_tag.get_text(strip=True) if price_tag else "価格不明"

            message = f"\n【新着 G-SHOCK 発見】\n{title}\n価格: {price}\n{item_url}"
            print(f"New item found: {title}")
            send_line_notification(message)
            
            seen_items.add(item_id)
            new_items_found = True

    if new_items_found:
        save_seen_items(seen_items)
    else:
        print("No new items found.")

if __name__ == "__main__":
    main()

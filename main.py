import json
import os
import random
import re
import time
from io import BytesIO

from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from PIL import Image
from playwright.sync_api import sync_playwright
import requests

# ==================================================
# 1. 環境変数からの設定読み込み & ストア設定
# ==================================================
API_KEY = os.environ.get("GEMINI_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

client = genai.Client(api_key=API_KEY)

# 巡回対象のストアリスト（全4店舗）
STORES = [
    {"name": "日本貴金属", "id": "BaLo7yetbAXTrZxWNL5iV2ESPpPb6"},
    {"name": "Fii", "id": "88qw1vitqMALJSEUEqiZDDTmNSoiQ"},
    {"name": "TAKARAYA", "id": "A9ajxEQZ34DuDBbQoJKDh4bFYA74N"},
    {"name": "TVCストア1号店", "id": "GHXf6dTmnn7SLWbqAEeiBU1rrJHM6"},
]
SEARCH_KEYWORD = "ソーラー"
EXCLUDE_KEYWORDS = ["ELGIN", "Elgin", "elgin", "エルジン"]

SEEN_FILE = "seen_solar.json"
MAX_ITEMS_PER_STORE = 15


# ==================================================
# 2. ヘルパー関数
# ==================================================
def load_seen_items():
  if os.path.exists(SEEN_FILE):
    try:
      with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))
    except Exception as e:
      print(f"⚠️ 既読ファイルの読み込みエラー: {e}")
      return set()
  return set()


def save_seen_items(seen):
  try:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
      json.dump(list(seen), f, ensure_ascii=False, indent=2)
  except Exception as e:
    print(f"⚠️ 既読ファイルの保存エラー: {e}")


def parse_price(price_val):
  if not price_val:
    return 0
  digits = re.sub(r"[^\d]", "", str(price_val))
  return int(digits) if digits else 0


def send_discord_notify(store_name, item, result, current_price=0):
  if not DISCORD_WEBHOOK_URL:
    print("⚠️ DISCORD_WEBHOOK_URLが未設定のため、通知をスキップします。")
    return

  price_str = f"{current_price:,}円" if current_price > 0 else "不明"

  message = f"""
🔔 **【ソーラー時計 仕入れチャンス到来！】**
----------------------------------------
🏪 **店舗**: {store_name}
📌 **タイトル**: {item['title']}
💰 **現在価格**: {price_str}
⏰ **残り時間**: {item['time']}
🔗 **URL**: {item['url']}

🏷️ **ブランド/型番**: {result.get('brand', '不明')} / {result.get('model', '不明')}
📊 **評価**: **{result.get('condition_score', '-')}**
💰 **稼働時想定売価**: {result.get('estimated_resale_normal', '-')}円
⚠️ **ジャンク時想定売価**: {result.get('estimated_resale_junk', '-')}円
🎯 **推奨落札上限 (目標利益確保)**: **{result.get('max_bid_price_target', '-')}円**
🛡️ **ジャンク防衛ライン (利益±0)**: {result.get('max_bid_price_break_even', '-')}円
💡 **理由・状態感**: {result.get('reasoning', '-')}
----------------------------------------
"""
  payload = {"content": message}
  headers = {"Content-Type": "application/json"}
  try:
    res = requests.post(
        DISCORD_WEBHOOK_URL,
        data=json.dumps(payload),
        headers=headers,
        timeout=10,
    )
    if res.status_code in [200, 204]:
      print(f"✅ Discord通知送信完了: {item['title'][:20]}")
    else:
      print(f"❌ Discord通知エラー: {res.status_code}, {res.text}")
  except Exception as e:
    print(f"❌ Discord通知例外: {e}")


# ==================================================
# 3. リスト取得（残り1時間未満の厳格抽出）
# ==================================================
def get_urgent_auction_urls(page, store_name, search_url, seen_items):
  print(
      f"🔍 【{store_name}】 内を検索中...\nURL: {search_url}\n", flush=True
  )

  try:
    page.goto(search_url, wait_until="networkidle", timeout=30000)
  except Exception:
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

  for i in range(1, 4):
    page.evaluate(f"window.scrollTo(0, {i * 800});")
    time.sleep(0.8)

  soup = BeautifulSoup(page.content(), "html.parser")
  urgent_items = []

  for a in soup.find_all("a", href=True):
    href = a["href"]
    if "/auction/" in href:
      clean_url = href.split("?")[0]
      if not clean_url.startswith("http"):
        clean_url = "https://page.auctions.yahoo.co.jp" + clean_url

      match = re.search(r"/auction/([a-zA-Z0-9]+)", clean_url)
      item_id = match.group(1) if match else clean_url.split("/")[-1]

      if item_id in seen_items:
        continue

      parent = a.find_parent(["li", "div", "article"])
      parent_text = parent.get_text(" ", strip=True) if parent else ""

      # 1時間未満（「分」または「秒」が含まれ、「日」「時間」が含まれない）
      if ("分" in parent_text or "秒" in parent_text) and not (
          "日" in parent_text or "時間" in parent_text
      ):
        time_match = re.search(r"(\d+分|\d+秒)", parent_text)
        time_str = time_match.group(0) if time_match else "1時間未満"

        title = a.get_text().strip()
        if not title or len(title) < 5:
          title = parent_text[:35] if parent_text else "タイトル不明"

        if any(keyword in title for keyword in EXCLUDE_KEYWORDS):
          print(
              f"🚫 除外対象ブランドのためスキップ: {title[:20]}...",
              flush=True,
          )
          continue

        if not any(x["item_id"] == item_id for x in urgent_items):
          urgent_items.append({
              "item_id": item_id,
              "title": title,
              "url": clean_url,
              "time": time_str,
          })

  return urgent_items


# ==================================================
# 4. 詳細情報・画像・現在価格取得
# ==================================================
def fetch_auction_details(page, url):
  page.goto(url, wait_until="domcontentloaded", timeout=15000)
  time.sleep(1)

  soup = BeautifulSoup(page.content(), "html.parser")

  title_tag = soup.find("h1") or soup.find("meta", property="og:title")
  title = (
      title_tag.get("content")
      if title_tag and title_tag.name == "meta"
      else (title_tag.text.strip() if title_tag else "不明")
  )

  desc_tag = soup.find(
      "div", class_="ProductExplanation__commentArea"
  ) or soup.find("section", class_="ProductExplanation")
  description = desc_tag.text.strip() if desc_tag else "説明文なし"

  current_price = 0
  price_tag = (
      soup.find("dd", class_="Price__value")
      or soup.find("span", class_="Price__value")
      or soup.find("p", class_="Price__value")
  )
  if price_tag:
    price_digits = re.sub(r"[^\d]", "", price_tag.get_text())
    if price_digits:
      current_price = int(price_digits)

  img_urls = []
  for img in soup.find_all("img"):
    src = img.get("src") or img.get("data-src") or ""
    if "auctions.c.yimg.jp" in src:
      clean_img_url = src.split("?")[0]
      if not clean_img_url.endswith(".gif") and clean_img_url not in img_urls:
        img_urls.append(clean_img_url)

  img_urls = img_urls[:8]

  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
      )
  }
  images = []
  for img_url in img_urls:
    try:
      res = requests.get(img_url, headers=headers, timeout=5)
      if res.status_code == 200:
        img = Image.open(BytesIO(res.content))
        img.thumbnail((800, 800))
        images.append(img)
    except Exception:
      continue

  return title, description, images, current_price


# ==================================================
# 5. Gemini 3.6 Flash 解析
# ==================================================
def analyze_watch(title, description, images):
  prompt = f"""
あなたは中古ソーラー時計の転売・仕入れ目利き専門家です。
添付された商品画像とタイトル・商品説明文を細部まで精査し、外観ダメージ（特に風防キズ）および二次電池（ソーラー充電）の状態を厳しく見極めた上で仕入れ判定を行ってください。

【確認・査定基準】
1. 風防（ガラス面）チェック:
   - 線キズ、深い傷、ヒビ割れ、欠け、内部の曇り・カビがないか厳重に確認。
   - 風防に明確なキズ・擦れがある場合は、研磨・手間に見合うか考慮し評価を降格（BまたはC）してください。
2. 二次電池・動作状況チェック:
   - 「充電しても動かない」「ソーラー非稼働」「ジャンク」表記がないか。
3. 外観ダメージ:
   - ケース・ベゼルの大きな打痕、メッキ剥げ、著しい錆・汚れの有無。

【計算ロジックの設定】
1. 想定相場（2パターン）:
   - ①「稼働品（正常動作品）」としての想定販売相場
   - ②「不動・ジャンク（パーツ取り）」としての想定販売相場
2. コスト前提：
   - 販売手数料（メルカリ等）：10%
   - 販売時発送送料：210円
   - ヤフオク仕入れ時送料：990円（一律）

以下のJSON形式でのみ回答してください：
{{
  "brand": "ブランド名",
  "model": "型番/キャリバー",
  "condition_score": "A（推奨） / B（慎重） / C（不可）",
  "estimated_resale_normal": 12000,
  "estimated_resale_junk": 3000,
  "max_bid_price_target": 6500,
  "max_bid_price_break_even": 1500,
  "reasoning": "風防・動作状態と仕入れ判定理由（80文字以内）"
}}

【商品タイトル】: {title}
【商品説明文】: {description}
"""

  max_retries = 3
  for attempt in range(max_retries):
    try:
      response = client.models.generate_content(
          model="gemini-3.6-flash",
          contents=images + [prompt],
          config=types.GenerateContentConfig(
              response_mime_type="application/json", temperature=0.1
          ),
      )
      return json.loads(response.text)
    except Exception as e:
      if attempt < max_retries - 1:
        wait_time = (attempt + 1) * 3
        print(
            f"  ⚠️ Gemini一時的エラー。{wait_time}秒後に再試行します..."
            f" ({attempt + 1}/{max_retries})",
            flush=True,
        )
        time.sleep(wait_time)
      else:
        print(f"❌ Gemini解析失敗: {e}")
        return None


# ==================================================
# 6. メイン実行処理
# ==================================================
def main():
  print(
      "🚀 ヤフオク ソーラー時計仕入れリサーチ（Playwright + Gemini 3.6"
      " Flash）を開始します...",
      flush=True,
  )
  seen_items = load_seen_items()

  with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="ja-JP",
    )
    page = context.new_page()

    for store in STORES:
      store_name = store["name"]
      seller_id = store["id"]

      target_search_url = (
          f"https://auctions.yahoo.co.jp/seller/{seller_id}?p={SEARCH_KEYWORD}&category_id=23140&select=22&is_auction=1&s1=end&o1=a"
      )

      print("\n========================================", flush=True)
      print(f"🏪 巡回開始: 【 {store_name} 】", flush=True)
      print("========================================", flush=True)

      target_items = get_urgent_auction_urls(
          page, store_name, target_search_url, seen_items
      )
      target_items = target_items[:MAX_ITEMS_PER_STORE]

      print(
          f"⏰ 残り1時間未満の上位【 {len(target_items)} 件 】を厳選してチェックします。\n",
          flush=True,
      )

      if not target_items:
        print(
            f"【{store_name}】に該当する未チェック商品（残り1時間未満）は見つかりませんでした。",
            flush=True,
        )
        continue

      for i, item in enumerate(target_items, 1):
        print(
            "────────────────────────────────────────",
            flush=True,
        )
        print(
            f"[{store_name}] 【{i}/{len(target_items)}】残り時間:"
            f" {item['time']} | {item['title'][:25]}...",
            flush=True,
        )

        try:
          title, description, images, current_price = fetch_auction_details(
              page, item["url"]
          )
          print(f"  💰 現在価格: {current_price:,}円", flush=True)
          print(
              "🤖 Gemini 3.6 Flashで目利き試算中（風防・外観厳密チェック）...",
              flush=True,
          )

          g_result = analyze_watch(title, description, images)

          if g_result:
            score = g_result.get("condition_score", "")
            max_bid_num = parse_price(g_result.get("max_bid_price_target"))

            # 赤字判定補正
            if (
                current_price > 0
                and max_bid_num > 0
                and current_price >= max_bid_num
            ):
              print(
                  f"  ⚠️ 赤字判定補正: 現在価格({current_price}円) >="
                  f" 推奨上限額({max_bid_num}円)"
              )
              score = "C（不可）"

            print(
                f"  └ 最終判定: {score} | 通常上限:"
                f" {g_result.get('max_bid_price_target')}円 | 防衛線:"
                f" {g_result.get('max_bid_price_break_even')}円"
            )
            print(f"  └ 添削理由: {g_result.get('reasoning')}")

            if "A" in score or "B" in score:
              send_discord_notify(store_name, item, g_result, current_price)
            else:
              print("  ⏩ スルー（評価Cのため通知なし）", flush=True)

          seen_items.add(item["item_id"])
          save_seen_items(seen_items)

        except Exception as e:
          print(f"❌ 解析エラー: {e}", flush=True)

        time.sleep(1)

    browser.close()

  print(
      "\n🎉 すべてのストアの自動処理が正常に終了しました！",
      flush=True,
  )


if __name__ == "__main__":
  main()

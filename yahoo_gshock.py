import json
import os
import re
import time
from io import BytesIO

from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from google.genai.errors import APIError
from PIL import Image
from playwright.sync_api import sync_playwright
import requests

# --------------------------------------------------
# 1. 設定値・定数 & API設定
# --------------------------------------------------
API_KEY = os.environ.get("GEMINI_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

client = genai.Client(api_key=API_KEY) if API_KEY else None

MAX_PRICE_LIMIT = 8000
URL_AUCTION = f"https://auctions.yahoo.co.jp/search/search?p=G-SHOCK&max={MAX_PRICE_LIMIT}&auccat=23140&is_auction=1&s1=end&o1=a&n=100"

SEEN_FILE = "seen_items_yahoo.json"
MAX_AUCTION_ITEMS = 100

# 物理的破損・再生不能な状態のみ弾くNGリスト
NG_KEYWORDS = [
    "加水分解",
    "割れ",
    "ベタつき",
    "ベタツキ",
    "ベゼル欠品",
    "ベゼル破損",
    "ベゼル割れ",
    "ベゼル不良",
    "遊環なし",
    "遊環欠品",
    "リング欠損",
    "ガラス傷",
    "ガラスキズ",
    "風防欠け",
    "風防傷",
    "黄ばみ",
    "色あせ",
    "変色",
    "日焼け",
]


# --------------------------------------------------
# 2. ヘルパー関数
# --------------------------------------------------
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


def send_discord_notification(item, g_result, seller_name):
  if not DISCORD_WEBHOOK_URL:
    print("❌ Discord Webhook URLが未設定です")
    return

  risk_text = (
      " / ".join(g_result.get("risk_flags", []))
      if g_result.get("risk_flags")
      else "なし"
  )

  message = f"""
🚨 **【ヤフオク】仕入れ候補 G-SHOCK 発見！** 🚨
----------------------------------------
🏪 **出品者/ストア**: {seller_name}
📌 **商品名**: {item['title']}
💰 **現在価格**: {item['price']:,}円 ({item['sale_type']})
⏰ **残り時間**: {item['time_left']}
🔗 **URL**: {item['url']}

🏷️ **モデル特定**: {g_result.get('brand', 'CASIO')} / {g_result.get('model', '不明')}
🛡 **純正性判定**: {g_result.get('authenticity_status', '不明')}
📊 **総合評価**: **{g_result.get('condition_score', '-')}**
🏷️ **状態・注記フラグ**: {risk_text}

💵 **想定売価**: {g_result.get('estimated_resale_normal', '-')}円
🎯 **推奨落札/購入上限額**: **{g_result.get('max_bid_price_target', '-')}円**
💡 **目利き添削・状態感**: {g_result.get('reasoning', '-')}
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
    print(f"❌ Discord送信例外: {e}")


# --------------------------------------------------
# 3. Gemini 解析
# --------------------------------------------------
def analyze_gshock_with_gemini(title, description, images, price):
  if not client:
    print("❌ Gemini APIキーが読み込めていません")
    return None

  prompt = f"""
あなたはG-SHOCKおよびブランドウォッチの転売・仕入れ目利き専門家です。
添付された商品画像と商品タイトル・説明文を詳細に添削・解析し、社外品や偽物を排除した上で、電池交換や清掃を行って利益が見込めるか仕入れ判定を行ってください。

【査定方針】
- 「電池切れ」「動作未確認」「ジャンク扱い」であっても、モジュール死の可能性が低く電池交換で稼働が見込める場合は前向きに評価してください。
- ただし、社外パーツ（メタルベゼル等）やMOD品、偽物の疑いがある場合は判定を「C（不可）」にしてください。

【メンテコスト計算の基準】
- 手数料10%、送料梱包代450円、仕入れ送料990円を一律コストとします。
- 電池/メンテナンス費用:
  ・通常の電池式（クォーツ）モデル: 200円
  ・タフソーラー / 電波ソーラーモデル: 二次電池交換代として【 1,200円 】で計算してください。

【利益判定】
- 現在の出品価格は【 {price} 円 】です。
- 上記コストを引いた上で推奨仕入れ上限額（max_bid_price_target）を算出してください。
- 現在価格（{price}円）が推奨上限額を超えている場合、または利益が出ない（赤字）場合は「C（不可）」と判定してください。

以下のJSON形式でのみ回答してください：
{{
  "brand": "CASIO",
  "model": "型番（例: DW-6900B-9 / GW-M5610等）",
  "authenticity_status": "純正品 / 社外パーツあり / 偽物・MODの疑い",
  "risk_flags": ["電池切れ疑い", "ソーラー機（二次電池想定）", "外観小傷あり", "純正パーツ" などの状態フラグ],
  "condition_score": "A（推奨） / B（慎重） / C（不可）",
  "estimated_resale_normal": 8500,
  "max_bid_price_target": 5500,
  "reasoning": "電池交換稼働見込み・真贋・外観状態の添削理由（100文字以内）"
}}

【商品タイトル】: {title}
【商品説明文】: {description}
"""

  try:
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=images + [prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0.1
        ),
    )
    return json.loads(response.text)

  except APIError as e:
    if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
      print(
          "⚠️ 【Quota上限検知】Gemini APIの制限枠に達しました。"
      )
      print("   以降の解析を中断し、スクリプトを安全に終了します。")
      return "QUOTA_EXCEEDED"
    else:
      print(f"❌ Gemini APIエラー: {e}")
      return None
  except Exception as e:
    print(f"❌ Gemini解析例外: {e}")
    return None


# --------------------------------------------------
# 4. 詳細ページ情報・画像取得
# --------------------------------------------------
def fetch_detail_page(page, url):
  page.goto(url, wait_until="domcontentloaded", timeout=15000)
  time.sleep(1)

  html = page.content()
  soup = BeautifulSoup(html, "html.parser")

  seller_tag = soup.select_one(".Seller__name, .Seller__link, [class*='Seller']")
  seller_name = seller_tag.get_text(strip=True) if seller_tag else "不明出品者"

  desc_tag = soup.select_one(
      ".ProductExplanation__commentArea, .ProductExplanation"
  )
  description = desc_tag.get_text(strip=True) if desc_tag else "説明文なし"

  img_urls = []
  for img in soup.find_all("img"):
    src = img.get("src") or img.get("data-src") or ""
    if "auctions.c.yimg.jp" in src:
      clean_url = src.split("?")[0]
      if clean_url not in img_urls and not clean_url.endswith(".gif"):
        img_urls.append(clean_url)

  img_urls = img_urls[:4]

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
        img.thumbnail((600, 600))
        images.append(img)
    except Exception:
      continue

  return seller_name, description, images


# --------------------------------------------------
# 5. メイン処理（直前〜3時間の全時間帯対応）
# --------------------------------------------------
def process_auction_list(
    page, target_url, max_limit, sale_type_label, seen_items
):
  print(f"\n🔍 【{sale_type_label}】検索URLへアクセス中: {target_url}")

  try:
    page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
  except Exception as e:
    print(f"❌ ページ移動エラー: {e}")
    return

  time.sleep(1)

  html = page.content()
  soup = BeautifulSoup(html, "html.parser")
  items = soup.select("li.Product") or soup.select(".Product")

  if not items:
    print("⚠️ 商品要素が見つかりませんでした。")
    return

  print(f"📦 検出件数: {len(items)}件（直前〜3時間の対象を精査）")

  processed_count = 0

  for item in items:
    if processed_count >= max_limit:
      print(f"⏱️ 上限{max_limit}件に達したため完了。")
      break

    title_tag = item.select_one(".Product__titleLink") or item.select_one("a")
    price_tag = item.select_one(".Product__priceValue") or item.select_one(
        "[class*='price']"
    )

    if not (title_tag and price_tag):
      continue

    title = title_tag.get_text(strip=True)
    price_digits = re.sub(r"[^\d]", "", price_tag.get_text(strip=True))
    if not price_digits:
      continue
    price = int(price_digits)

    if price > MAX_PRICE_LIMIT:
      continue

    url = title_tag.get("href", "")
    item_id = item.get("data-auction-id")
    if not item_id and url:
      match = re.search(r"/auction/([a-zA-Z0-9]+)", url)
      item_id = match.group(1) if match else url

    if item_id in seen_items:
      continue

    # --- 残り時間判定（秒〜3時間前まで対応） ---
    time_tag = item.select_one(".Product__time") or item.select_one(
        "[class*='time']"
    )
    time_text = (
        time_tag.get_text(strip=True)
        if time_tag
        else item.get_text(" ", strip=True)
    )

    if "日" in time_text:
      continue

    minutes_left = None

    if "時間" in time_text:
      hour_match = re.search(r"(\d+)\s*時間", time_text)
      min_in_hour_match = re.search(r"(\d+)\s*分", time_text)

      hours = int(hour_match.group(1)) if hour_match else 0
      mins = int(min_in_hour_match.group(1)) if min_in_hour_match else 0

      minutes_left = (hours * 60) + mins
    elif "分" in time_text:
      min_match = re.search(r"(\d+)\s*分", time_text)
      if min_match:
        minutes_left = int(min_match.group(1))
    elif "秒" in time_text:
      minutes_left = 0  # 残り数秒〜数十秒

    if minutes_left is None:
      continue

    # ★ 0分（数秒前）〜180分（3時間前）まで許可
    if not (0 <= minutes_left <= 180):
      continue

    time_left_str = (
        f"{minutes_left // 60}時間{minutes_left % 60}分"
        if minutes_left >= 60
        else (f"{minutes_left}分" if minutes_left > 0 else "1分未満(直前)")
    )

    # タイトルの物理NGチェック（加水分解など）
    if any(ng.lower() in title.lower() for ng in NG_KEYWORDS):
      continue

    processed_count += 1
    print(
        f"🎯 解析対象 [{sale_type_label} {processed_count}/{max_limit}]:"
        f" 残り{time_left_str} | 価格: {price}円 | {title[:30]}..."
    )

    try:
      seller_name, description, images = fetch_detail_page(page, url)

      text_to_check = f"{title} {description}".lower()
      found_ng = [ng for ng in NG_KEYWORDS if ng.lower() in text_to_check]
      if found_ng:
        print(f"  ⏩ 本文NGワード検出のためスキップ: {', '.join(found_ng)}")
        seen_items.add(item_id)
        save_seen_items(seen_items)
        continue

      print("🤖 Geminiで仕入れ目利き試算中...")
      g_result = analyze_gshock_with_gemini(title, description, images, price)

      if g_result == "QUOTA_EXCEEDED":
        print("⛔ 制限のため処理をここで安全に停止します。")
        break

      if g_result:
        score = g_result.get("condition_score", "")
        auth = g_result.get("authenticity_status", "")

        try:
          max_target = int(g_result.get("max_bid_price_target", 0))
        except Exception:
          max_target = 0

        if price >= max_target and max_target > 0:
          print(
              f"  ⚠️ 赤字判定補正: 現在価格({price}円) >="
              f" 推奨上限額({max_target}円)"
          )
          score = "C（不可）"

        print(
            f"  └ 最終判定: {score} | 純正性: {auth} | 推奨上限:"
            f" {max_target}円"
        )
        print(f"  └ 添削理由: {g_result.get('reasoning')}")

        if "A" in score or "B" in score:
          item_data = {
              "id": item_id,
              "title": title,
              "price": price,
              "sale_type": sale_type_label,
              "time_left": time_left_str,
              "url": url,
          }
          send_discord_notification(item_data, g_result, seller_name)
        else:
          print("  ⏩ スルー（利益なし/社外品/偽物疑い）")

      seen_items.add(item_id)
      save_seen_items(seen_items)

    except Exception as e:
      print(f"❌ 詳細解析エラー: {e}")

    time.sleep(1)


# --------------------------------------------------
# 6. メイン実行
# --------------------------------------------------
def main():
  print("🚀 ヤフオク G-SHOCK仕入れリサーチ（直前〜3時間前対応）を開始します...")
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
        ),
        viewport={"width": 1280, "height": 800},
        locale="ja-JP",
    )
    page = context.new_page()

    process_auction_list(
        page,
        URL_AUCTION,
        MAX_AUCTION_ITEMS,
        "ヤフオク(終了直前〜3時間前)",
        seen_items,
    )

    browser.close()

  print("\n✨ リサーチ処理が完了しました。")


if __name__ == "__main__":
  main()

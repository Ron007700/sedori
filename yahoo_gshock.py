import json
import os
import re
import time
import warnings
from io import BytesIO

from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from google.genai.errors import APIError
from PIL import Image
from playwright.sync_api import sync_playwright
import requests

# 警告ログの非表示化
warnings.filterwarnings("ignore")

# --------------------------------------------------
# 1. 設定値・定数 & API設定
# --------------------------------------------------
API_KEY = os.environ.get("GEMINI_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

client = genai.Client(api_key=API_KEY) if API_KEY else None

# 価格上限設定
MAX_PRICE_LIMIT = 8000

# 8,000円以下 × 終了が近い順 (s1=end, o1=a) × 100件一括取得
URL_AUCTION = (
    "https://auctions.yahoo.co.jp/search/search?"
    f"p=G-SHOCK&max={MAX_PRICE_LIMIT}&auccat=23140&is_auction=1&s1=end&o1=a&n=100"
)

SEEN_FILE = "seen_items_yahoo.json"

# 画像は目利き精度重視でしっかり5枚取得
MAX_IMAGE_COUNT = 5

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
      print(f"⚠️ 既読ファイルの読み込みエラー: {e}", flush=True)
      return set()
  return set()


def save_seen_items(seen):
  try:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
      json.dump(list(seen), f, ensure_ascii=False, indent=2)
  except Exception as e:
    print(f"⚠️ 既読ファイルの保存エラー: {e}", flush=True)


def send_discord_notification(item, g_result, seller_name):
  if not DISCORD_WEBHOOK_URL:
    print("❌ Discord Webhook URLが未設定です", flush=True)
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
      print(f"✅ Discord通知送信完了: {item['title'][:20]}", flush=True)
    else:
      print(
          f"❌ Discord通知エラー: {res.status_code}, {res.text}", flush=True
      )
  except Exception as e:
    print(f"❌ Discord送信例外: {e}", flush=True)


# --------------------------------------------------
# 3. Gemini 解析処理
# --------------------------------------------------
def analyze_gshock_with_gemini(title, description, images, price):
  if not client:
    print("❌ Gemini APIキーが読み込めていません", flush=True)
    return None

  prompt = f"""
あなたはG-SHOCKおよびブランドウォッチの転売・仕入れ目利き専門家です。
添付された商品画像（最大5枚）と商品タイトル・説明文を詳細に添削・解析し、社外品や偽物を排除した上で、電池交換や清掃を行って利益が見込めるか仕入れ判定を行ってください。

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
          "⚠️ 【Quota上限検知】Gemini APIの制限枠に達しました。",
          flush=True,
      )
      print(
          "   以降の解析を中断し、スクリプトを安全に終了します。",
          flush=True,
      )
      return "QUOTA_EXCEEDED"
    else:
      print(f"❌ Gemini APIエラー: {e}", flush=True)
      return None
  except Exception as e:
    print(f"❌ Gemini解析例外: {e}", flush=True)
    return None


# --------------------------------------------------
# 4. 詳細ページ情報・画像5枚取得
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

  # 画像はしっかり5枚取得
  img_urls = img_urls[:MAX_IMAGE_COUNT]

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
# 5. 一覧抽出 ＆ ピンポイント解析処理
# --------------------------------------------------
def process_auction_list(page, target_url, sale_type_label, seen_items):
  print(f"\n🔍 一覧ページ取得中 (8,000円以下・終了が近い順 100件): {target_url}", flush=True)

  try:
    page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
  except Exception as e:
    print(f"❌ ページ移動エラー: {e}", flush=True)
    return

  time.sleep(1)

  html = page.content()
  soup = BeautifulSoup(html, "html.parser")
  items = soup.select("li.Product") or soup.select(".Product")

  if not items:
    print("⚠️ 商品要素が見つかりませんでした。", flush=True)
    return

  print(
      f"📦 取得完了: {len(items)}件一覧から「5分〜59分」の対象を抽出します...",
      flush=True,
  )

  target_items = []

  # --- 【第1段階】一覧画面での高速フィルタリング ---
  for item in items:
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

    # 念のため価格上限チェック
    if price > MAX_PRICE_LIMIT:
      continue

    url = title_tag.get("href", "")
    item_id = item.get("data-auction-id")
    if not item_id and url:
      match = re.search(r"/auction/([a-zA-Z0-9]+)", url)
      item_id = match.group(1) if match else url

    if item_id in seen_items:
      continue

    # 残り時間判定
    time_tag = item.select_one(".Product__time") or item.select_one(
        "[class*='time']"
    )
    time_text = (
        time_tag.get_text(strip=True)
        if time_tag
        else item.get_text(" ", strip=True)
    )

    # 「日」や「時間」が含まれる場合は1時間以上確定なのでスルー
    if "日" in time_text or "時間" in time_text:
      continue

    minutes_left = None
    if "分" in time_text:
      min_match = re.search(r"(\d+)\s*分", time_text)
      if min_match:
        minutes_left = int(min_match.group(1))

    # ★ 残り「5分〜59分」のみ抽出（直前スナイプのターゲット層）
    if minutes_left is None or not (5 <= minutes_left <= 59):
      continue

    # タイトルの物理NGチェック
    if any(ng.lower() in title.lower() for ng in NG_KEYWORDS):
      continue

    target_items.append({
        "id": item_id,
        "title": title,
        "price": price,
        "url": url,
        "time_left": f"{minutes_left}分",
    })

  print(
      f"🎯 抽出成功: 該当商品 {len(target_items)}件（この商品のみ詳細解析します）",
      flush=True,
  )

  # --- 【第2段階】抽出された本命商品のみ詳細取得 ＆ Gemini判定 ---
  for idx, target in enumerate(target_items, 1):
    print(
        f"\n[{idx}/{len(target_items)}] 🎯 解析中: 残り{target['time_left']} |"
        f" 価格: {target['price']}円 | {target['title'][:30]}...",
        flush=True,
    )

    try:
      seller_name, description, images = fetch_detail_page(page, target["url"])

      text_to_check = f"{target['title']} {description}".lower()
      found_ng = [ng for ng in NG_KEYWORDS if ng.lower() in text_to_check]
      if found_ng:
        print(
            f"  ⏩ 本文NGワード検出のためスキップ: {', '.join(found_ng)}",
            flush=True,
        )
        seen_items.add(target["id"])
        save_seen_items(seen_items)
        continue

      print(
          f"🤖 Geminiで目利き試算中 (画像{len(images)}枚)...", flush=True
      )
      g_result = analyze_gshock_with_gemini(
          target["title"], description, images, target["price"]
      )

      if g_result == "QUOTA_EXCEEDED":
        print("⛔ API制限のため処理を安全に停止します。", flush=True)
        break

      if g_result:
        score = g_result.get("condition_score", "")
        auth = g_result.get("authenticity_status", "")

        try:
          max_target = int(g_result.get("max_bid_price_target", 0))
        except Exception:
          max_target = 0

        if target["price"] >= max_target and max_target > 0:
          print(
              f"  ⚠️ 赤字判定補正: 現在価格({target['price']}円) >="
              f" 推奨上限額({max_target}円)",
              flush=True,
          )
          score = "C（不可）"

        print(
            f"  └ 最終判定: {score} | 純正性: {auth} | 推奨上限:"
            f" {max_target}円",
            flush=True,
        )
        print(f"  └ 添削理由: {g_result.get('reasoning')}", flush=True)

        if "A" in score or "B" in score:
          item_data = {
              "id": target["id"],
              "title": target["title"],
              "price": target["price"],
              "sale_type": sale_type_label,
              "time_left": target["time_left"],
              "url": target["url"],
          }
          send_discord_notification(item_data, g_result, seller_name)
        else:
          print("  ⏩ スルー（利益なし/社外品/偽物疑い）", flush=True)

      seen_items.add(target["id"])
      save_seen_items(seen_items)

    except Exception as e:
      print(f"❌ 詳細解析エラー: {e}", flush=True)

    time.sleep(1)


# --------------------------------------------------
# 6. メイン実行
# --------------------------------------------------
def main():
  print(
      "🚀 ヤフオク G-SHOCK仕入れリサーチ（8,000円以下・5〜59分抽出・5枚解析版）を開始します...",
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
        ),
        viewport={"width": 1280, "height": 800},
        locale="ja-JP",
    )
    page = context.new_page()

    process_auction_list(page, URL_AUCTION, "ヤフオク(残り5〜59分)", seen_items)

    browser.close()

  print("\n✨ リサーチ処理が完了しました。", flush=True)


if __name__ == "__main__":
  main()

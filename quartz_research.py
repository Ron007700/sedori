import json
import os
import re
import time
from io import BytesIO

from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from PIL import Image
from playwright.sync_api import sync_playwright
import requests

# --------------------------------------------------
# 1. 設定値・定数 & API設定
# --------------------------------------------------
API_KEY = os.environ.get("GEMINI_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

client = genai.Client(api_key=API_KEY) if API_KEY else None

MAX_PRICE_LIMIT = 10000

# ヤフオク（クォーツ / オークションのみ / 終了が近い順）
URL_AUCTION = f"https://auctions.yahoo.co.jp/search/search?p=%E3%82%AF%E3%82%A9%E3%83%BC%E3%83%84&max={MAX_PRICE_LIMIT}&is_auction=1&s1=end&o1=a"

SEEN_FILE = "seen_quartz.json"
MAX_AUCTION_ITEMS = 20

TARGET_KEYWORDS = [
    # SEIKO
    "SEIKO",
    "セイコー",
    "7T92",
    "7T62",
    "クロノグラフ",
    "SPEEDMASTER",
    "スピードマスター",
    "DOLCE",
    "ドルチェ",
    "SUS",
    "SPIRIT",
    "スピリット",
    "KINETIC",
    "キネティック",
    # CITIZEN
    "CITIZEN",
    "シチズン",
    "PROMASTER",
    "プロマスター",
    "ATTESA",
    "アテッサ",
    # CASIO
    "EDIFICE",
    "エディフィス",
    "PROTREK",
    "プロトレック",
    "OCEANUS",
    "オシアナス",
    # ブランド
    "HAMILTON",
    "ハミルトン",
    "BURBERRY",
    "バーバリー",
    "DIESEL",
    "ディーゼル",
    "NIXON",
    "ニクソン",
    "TISSOT",
    "ティソ",
    "TAG HEUER",
    "タグホイヤー",
    "GUCCI",
    "グッチ",
]

NG_KEYWORDS = [
    "液漏れ",
    "錆",
    "サビ",
    "リューズ破損",
    "リューズ動かない",
    "針外れ",
    "ガラス割れ",
    "風防割れ",
    "オーバーホール前提",
    "OH前提",
    "パーツ取り",
    "コピー",
    "偽物",
]


# --------------------------------------------------
# 2. ヘルパー関数
# --------------------------------------------------
def load_seen_items():
  if os.path.exists(SEEN_FILE):
    try:
      with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))
    except Exception:
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
      else "特筆なし"
  )

  message = f"""
🔥 **【ヤフオク】仕入れ候補 クォーツ腕時計 発見！** 🔥
----------------------------------------
🏪 **出品者**: {seller_name}
📌 **商品名**: {item['title']}
💰 **現在価格**: {item['price']:,}円
⏰ **残り時間**: {item['time_left']}
🏷 **ヒット属性**: {item['matched_keyword']}
🔗 **URL**: {item['url']}

⌚ **ブランド/モデル**: {g_result.get('brand_model', '不明')}
🔋 **電池交換稼働見込み**: {g_result.get('movable_probability', '不明')}
📊 **総合評価**: **{g_result.get('condition_score', '-')}**
⚠️ **状態フラグ**: {risk_text}

💵 **想定売価(動作時)**: {g_result.get('estimated_resale_normal', '-')}円
📈 **想定純利益**: **{g_result.get('estimated_profit', '-')}円** (利益率: {g_result.get('profit_margin', '-')}%)
🎯 **推奨購入上限額**: **{g_result.get('max_bid_price_target', '-')}円**
💡 **目利き添削理由**: {g_result.get('reasoning', '-')}
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
      print(f"❌ Discord通知エラー: {res.status_code}")
  except Exception as e:
    print(f"❌ Discord送信例外: {e}")


# --------------------------------------------------
# 3. Gemini 解析
# --------------------------------------------------
def analyze_quartz_with_gemini(title, description, images, price):
  if not client:
    print("❌ Gemini APIキーが読み込めていません")
    return None

  prompt = f"""
あなたはクォーツ（電池式）腕時計の転売・整備目利き専門家です。
添付された画像と商品タイトル・説明文を詳細に解析し、電池交換で稼働させて高利益が得られるか仕入れ判定を行ってください。

【査定・判断条件】
1. 電池交換による復調の見込（液漏れ跡の有無、文字盤のきれいさ、リューズ状態）。
2. 想定売価はメルカリでの過去相場（動作確認済み・美品〜並品）をベースに算出。
3. 利益計算ルール:
   - コスト: 手数料10%、送料梱包代(ネコポス/ゆうパケット)250円、仕入れ送料500円、電池代150円。
   - 【絶対条件1】手取り純利益が 2,000円 以上 であること。
   - 【絶対条件2】利益率（純利益 ÷ 想定売価）が 25% 以上 であること。
4. 条件を満たさない場合、または液漏れ・故障リスクが高い場合は「C（不可）」と判定してください。

以下のJSON形式でのみ回答してください：
{{
  "brand_model": "ブランド名・型番（例: SEIKO 7T92-0CF0）",
  "movable_probability": "高（電池切れの可能性大） / 中 / 低（基板・機械故障の懸念あり）",
  "risk_flags": ["外観小傷あり", "ベルト社外品", "動作未確認" などのリスク],
  "condition_score": "A（推奨） / B（慎重） / C（不可）",
  "estimated_resale_normal": 8500,
  "estimated_profit": 2800,
  "profit_margin": 32.9,
  "max_bid_price_target": 4500,
  "reasoning": "利益計算および電池交換稼働見込みの判定理由（100文字以内）"
}}

【商品タイトル】: {title}
【商品説明文】: {description}
【現在価格】: {price}円
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
        time.sleep((attempt + 1) * 2)
      else:
        print(f"❌ Gemini解析失敗: {e}")
        return None


# --------------------------------------------------
# 4. 詳細ページ取得
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

  img_urls = img_urls[:5]

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
# 5. リスト取得・精査（デバッグログ付き）
# --------------------------------------------------
def process_auction_list(
    page, target_url, max_limit, sale_type_label, seen_items
):
  print(f"\n🔍 【{sale_type_label}】検索URLへアクセス中: {target_url}")

  try:
    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
  except Exception as e:
    print(f"❌ ページ移動エラー: {e}")
    return

  time.sleep(2)

  html = page.content()
  soup = BeautifulSoup(html, "html.parser")

  # ヤフオクの商品要素を取得
  items = soup.select("li.Product") or soup.select(".Product")

  if not items:
    print("⚠️ 商品要素が見つかりませんでした。HTMLの構造が変わった可能性があります。")
    return

  print(f"📦 検出件数: {len(items)}件 (精査開始)")

  processed_count = 0

  for idx, item in enumerate(items, 1):
    if processed_count >= max_limit:
      print(f"⏱ 上限{max_limit}件に達したため完了。")
      break

    # タイトル
    title_tag = item.select_one(".Product__titleLink") or item.select_one("a")
    if not title_tag:
      continue
    title = title_tag.get_text(strip=True)
    url = title_tag.get("href", "")

    # 価格
    price_tag = item.select_one(".Product__priceValue") or item.select_one(
        "[class*='price']"
    )
    if not price_tag:
      continue
    price_digits = re.sub(r"[^\d]", "", price_tag.get_text(strip=True))
    if not price_digits:
      continue
    price = int(price_digits)

    # 残り時間
    time_tag = item.select_one(".Product__time") or item.select_one(
        "[class*='time']"
    )
    time_text = (
        time_tag.get_text(strip=True)
        if time_tag
        else item.get_text(" ", strip=True)
    )

    # ID抽出
    item_id = item.get("data-auction-id")
    if not item_id and url:
      match = re.search(r"/auction/([a-zA-Z0-9]+)", url)
      item_id = match.group(1) if match else url

    # デバッグ判定
    if item_id in seen_items:
      continue

    # キーワードチェック
    matched_keyword = None
    for kw in TARGET_KEYWORDS:
      if kw.lower() in title.lower():
        matched_keyword = kw
        break

    if not matched_keyword:
      # キーワード不一致のデバッグ表示（最初の5件のみ表示）
      if idx <= 5:
        print(f"  [#{idx} スキップ] 対象キーワードなし: {title[:20]}...")
      continue

    # 残り時間判定（日・時間が含まれるものはまだ時間が残っているためスキップ）
    if "日" in time_text or "時間" in time_text:
      print(
          f"  [#{idx} スキップ] 残り時間が長いため: {time_text} |"
          f" {title[:20]}"
      )
      continue

    min_match = re.search(r"(\d+)\s*分", time_text)
    if not min_match:
      print(f"  [#{idx} スキップ] 時間取得不可: {time_text} | {title[:20]}")
      continue

    minutes_left = int(min_match.group(1))
    time_left_str = f"{minutes_left}分"

    if any(ng.lower() in title.lower() for ng in NG_KEYWORDS):
      print(f"  [#{idx} スキップ] NGワード検出(タイトル): {title[:20]}")
      continue

    # 条件突破！
    processed_count += 1
    print(
        f"\n🎯 ターゲット検知 [{processed_count}/{max_limit}]:"
        f" 残り{time_left_str} | [{matched_keyword}] | {price}円 |"
        f" {title[:25]}..."
    )

    try:
      seller_name, description, images = fetch_detail_page(page, url)

      text_to_check = f"{title} {description}".lower()
      found_ng = [ng for ng in NG_KEYWORDS if ng.lower() in text_to_check]
      if found_ng:
        print(f"  ⏩ 本文NGワードのためスキップ: {', '.join(found_ng)}")
        seen_items.add(item_id)
        save_seen_items(seen_items)
        continue

      print("🤖 GeminiでAI目利き試算中...")
      g_result = analyze_quartz_with_gemini(title, description, images, price)

      if g_result:
        score = g_result.get("condition_score", "")
        profit = g_result.get("estimated_profit", 0)
        margin = g_result.get("profit_margin", 0)

        try:
          max_target = int(g_result.get("max_bid_price_target", 0))
        except Exception:
          max_target = 0

        if price >= max_target or profit < 2000 or margin < 25.0:
          score = "C（不可）"

        print(
            f"  └ 判定: {score} | 想定利益: {profit}円 ({margin}%) | 推奨上限:"
            f" {max_target}円"
        )
        print(f"  └ 理由: {g_result.get('reasoning')}")

        if "A" in score or "B" in score:
          item_data = {
              "id": item_id,
              "title": title,
              "price": price,
              "sale_type": sale_type_label,
              "matched_keyword": matched_keyword,
              "time_left": time_left_str,
              "url": url,
          }
          send_discord_notification(item_data, g_result, seller_name)
        else:
          print("  ⏩ スルー（利益条件未達）")

      seen_items.add(item_id)
      save_seen_items(seen_items)

    except Exception as e:
      print(f"❌ 詳細解析エラー: {e}")

    time.sleep(1)


# --------------------------------------------------
# 6. メイン実行処理
# --------------------------------------------------
def main():
  print("🚀 ヤフオク クォーツ高利益リサーチを開始します...")
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
        "ヤフオク(終了間近)",
        seen_items,
    )

    browser.close()

  print("\n✨ リサーチ処理が完了しました。")


if __name__ == "__main__":
  main()

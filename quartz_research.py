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

client = genai.Client(api_key=API_KEY)

# 検索上限価格（高利益狙いのため上限10,000円までに設定）
MAX_PRICE_LIMIT = 10000

# オークション専用URL（クォーツ / 競売のみ / 残り時間の短い順）
URL_AUCTION = f"https://auctions.yahoo.co.jp/search/search?p=%E3%82%AF%E3%82%A9%E3%83%BC%E3%83%84&max={MAX_PRICE_LIMIT}&is_auction=1&s1=end&o1=a"

SEEN_FILE = "seen_quartz.json"

MAX_AUCTION_ITEMS = 20

# 手取り2000円以上・利益率25%以上を狙える高価値クォーツ用キーワード
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
    # 海外ブランド・人気ファッション
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

# 修理不可・重度ジャンクの除外キーワード
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
      print(f"❌ Discord通知エラー: {res.status_code}, {res.text}")
  except Exception as e:
    print(f"❌ Discord送信例外: {e}")


# --------------------------------------------------
# 3. Gemini 解析 (高利益・電池交換条件)
# --------------------------------------------------
def analyze_quartz_with_gemini(title, description, images, price):
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
        wait_time = (attempt + 1) * 3
        print(
            f"  ⚠️ Gemini一時的エラー。{wait_time}秒後に再試行します..."
            f" ({attempt + 1}/{max_retries})"
        )
        time.sleep(wait_time)
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

  seller_tag = soup.find(
      "a", class_=re.compile("Seller__name|Seller__link")
  ) or soup.find("p", class_=re.compile("Seller"))
  seller_name = seller_tag.text.strip() if seller_tag else "不明出品者"

  desc_tag = soup.find(
      "div", class_="ProductExplanation__commentArea"
  ) or soup.find("section", class_="ProductExplanation")
  description = desc_tag.text.strip() if desc_tag else "説明文なし"

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
          " (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
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
# 5. リスト取得・精査（残り10分〜59分判定の改善版）
# --------------------------------------------------
def process_auction_list(
    page, target_url, max_limit, sale_type_label, seen_items
):
  print(f"\n🔍 【{sale_type_label}】検索URLへアクセス中: {target_url}")

  try:
    page.goto(target_url, wait_until="networkidle", timeout=30000)
  except Exception:
    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

  for i in range(1, 4):
    page.evaluate(f"window.scrollTo(0, {i * 800});")
    time.sleep(0.8)

  html = page.content()
  soup = BeautifulSoup(html, "html.parser")

  items = (
      soup.select("li.Product")
      or soup.select(".Product")
      or soup.select("div[data-auction-id]")
      or soup.select("li[class*='Product']")
  )

  if not items:
    items = soup.find_all("a", re.compile("Product__titleLink"))

  print(f"📦 検出件数: {len(items)}件（残り10〜59分の対象を精査）")

  processed_count = 0

  for item in items:
    if processed_count >= max_limit:
      print(f"⏱️️ 上限{max_limit}件に達したため完了。")
      break

    if item.name == "a":
      title_tag = item
      parent = item.find_parent("li") or item.find_parent("div")
      price_tag = (
          parent.select_one("[class*='price'] or [class*='Price']")
          if parent
          else None
      )
      time_tag = (
          parent.select_one("[class*='time'] or [class*='Time']")
          if parent
          else None
      )
    else:
      title_tag = (
          item.select_one(".Product__titleLink")
          or item.select_one("a[data-auction-title]")
          or item.find("a", class_=re.compile("title", re.I))
      )
      price_tag = (
          item.select_one(".Product__priceValue")
          or item.select_one("[class*='price']")
          or item.select_one("[class*='Price']")
      )
      time_tag = item.select_one(".Product__time") or item.select_one(
          "[class*='time']"
      )

    if not (title_tag and price_tag):
      continue

    title = title_tag.get_text(strip=True)
    price_raw = price_tag.get_text(strip=True)

    price_digits = re.sub(r"[^\d]", "", price_raw)
    if not price_digits:
      continue
    price = int(price_digits)

    if price > MAX_PRICE_LIMIT:
      continue

    url = title_tag.get("href", "")

    item_id = item.get("data-auction-id")
    if not item_id and url:
      match = re.search(r"/auction/([a-zA-Z0-9]+)", url)
      if match:
        item_id = match.group(1)
      else:
        item_id = url.split("/")[-1].split("?")[0]

    if not item_id or item_id in seen_items:
      continue

    # --------------------------------------------------
    # 残り時間判定（ピンポイント取得）
    # --------------------------------------------------
    time_text = (
        time_tag.get_text(strip=True)
        if time_tag
        else item.get_text(" ", strip=True)
    )

    # 「日」や「時間」が含まれている場合はスキップ（1時間以上残っている）
    if "日" in time_text or "時間" in time_text:
      continue

    # 「分」の数字を抽出
    min_match = re.search(r"(\d+)\s*分", time_text)
    if not min_match:
      continue

    minutes_left = int(min_match.group(1))
    if not (10 <= minutes_left < 60):
      continue

    time_left_str = f"{minutes_left}分"

    if any(ng.lower() in title.lower() for ng in NG_KEYWORDS):
      continue

    matched_keyword = None
    for kw in TARGET_KEYWORDS:
      if kw.lower() in title.lower():
        matched_keyword = kw
        break

    if not matched_keyword:
      continue

    processed_count += 1
    print(
        f"🎯 狙い目 [{sale_type_label} {processed_count}/{max_limit}]:"
        f" 残り{time_left_str} | [{matched_keyword}] | 価格: {price}円 |"
        f" {title[:25]}..."
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

      print("🤖 Gemini 3.6 Flashでクォーツ高利益試算中...")
      g_result = analyze_quartz_with_gemini(title, description, images, price)

      if g_result:
        score = g_result.get("condition_score", "")
        profit = g_result.get("estimated_profit", 0)
        margin = g_result.get("profit_margin", 0)

        try:
          max_target = int(g_result.get("max_bid_price_target", 0))
        except Exception:
          max_target = 0

        # 条件（手取り2000円以上、利益率25%以上）を満たさない場合補正
        if price >= max_target or profit < 2000 or margin < 25.0:
          score = "C（不可）"

        print(
            f"  └ 最終判定: {score} | 想定利益: {profit}円 ({margin}%) |"
            f" 推奨上限: {max_target}円"
        )
        print(f"  └ 添削理由: {g_result.get('reasoning')}")

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
          print("  ⏩ スルー（利益条件未達/不適格のため通知なし）")

      seen_items.add(item_id)
      save_seen_items(seen_items)

    except Exception as e:
      print(f"❌ 詳細解析エラー: {e}")

    time.sleep(1)


# --------------------------------------------------
# 6. メイン実行処理
# --------------------------------------------------
def main():
  print("🚀 ヤフオク クォーツ高利益リサーチ（残り10〜59分前）を開始します...")
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

    process_auction_list(
        page,
        URL_AUCTION,
        MAX_AUCTION_ITEMS,
        "ヤフオク(終了10〜59分前)",
        seen_items,
    )

    browser.close()

  print("\n✨ すべてのリサーチが正常に完了しました。")


if __name__ == "__main__":
  main()

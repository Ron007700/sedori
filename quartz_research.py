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

# ==================================================
# 1. 設定値・定数 & API設定
# ==================================================
API_KEY = os.environ.get("GEMINI_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

client = genai.Client(api_key=API_KEY) if API_KEY else None

MIN_PRICE_LIMIT = 1000   # 最低価格（ジャンク雑貨排除）
MAX_PRICE_LIMIT = 10000  # 最高価格

SEEN_FILE = "seen_quartz.json"
MAX_ITEMS_PER_BRAND = 20  # 各ブランド上位20件をスキャン

# ★ 巡回対象のブランド・メーカーリスト
BRANDS = [
    "SEIKO",
    "セイコー",
    "CITIZEN",
    "シチズン",
    "CASIO",
    "カシオ",
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

# ★ 検索時マイナス検索クエリ（ノイズ・まとめ売りの完全排除）
EXCLUDE_QUERY = "-まとめ -セット -大量 -山 -まとめ売り -福袋 -オマージュ -コピー -風 -互換"

# ★ Python側での強力除外キーワード（レディース・置き時計・パーツ等）
NG_KEYWORDS = [
    # 機械・壊れすぎリスク
    "液漏れ", "錆", "サビ", "リューズ破損", "リューズ動かない",
    "針外れ", "ガラス割れ", "風防割れ", "オーバーホール前提", "OH前提",
    "パーツ取り", "コピー", "偽物",
    # リセール単価が低い/対象外カテゴリ
    "レディース", "ウィメンズ", "女性", "キッズ", "子供",
    "掛け時計", "置時計", "クロック", "目覚まし",
    "ベルトのみ", "コマ", "空箱", "尾錠", "ケースのみ", "ジャンク品" # 単体ジャンクの重複表現対策
]


# ==================================================
# 2. ヘルパー関数
# ==================================================
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
    print(f"⚠️ 既読ファイルの保存エラー: {e}", flush=True)


def send_discord_notification(item, g_result, seller_name):
  if not DISCORD_WEBHOOK_URL:
    print("❌ Discord Webhook URLが未設定です", flush=True)
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
🔍 **検索ブランド**: {item['brand']}
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
      print(f"✅ Discord通知送信完了: {item['title'][:20]}", flush=True)
    else:
      print(f"❌ Discord通知エラー: {res.status_code}", flush=True)
  except Exception as e:
    print(f"❌ Discord送信例外: {e}", flush=True)


# ==================================================
# 3. Gemini 解析 (429エラーハンドリング付き)
# ==================================================
def analyze_quartz_with_gemini(title, description, images, price):
  if not client:
    print("❌ Gemini APIキーが読み込めていません", flush=True)
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

  try:
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=images + [prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0.1
        ),
    )
    return json.loads(response.text)

  except APIError as e:
    if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
      print("⚠️ 【Quota上限検知】Gemini APIの制限枠に達しました。", flush=True)
      print("   以降の解析を中断し、スクリプトを安全に終了します。", flush=True)
      return "QUOTA_EXCEEDED"
    else:
      print(f"❌ Gemini APIエラー: {e}", flush=True)
      return None
  except Exception as e:
    print(f"❌ Gemini解析例外: {e}", flush=True)
    return None


# ==================================================
# 4. 詳細ページ取得
# ==================================================
def fetch_detail_page(page, url):
  page.goto(url, wait_until="domcontentloaded", timeout=15000)
  time.sleep(1)

  html = page.content()
  soup = BeautifulSoup(html, "html.parser")

  seller_tag = soup.select_one(".Seller__name, .Seller__link, [class*='Seller']")
  seller_name = seller_tag.get_text(strip=True) if seller_tag else "不明出品者"

  desc_tag = soup.select_one(".ProductExplanation__commentArea, .ProductExplanation")
  description = desc_tag.get_text(strip=True) if desc_tag else "説明文なし"

  img_urls = []
  for img in soup.find_all("img"):
    src = img.get("src") or img.get("data-src") or ""
    if "auctions.c.yimg.jp" in src:
      clean_url = src.split("?")[0]
      if clean_url not in img_urls and not clean_url.endswith(".gif"):
        img_urls.append(clean_url)

  img_urls = img_urls[:3]

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


# ==================================================
# 5. ブランド別巡回・精査処理（残り10分〜3時間対象）
# ==================================================
def process_brand_query(page, brand, seen_items):
  search_phrase = f"{brand} クォーツ ジャンク {EXCLUDE_QUERY}"
  encoded_kw = requests.utils.quote(search_phrase)

  # メンズ腕時計カテゴリ（23140）+ 1,000円〜10,000円 + 残り時間の短い順（s1=end&o1=a）
  target_url = f"https://auctions.yahoo.co.jp/search/search?p={encoded_kw}&category_id=23140&min={MIN_PRICE_LIMIT}&max={MAX_PRICE_LIMIT}&is_auction=1&s1=end&o1=a&n=50"

  print(f"\n🔍 検索ブランド: 【{brand}】へアクセス中...", flush=True)

  try:
    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
  except Exception as e:
    print(f"❌ ページ移動エラー: {e}", flush=True)
    return True

  time.sleep(1.5)

  html = page.content()
  soup = BeautifulSoup(html, "html.parser")
  items = soup.select("li.Product") or soup.select(".Product")

  if not items:
    print("  ⚠️ 対象商品が見つかりませんでした。", flush=True)
    return True

  # 上位20件をスキャン対象とする
  items = items[:MAX_ITEMS_PER_BRAND]
  print(f"📦 【{brand}】の上位{len(items)}件をスキャン中...", flush=True)

  processed_count = 0

  for idx, item in enumerate(items, 1):
    title_tag = item.select_one(".Product__titleLink") or item.select_one("a")
    if not title_tag:
      continue
    title = title_tag.get_text(strip=True)
    url = title_tag.get("href", "")

    price_tag = item.select_one(".Product__priceValue") or item.select_one("[class*='price']")
    if not price_tag:
      continue
    price_digits = re.sub(r"[^\d]", "", price_tag.get_text(strip=True))
    if not price_digits:
      continue
    price = int(price_digits)

    time_tag = item.select_one(".Product__time") or item.select_one("[class*='time']")
    time_text = time_tag.get_text(strip=True) if time_tag else item.get_text(" ", strip=True)

    item_id = item.get("data-auction-id")
    if not item_id and url:
      match = re.search(r"/auction/([a-zA-Z0-9]+)", url)
      item_id = match.group(1) if match else url

    if item_id in seen_items:
      continue

    # ★ フィルター1: 「日」が含まれるものは即スキップ（1日以上の残り時間）
    if "日" in time_text:
      continue

    minutes_left = None

    # 残り時間表現のパース（「1時間20分」「45分」など）
    hour_match = re.search(r"(\d+)時間(?:(\d+)分)?", time_text)
    min_match = re.search(r"^(\d+)分|[\s](\d+)分|(\d+)分", time_text)

    if hour_match:
      hours = int(hour_match.group(1))
      mins = int(hour_match.group(2)) if hour_match.group(2) else 0
      minutes_left = hours * 60 + mins
    elif min_match:
      # 該当するマッチグループを取得
      mins_val = next(g for g in min_match.groups() if g is not None)
      minutes_left = int(mins_val)

    # ★ フィルター2: 残り時間が「10分〜180分（10分前〜3時間未満）」以外はスキップ
    if minutes_left is None or not (10 <= minutes_left < 180):
      continue

    if minutes_left >= 60:
      time_left_str = f"{minutes_left // 60}時間{minutes_left % 60}分"
    else:
      time_left_str = f"{minutes_left}分"

    # タイトルのNGワードチェック
    if any(ng.lower() in title.lower() for ng in NG_KEYWORDS):
      continue

    # --- 条件クリア：精査ターゲット検知 ---
    processed_count += 1
    print(
        f"\n🎯 ターゲット検知 [{processed_count}件目]: 残り{time_left_str} | {price}円 | {title[:25]}...",
        flush=True,
    )

    try:
      seller_name, description, images = fetch_detail_page(page, url)

      # 本文も含めたNGワード最終チェック
      text_to_check = f"{title} {description}".lower()
      found_ng = [ng for ng in NG_KEYWORDS if ng.lower() in text_to_check]
      if found_ng:
        print(f"  ⏩ 本文NGワードのためスキップ: {', '.join(found_ng)}", flush=True)
        seen_items.add(item_id)
        save_seen_items(seen_items)
        continue

      print("🤖 GeminiでAI目利き試算中...", flush=True)
      g_result = analyze_quartz_with_gemini(title, description, images, price)

      if g_result == "QUOTA_EXCEEDED":
        print("⛔ API上限到達のため処理を安全停止します。", flush=True)
        return False

      if g_result:
        score = g_result.get("condition_score", "")
        profit = g_result.get("estimated_profit", 0)
        margin = g_result.get("profit_margin", 0)

        try:
          max_target = int(g_result.get("max_bid_price_target", 0))
        except Exception:
          max_target = 0

        # 赤字・利益未達の判定補正
        if price >= max_target or profit < 2000 or margin < 25.0:
          score = "C（不可）"

        print(
            f"  └ 判定: {score} | 想定利益: {profit}円 ({margin}%) | 推奨上限: {max_target}円",
            flush=True,
        )
        print(f"  └ 理由: {g_result.get('reasoning')}", flush=True)

        if "A" in score or "B" in score:
          item_data = {
              "id": item_id,
              "title": title,
              "price": price,
              "brand": brand,
              "time_left": time_left_str,
              "url": url,
          }
          send_discord_notification(item_data, g_result, seller_name)
        else:
          print("  ⏩ スルー（利益条件未達）", flush=True)

      seen_items.add(item_id)
      save_seen_items(seen_items)

    except Exception as e:
      print(f"❌ 詳細解析エラー: {e}", flush=True)

    time.sleep(1)

  return True


# ==================================================
# 6. メイン実行処理
# ==================================================
def main():
  print("🚀 ヤフオク クォーツジャンク高利益リサーチ（ブランド順巡回・10分〜3時間版）を開始します...", flush=True)
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

    for brand in BRANDS:
      success = process_brand_query(page, brand, seen_items)
      if not success:
        break

    browser.close()

  print("\n✨ すべてのブランドの巡回が完了しました。", flush=True)


if __name__ == "__main__":
  main()

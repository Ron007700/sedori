name: 2ndstreet G-SHOCK Scraper (Cron)

on:
  schedule:
    # 日本時間 17:00〜21:00 の30分おき（UTC 08:00〜12:00）
    - cron: '0,30 8-12 * * *'
  workflow_dispatch: # 手動実行ボタンを有効化

jobs:
  run-scraper:
    runs-on: ubuntu-latest

    steps:
    - name: リポジトリのチェックアウト
      uses: actions/checkout@v4
      with:
        ref: master  # masterブランチを指定

    - name: Python環境のセットアップ
      uses: actions/setup-python@v5
      with:
        python-version: '3.10'

    - name: 依存ライブラリのインストール
      run: |
        python -m pip install --upgrade pip
        pip install requests beautifulsoup4 cloudscraper

    - name: スクレイピング実行
      env:
        DISCORD_WEBHOOK_URL_2NDSTREET: ${{ secrets.DISCORD_WEBHOOK_URL_2NDSTREET }}
      run: |
        python 2ndstreet_gshock.py

    - name: 重複チェック用ファイルの自動更新・コミット
      run: |
        git config --global user.name "github-actions[bot]"
        git config --global user.email "github-actions[bot]@users.noreply.github.com"
        git add seen_items_2ndstreet.json || true
        git diff --staged --quiet || (git commit -m "Update seen items" && git push)

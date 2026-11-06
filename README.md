# watch

URL の変化を見張って Discord に知らせる。GitHub Actions で 10 分ごとに動く。

対応しているのは 2 種類。

| adapter | 内容 |
|---|---|
| `static_html` | ページ本文のハッシュ比較（script / style は無視） |
| `playwright_grow` | JS で描画される予約枠の空き状況 |

## 使い方

リポジトリの Settings → Secrets and variables → Actions に 2 つ登録する。

| Secret | 中身 |
|---|---|
| `DISCORD_WEBHOOK_URL` | Discord のウェブフック URL |
| `WATCH_CONFIG` | 監視対象の YAML（下記） |

監視対象をリポジトリに置かないので、公開しても何を見ているかは分からない。

```yaml
collections:
  - name: pages
    mode: watch
    adapter: static_html
    schedule:
      skip_hours_jst: [7]        # この時間帯は動かさない
    notify:
      templates:
        changed: "[{label}] 更新があります"
        first_detect: "[{label}] 更新があります"
    sources:
      - url: https://example.com/a.html
        label: A                  # 通知に出る名前。省略可
```

`{label}` と `{url}` がテンプレートで使える。URL は通知本文ではなく
埋め込みに入る（スマホのプッシュ通知が URL で埋まらないように）。

## 手元で試す

```bash
pip install -r requirements.txt
python -m playwright install chromium
WATCH_CONFIG="$(cat config.yaml)" python watch.py --dry-run
```

`--dry-run` は通知を送らずに取得と差分判定だけ行う。

## 状態

前回の指紋は `state.json` に置く。Actions では `actions/cache` で
実行間を受け渡す。通知に失敗した場合は指紋を戻すので、次回あらためて
通知される（取りこぼさない）。

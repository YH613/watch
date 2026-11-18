# watch

URL の変化を見張って Discord に知らせる。GitHub Actions で動く。
**`schedule` だけでは 10 分ごとに回らない**（下記「実行間隔」）。

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

## 実行間隔

**`schedule:` の cron は無料枠では額面通りに動かない。**
`*/10 * * * *`（10 分ごと）で 11 日間計測した実測値:

| 項目 | 実測 |
|---|---|
| 実行回数 | 47 回（10 分ごとなら 1,584 回） |
| 間隔の中央値 | **382 分（6.4 時間）** |
| 最小 / 最大 | 33 分 / 629 分 |

予約枠のような早い者勝ちの対象には間に合わない（実際に、空きを見つけてから
40 分で埋まった）。

GitHub が遅延させているのは `schedule` で、**API 経由の `workflow_dispatch`
には効かない**。そこで外部の cron サービスから API を叩いて起動する。

### 外部 cron から起動する

**1. 細粒度の個人アクセストークンを作る**

https://github.com/settings/personal-access-tokens/new

- Repository access: **Only select repositories** → このリポジトリだけ
- Permissions → Repository permissions → **Actions: Read and write**
- 他の権限は付けない

**2. cron サービスに登録する**（cron-job.org など。1 分間隔まで無料）

| 項目 | 値 |
|---|---|
| URL | `https://api.github.com/repos/<owner>/<repo>/actions/workflows/watch.yml/dispatches` |
| Method | `POST` |
| Header | `Authorization: Bearer <トークン>` |
| Header | `Accept: application/vnd.github+json` |
| Header | `X-GitHub-Api-Version: 2022-11-28` |
| Body | `{"ref":"main"}` |
| 間隔 | 10 分ごと |

成功すると **204 No Content** が返る（本文は空）。

**3. `schedule` は消さずに残す。** cron サービスが止まったときの保険になり、
そのときに下記の間隔監視が働く。

### 止まったことに気づく仕組み

外部 cron に任せると、**cron サービスの停止やトークンの期限切れで
「実行自体が起きない」**。通知が来ないだけなので、空きが無いのと区別がつかない。
実際にこの形で 10 日気づけなかったことがある。

そこで state に前回実行時刻を持ち、間隔が開いたら Discord で知らせる。

- 既定は **60 分**を超えたら通知（`WATCH_ALERT_GAP_MINUTES` で変更できる）
- 止まっている間ずっと鳴らないよう、**12 時間に 1 回**まで
- `schedule` の保険があるので、cron が死んでいても通知は届く

取得の失敗も同様に Discord へ送る（`notify_health`）。同じ故障で
鳴り続けないよう、エラーの指紋が変わったときだけ送り、復旧も知らせる。

**黙って 0 件を返す実装にしないこと。** 故障が「異常なし」と同じ見た目になる。

## 状態

前回の指紋は `state.json` に置く。Actions では `actions/cache` で
実行間を受け渡す。通知に失敗した場合は指紋を戻すので、次回あらためて
通知される（取りこぼさない）。

`__` で始まるキーは監視対象ではなく内部用（最終実行時刻・エラーの指紋）。

踏んだ罠:

- **`actions/cache`（まとめ版）の保存は後処理で走るため、ジョブが失敗すると
  保存されない。** 状態が前に進まず、失敗が延々と続く（28 回連続で起きた）。
  `actions/cache/restore` と `actions/cache/save` に分け、save は `if: always()`
- **導入ステップを `if: cache-hit == 'true'` で飛ばすと、一度不完全な
  キャッシュが保存された時点から永久に壊れる**（キーが固定なら自然復旧しない）。
  `playwright install` は冪等で速いので、キャッシュがあっても毎回走らせ、
  起動確認まで行う
- ブラウザの起動失敗を各 source の try の外で起こすと、例外が main まで
  抜けて**状態ファイルが一切書かれない**。受け止めて「全 source がエラー」にする

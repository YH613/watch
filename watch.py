#!/usr/bin/env python3
"""URL の変化を見張って Discord に知らせる。

GitHub Actions から定期実行して使う。監視対象は環境変数 WATCH_CONFIG に
YAML で渡すので、このファイル自体には何も固有の情報が入らない。

状態（前回の指紋）は --state のファイルに置く。Actions では
actions/cache で前後に受け渡す。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))

# UA を送らないと 403 を返すサイトがある。旧実装はこれが無く、
# エラーページの指紋を正常値として記録していた
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}
EMBED_COLOR = 0x62A8E5


# ---------- 取得 ----------

def fetch_static_html(url: str, cfg: dict) -> tuple[str, dict]:
    """ページ本文のハッシュ。script/style は無視する。"""
    res = requests.get(url, timeout=int(cfg.get("timeout", 15)), headers=HEADERS)
    res.raise_for_status()
    if res.encoding and res.encoding.lower() in ("iso-8859-1", "ascii"):
        res.encoding = res.apparent_encoding or "utf-8"
    soup = BeautifulSoup(res.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text()
    return hashlib.md5(text.encode()).hexdigest(), {"length": len(text)}


# コース選択ボタン。スマホ用(.sp)が先に並んでおり非表示なので、
# class だけで掴むと見えない方に当たって何も起きない
CLICK_COURSE_JS = """() => {
  const btns = Array.from(document.querySelectorAll('a.selbtn'));
  const target = btns.find(b => b.offsetWidth || b.offsetHeight) || btns[0];
  if (!target) return false;
  target.click();
  return true;
}"""

CALENDAR_READY_JS = """() => {
  const t = document.querySelector('table');
  return !!(t && t.querySelectorAll('td').length);
}"""

# ○=空き / ×=予約済 / -=営業時間外。
# 日付は見出し行の th、時刻は各行の先頭セルから取る
EXTRACT_SLOTS_JS = """() => {
  const table = document.querySelector('table');
  if (!table) return null;
  const tds = table.querySelectorAll('td').length;
  if (!tds) return null;
  const rows = Array.from(table.querySelectorAll('tr'));
  let dates = [];
  for (const r of rows) {
    const ths = Array.from(r.querySelectorAll('th'))
      .map(x => (x.innerText || '').replace(/\\s+/g, ' ').trim());
    if (ths.filter(x => /\\d/.test(x)).length >= 5) { dates = ths; break; }
  }
  const open = [];
  let time = '';
  for (const r of rows) {
    const cells = Array.from(r.children);
    const head = (cells[0] && (cells[0].innerText || '').trim()) || '';
    if (/^\\d{1,2}:\\d{2}$/.test(head)) time = head;
    cells.forEach((c, i) => {
      if ((c.innerText || '').trim() === '\u25cb') {
        open.push({ date: dates[i] || '?', time: time });
      }
    });
  }
  return { open: open, cells: tds };
}"""


def fetch_grow_slots(page, url: str, timeout_ms: int, settle_ms: int) -> tuple[str, dict]:
    """予約枠の空きを読む。

    2026-11 にサイトのフローが変わり、?page=time を直接開いても
    「メニューを選択」の画面に着地するようになった。コースを選んで
    初めてカレンダーが描かれる（実測 6〜12 秒）。
    旧実装は span[1] をクリックして 5 秒待つだけだったため、
    カレンダーに到達せず「空き 0 枠」を正常値として記録し続けていた。
    固定待ちをやめ、描画されるまで待ち、出なければ例外にする
    （黙って 0 を返すと故障が通知の不在と区別できない）。
    """
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_selector("a.selbtn", state="attached", timeout=timeout_ms)
    if not page.evaluate(CLICK_COURSE_JS):
        raise RuntimeError("コース選択ボタンが無い（サイト構成の変更か）")
    page.wait_for_function(CALENDAR_READY_JS, timeout=timeout_ms)
    page.wait_for_timeout(settle_ms)
    data = page.evaluate(EXTRACT_SLOTS_JS)
    if not data:
        raise RuntimeError("カレンダーが描画されなかった")
    slots = sorted(
        ({"date": o["date"], "time": o["time"]} for o in data["open"]),
        key=lambda o: (o["date"], o["time"]),
    )
    # 指紋は空き枠だけから作る。× の並びを混ぜると無関係な変化で鳴る
    return json.dumps(slots, ensure_ascii=False, sort_keys=True), {
        "slot_count": len(slots),
        "slots": slots,
        "cells": data["cells"],
    }


# ---------- 通知 ----------

def format_message(templates: dict, label: str, url: str, first: bool) -> str:
    tpl = templates.get("first_detect" if first else "changed")
    if not tpl:
        return ""
    msg = tpl.format(url=url, label=label or "")
    # 名前が無いと [] だけ残るので畳む
    return re.sub(r"[\[［(（]\s*[\]］)）]\s*", "", msg)


def notify(webhook: str, message: str, links: list[tuple[str, str]]) -> None:
    """content はスマホのプッシュ通知に出る。URL は埋め込みへ逃がす。"""
    payload: dict[str, Any] = {"content": message}
    if links:
        payload["embeds"] = [
            {
                "description": "\n".join(
                    f"[{lab or u}]({u})" for lab, u in links
                )[:4000],
                "color": EMBED_COLOR,
            }
        ]
    res = requests.post(webhook, json=payload, timeout=20)
    res.raise_for_status()


ERROR_STATE_KEY = "__last_error_signature__"


def error_signature(errors: list[str]) -> str:
    """同じ故障で鳴り続けないための指紋。対象名と例外の種類だけで作る。"""
    parts = []
    for e in errors:
        who, _, rest = e.partition(": ")
        parts.append(f"{who}:{rest.split('(')[0].strip()[:60]}")
    return " | ".join(sorted(set(parts)))


def notify_health(webhook: str, state: dict, errors: list[str], dry: bool) -> None:
    """監視が壊れたら黙らずに知らせる。

    枠の検出が壊れていた間、ログ上は「変化なし・エラーなし」に見えており、
    通知が来ないことと空きが無いことが区別できなかった。
    故障が故障として届かないと、気づけるのは何日も後になる。
    同じ故障で鳴り続けないよう、指紋が変わったときだけ送る。
    """
    sig = error_signature(errors)
    if sig == state.get(ERROR_STATE_KEY, ""):
        return

    if sig:
        names = ", ".join(sorted({e.partition(": ")[0] for e in errors}))
        message = f"監視が失敗しています（{len(errors)}件）: {names}"
    else:
        message = "監視が復旧しました"

    if dry:
        print(f"  [dry-run] 健全性通知: {message}")
    else:
        try:
            # content はプッシュに出るので短く。詳細は埋め込みへ
            payload: dict[str, Any] = {"content": message}
            if errors:
                payload["embeds"] = [
                    {
                        "description": "\n".join(f"- {e}" for e in errors)[:4000],
                        "color": 0xE57373,
                    }
                ]
            res = requests.post(webhook, json=payload, timeout=20)
            res.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"  健全性の通知に失敗: {exc}", file=sys.stderr)
            return

    state[ERROR_STATE_KEY] = sig


# ---------- 本体 ----------

def should_skip(schedule: dict) -> str | None:
    hours = schedule.get("skip_hours_jst") or []
    hour = datetime.now(JST).hour
    return f"JST {hour}時台はスキップ設定" if hour in hours else None


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run_collection(coll: dict, state: dict, webhook: str, dry: bool) -> tuple[int, list[str]]:
    name = coll.get("name", "(無名)")
    adapter = coll.get("adapter", "static_html")
    cfg = coll.get("adapter_config") or {}
    notify_cfg = coll.get("notify") or {}
    templates = notify_cfg.get("templates") or {}
    errors: list[str] = []

    skip = should_skip(coll.get("schedule") or {})
    if skip:
        print(f"[{name}] スキップ: {skip}")
        return 0, errors

    sources = []
    for s in coll.get("sources") or []:
        if isinstance(s, str):
            sources.append({"url": s, "label": None})
        elif isinstance(s, dict) and s.get("url"):
            sources.append({"url": s["url"], "label": s.get("label")})

    browser = ctx = page = pw = None
    if adapter == "playwright_grow":
        # ブラウザの起動失敗を各 source の try の外で起こすと、例外が
        # main まで抜けて状態ファイルが一切書かれない。実際に Actions 側で
        # 28 回連続して状態が前に進まなかった。ここで受け止めて
        # 「全 source がエラー」として扱う
        try:
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            browser = pw.chromium.launch()
            ctx = browser.new_context(locale="ja-JP", user_agent=USER_AGENT)
            page = ctx.new_page()
        except Exception as exc:
            for src in sources:
                errors.append(f"{src['label'] or src['url']}: ブラウザを起動できない: {exc}")
            print(f"[{name}] 確認=0 変化=0 エラー={len(errors)}（ブラウザ起動不可）")
            for e in errors:
                print(f"  error: {e}", file=sys.stderr)
            return 0, errors

    changes: list[tuple[str, str, str, bool]] = []  # label, url, message, is_first
    previous: dict[str, Any] = {}
    try:
        for src in sources:
            url, label = src["url"], src["label"]
            try:
                if adapter == "playwright_grow":
                    fp, meta = fetch_grow_slots(
                        page,
                        url,
                        int(cfg.get("timeout_ms", 60000)),
                        int(cfg.get("settle_ms", 800)),
                    )
                else:
                    fp, meta = fetch_static_html(url, cfg)
            except Exception as exc:
                errors.append(f"{label or url}: {exc}")
                continue

            prev = state.get(url)
            first = prev is None
            if first:
                # 予約枠は初回でも空きがあれば知らせる。ページ監視は知らせない
                hit = adapter == "playwright_grow" and meta.get("slot_count", 0) > 0
            elif prev != fp:
                hit = meta.get("slot_count", 1) > 0 if adapter == "playwright_grow" else True
            else:
                hit = False

            previous[url] = prev
            state[url] = fp
            if hit:
                msg = format_message(templates, label or "", url, first)
                if msg:
                    changes.append((label or url, url, msg, first))
    finally:
        for closer in (ctx, browser):
            try:
                closer and closer.close()
            except Exception:
                pass
        try:
            pw and pw.stop()
        except Exception:
            pass

    print(f"[{name}] 確認={len(sources)} 変化={len(changes)} エラー={len(errors)}")
    for e in errors:
        print(f"  error: {e}", file=sys.stderr)

    if changes and not dry:
        try:
            notify(
                webhook,
                "\n\n".join(c[2] for c in changes),
                [(c[0], c[1]) for c in changes],
            )
        except Exception as exc:
            # 指紋は更新済みなので、戻さないと次回は差分なしと見なされ
            # 通知が永久に失われる
            for _, url, _, _ in changes:
                if previous.get(url) is None:
                    state.pop(url, None)
                else:
                    state[url] = previous[url]
            errors.append(f"通知に失敗（次回再通知します）: {exc}")
            print(f"  通知に失敗: {exc}", file=sys.stderr)
            return 0, errors

    for label, _, _, _ in changes:
        print(f"  notify: {label}")
    return len(changes), errors


def main() -> int:
    ap = argparse.ArgumentParser(description="URL の変化を Discord に知らせる")
    ap.add_argument("--state", default="state.json", help="前回の指紋を置くファイル")
    ap.add_argument("--config", help="YAML ファイル（既定は環境変数 WATCH_CONFIG）")
    ap.add_argument("--collection", help="この名前のコレクションだけ実行")
    ap.add_argument("--dry-run", action="store_true", help="通知を送らずに確認だけ")
    args = ap.parse_args()

    raw = (
        Path(args.config).read_text(encoding="utf-8")
        if args.config
        else os.environ.get("WATCH_CONFIG", "")
    )
    if not raw.strip():
        print("監視対象がありません。WATCH_CONFIG か --config を指定してください。", file=sys.stderr)
        return 2

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook and not args.dry_run:
        print("DISCORD_WEBHOOK_URL が未設定です。", file=sys.stderr)
        return 2

    try:
        config = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        print(f"WATCH_CONFIG が YAML として読めません: {exc}", file=sys.stderr)
        return 2

    # URL をそのまま貼ると文字列として読まれる。実際にあった間違いなので明示する
    if not isinstance(config, dict):
        head = raw.strip()[:60]
        print("WATCH_CONFIG の中身が設定 YAML になっていません。", file=sys.stderr)
        if head.startswith("http"):
            print(
                "  ウェブフック URL が入っているようです。"
                "DISCORD_WEBHOOK_URL と取り違えていないか確認してください。",
                file=sys.stderr,
            )
        print(f"  先頭: {head}", file=sys.stderr)
        print("  期待する形: collections: から始まる YAML", file=sys.stderr)
        return 2

    collections = [
        c
        for c in (config.get("collections") or [])
        if c.get("enabled", True) and c.get("mode", "watch") == "watch"
    ]
    if args.collection:
        collections = [c for c in collections if c.get("name") == args.collection]

    state_path = Path(args.state)
    state = load_state(state_path)
    total_changes = 0
    total_errors: list[str] = []

    for coll in collections:
        n, errs = run_collection(coll, state, webhook, args.dry_run)
        total_changes += n
        total_errors += errs

    # 壊れていることは、通知が来ないという形ではなく通知で伝える
    notify_health(webhook, state, total_errors, args.dry_run)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    print(f"合計: 変化 {total_changes} 件 / エラー {len(total_errors)} 件")

    # 全滅（どこにも到達できない）のときだけ赤くする。
    # 一部の失敗は Discord 側で伝わるので、実行自体は成功扱いにして
    # 状態キャッシュを前に進める
    checked = sum(
        len([x for x in (c.get("sources") or [])]) for c in collections
    )
    if checked and len(total_errors) >= checked:
        print("すべての対象に到達できませんでした。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

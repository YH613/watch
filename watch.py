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


def fetch_grow_slots(page, url: str, wait_ms: int) -> tuple[str, dict]:
    """予約枠の空き。JS で描画されるので実ブラウザで読む。"""
    page.goto(url)
    page.wait_for_timeout(wait_ms)
    # 対象の span は非表示で、click() は可視性チェックでタイムアウトする。
    # DOM 側で直接クリックする
    page.evaluate(
        "() => { const s = document.querySelectorAll('span');"
        " if (s.length > 1) s[1].click(); }"
    )
    page.wait_for_timeout(wait_ms)
    cells = page.evaluate(
        "Array.from(document.querySelectorAll('td'))"
        ".map(td => ({className: td.className, text: td.innerText}))"
    )
    slots = [c for c in cells if c.get("className") not in ("empty", "")]
    return json.dumps(slots, ensure_ascii=False, sort_keys=True), {
        "slot_count": len(slots),
        "cells": len(cells),
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
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.launch()
        ctx = browser.new_context(locale="ja-JP")
        page = ctx.new_page()

    changes: list[tuple[str, str, str, bool]] = []  # label, url, message, is_first
    previous: dict[str, Any] = {}
    try:
        for src in sources:
            url, label = src["url"], src["label"]
            try:
                if adapter == "playwright_grow":
                    fp, meta = fetch_grow_slots(page, url, int(cfg.get("wait_ms", 5000)))
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

    config = yaml.safe_load(raw) or {}
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

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    print(f"合計: 変化 {total_changes} 件 / エラー {len(total_errors)} 件")
    return 1 if total_errors and total_changes == 0 and not state else 0


if __name__ == "__main__":
    raise SystemExit(main())

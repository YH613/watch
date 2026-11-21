#!/usr/bin/env python3
"""URL の変化を見張って Discord に知らせる。

GitHub Actions から定期実行して使う。監視対象は環境変数 WATCH_CONFIG に
YAML で渡すので、このファイル自体には何も固有の情報が入らない。

状態（前回の指紋）は --state のファイルに置く。Actions では
actions/cache で前後に受け渡す。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import time
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


# 1 回の実行に使ってよい時間。Actions 側の timeout-minutes より十分短くする。
# 上限で殺されると、状態を書く前に終わるため進捗が丸ごと消える
DEFAULT_RUN_BUDGET_SECONDS = 480


def gh_warning(message: str) -> None:
    """GitHub Actions の注釈として警告を出す。読み方は gh_error と同じ。"""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        flat = message.replace("\r", " ").replace("\n", " ")
        print(f"::warning::{flat}", flush=True)


def gh_error(message: str) -> None:
    """GitHub Actions の注釈としてエラーを出す。

    Actions のログ本体は認証が無いと読めないが、注釈は公開 API から読める。
    設定の取り違えのような「ログを見ないと分からない」失敗を、
    外から診断できるようにしておく。**秘密の値は絶対に入れない。**
    """
    if os.environ.get("GITHUB_ACTIONS") == "true":
        flat = message.replace("\r", " ").replace("\n", " ")
        print(f"::error::{flat}", flush=True)


# ---------- 取得 ----------

# 15 秒では 15 件中 2 件が時間切れになった。到達できないわけではなく
# 遅いだけなので、待ち時間を伸ばし、一度だけ取り直す。
# 時間切れをそのままエラーにすると「監視が失敗しています」が無駄に鳴る
DEFAULT_TIMEOUT = 30


def get_with_retry(url: str, timeout: int) -> Any:
    """GET する。接続の不調は一度だけ取り直す。"""
    for attempt in (1, 2):
        try:
            res = requests.get(url, timeout=timeout, headers=HEADERS)
            res.raise_for_status()
            return res
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise


# 出勤表は「今日から7日分」の窓。日付が毎朝ひとつずれるので、
# 本文ごとハッシュにすると中身が同じでも全ページが毎朝「変化」になる
SCHEDULE_WRAP_SELECTOR = "div.weekScheduleWrap"
# 「本日出勤」バッジも日付で出入りするため本文から外す
TODAY_BADGE_SELECTOR = "span.todayWorkIcon"
DATE_RE = re.compile(r"\d{1,2}/\d{1,2}")
STATIC_FP_VERSION = 2


def extract_week_schedule(soup: Any) -> dict[str, str]:
    """出勤表を 日付→状態 の対応に直す。

    位置ではなく日付で突き合わせられるようにしておくと、窓がずれただけの
    変化と、実際に予定が変わった変化を分けられる。
    """
    schedule: dict[str, str] = {}
    for wrap in soup.select(SCHEDULE_WRAP_SELECTOR):
        for row in wrap.select("tr"):
            date = ""
            for cell in row.find_all(["th", "td"], recursive=False):
                text = " ".join(cell.get_text(" ").split())
                if cell.name == "th":
                    found = DATE_RE.search(text)
                    date = found.group(0) if found else ""
                elif date:
                    schedule[date] = text
    return schedule


def fetch_static_html(url: str, cfg: dict) -> tuple[str, dict]:
    """ページ本文のハッシュ。script/style と、日付でずれる部分は外す。

    出勤表と「本日出勤」バッジは本文から切り離して別に持つ。
    どちらも中身が同じでも日付が変わるだけで文字列が変わるため、
    本文に混ぜると毎朝、全員分が「更新あり」として鳴ってしまう。
    """
    res = get_with_retry(url, int(cfg.get("timeout", DEFAULT_TIMEOUT)))
    if res.encoding and res.encoding.lower() in ("iso-8859-1", "ascii"):
        res.encoding = res.apparent_encoding or "utf-8"
    soup = BeautifulSoup(res.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    schedule = extract_week_schedule(soup)
    for selector in (SCHEDULE_WRAP_SELECTOR, TODAY_BADGE_SELECTOR):
        for tag in soup.select(selector):
            tag.decompose()
    text = soup.get_text()
    fingerprint = json.dumps(
        {
            "v": STATIC_FP_VERSION,
            "body": hashlib.md5(text.encode()).hexdigest(),
            "schedule": schedule,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return fingerprint, {"length": len(text), "schedule": schedule}


def static_changed(prev: str, now: str) -> tuple[bool, str]:
    """ページが実質的に変わったかを、変わった中身つきで返す。

    出勤表は「前回と今回の両方に載っている日付」だけで比べる。
    窓がずれて消えた日・現れた日は変化として扱わない。

    指紋が旧い形式のときは、変化なしとして黙って作り直す。
    形式を変えた直後に全員分が一斉に鳴るのを避けるため
    （この取りこぼしは一度きり）。
    """
    try:
        before = json.loads(prev)
        after = json.loads(now)
    except (TypeError, ValueError):
        return False, ""
    if not isinstance(before, dict) or before.get("v") != STATIC_FP_VERSION:
        return False, ""
    if before.get("body") != after.get("body"):
        return True, "ページの内容が変わりました"
    old_days = before.get("schedule") or {}
    new_days = after.get("schedule") or {}
    diffs = [
        f"{day} {old_days[day]} → {new_days[day]}"
        for day in sorted(set(old_days) & set(new_days), key=sort_key_for_day)
        if old_days[day] != new_days[day]
    ]
    if diffs:
        return True, "出勤が変わりました: " + " / ".join(diffs)
    return False, ""


def sort_key_for_day(day: str) -> tuple[int, int]:
    month, _, date = day.partition("/")
    try:
        return int(month), int(date)
    except ValueError:
        return 99, 99


# コース選択ボタン。スマホ用(.sp)が先に並んでおり非表示なので、
# class だけで掴むと見えない方に当たって何も起きない
CLICK_COURSE_JS = """() => {
  const btns = Array.from(document.querySelectorAll('a.selbtn'));
  const target = btns.find(b => b.offsetWidth || b.offsetHeight) || btns[0];
  if (!target) return false;
  target.click();
  return true;
}"""

# 失敗したとき、何本取れて何KB届いたかを見る。
# 「遅い」のか「応答が止まっている」のか「JS が足りず画面が死んだ」のかは
# 外から区別できないと直せない
DIAGNOSE_JS = """() => {
  const r = performance.getEntriesByType('resource')
    .filter(x => x.name.includes('/_next/static/'));
  return {
    done: r.length,
    kb: Math.round(r.reduce((s, x) => s + (x.transferSize || 0), 0) / 1024),
    body: ((document.body && document.body.innerText) || '')
      .replace(/\\s+/g, ' ').trim().slice(0, 40)
  };
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


# 予約サイトは同時アクセスを捌けない。コンテキスト（Cookie）を分けても、
# 同時 3 件で 16 件すべてが時間切れになった（goto すら返らない）。
# 速くするために並べるのは諦め、1 件ずつ順に回す。
# 代わりに、時間内に回りきらなかったぶんは次回の先頭から続ける
GROW_CONCURRENCY = 1

# ブラウザのプロファイル置き場。ここに Chromium の HTTP キャッシュが残る。
# 予約ページは 1 回開くのに 28 ファイル・2.9MB を取りに行くので、実行ごとに
# 空から始めると、サイトが遅い日は 1 件目だけで上限を使い切る（2026-11-21 に
# 850KB の JS が 11KB/秒まで落ち、7 件が 7 件とも時間切れになった）。
# Actions 側で actions/cache に出し入れし、実行間で持ち越す。
# JS はファイル名に内容のハッシュが付いていて 1 年キャッシュ可なので、
# サイトが更新されれば名前ごと変わる。古いものを掴む心配は無い
DEFAULT_PROFILE_DIR = ".browser-profile"


def dir_size_mb(path: Path) -> float:
    """持ち越せたキャッシュの量。0 なら実行間の受け渡しが効いていない。"""
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total / 1024 / 1024


def probe_plain(url: str) -> str:
    """ブラウザを通さず 1 回だけ取る。403 の出どころを切り分けるため。"""
    try:
        res = requests.get(url, timeout=20, headers=HEADERS, allow_redirects=True)
        return (
            f"{res.status_code} / server={res.headers.get('server', '?')} "
            f"/ {len(res.content)}B"
        )
    except Exception as exc:  # noqa: BLE001
        return f"取れず {type(exc).__name__}"


async def open_grow_browser(pw: Any, profile_dir: Path) -> tuple[Any, Any]:
    """プロファイルを使い回してブラウザを開く。壊れていたら捨てて作り直す。"""
    for attempt in (1, 2):
        try:
            context = await pw.chromium.launch_persistent_context(
                str(profile_dir),
                locale="ja-JP",
                user_agent=USER_AGENT,
                args=["--disk-cache-size=67108864"],
            )
            return context, context.browser
        except Exception:
            if attempt == 2:
                raise
            # 壊れたプロファイルが保存されると、キャッシュから戻される限り
            # 毎回同じ失敗を繰り返す（自然復旧しない）。捨ててやり直す
            shutil.rmtree(profile_dir, ignore_errors=True)
            gh_warning("ブラウザのプロファイルが開けないので作り直しました")
    raise RuntimeError("到達しない")


async def fetch_grow_one(context: Any, url: str, timeout_ms: int, settle_ms: int) -> tuple[str, dict]:
    """予約枠の空きを 1 件読む。

    2026-11 にサイトのフローが変わり、?page=time を直接開いても
    「メニューを選択」の画面に着地するようになった。コースを選んで
    初めてカレンダーが描かれる（実測 6〜12 秒）。
    旧実装は span[1] をクリックして 5 秒待つだけだったため、
    カレンダーに到達せず「空き 0 枠」を正常値として記録し続けていた。
    固定待ちをやめ、描画されるまで待ち、出なければ例外にする
    （黙って 0 を返すと故障が通知の不在と区別できない）。
    """
    # サイトは応答の途中で止まることがある（本体が来ないまま終わらない）。
    # そうなると JS が足りずに画面が死に、待ち時間を伸ばしても意味が無い。
    # つなぎ直せば通ることがあるので、一度だけ取り直す
    last: Exception | None = None
    for attempt in (1, 2):
        page = await context.new_page()
        asked = 0

        def count(_: Any) -> None:
            nonlocal asked
            asked += 1

        page.on("request", count)
        status: list[int] = []

        def note(res: Any) -> None:
            if res.request.resource_type == "document":
                status.append(res.status)

        page.on("response", note)
        data = None
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_selector(
                "a.selbtn", state="attached", timeout=timeout_ms
            )
            if not await page.evaluate(CLICK_COURSE_JS):
                raise RuntimeError("コース選択ボタンが無い（サイト構成の変更か）")
            await page.wait_for_function(CALENDAR_READY_JS, timeout=timeout_ms)
            await page.wait_for_timeout(settle_ms)
            data = await page.evaluate(EXTRACT_SLOTS_JS)
        except Exception as exc:  # noqa: BLE001
            got: dict = {}
            try:
                got = await page.evaluate(DIAGNOSE_JS)
            except Exception:  # noqa: BLE001
                pass
            kind = type(exc).__name__
            last = RuntimeError(
                f"{kind} 文書={status[0] if status else '?'} "
                f"静的{got.get('done', '?')}/{asked}本 "
                f"{got.get('kb', '?')}KB 画面=[{got.get('body', '')}] {attempt}回目"
            )
        finally:
            await page.close()
        if data:
            break
        if attempt == 2:
            raise last or RuntimeError("カレンダーが描画されなかった")
    if not data:
        raise last or RuntimeError("カレンダーが描画されなかった")
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


async def gather_grow_slots(
    urls: list[str], timeout_ms: int, settle_ms: int, concurrency: int, budget: float
) -> dict[str, Any]:
    """URL ごとに結果か例外を入れた対応表を返す。

    予算を超えたら、そこまでに取れたぶんだけを返す。
    全部そろうまで粘って実行ごと殺されると、状態が一切進まない。
    """
    from playwright.async_api import async_playwright

    results: dict[str, Any] = {}
    profile_dir = Path(
        os.environ.get("WATCH_BROWSER_PROFILE", DEFAULT_PROFILE_DIR)
    )
    urls_given = list(urls)
    warm_mb = dir_size_mb(profile_dir)
    async with async_playwright() as pw:
        kept, browser = await open_grow_browser(pw, profile_dir)
        extra: list[Any] = []
        try:
            # 予約サイトは選択状態をサーバー側のセッションで持つので、
            # 同時に動かすぶんだけコンテキスト（= Cookie）を分ける。
            # ただし 1 件ごとに作り直してはいけない。キャッシュが毎回空になり、
            # 1 件 26 秒だったものが 65 秒に落ちて 16 件中 7 件しか回れなかった。
            # 使い回す本数を同時実行数に合わせ、順番に貸し出す。
            # 1 本目はディスクに残るプロファイル。2 本目以降は使い捨てなので
            # キャッシュを共有せず、同時実行を増やすほど通信は増える
            pool: asyncio.Queue = asyncio.Queue()
            await pool.put(kept)
            for _ in range(max(1, concurrency) - 1):
                context = await browser.new_context(
                    locale="ja-JP", user_agent=USER_AGENT
                )
                extra.append(context)
                await pool.put(context)

            spent: list[float] = []

            async def one(url: str, ms: int | None = None) -> None:
                context = await pool.get()
                at = time.monotonic()
                try:
                    results[url] = await fetch_grow_one(
                        context, url, ms or timeout_ms, settle_ms
                    )
                    spent.append(time.monotonic() - at)
                except Exception as exc:  # noqa: BLE001
                    results[url] = exc
                finally:
                    await pool.put(context)

            started = time.monotonic()
            # キャッシュが空のままだと、サイトが遅い日は 1 件も完走できず、
            # 次回に渡すキャッシュも溜まらない（落としかけのファイルは
            # キャッシュされない）。実測 2026-11-21: 7 件が 7 件とも時間切れ、
            # 持ち越し 0MB のまま何度やっても同じ。この輪を断つため、
            # 空のときだけ 1 件目に長い時間を与えて温める。
            # その 1 件だけで実行が終わってもいい。次回から全件が速くなる
            if warm_mb < 1 and urls:
                prime = min(240.0, max(timeout_ms / 1000, budget * 0.5))
                gh_warning(
                    f"予約枠: キャッシュが空なので 1 件目に最大 {prime:.0f} 秒かける"
                )
                try:
                    await asyncio.wait_for(
                        one(urls[0], int(prime * 1000)), timeout=prime + 30
                    )
                except asyncio.TimeoutError:
                    pass
                urls = urls[1:]

            rest = budget - (time.monotonic() - started)
            if urls and rest > 5:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(one(u) for u in urls)), timeout=rest
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            # 1 件も読めないときは、ブラウザのせいか回線（IP）のせいかを分ける。
            # 素の取得も 403 なら、ブラウザの作りを直しても無駄
            if not spent and urls_given:
                gh_warning("予約枠の素の取得: " + await asyncio.to_thread(
                    probe_plain, urls_given[0]))

            # キャッシュが効いているかは外から見えないと確かめられない
            gh_warning(
                f"予約枠: 持ち越したキャッシュ {warm_mb:.0f}MB / "
                f"読めた {len(spent)} 件 / "
                f"1 件あたり {sum(spent) / len(spent):.0f} 秒"
                if spent
                else f"予約枠: 持ち越したキャッシュ {warm_mb:.0f}MB / 1 件も読めず"
            )
            for context in extra:
                await context.close()
            # close() でキャッシュの索引が書き出される。
            # 待たずに殺すと、せっかく落としたものが次回に効かない
            await kept.close()
    return results


def fetch_grow_slots(
    urls: list[str], timeout_ms: int, settle_ms: int, concurrency: int, budget: float
) -> dict[str, Any]:
    return asyncio.run(
        gather_grow_slots(urls, timeout_ms, settle_ms, concurrency, budget)
    )


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


# 時間内に回りきらなかったとき、次回どこから始めるか。
# 毎回先頭から始めると、後ろのセラピストが永久に確認されない
CURSOR_KEY = "__cursor__"
# 対象ごとの「最後に読めた時刻」。失敗の有無ではなく、これで健全性を見る
LAST_OK_KEY = "__last_ok__"
# これだけ読めていなければ、監視に穴が空いていると見なす。
# 一周が約 23 分なので、4〜5 周ぶん
DEFAULT_STALE_MINUTES = 120
ERROR_STATE_KEY = "__last_error_signature__"
LAST_RUN_KEY = "__last_run_at__"
GAP_ALERT_KEY = "__last_gap_alert_at__"
# 10 分間隔の想定。これを大きく超えたら「回っていない」と見なす
DEFAULT_ALERT_GAP_MINUTES = 60
# 回っていない間ずっと鳴り続けないための間引き
GAP_ALERT_COOLDOWN_HOURS = 12


def error_signature(errors: list[str]) -> str:
    """同じ故障で鳴り続けないための指紋。対象名と例外の種類だけで作る。

    件数や秒数は実行ごとに揺れる。そのまま指紋に入れると
    「時間切れで 13 件」「12 件」が別の故障と見なされ、
    同じ不調で鳴り続けてしまうので、数字は伏せる。
    """
    parts = []
    for e in errors:
        who, _, rest = e.partition(": ")
        kind = re.sub(r"\d+", "N", rest.split("(")[0].strip())[:60]
        parts.append(f"{who}:{kind}")
    return " | ".join(sorted(set(parts)))


def find_stale(state: dict, watched: dict[str, str], limit_minutes: float) -> list[str]:
    """長く読めていない対象を「ラベル（N分）」の形で返す。

    1 回の時間切れは故障ではない。順番に回しているので、
    失敗したものは次の周で読み直される。
    それを失敗として鳴らすと、毎回ちがう名前で鳴り続ける。
    見るべきは「一定時間ぶん読めていないか」だけ。
    """
    now = datetime.now(timezone.utc)
    last_ok = state.setdefault(LAST_OK_KEY, {})
    stale = []
    for url, label in watched.items():
        # 初めて見る対象は、ここを起点に数える
        seen = last_ok.setdefault(url, now.isoformat())
        try:
            minutes = (now - datetime.fromisoformat(seen)).total_seconds() / 60
        except (TypeError, ValueError):
            last_ok[url] = now.isoformat()
            continue
        if minutes > limit_minutes:
            stale.append(f"{label or url}（{int(minutes)}分）")
    return sorted(stale)


def notify_health(webhook: str, state: dict, stale: list[str], dry: bool) -> None:
    """監視に穴が空いたら黙らずに知らせる。

    枠の検出が壊れていた間、ログ上は「変化なし・エラーなし」に見えており、
    通知が来ないことと空きが無いことが区別できなかった。
    故障が故障として届かないと、気づけるのは何日も後になる。
    同じ状態で鳴り続けないよう、顔ぶれが変わったときだけ送る。
    """
    sig = " | ".join(x.partition("（")[0] for x in stale)
    if sig == state.get(ERROR_STATE_KEY, ""):
        return

    if stale:
        message = f"{len(stale)} 件が長く確認できていません"
    else:
        message = "監視が復旧しました"

    if dry:
        print(f"  [dry-run] 健全性通知: {message} {stale}")
    else:
        try:
            # content はプッシュに出るので短く。詳細は埋め込みへ
            payload: dict[str, Any] = {"content": message}
            if stale:
                payload["embeds"] = [
                    {
                        "description": "\n".join(f"- {x}" for x in stale)[:4000],
                        "color": 0xE57373,
                    }
                ]
            res = requests.post(webhook, json=payload, timeout=20)
            res.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"  健全性の通知に失敗: {exc}", file=sys.stderr)
            return

    state[ERROR_STATE_KEY] = sig


def measure_interval(state: dict) -> float | None:
    """前回実行からの間隔（分）を返し、今回の時刻を記録する。"""
    now = datetime.now(timezone.utc)
    prev_raw = state.get(LAST_RUN_KEY)
    state[LAST_RUN_KEY] = now.isoformat()
    if not prev_raw:
        return None
    try:
        prev = datetime.fromisoformat(prev_raw)
    except (TypeError, ValueError):
        return None
    return (now - prev).total_seconds() / 60


def notify_interval(
    webhook: str, state: dict, gap_minutes: float | None, limit: float, dry: bool
) -> None:
    """実行が止まっていることを知らせる。

    外部 cron から API で起動する構成では、cron サービスが止まったり
    トークンが期限切れになると「実行自体が起きない」。すると通知も来ないので、
    空きが無いのと区別がつかない（今回 10 日気づけなかったのと同じ形）。
    schedule の定期実行が保険で残っているので、そのときに間隔を見て知らせる。
    """
    if gap_minutes is None or gap_minutes <= limit:
        return

    now = datetime.now(timezone.utc)
    last_raw = state.get(GAP_ALERT_KEY)
    if last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
            if (now - last).total_seconds() < GAP_ALERT_COOLDOWN_HOURS * 3600:
                return
        except (TypeError, ValueError):
            pass

    message = f"監視が {int(gap_minutes)} 分動いていません（10分間隔の想定）"
    if dry:
        print(f"  [dry-run] 間隔の通知: {message}")
    else:
        try:
            res = requests.post(
                webhook,
                json={
                    "content": message,
                    "embeds": [
                        {
                            "description": (
                                "外部 cron からの起動が止まっている可能性があります。"
                                "cron サービスの実行履歴と、GitHub トークンの有効期限を"
                                "確認してください。"
                            ),
                            "color": 0xE57373,
                        }
                    ],
                },
                timeout=20,
            )
            res.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"  間隔の通知に失敗: {exc}", file=sys.stderr)
            return
    state[GAP_ALERT_KEY] = now.isoformat()


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


def run_collection(
    coll: dict,
    state: dict,
    webhook: str,
    dry: bool,
    deadline: float,
    save_state: Any,
) -> tuple[int, list[str]]:
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

    # 前回の続きから回す。1 周したら先頭に戻る
    cursors = state.setdefault(CURSOR_KEY, {})
    start = int(cursors.get(name, 0)) % len(sources) if sources else 0
    sources = sources[start:] + sources[:start]

    grow: dict[str, Any] = {}
    if adapter == "playwright_grow":
        # ブラウザの起動失敗を各 source の try の外で起こすと、例外が
        # main まで抜けて状態ファイルが一切書かれない。実際に Actions 側で
        # 28 回連続して状態が前に進まなかった。ここで受け止めて
        # 「全 source がエラー」として扱う
        try:
            grow = fetch_grow_slots(
                [s["url"] for s in sources],
                int(cfg.get("timeout_ms", 60000)),
                int(cfg.get("settle_ms", 800)),
                int(cfg.get("concurrency", GROW_CONCURRENCY)),
                max(5.0, deadline - time.monotonic()),
            )
        except Exception as exc:
            for s in sources:
                errors.append(f"{s['label'] or s['url']}: ブラウザを起動できない: {exc}")
            print(f"[{name}] 確認=0/{len(sources)} 変化=0 エラー={len(errors)}（ブラウザ起動不可）")
            for e in errors:
                print(f"  error: {e}", file=sys.stderr)
            return 0, errors

    changes: list[tuple[str, str, str, bool]] = []  # label, url, message, is_first
    previous: dict[str, Any] = {}
    checked = 0
    for src in sources:
        url, label = src["url"], src["label"]
        # 予約枠は取得済みなので期限を見ない（見ると全部捨てることになる）
        if adapter != "playwright_grow" and time.monotonic() > deadline:
            # 途中で殺されるより、自分で止まって状態を残す方がよい。
            # 残りは次回この続きから見るので、故障としては扱わない
            break
        try:
            if adapter == "playwright_grow":
                got = grow.get(url)
                if got is None:
                    # 時間内に届かなかっただけ。次回ここから始めるので
                    # 故障としては鳴らさない（鳴らすと毎回名前が変わり、
                    # 同じ不調で鳴り続ける）
                    continue
                checked += 1
                if isinstance(got, Exception):
                    raise got
                fp, meta = got
            else:
                checked += 1
                fp, meta = fetch_static_html(url, cfg)
        except Exception as exc:
            errors.append(f"{label or url}: {exc}")
            continue

        # 読めた時刻を残す。1 回の失敗は故障ではないが、
        # 長く読めていないのは故障
        state.setdefault(LAST_OK_KEY, {})[url] = datetime.now(timezone.utc).isoformat()

        prev = state.get(url)
        first = prev is None
        reason = ""
        if first:
            # 予約枠は初回でも空きがあれば知らせる。ページ監視は知らせない
            hit = adapter == "playwright_grow" and meta.get("slot_count", 0) > 0
        elif adapter == "playwright_grow":
            hit = prev != fp and meta.get("slot_count", 1) > 0
        else:
            # 単純な差ではなく「何が変わったか」で判断する。
            # 出勤表の窓が毎朝ずれるだけで鳴っていた
            hit, reason = static_changed(prev, fp)

        previous[url] = prev
        state[url] = fp
        # 1 件ごとに書き出す。実行が途中で殺されても進捗が消えないように。
        # 以前は最後にまとめて書いていたため、上限で殺されるたびに
        # 状態が巻き戻り、同じ通知が 10 分おきに繰り返されていた
        save_state()
        if hit:
            msg = format_message(templates, label or "", url, first)
            if msg:
                # 開いても何が変わったか分からない、を避ける
                if reason:
                    msg = f"{msg}\n{reason}"
                changes.append((label or url, url, msg, first))

    cursors[name] = (start + checked) % len(sources) if sources else 0
    missed = len(sources) - checked
    if missed:
        # ログは認証が要るので、取りこぼしは注釈に出しておく
        gh_warning(f"{name}: {missed}/{len(sources)} 件は時間内に確認できず、次回に回しました")
    if sources and checked == 0:
        # 1 件も見られないのは、遅いのではなく壊れている
        errors.append(f"{name}: 1 件も確認できませんでした")
    save_state()

    print(f"[{name}] 確認={checked}/{len(sources)} 未確認={missed} 変化={len(changes)} エラー={len(errors)}")
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
        gh_error("WATCH_CONFIG が空です。監視対象の YAML を設定してください。")
        return 2

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook and not args.dry_run:
        print("DISCORD_WEBHOOK_URL が未設定です。", file=sys.stderr)
        gh_error("DISCORD_WEBHOOK_URL が空です。シークレットを取り違えていないか確認してください。")
        return 2

    try:
        config = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        print(f"WATCH_CONFIG が YAML として読めません: {exc}", file=sys.stderr)
        # 例外文には設定の断片が入りうるので、注釈には種類だけ出す
        gh_error(f"WATCH_CONFIG が YAML として読めません（{type(exc).__name__}）。貼り付け時に前の内容が残っていないか確認してください。")
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
        kind = "URL らしきもの" if head.startswith("http") else f"{type(config).__name__} 型"
        gh_error(
            f"WATCH_CONFIG が設定 YAML ではありません（{kind}が入っています）。"
            "collections: から始まる YAML を貼り直してください。"
        )
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

    def save_state() -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    budget = float(os.environ.get("WATCH_RUN_BUDGET_SECONDS", DEFAULT_RUN_BUDGET_SECONDS))
    started = time.monotonic()
    deadline = started + budget

    gap_minutes = measure_interval(state)
    if gap_minutes is not None:
        print(f"前回実行から {gap_minutes:.0f} 分")
    total_changes = 0
    total_errors: list[str] = []

    timings: list[str] = []
    for position, coll in enumerate(collections):
        at = time.monotonic()
        # 先頭のコレクションが予算を使い切ると、後ろ（予約枠）に一度も
        # 届かなくなる。残り時間を残りのコレクション数で割って分け合う。
        # 早く終わったぶんは自然と後ろに回る
        share = max(0.0, deadline - at) / max(1, len(collections) - position)
        n, errs = run_collection(
            coll, state, webhook, args.dry_run, at + share, save_state
        )
        timings.append(f"{coll.get('name', '(無名)')} {time.monotonic() - at:.0f}秒")
        total_changes += n
        total_errors += errs

    elapsed = time.monotonic() - started
    # どこに時間を使っているかはログを見ないと分からないが、
    # ログは認証が無いと読めない。遅いときだけ注釈に出しておく
    if elapsed > budget / 2:
        gh_warning(f"実行に {elapsed:.0f} 秒（予算 {budget:.0f} 秒）: " + " / ".join(timings))

    # 1 回の失敗ではなく「長く読めていないこと」を故障として伝える
    watched: dict[str, str] = {}
    for coll in collections:
        for s in coll.get("sources") or []:
            if isinstance(s, str):
                watched[s] = ""
            elif isinstance(s, dict) and s.get("url"):
                watched[s["url"]] = s.get("label") or ""
    stale = find_stale(
        state,
        watched,
        float(os.environ.get("WATCH_STALE_MINUTES", DEFAULT_STALE_MINUTES)),
    )
    notify_health(webhook, state, stale, args.dry_run)
    if total_errors:
        # 失敗そのものは鳴らさないが、外から診断できるよう注釈には残す
        gh_warning(f"今回の失敗 {len(total_errors)} 件: {error_signature(total_errors)[:300]}")
        # 署名は数字を伏せるので、原因を見るには生のまま 1 件要る
        gh_warning(f"失敗の内訳（1 件目）: {total_errors[0][:300]}")
    notify_interval(
        webhook,
        state,
        gap_minutes,
        float(os.environ.get("WATCH_ALERT_GAP_MINUTES", DEFAULT_ALERT_GAP_MINUTES)),
        args.dry_run,
    )

    save_state()
    print(f"合計: 変化 {total_changes} 件 / エラー {len(total_errors)} 件 / {elapsed:.0f} 秒")

    # 全滅（どこにも到達できない）のときだけ赤くする。
    # 一部の失敗は Discord 側で伝わるので、実行自体は成功扱いにして
    # 状態キャッシュを前に進める
    checked = sum(
        len([x for x in (c.get("sources") or [])]) for c in collections
    )
    if checked and len(total_errors) >= checked:
        print("すべての対象に到達できませんでした。", file=sys.stderr)
        gh_error(f"すべての対象（{checked} 件）に到達できませんでした。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

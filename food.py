import os
import re
import io
import asyncio
import calendar
import logging
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import discord
from discord import app_commands
from discord.ext import tasks
import pdfplumber
import requests
from datetime import datetime, timedelta, timezone, time as dtime
from dotenv import load_dotenv

# ===== 設定 =====
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

# 日本標準時 (UTC+9)
JST = timezone(timedelta(hours=9))

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("food_bot")

# Discord Bot 設定
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ============================
#  PDF URL 構築 / ダウンロード
# ============================

def get_reiwa_year_and_month(target_date):
    """対象日から令和の年と月を返す。"""
    reiwa_year = target_date.year - 2018
    return reiwa_year, target_date.month


def build_pdf_url(target_date):
    """対象日の献立 PDF URL（優先候補）を生成"""
    return build_pdf_url_candidates(target_date)[0]


def build_pdf_url_candidates(target_date):
    """対象日の献立 PDF URL 候補を優先順で返す"""
    reiwa_year, month = get_reiwa_year_and_month(target_date)
    base_url = "https://www.kushiro-ct.ac.jp/wordpress/wp-content/uploads/2015/11/"

    # ベースファイル名パターン（拡張子なし）
    base_patterns = [
        f"寮務提出用R{reiwa_year}年度{month}月__献立表-",
        f"寮務提出用R{reiwa_year}年度__献立表-",
        f"寮務提出用R{reiwa_year}年{month}月度__献立表-",
        f"寮務提出用R{reiwa_year}年{month}月度_献立表-",
    ]

    # 改訂版サフィックス（新しい改訂版を優先: -10, -9, ..., -2, なし）
    revision_suffixes = [str(i) for i in range(10, 1, -1)] + [""]

    filenames = []
    # 改訂版を優先的に試行（最新の改訂番号から）
    for suffix in revision_suffixes:
        for pattern in base_patterns:
            if suffix:
                filenames.append(f"{pattern}{suffix}.pdf")
            else:
                filenames.append(f"{pattern}.pdf")

    # サフィックスなしのパターン
    filenames.extend([
        f"寮務提出用R{reiwa_year}年{month}月度献立表.pdf",
        f"R{reiwa_year}.{month}_ryokondate.pdf",
        f"kondate{month}gatu.pdf",
    ])

    urls = []
    for name in filenames:
        if re.search(r"[^\x00-\x7F]", name):
            urls.append(base_url + quote(name))
        else:
            urls.append(base_url + name)
    return urls


def download_pdf(url):
    """PDF をダウンロードして BytesIO で返す"""
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return io.BytesIO(resp.content)


# ============================
#  PDF パース
# ============================

def _find_date_column(header_row, month, day):
    """ヘッダー行から対象日の列インデックスを返す"""
    for j, cell in enumerate(header_row):
        if not cell:
            continue
        # 「3月6日」や「3/6」のようなパターンから抽出。空白や改行も許容
        m = re.search(r"(\d{1,2})\s*(?:月|/)\s*(\d{1,2})\s*日?", str(cell))
        if m and int(m.group(1)) == month and int(m.group(2)) == day:
            return j
    return None


def _find_meal_rows(table):
    """各行のラベルから朝食・昼食・夕食の行インデックスを特定"""
    meal_rows = {}
    for i, row in enumerate(table):
        if not row or not row[0]:
            continue
        label = str(row[0]).replace("\n", "")
        if re.search(r"朝", label) and "朝食" not in meal_rows:
            meal_rows["朝食"] = i
        elif re.search(r"昼", label) and "昼食" not in meal_rows:
            meal_rows["昼食"] = i
        elif re.search(r"夕", label) and "夕食" not in meal_rows:
            meal_rows["夕食"] = i
    return meal_rows


def _clean_menu_text(text):
    """メニューテキストから不要な部分を除去"""
    if not text:
        return ""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 単独の "A" や "B"、"AB共通メニュー" のみの行を除去
        if re.match(r"^[AB]$", line):
            continue
        if line == "AB共通メニュー":
            continue
        # 行頭の "A " "B " プレフィックスを除去
        line = re.sub(r"^[AB]\s+", "", line)
        cleaned.append(line)
    return "\n".join(cleaned)


def _join_wrapped_lines(items):
    """PDF セル内の折り返しで途中切れした行を結合する。
    1 行が折り返し幅 (全角約 11 文字) 以上なら、次の行は続きとみなす。"""
    joined = []
    for item in items:
        if joined and len(joined[-1]) >= 11 and not joined[-1].endswith(("】", "）", ")")):
            joined[-1] += item
        else:
            joined.append(item)
    return joined


def _parse_dinner_ab(text):
    """夕食のテキストを A食・B食・共通 に分類して返す
    戻り値: dict{"A": str or None, "B": str or None, "共通": str or None}
    """
    if not text:
        return {"A": None, "B": None, "共通": None}

    lines = text.split("\n")
    a_items = []
    b_items = []
    common_items = []
    pre_items = []  # 最初のラベルより前の行（誕生日会などラベル無しの A 食用）
    current = pre_items
    seen_a = False
    seen_b = False

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # "AB共通メニュー" ラベル
        if line == "AB共通メニュー":
            current = common_items
            continue
        # 単独の "A" → A食セクション開始
        if re.match(r"^A$", line):
            current = a_items
            seen_a = True
            continue
        # 単独の "B" → B食セクション開始
        if re.match(r"^B$", line):
            current = b_items
            seen_b = True
            continue
        # "A メニュー名" プレフィックス付き
        m = re.match(r"^A\s+(.+)", line)
        if m:
            a_items.append(m.group(1))
            current = a_items
            seen_a = True
            continue
        # "B メニュー名" プレフィックス付き
        m = re.match(r"^B\s+(.+)", line)
        if m:
            b_items.append(m.group(1))
            current = b_items
            seen_b = True
            continue
        current.append(line)

    # ラベル無しで始まり B だけ後に出てくる場合は、その行群が A 食
    if pre_items:
        if seen_b and not seen_a:
            a_items = pre_items + a_items
        else:
            common_items = pre_items + common_items

    a_items = _join_wrapped_lines(a_items)
    b_items = _join_wrapped_lines(b_items)

    return {
        "A": "\n".join(a_items) if a_items else None,
        "B": "\n".join(b_items) if b_items else None,
        "共通": "\n".join(common_items) if common_items else None,
    }


def _is_nutrition_row(row, col_idx):
    """栄養価データの行かどうか判定"""
    for cell in row:
        if cell and re.search(r"[ＥＰＦＣS]|kcal", str(cell)):
            return True
    return False


def _parse_text(text, month, day):
    """テキストから指定日のメニューをフォールバック抽出"""
    # 対象日を含む行を探す。空白や「/」区切りも考慮
    # 前後に数字が続く場合は除外（「4月1」が「4月10日」にマッチしないように）
    day_pattern = re.compile(rf"(?<!\d){month}\s*(?:月|/)\s*{day}(?!\d)\s*日?")
    if not day_pattern.search(text):
        return None

    menu = {}
    meals = {"朝食": r"朝\s*食?", "昼食": r"昼\s*食?", "夕食": r"夕\s*食?"}
    lines = text.split("\n")

    for meal_name, pattern in meals.items():
        collecting = False
        parts = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if re.search(pattern, stripped):
                collecting = True
                # ラベル以降のテキストがあれば取得
                after = re.split(pattern, stripped)[-1].strip()
                if after:
                    parts.append(after)
                continue
            if collecting:
                # 別の食事ラベルや栄養情報が来たら終了
                if any(re.search(p, stripped) for p in meals.values()):
                    break
                if re.search(r"栄養|kcal|ご飯", stripped):
                    break
                parts.append(stripped)
        cleaned = _clean_menu_text("\n".join(parts))
        if cleaned:
            menu[meal_name] = cleaned

    return menu


def _parse_table(table, month, day):
    """テーブルから指定日のメニューを検索
    PDF は日付が列、食事が行のレイアウト"""
    if not table or len(table) < 2:
        return None

    # ヘッダー行（Row 0）から対象日の列を特定
    col_idx = _find_date_column(table[0], month, day)
    if col_idx is None:
        return None

    # 各食事の行を特定
    meal_rows = _find_meal_rows(table)
    if not meal_rows:
        return {}  # 日付列は見つかったが食事行なし

    # 各食事の行インデックスの集合（他の食事の境界として使う）
    all_meal_row_indices = set(meal_rows.values())

    menu = {}
    for meal_name, row_idx in meal_rows.items():
        parts = []

        # 夕食は「夕」ラベル行より前にAメニュー行がある場合がある
        if meal_name == "夕食":
            lunch_idx = meal_rows.get("昼食", 0)
            for i in range(lunch_idx + 1, row_idx):
                row = table[i]
                if not row:
                    continue
                if _is_nutrition_row(row, col_idx):
                    continue
                if col_idx < len(row) and row[col_idx]:
                    cell_text = str(row[col_idx]).strip()
                    if cell_text and not re.match(r"^[AB]?$", cell_text):
                        parts.append(cell_text)

        cell = table[row_idx][col_idx] if col_idx < len(table[row_idx]) else None
        if cell:
            parts.append(str(cell))

        # 朝食・昼食はメイン行に完全なメニューがあるため後続行不要
        if meal_name != "夕食":
            combined = "\n".join(parts)
            cleaned = _clean_menu_text(combined)
            if cleaned:
                menu[meal_name] = cleaned
            continue

        # 夕食のみ後続行からデータを収集
        for offset in range(1, 5):
            next_idx = row_idx + offset
            if next_idx >= len(table):
                break
            next_row = table[next_idx]
            if not next_row:
                continue
            # 栄養価の行や「ご飯」サイズ行はスキップ
            first_cell = str(next_row[0]).replace("\n", "") if next_row[0] else ""
            if re.search(r"栄養|ご飯", first_cell):
                break
            if _is_nutrition_row(next_row, col_idx):
                continue
            if col_idx < len(next_row) and next_row[col_idx]:
                parts.append(str(next_row[col_idx]))

        combined = "\n".join(parts)
        # 夕食は A食/B食 を分類して格納
        dinner_ab = _parse_dinner_ab(combined)
        menu[meal_name] = dinner_ab

    return menu  # {} なら日付列はあるがデータなし


# ============================
#  メニュー取得 & フォーマット
# ============================

# 解析済みメニューのキャッシュ {(url, year, month): {date: menu_dict}}
# 改訂版は別 URL (-2, -3, ...) で公開されるため、URL が同じなら中身も同じとみなす
_month_menu_cache: dict[tuple[str, int, int], dict] = {}

# 存在確認済み URL のキャッシュ {(year, month): (確認時刻, [url, ...])}
_existing_url_cache: dict[tuple[int, int], tuple[datetime, list[str]]] = {}
_EXISTING_URL_TTL = timedelta(minutes=10)


def _url_exists(url):
    """HEAD リクエストで URL が存在するか確認"""
    try:
        resp = requests.head(url, timeout=5, allow_redirects=True)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def _find_existing_pdf_urls(target_date):
    """全候補に並列で HEAD を送り、存在する PDF URL を優先順で返す。
    改訂版の公開を拾えるよう、結果は一定時間だけキャッシュする。"""
    cache_key = (target_date.year, target_date.month)
    cached = _existing_url_cache.get(cache_key)
    if cached and datetime.now(JST) - cached[0] < _EXISTING_URL_TTL:
        return cached[1]

    candidates = build_pdf_url_candidates(target_date)
    with ThreadPoolExecutor(max_workers=16) as executor:
        exists = list(executor.map(_url_exists, candidates))
    urls = [url for url, ok in zip(candidates, exists) if ok]
    if urls:
        _existing_url_cache[cache_key] = (datetime.now(JST), urls)
    return urls


def extract_menus_for_month(pdf_bytes, year, month):
    """PDF から指定月の全日分のメニューを {date: menu_dict} で返す。
    PDF は 1 回だけ開き、全ページのテキスト抽出はテーブルで見つからない日がある場合のみ行う。"""
    from datetime import date
    menus = {}
    with pdfplumber.open(pdf_bytes) as pdf:
        # 別の月の PDF なら重いテーブル解析をせずに終了（1 ページ目に日付が並ぶ）
        first_text = pdf.pages[0].extract_text() or "" if pdf.pages else ""
        if not re.search(rf"(?<!\d){month}\s*(?:月|/)\s*\d", first_text):
            return menus

        tables = [t for page in pdf.pages for t in page.extract_tables()]
        texts = None
        for day in range(1, calendar.monthrange(year, month)[1] + 1):
            menu = None
            for table in tables:
                menu = _parse_table(table, month, day)
                if menu is not None:
                    break
            if menu is None:
                if texts is None:
                    texts = [page.extract_text() or "" for page in pdf.pages]
                for text in texts:
                    menu = _parse_text(text, month, day)
                    if menu is not None:
                        break
            if menu:
                menus[date(year, month, day)] = menu
    return menus


def _get_menus_from_url(url, year, month):
    """URL の PDF から指定月のメニューを取得（キャッシュ付き）"""
    key = (url, year, month)
    if key not in _month_menu_cache:
        pdf_bytes = download_pdf(url)
        _month_menu_cache[key] = extract_menus_for_month(pdf_bytes, year, month)
        logger.info("PDF を解析しました: %s (%d 日分)", url, len(_month_menu_cache[key]))
    return _month_menu_cache[key]


def get_menus_for_month(year, month):
    """指定月の全メニューを ({date: menu_dict}, url) で返す"""
    from datetime import date
    first_day = date(year, month, 1)

    # HEAD が使えないサーバーへの対応として、見つからなければ全候補を GET で試す
    urls = _find_existing_pdf_urls(first_day) or build_pdf_url_candidates(first_day)

    for url in urls:
        try:
            menus = _get_menus_from_url(url, year, month)
        except requests.HTTPError:
            continue
        if menus:
            return menus, url

    raise requests.HTTPError(f"{year}年{month}月の献立 PDF が見つかりません。")


def get_menu_for_date(target_date):
    """指定日のメニューを (menu_dict, url) で返す。
    PDF に載っていない日は空の dict を返す。"""
    menus, url = get_menus_for_month(target_date.year, target_date.month)
    return menus.get(target_date, {}), url


def _normalize_for_search(text):
    """検索用に表記ゆれを吸収する（全角半角・大小文字・ひらがな→カタカナ）"""
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(
        chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c
        for c in text
    )


def _iter_menu_lines(menu):
    """メニュー dict から (食事ラベル, 料理名) を順に返す"""
    for meal in ["朝食", "昼食", "夕食"]:
        value = menu.get(meal)
        if not value:
            continue
        if isinstance(value, dict):
            # 夕食の A食/B食/共通
            for key in ["A", "B", "共通"]:
                if value.get(key):
                    label = f"{meal}({key})" if key != "共通" else meal
                    for line in value[key].split("\n"):
                        yield label, line
        else:
            for line in value.split("\n"):
                yield meal, line


def search_menus(menus, keyword):
    """キーワードを含む料理を [(date, 食事ラベル, 料理名), ...] で返す"""
    needle = _normalize_for_search(keyword)
    hits = []
    for d in sorted(menus):
        for label, line in _iter_menu_lines(menus[d]):
            if needle in _normalize_for_search(line):
                hits.append((d, label, line))
    return hits


def format_search_embeds(keyword, year, month, hits, url):
    """検索結果を Discord Embed のリストに整形。
    Embed の上限 (フィールド 25 個・合計 6000 文字) を超える分は次の Embed に送る。"""
    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    emoji_map = {"朝食": "🌅", "昼食": "☀️", "夕食": "🌙"}
    title = f"🔍 「{keyword}」の検索結果（{year}年{month}月）"

    if not hits:
        embed = discord.Embed(title=title, color=0x00BFFF, url=url)
        embed.description = "該当するメニューはありませんでした。"
        return [embed]

    # 日付ごとにまとめる
    by_date = {}
    for d, label, line in hits:
        by_date.setdefault(d, []).append(f"{emoji_map.get(label[:2], '🍴')} {label}: {line}")

    # (フィールド名, 値) の一覧を作る。値が 1024 文字を超える日は複数フィールドに分割
    today = datetime.now(JST).date()
    fields = []
    for d in sorted(by_date):
        wd = weekday_names[d.weekday()]
        mark = "（今日）" if d == today else ""
        name = f"{d.month}/{d.day}（{wd}）{mark}"
        chunk = []
        for line in by_date[d]:
            if chunk and len("\n".join(chunk + [line])) > 1024:
                fields.append((name, "\n".join(chunk)))
                name = f"{d.month}/{d.day}（続き）"
                chunk = []
            chunk.append(line[:1024])
        fields.append((name, "\n".join(chunk)))

    embed = discord.Embed(
        title=title, color=0x00BFFF, url=url,
        description=f"{len(by_date)} 日分ヒットしました。",
    )
    embeds = [embed]
    for name, value in fields:
        if len(embed.fields) >= 25 or len(embed) + len(name) + len(value) > 5800:
            embed = discord.Embed(title=f"{title}（続き）", color=0x00BFFF, url=url)
            embeds.append(embed)
        embed.add_field(name=name, value=value, inline=False)
    return embeds


def format_menu_embed(target_date, menu, url):
    """メニュー情報を Discord Embed に整形"""
    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    wd = weekday_names[target_date.weekday()]
    title = f"🍽️ {target_date.year}年{target_date.month}月{target_date.day}日（{wd}）の献立"

    embed = discord.Embed(title=title, color=0x00BFFF, url=url)

    if not menu:
        embed.description = "⚠️ この日のメニュー情報を取得できませんでした。"
        return embed

    # テキストフォールバック結果
    if "メニュー" in menu:
        embed.description = menu["メニュー"]
        return embed

    # テーブル抽出結果
    emoji_map = {"朝食": "🌅", "昼食": "☀️", "夕食": "🌙"}
    for meal in ["朝食", "昼食", "夕食"]:
        if meal not in menu:
            continue

        # 夕食は A食/B食 の dict の場合がある
        if meal == "夕食" and isinstance(menu[meal], dict):
            ab = menu[meal]
            parts = []
            a_text = ab.get("A")
            b_text = ab.get("B")
            common_text = ab.get("共通")
            if a_text and b_text:
                parts.append(f"**【A食】**\n{a_text}")
                parts.append(f"**【B食】**\n{b_text}")
            elif a_text or b_text:
                # A/B の選択が無い日（カレー等）は「メイン」として表示
                parts.append(f"**【メイン】**\n{a_text or b_text}")
            if common_text:
                parts.append(f"**【共通】**\n{common_text}")
            value = "\n".join(parts)
            embed.add_field(
                name=f"{emoji_map.get(meal, '🍴')} {meal}",
                value=value or "情報なし",
                inline=False,
            )
        else:
            embed.add_field(
                name=f"{emoji_map.get(meal, '🍴')} {meal}",
                value=menu[meal] or "情報なし",
                inline=False,
            )

    return embed


# ============================
#  Bot イベント / タスク
# ============================

@client.event
async def on_ready():
    await tree.sync()
    logger.info("%s としてログインしました（スラッシュコマンド同期完了）", client.user)
    if not daily_menu_notify.is_running():
        daily_menu_notify.start()
    asyncio.create_task(warm_menu_cache())


async def warm_menu_cache():
    """今月・来月の献立を先読みしてキャッシュしておく（初回コマンドの高速化）"""
    today = datetime.now(JST).date()
    next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    for d in (today, next_month):
        try:
            await asyncio.to_thread(get_menus_for_month, d.year, d.month)
        except Exception as e:
            logger.info("%d年%d月の献立の先読みをスキップ: %s", d.year, d.month, e)


@tasks.loop(time=dtime(hour=22, minute=0, tzinfo=JST))
async def daily_menu_notify():
    """毎日 JST 22:00 に翌日の献立を通知"""
    channel = client.get_channel(CHANNEL_ID)
    if not channel:
        logger.error("チャンネル ID %s が見つかりません", CHANNEL_ID)
        return

    tomorrow = datetime.now(JST).date() + timedelta(days=1)
    try:
        menu, url = await asyncio.to_thread(get_menu_for_date, tomorrow)
        embed = format_menu_embed(tomorrow, menu, url)
        await channel.send(embed=embed)
        logger.info("翌日 (%s) の献立を送信しました", tomorrow)
    except requests.HTTPError as e:
        await channel.send(f"⚠️ 献立 PDF の取得に失敗しました（{e}）")
    except Exception:
        logger.exception("献立通知でエラーが発生しました")
        await channel.send("⚠️ 献立の取得中にエラーが発生しました。")


# ============================
#  日付パース
# ============================

def parse_date_input(date_str):
    """日付文字列をパースする。MM/DD, MM-DD, YYYY-MM-DD 形式に対応。
    MM/DD, MM-DD の場合は現在の年を自動補完する。
    戻り値: date or None
    """
    today = datetime.now(JST).date()
    # MM/DD or MM-DD
    m = re.fullmatch(r"(\d{1,2})[/\-](\d{1,2})", date_str.strip())
    if m:
        try:
            from datetime import date
            return date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    # YYYY/MM/DD or YYYY-MM-DD
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


# ============================
#  スラッシュコマンド
# ============================

@tree.command(name="menu", description="指定日の献立を表示（省略で今日）")
@app_commands.describe(date_str="日付 (例: 4/1, 4-1, 2026/04/01)。省略すると今日。")
async def cmd_menu(interaction: discord.Interaction, date_str: str = None):
    if date_str:
        target = parse_date_input(date_str)
        if target is None:
            await interaction.response.send_message(
                "⚠️ 日付形式が不正です。`4/1` `4-1` `2026/04/01` のように指定してください。", ephemeral=True
            )
            return
    else:
        target = datetime.now(JST).date()

    await interaction.response.defer()
    try:
        menu, url = await asyncio.to_thread(get_menu_for_date, target)
        embed = format_menu_embed(target, menu, url)
        await interaction.followup.send(embed=embed)
    except requests.HTTPError:
        await interaction.followup.send(f"⚠️ 献立 PDF が見つかりません。\nURL: {build_pdf_url(target)}")
    except Exception as e:
        logger.exception("/menu コマンドでエラー")
        await interaction.followup.send(f"⚠️ エラーが発生しました: {e}")


@tree.command(name="tomorrow", description="明日の献立を表示")
async def cmd_tomorrow(interaction: discord.Interaction):
    await interaction.response.defer()
    tomorrow = datetime.now(JST).date() + timedelta(days=1)
    try:
        menu, url = await asyncio.to_thread(get_menu_for_date, tomorrow)
        embed = format_menu_embed(tomorrow, menu, url)
        await interaction.followup.send(embed=embed)
    except requests.HTTPError:
        await interaction.followup.send(f"⚠️ 献立 PDF が見つかりません。\nURL: {build_pdf_url(tomorrow)}")
    except Exception as e:
        logger.exception("/tomorrow コマンドでエラー")
        await interaction.followup.send(f"⚠️ エラーが発生しました: {e}")


@tree.command(name="search", description="キーワードで献立を検索（例: カレー）")
@app_commands.describe(
    keyword="検索する料理名の一部 (例: カレー, 唐揚げ)",
    month="検索する月 (1〜12)。省略すると今月。",
)
async def cmd_search(
    interaction: discord.Interaction,
    keyword: str,
    month: app_commands.Range[int, 1, 12] = None,
):
    today = datetime.now(JST).date()
    year = today.year
    if month is None:
        month = today.month

    await interaction.response.defer()
    try:
        # 月全体の PDF 解析は時間がかかるため別スレッドで実行
        menus, url = await asyncio.to_thread(get_menus_for_month, year, month)
        hits = search_menus(menus, keyword)
        # 1 メッセージの Embed 合計は 6000 文字までなので 1 件ずつ送る
        for embed in format_search_embeds(keyword, year, month, hits, url):
            await interaction.followup.send(embed=embed)
    except requests.HTTPError:
        await interaction.followup.send(f"⚠️ {year}年{month}月の献立 PDF が見つかりません。")
    except Exception as e:
        logger.exception("/search コマンドでエラー")
        await interaction.followup.send(f"⚠️ エラーが発生しました: {e}")


@tree.command(name="debug", description="PDF の生データを表示（デバッグ用）")
@app_commands.describe(date_str="日付 (例: 4/6, 4-1, 2026/04/01)。省略すると今日。")
async def cmd_debug(interaction: discord.Interaction, date_str: str = None):
    if date_str:
        target = parse_date_input(date_str)
        if target is None:
            await interaction.response.send_message("⚠️ 日付形式が不正です。", ephemeral=True)
            return
    else:
        target = datetime.now(JST).date()

    await interaction.response.defer()
    url = build_pdf_url(target)
    msgs = [f"📄 PDF URL: {url}"]

    try:
        pdf_bytes = download_pdf(url)
    except requests.HTTPError as e:
        await interaction.followup.send(f"⚠️ PDF の取得に失敗: {e}")
        return

    with pdfplumber.open(pdf_bytes) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables()
            msg = f"**ページ {page_num}**: テーブル {len(tables)} 個検出\n"
            for t_idx, table in enumerate(tables):
                msg += f"```\nテーブル {t_idx + 1} ({len(table)} 行):\n"
                for row in table[:5]:
                    msg += str(row) + "\n"
                if len(table) > 5:
                    msg += f"... 他 {len(table) - 5} 行\n"
                msg += "```\n"
            if len(msg) > 1900:
                msg = msg[:1900] + "\n... (切り詰め)"
            msgs.append(msg)

    await interaction.followup.send("\n".join(msgs[:3]))


# ============================
#  起動
# ============================

if __name__ == "__main__":
    if not TOKEN:
        logger.error("DISCORD_TOKEN が設定されていません。.env を確認してください。")
    else:
        client.run(TOKEN)

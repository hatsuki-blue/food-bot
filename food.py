import os
import re
import io
import logging

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
    """対象日の献立 PDF URL を生成"""
    reiwa_year, month = get_reiwa_year_and_month(target_date)
    return (
        "https://www.kushiro-ct.ac.jp/wordpress/wp-content/uploads/"
        f"2015/11/R{reiwa_year}.{month}_ryokondate.pdf"
    )


def download_pdf(url):
    """PDF をダウンロードして BytesIO で返す"""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return io.BytesIO(resp.content)


# ============================
#  PDF パース
# ============================

def extract_menu_for_day(pdf_bytes, day):
    """PDF から指定日 (日にち) の三食メニューを抽出して返す。
    戻り値: dict{"朝食": ..., "昼食": ..., "夕食": ...} or None
    """
    with pdfplumber.open(pdf_bytes) as pdf:
        # 1) テーブル抽出を試行
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                result = _parse_table(table, day)
                if result is not None:
                    # 日付列が見つかった（データ有無問わず）→ テキスト解析に落とさない
                    return result or None

        # 2) テーブルに日付列が無い場合のみテキストベースで試行
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                result = _parse_text(text, day)
                if result:
                    return result

    return None


def _find_date_column(header_row, day):
    """ヘッダー行から対象日の列インデックスを返す"""
    for j, cell in enumerate(header_row):
        if not cell:
            continue
        # 「3月6日」のようなパターンから日を抽出
        m = re.search(r"(\d{1,2})月(\d{1,2})日", str(cell))
        if m and int(m.group(2)) == day:
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
    current = common_items  # デフォルトは共通

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
            continue
        # 単独の "B" → B食セクション開始
        if re.match(r"^B$", line):
            current = b_items
            continue
        # "A メニュー名" プレフィックス付き
        m = re.match(r"^A\s+(.+)", line)
        if m:
            a_items.append(m.group(1))
            current = a_items
            continue
        # "B メニュー名" プレフィックス付き
        m = re.match(r"^B\s+(.+)", line)
        if m:
            b_items.append(m.group(1))
            current = b_items
            continue
        current.append(line)

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


def _parse_text(text, day):
    """テキストから指定日のメニューをフォールバック抽出"""
    # 対象日を含む行を探す
    day_pattern = re.compile(rf"(\d{{1,2}})月\s*{day}日")
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

    return menu if menu else None


def _parse_table(table, day):
    """テーブルから指定日のメニューを検索
    PDF は日付が列、食事が行のレイアウト"""
    if not table or len(table) < 2:
        return None

    # ヘッダー行（Row 0）から対象日の列を特定
    col_idx = _find_date_column(table[0], day)
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

def get_menu_for_date(target_date):
    """指定日のメニューを (menu_dict, url) で返す"""
    url = build_pdf_url(target_date)
    logger.info("PDF URL: %s", url)
    pdf_bytes = download_pdf(url)
    menu = extract_menu_for_day(pdf_bytes, target_date.day)
    return menu, url


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
            parts.append(f"**【A食】**\n{a_text if a_text else 'なし'}")
            parts.append(f"**【B食】**\n{b_text if b_text else 'なし'}")
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


@tasks.loop(time=dtime(hour=22, minute=0, tzinfo=JST))
async def daily_menu_notify():
    """毎日 JST 22:00 に翌日の献立を通知"""
    channel = client.get_channel(CHANNEL_ID)
    if not channel:
        logger.error("チャンネル ID %s が見つかりません", CHANNEL_ID)
        return

    tomorrow = datetime.now(JST).date() + timedelta(days=1)
    try:
        menu, url = get_menu_for_date(tomorrow)
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
        menu, url = get_menu_for_date(target)
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
        menu, url = get_menu_for_date(tomorrow)
        embed = format_menu_embed(tomorrow, menu, url)
        await interaction.followup.send(embed=embed)
    except requests.HTTPError:
        await interaction.followup.send(f"⚠️ 献立 PDF が見つかりません。\nURL: {build_pdf_url(tomorrow)}")
    except Exception as e:
        logger.exception("/tomorrow コマンドでエラー")
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

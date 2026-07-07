import os
import re
import json
from collections import Counter
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, JoinEvent
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ── Google Sheets 連線 ──
def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID).sheet1

# ── 工具函式 ──
def get_source_id(event):
    source = event.source
    if source.type == "group":
        return source.group_id
    elif source.type == "room":
        return source.room_id
    else:
        return source.user_id

def get_all_orders(sheet, source_id):
    """取得指定 source_id 的所有訂單"""
    records = sheet.get_all_records()
    return {
        int(r["order_id"]): {
            "name": r["name"],
            "meal": r["meal"],
            "price": r["price"]
        }
        for r in records
        if str(r["source_id"]) == str(source_id)
    }

def get_next_order_id(sheet, source_id):
    """取得下一個訂單號碼"""
    orders = get_all_orders(sheet, source_id)
    if not orders:
        return 1
    return max(orders.keys()) + 1

def get_status_row(sheet, source_id):
    """找到 source_id 對應的狀態列（用 order_id=0 代表狀態列）"""
    records = sheet.get_all_records()
    for i, r in enumerate(records):
        if str(r["source_id"]) == str(source_id) and str(r["order_id"]) == "0":
            return i + 2  # +2 因為有標題列且從1開始
    return None

def is_active(sheet, source_id):
    """檢查該群組是否在接受訂單狀態"""
    records = sheet.get_all_records()
    for r in records:
        if str(r["source_id"]) == str(source_id) and str(r["order_id"]) == "0":
            return str(r["name"]) == "active"
    return False

def set_status(sheet, source_id, status):
    """設定狀態（active / inactive）"""
    row = get_status_row(sheet, source_id)
    if row:
        sheet.update_cell(row, 3, status)  # col 3 = name
    else:
        sheet.append_row([source_id, 0, status, "", ""])

def clear_orders(sheet, source_id):
    """清除該群組所有訂單（保留狀態列）"""
    records = sheet.get_all_records()
    rows_to_delete = []
    for i, r in enumerate(records):
        if str(r["source_id"]) == str(source_id) and str(r["order_id"]) != "0":
            rows_to_delete.append(i + 2)
    # 從後面刪，避免行號跑掉
    for row in sorted(rows_to_delete, reverse=True):
        sheet.delete_rows(row)

def build_summary(orders, title="📋 訂餐明細"):
    lines = [title, ""]
    lines.append("─────────────────")
    total_amount = 0
    for oid, o in sorted(orders.items()):
        lines.append(f"#{oid}  {o['name']}　{o['meal']}　${o['price']}")
        total_amount += int(o['price'])
    lines.append("─────────────────")
    meal_counter = Counter(o['meal'] for o in orders.values())
    lines.append("📊 數量小計：")
    for meal, count in meal_counter.most_common():
        lines.append(f"   {meal} × {count}")
    lines.append("")
    name_total = {}
    for o in orders.values():
        name_total[o['name']] = name_total.get(o['name'], 0) + int(o['price'])
    lines.append("💰 金額小計（每人）：")
    for name, amt in name_total.items():
        lines.append(f"   {name}：${amt}")
    lines.append("")
    lines.append(f"💵 總金額：${total_amount}")
    lines.append(f"🧾 共 {len(orders)} 筆訂單")
    return "\n".join(lines)

def parse_order_line(line):
    match = re.match(r'^(\S+)\s+(\S+)\s+\$(\d+(\.\d+)?)$', line.strip())
    if match:
        name = match.group(1)
        meal = match.group(2)
        price = float(match.group(3))
        price_display = int(price) if price == int(price) else price
        return name, meal, price_display
    return None

# ── Webhook ──
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(JoinEvent)
def handle_join(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(
            text="🍱 午餐整理大師已加入！\n\n輸入「開始點餐」來開始接受訂單。"
        )
    )

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    source_id = get_source_id(event)

    try:
        sheet = get_sheet()
    except Exception as e:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"⚠️ 無法連線到資料庫，請稍後再試。")
        )
        return

    # ── 1. 開始點餐 ──
    if text == "開始點餐":
        clear_orders(sheet, source_id)
        set_status(sheet, source_id, "active")
        reply = (
            "🍱 開始接受訂單！\n\n"
            "📌 單筆格式：\n"
            "   姓名 餐點 $價格\n"
            "   例：小明 雞腿便當 $80\n\n"
            "📌 多筆格式（換行輸入）：\n"
            "   小明 雞腿便當 $80\n"
            "   小花 排骨便當 $75\n\n"
            "📌 其他指令：\n"
            "   !! → 查看明細\n"
            "   刪除 訂單號碼 → 刪除訂單\n"
            "   結束訂單 → 完成統計"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ── 2. 結束訂單 ──
    if text == "結束訂單":
        orders = get_all_orders(sheet, source_id)
        if not orders and not is_active(sheet, source_id):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ 目前沒有進行中的訂單。")
            )
            return
        set_status(sheet, source_id, "inactive")
        if not orders:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="📭 訂單已結束，但沒有任何訂單紀錄。")
            )
            return
        reply = build_summary(orders, title="✅ 訂單已結束！最終統計如下：")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ── 3. 查看明細 ──
    if text == "!!":
        orders = get_all_orders(sheet, source_id)
        if not orders:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="📭 目前沒有任何訂單。")
            )
            return
        active = is_active(sheet, source_id)
        status = "（接受中）" if active else "（已結束）"
        reply = build_summary(orders, title=f"📋 訂餐明細 {status}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ── 4. 刪除訂單 ──
    if text.startswith("刪除 ") or text.startswith("刪除　"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ 格式錯誤，請輸入：刪除 訂單號碼\n例：刪除 3")
            )
            return
        order_id = int(parts[1])
        orders = get_all_orders(sheet, source_id)
        if order_id not in orders:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"⚠️ 找不到訂單編號 #{order_id}，請確認號碼是否正確。")
            )
            return
        # 找到該列並刪除
        records = sheet.get_all_records()
        for i, r in enumerate(records):
            if str(r["source_id"]) == str(source_id) and str(r["order_id"]) == str(order_id):
                sheet.delete_rows(i + 2)
                break
        deleted = orders[order_id]
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"🗑️ 已刪除訂單 #{order_id}\n"
                     f"   {deleted['name']} — {deleted['meal']} ${deleted['price']}"
            )
        )
        return

    # ── 5. 登記訂單 ──
    if is_active(sheet, source_id):
        lines = text.splitlines()
        lines = [l.strip() for l in lines if l.strip()]
        parsed = []
        failed = []
        for line in lines:
            result = parse_order_line(line)
            if result:
                parsed.append(result)
            else:
                failed.append(line)

        if parsed:
            reply_lines = []
            next_id = get_next_order_id(sheet, source_id)
            for name, meal, price in parsed:
                sheet.append_row([source_id, next_id, name, meal, price])
                reply_lines.append(f"   訂單 #{next_id}：{name} — {meal} ${price}")
                next_id += 1
            reply = f"✅ 收到 {len(parsed)} 筆訂單！\n" + "\n".join(reply_lines)
            if failed:
                reply += f"\n\n⚠️ 以下 {len(failed)} 行格式有誤，未登記：\n"
                reply += "\n".join([f"   {l}" for l in failed])
                reply += "\n\n格式請參考：姓名 餐點 $價格"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply)
            )
            return

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

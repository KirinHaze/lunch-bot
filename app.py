import os
import re
from collections import Counter
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, JoinEvent

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ── 狀態儲存（記憶體版）──
sessions = {}

def get_source_id(event):
    source = event.source
    if source.type == "group":
        return source.group_id
    elif source.type == "room":
        return source.room_id
    else:
        return source.user_id

def get_session(source_id):
    if source_id not in sessions:
        sessions[source_id] = {"active": False, "next_order_id": 1, "orders": {}}
    return sessions[source_id]

def build_summary(orders, title="📋 訂餐明細"):
    lines = [title, ""]
    lines.append("─────────────────")

    total_amount = 0
    for oid, o in sorted(orders.items()):
        lines.append(f"#{oid}  {o['name']}　{o['meal']}　${o['price']}")
        total_amount += o['price']

    lines.append("─────────────────")

    meal_counter = Counter(o['meal'] for o in orders.values())
    lines.append("📊 數量小計：")
    for meal, count in meal_counter.most_common():
        lines.append(f"   {meal} × {count}")

    lines.append("")

    name_total = {}
    for o in orders.values():
        name_total[o['name']] = name_total.get(o['name'], 0) + o['price']
    lines.append("💰 金額小計（每人）：")
    for name, amt in name_total.items():
        lines.append(f"   {name}：${amt}")

    lines.append("")
    lines.append(f"💵 總金額：${total_amount}")
    lines.append(f"🧾 共 {len(orders)} 筆訂單")

    return "\n".join(lines)

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
    session = get_session(source_id)

    # ── 1. 開始點餐 ──
    if text == "開始點餐":
        session["active"] = True
        session["next_order_id"] = 1
        session["orders"] = {}
        reply = (
            "🍱 開始接受訂單！\n\n"
            "📌 訂餐格式：\n"
            "   姓名 餐點 $價格\n"
            "   例：小明 雞腿便當 $80\n\n"
            "📌 其他指令：\n"
            "   !! → 查看明細\n"
            "   刪除 訂單號碼 → 刪除訂單\n"
            "   結束訂單 → 完成統計"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ── 2. 結束訂單 ──
    if text == "結束訂單":
        if not session["active"] and not session["orders"]:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ 目前沒有進行中的訂單。")
            )
            return
        session["active"] = False
        orders = session["orders"]
        if not orders:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="📭 訂單已結束，但沒有任何訂單紀錄。")
            )
            return
        reply = build_summary(orders, title="✅ 訂單已結束！最終統計如下：")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ── 3. 查看明細 (!!) ──
    if text == "!!":
        orders = session["orders"]
        if not orders:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="📭 目前沒有任何訂單。")
            )
            return
        status = "（接受中）" if session["active"] else "（已結束）"
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
        if order_id not in session["orders"]:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"⚠️ 找不到訂單編號 #{order_id}，請確認號碼是否正確。")
            )
            return
        deleted = session["orders"].pop(order_id)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"🗑️ 已刪除訂單 #{order_id}\n"
                     f"   {deleted['name']} — {deleted['meal']} ${deleted['price']}"
            )
        )
        return

    # ── 5. 登記訂單（格式：姓名 餐點 $價格）──
    if session["active"]:
        match = re.match(r'^(\S+)\s+(\S+)\s+\$(\d+(\.\d+)?)$', text)
        if match:
            name = match.group(1)
            meal = match.group(2)
            price = float(match.group(3))
            price_display = int(price) if price == int(price) else price

            order_id = session["next_order_id"]
            session["orders"][order_id] = {
                "name": name,
                "meal": meal,
                "price": price_display
            }
            session["next_order_id"] += 1

            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=f"✅ 收到！\n"
                         f"   訂單 #{order_id}：{name} — {meal} ${price_display}"
                )
            )
            return

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

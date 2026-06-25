#!/usr/bin/env python3
"""信用卡账单提醒 - 按需监听版
命令：
- send: 只发送提醒消息（带"启动监听"按钮）
- watchdog: 轻量级看门狗，监听"启动"按钮
- start: 启动完整监听进程（30分钟超时）
- stop: 停止所有进程，清除按钮
"""
import json, os, sys, time, signal, subprocess
from datetime import datetime
from urllib.request import Request, urlopen

def load_env():
    env = {}
    with open("/opt/data/.env") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

ENV = load_env()
BOT_TOKEN = ENV.get("BILL_BOT_TOKEN", ENV.get("TELEGRAM_BOT_TOKEN", ""))
CHAT_ID = ENV.get("TELEGRAM_HOME_CHANNEL", "")
STATE_FILE = os.path.expanduser("~/credit-card-bill-monitor/state.json")
PID_FILE = os.path.expanduser("~/credit-card-bill-monitor/.poll.pid")
WD_PID_FILE = os.path.expanduser("~/credit-card-bill-monitor/.watchdog.pid")
MSG_FILE = os.path.expanduser("~/credit-card-bill-monitor/.last_msg_ids.json")
SCRIPT = os.path.expanduser("~/credit-card-bill-monitor/scripts/tg_bill_reminder.py")
TIMEOUT = 1800  # 30分钟

def tg_api(method, data=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    if data:
        req = Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    else:
        req = Request(url)
    try:
        resp = urlopen(req, timeout=35)
        return json.loads(resp.read())
    except Exception as e:
        if "409" in str(e): return {"ok": False, "error": "409"}
        return None

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f: return json.load(f)
    return {"cards": [], "last_update": None}

def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f: json.dump(s, f, ensure_ascii=False, indent=2)

def load_msg_ids():
    if os.path.exists(MSG_FILE):
        with open(MSG_FILE, "r") as f: return json.load(f)
    return []

def save_msg_ids(ids):
    with open(MSG_FILE, "w") as f: json.dump(ids, f)

def add_msg_id(mid):
    ids = load_msg_ids()
    if mid not in ids: ids.append(mid); save_msg_ids(ids)

def mark_processed(person, bank, bill_day=None):
    state = load_state()
    for c in state["cards"]:
        if c.get("person") == person and c.get("bank") == bank:
            if bill_day is None or c.get("bill_day") == bill_day:
                c["status"] = "已处理"; c["processed_at"] = datetime.now().isoformat()
                save_state(state); return True
    return False

def mark_all():
    state = load_state(); count = 0
    for c in state["cards"]:
        if c.get("status") != "已处理":
            c["status"] = "已处理"; c["processed_at"] = datetime.now().isoformat(); count += 1
    save_state(state); return count

def refresh_bills_async(msg_id, expanded, listening):
    """异步刷新账单数据"""
    import threading
    
    def do_refresh():
        script = os.path.expanduser("~/credit-card-bill-monitor/scripts/bill_manager_final.py")
        try:
            r = subprocess.run(["python3", script], capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                # 刷新成功，更新消息
                update_msg(msg_id, expanded, listening)
                # 发送成功通知
                tg_api("sendMessage", {"chat_id": CHAT_ID, "text": "✅ 刷新完成！账单数据已更新"})
            else:
                # 刷新失败，发送错误提示
                tg_api("sendMessage", {"chat_id": CHAT_ID, "text": f"❌刷新失败: {r.stderr[:200]}"})
        except Exception as e:
            tg_api("sendMessage", {"chat_id": CHAT_ID, "text": f"❌刷新异常: {str(e)[:200]}"})
    
    t = threading.Thread(target=do_refresh, daemon=True)
    t.start()

def get_unprocessed():
    state = load_state(); today = datetime.now().strftime("%Y-%m-%d")
    unprocessed = [c for c in state["cards"] if c.get("status") != "已处理"]
    def uk(c):
        try: return (datetime.strptime(c["pay_date"], "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days
        except: return 999
    unprocessed.sort(key=uk); return unprocessed

def build_text():
    today = datetime.now().strftime("%Y-%m-%d"); unprocessed = get_unprocessed()
    if not unprocessed: return "✅ 所有账单已处理！"
    lines = ["💳 *未处理账单*\n"]; total = 0
    for i, c in enumerate(unprocessed):
        p, b = c.get("person",""), c.get("bank","")
        amt, pd, ok = c.get("amount",""), c.get("pay_date",""), c.get("amount_confirmed", False)
        try:
            dl = (datetime.strptime(pd, "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days
            urg = "❌已过期" if dl < 0 else f"⚠️{dl}天" if dl <= 3 else f"✅{dl}天"
        except: urg = "❓"
        amt_s = f"￥{amt}" if ok and float(amt) > 0 else "￥0" if ok else "❓"
        card_display = c.get('card_name', '') or b
        lines.append(f"{i+1}. {p}-{card_display} {amt_s} {urg}")
        if ok:
            try: total += float(amt)
            except: pass
    lines.append(f"\n💰 合计: ￥{total:.2f}"); return "\n".join(lines)

def build_kb(expanded=False, listening=False):
    unprocessed = get_unprocessed()
    if not unprocessed: return None
    kb = []
    if listening:
        if expanded:
            row = []
            for c in unprocessed:
                p, b = c.get("person",""), c.get("bank","")
                days = c.get('days_until_due', 0)
                days_str = f"{days}天" if days >= 0 else "已过期"
                btn_text = f"✅{p}-{c.get('card_name','') or b} {days_str}"
                row.append({"text": btn_text, "callback_data": f"pay|{p}|{b}|{c.get('bill_day','')}"})
                if len(row) == 2: kb.append(row); row = []
            if row: kb.append(row)
            kb.append([{"text": "📋折叠", "callback_data": "collapse"}])
        else:
            kb.append([{"text": f"📋展开({len(unprocessed)}张)", "callback_data": "expand"}])
        kb.append([{"text": "🔄刷新", "callback_data": "refresh"}, {"text": "⏹停止监听", "callback_data": "stop"}])
    else:
        # 未监听状态：只显示启动按钮和刷新按钮
        kb.append([{"text": f"🔔启动监听({len(unprocessed)}张待处理)", "callback_data": "start_listen"}])
        kb.append([{"text": "🔄刷新", "callback_data": "refresh"}])
    return {"inline_keyboard": kb}

def update_msg(msg_id, expanded=False, listening=True):
    text = build_text(); kb = build_kb(expanded, listening)
    d = {"chat_id": CHAT_ID, "message_id": msg_id, "text": text, "parse_mode": "Markdown"}
    if kb: d["reply_markup"] = kb
    else: d["reply_markup"] = {"inline_keyboard": []}
    return tg_api("editMessageText", d)

def clear_all_buttons():
    ids = load_msg_ids(); cleared = 0
    for mid in ids:
        try:
            r = tg_api("editMessageReplyMarkup", {"chat_id": CHAT_ID, "message_id": mid, "reply_markup": {"inline_keyboard": []}})
            if r and r.get("ok"): cleared += 1
        except: pass
    save_msg_ids([]); return cleared

def send():
    """发送消息（不启动监听）"""
    text = build_text(); kb = build_kb(expanded=False, listening=False)
    d = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if kb: d["reply_markup"] = kb
    r = tg_api("sendMessage", d)
    if r and r.get("ok"):
        mid = r["result"]["message_id"]; print(f"消息已发送: msg_id={mid}"); add_msg_id(mid); return mid
    return None

def watchdog():
    """轻量级看门狗：只监听"启动"按钮，然后启动完整监听"""
    with open(WD_PID_FILE, "w") as f: f.write(str(os.getpid()))
    print(f"看门狗启动 PID:{os.getpid()}")
    offset = 0

    def stop(s=None, f=None):
        print("\n看门狗停止")
        try: os.remove(WD_PID_FILE)
        except: pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)

    while True:
        try:
            r = tg_api("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ["callback_query"]})
            if not r or not r.get("ok"):
                time.sleep(3); continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                cb = u.get("callback_query")
                if not cb: continue
                data = cb.get("data", ""); cid = cb["id"]
                if data == "start_listen":
                    tg_api("answerCallbackQuery", {"callback_query_id": cid, "text": "🔔启动中..."})
                    # 获取消息ID
                    msg_id = cb.get("message", {}).get("message_id")
                    # 启动完整监听进程（等待退出后再继续轮询，避免409冲突）
                    print(f"启动监听进程 (msg_id={msg_id})")
                    subprocess.run([sys.executable, SCRIPT, "poll", str(msg_id)])
                    print("监听进程已退出，恢复看门狗")
                elif data == "stop":
                    tg_api("answerCallbackQuery", {"callback_query_id": cid, "text": "⏹已停止"})
                    # 停止监听进程
                    if os.path.exists(PID_FILE):
                        try:
                            pid = int(open(PID_FILE).read().strip())
                            os.kill(pid, signal.SIGTERM)
                        except: pass
                    # 恢复为"启动监听"按钮
                    msg_id = cb.get("message", {}).get("message_id")
                    if msg_id:
                        text = build_text()
                        tg_api("editMessageText", {"chat_id": CHAT_ID, "message_id": msg_id, "text": text + "\n\n⏹_监听已停止_", "parse_mode": "Markdown", "reply_markup": build_kb(expanded=False, listening=False)})
                elif data == "refresh":
                    tg_api("answerCallbackQuery", {"callback_query_id": cid, "text": "🔄刷新中，请稍候..."})
                    msg_id = cb.get("message", {}).get("message_id")
                    if msg_id:
                        refresh_bills_async(msg_id, expanded=False, listening=False)
        except KeyboardInterrupt: stop()
        except Exception as e: print(f"err: {e}"); time.sleep(5)

def poll(msg_id):
    """完整监听进程：处理所有 callback"""
    with open(PID_FILE, "w") as f: f.write(str(os.getpid()))
    print(f"监听中 PID:{os.getpid()} ... {TIMEOUT//60}分钟超时")
    # 更新消息为监听状态
    update_msg(msg_id, expanded=False, listening=True)
    offset = 0; last_activity = time.time(); expanded = False

    def stop(s=None, f=None):
        print("\n监听停止")
        try: os.remove(PID_FILE)
        except: pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)

    while True:
        if time.time() - last_activity > TIMEOUT:
            print(f"\n{TIMEOUT//60}分钟超时，自动停止")
            # 恢复为"启动监听"按钮，允许重新启动
            text = build_text()
            tg_api("editMessageText", {"chat_id": CHAT_ID, "message_id": msg_id, "text": text + f"\n\n⏰_{TIMEOUT//60}分钟超时，监听已停止_", "parse_mode": "Markdown", "reply_markup": build_kb(expanded=False, listening=False)})
            stop()
        try:
            r = tg_api("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ["callback_query"]})
            if not r or not r.get("ok"):
                if r and "409" in str(r.get("error","")): time.sleep(5)
                else: time.sleep(3)
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1; last_activity = time.time()
                cb = u.get("callback_query")
                if not cb: continue
                data = cb.get("data", ""); cid = cb["id"]
                if data == "stop":
                    tg_api("answerCallbackQuery", {"callback_query_id": cid, "text": "⏹已停止"})
                    # 恢复为"启动监听"按钮，允许重新启动
                    update_msg(msg_id, expanded=False, listening=False)
                    stop()
                elif data == "expand":
                    expanded = True; tg_api("answerCallbackQuery", {"callback_query_id": cid, "text": "📋已展开"}); update_msg(msg_id, expanded=True, listening=True)
                elif data == "collapse":
                    expanded = False; tg_api("answerCallbackQuery", {"callback_query_id": cid, "text": "📋已折叠"}); update_msg(msg_id, expanded=False, listening=True)
                elif data == "pay_all":
                    n = mark_all(); tg_api("answerCallbackQuery", {"callback_query_id": cid, "text": f"✅已标记{n}张"}); update_msg(msg_id, expanded, listening=True)
                elif data.startswith("pay|"):
                    parts = data.split("|")
                    p, b = parts[1], parts[2]
                    bd = int(parts[3]) if len(parts) > 3 else None
                    if mark_processed(p, b, bd): tg_api("answerCallbackQuery", {"callback_query_id": cid, "text": f"✅{p}-{b}已标记"}); update_msg(msg_id, expanded, listening=True)
                    else: tg_api("answerCallbackQuery", {"callback_query_id": cid, "text": "❌未找到"})
                elif data == "refresh":
                    tg_api("answerCallbackQuery", {"callback_query_id": cid, "text": "🔄刷新中，请稍候..."})
                    refresh_bills_async(msg_id, expanded, listening=True)
        except KeyboardInterrupt: stop()
        except Exception as e: print(f"err: {e}"); time.sleep(5)

def main():
    if len(sys.argv) < 2: print("用法: send|watchdog|start|stop|poll <msg_id>"); return
    cmd = sys.argv[1]
    if cmd == "stop":
        for pf in [PID_FILE, WD_PID_FILE]:
            if os.path.exists(pf):
                try: pid = int(open(pf).read().strip()); os.kill(pid, signal.SIGTERM); print(f"已停止 PID:{pid}")
                except: pass
                try: os.remove(pf)
                except: pass
        cleared = clear_all_buttons(); print(f"已清除 {cleared} 条历史消息按钮")
    elif cmd == "send": send()
    elif cmd == "watchdog": watchdog()
    elif cmd == "start":
        # 停止旧进程
        for pf in [PID_FILE, WD_PID_FILE]:
            if os.path.exists(pf):
                try: pid = int(open(pf).read().strip()); os.kill(pid, signal.SIGTERM)
                except: pass
                try: os.remove(pf)
                except: pass
        clear_all_buttons()
        # 清除旧 callback
        r = tg_api('getUpdates', {'offset': 0, 'timeout': 1, 'allowed_updates': ['callback_query']})
        if r and r.get('ok') and r.get('result'):
            last_id = r['result'][-1]['update_id'] + 1
            tg_api('getUpdates', {'offset': last_id, 'timeout': 1})
        mid = send()
        if mid: watchdog()  # 启动看门狗
    elif cmd == "poll":
        if len(sys.argv) < 3: print("用法: poll <msg_id>"); return
        poll(int(sys.argv[2]))

if __name__ == "__main__":
    main()

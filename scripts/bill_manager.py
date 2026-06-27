#!/usr/bin/env python3
"""信用卡账单管理系统 v4 - SQLite架构"""
import json
import os
import sys
from datetime import datetime

# 添加项目路径
sys.path.insert(0, os.path.expanduser("~/credit-card-bill-monitor"))
from scripts.db import init_db, get_db
from scripts.db.email_store import get_latest_email, get_email_count, get_email_by_bill_day, get_emails_by_bill_day
from scripts.rules.due_date import calc_billing_date, calc_due_date, get_billing_cycle, days_until_due

CONFIG_DIR = os.path.expanduser("~/credit-card-bill-monitor/config")
STATE_FILE = os.path.expanduser("~/credit-card-bill-monitor/state.json")

def load_cards():
    """加载卡片配置"""
    path = os.path.join(CONFIG_DIR, "cards.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_cardholders():
    """加载持卡人配置"""
    path = os.path.join(CONFIG_DIR, "cardholders.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"cardholders": {}, "person_map": {}}

def load_state():
    """加载状态"""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"cards": [], "last_update": None}

def save_state(state):
    """保存状态"""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def main(verbose=False):
    print("💳 信用卡账单管理系统 v4")
    print("=" * 60)
    
    init_db()
    cards_config = load_cards()
    cardholders = load_cardholders()
    state = load_state()
    
    today = datetime.now()
    print(f"\n📅 今天: {today.strftime('%Y-%m-%d')}")
    print(f"📧 邮件库: {get_email_count()} 封")
    print()
    
    # 遍历每张卡
    for card in cards_config:
        card_id = card["id"]
        person = card["person"]
        bank = card["bank"]
        card_name = card["card_name"]
        bill_day = card["bill_day"]
        due_rule = card["due_rule"]
        
        # 计算日期
        billing_date = calc_billing_date(bill_day, today)
        due_date = calc_due_date(bill_day, due_rule, today)
        billing_cycle = get_billing_cycle(bill_day, today)
        days_left = days_until_due(due_date, today)
        
        # 从邮件库查找匹配的邮件（按 bank + person + bill_day 匹配）
        billing_month = billing_date.strftime("%Y-%m")
        emails = get_emails_by_bill_day(bank, person, bill_day, billing_month)
        
        # 准备状态数据
        cs = {
            "card_id": card_id,
            "person": person,
            "bank": bank,
            "card_name": card_name,
            "bill_day": bill_day,
            "billing_cycle": billing_cycle,
            "billing_date": billing_date.strftime("%Y-%m-%d"),
            "pay_date": due_date.strftime("%Y-%m-%d"),
            "days_until_due": days_left,
            "amount": "",
            "amount_confirmed": False,
            "min_payment": "",
            "cardholder": "",
            "status": "未处理",
            "last_update": today.isoformat()
        }
        
        if emails:
            # 有匹配邮件 - 汇总金额
            total_amount = 0
            has_amount = False
            for email in emails:
                amt = email.get("parsed_amount", "")
                if amt:
                    try:
                        total_amount += float(amt)
                        has_amount = True
                    except:
                        pass
            cs["amount"] = str(total_amount) if has_amount else ""
            cs["amount_confirmed"] = has_amount
            cs["min_payment"] = emails[0].get("parsed_min_payment", "")
            cs["cardholder"] = emails[0].get("parsed_cardholder", "")
            cs["email_id"] = emails[0]["id"]
            cs["email_subject"] = emails[0].get("subject", "")
            
            if verbose:
                amount_str = f"￥{cs['amount']}" if cs['amount'] else "❓未解析"
                status_icon = "✅" if cs["amount_confirmed"] else "❓"
                due_icon = "❌已过期" if days_left < 0 else f"⚠️{days_left}天" if days_left <= 3 else f"✅{days_left}天"
                print(f"  {status_icon} {person:4}|{bank:10}|{amount_str:>12}|{due_date.strftime('%Y-%m-%d')}|{due_icon}")
        else:
            # 无匹配邮件
            if verbose:
                print(f"  ⚠️ {person:4}|{bank:10}|{'❓待解析':>12}|{due_date.strftime('%Y-%m-%d')}|")
        
        # 更新状态（用 card_id 作唯一键，避免重复）
        existing = next((c for c in state["cards"] 
                        if c.get("card_id") == card_id), None)
        
        if existing:
            # 保留已处理状态
            if existing.get("status") == "已处理":
                cs["status"] = "已处理"
                cs["processed_at"] = existing.get("processed_at")
            existing.update(cs)
        else:
            state["cards"].append(cs)
    
    # 保存状态
    state["last_update"] = today.isoformat()
    save_state(state)
    
    # 打印汇总
    print("\n" + "=" * 60)
    print("📋 账单汇总:\n")
    
    bp = {}
    for c in state["cards"]:
        bp.setdefault(c.get("person", ""), []).append(c)
    
    total = 0
    for person, cl in bp.items():
        print(f"👤 {person}:")
        t = 0
        for c in cl:
            a = c.get("amount", "") or "0"
            ok = c.get("amount_confirmed", False)
            pd = c.get("pay_date", "")
            st = c.get("status", "未处理")
            days = c.get("days_until_due", 0)
            
            if days < 0:
                due_str = "❌已过期"
            elif days <= 3:
                due_str = f"⚠️{days}天"
            else:
                due_str = f"✅{days}天"
            
            si = "✅" if st == "已处理" else "⏳"
            
            if ok:
                astr = f"{a}" if float(a) > 0 else "0(确认)"
            else:
                astr = "❓待解析"
            
            print(f"  • {c.get('card_name', c['bank']):12}|￥{astr:>12}|{pd}|{due_str}|{si}{st}")
            
            if ok:
                try:
                    t += float(a)
                except:
                    pass
        
        print(f"  💰 小计: ￥{t:.2f}")
        total += t
    
    print(f"\n💰 总计: ￥{total:.2f}")

if __name__ == "__main__":
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    main(verbose=verbose)

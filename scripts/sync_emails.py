#!/usr/bin/env python3
"""邮件同步脚本 - 增量拉取并解析邮件到SQLite"""
import json
import os
import sys
import subprocess
import re
import tempfile
from datetime import datetime

# 添加项目路径
sys.path.insert(0, os.path.expanduser("~/credit-card-bill-monitor"))
from scripts.db import init_db, get_db
from scripts.db.email_store import insert_email, email_exists, get_email_count

HIMALAYA_CMD = os.path.expanduser("~/.local/bin/himalaya")
CONFIG_DIR = os.path.expanduser("~/credit-card-bill-monitor/config")

def load_bank_rules():
    """加载银行规则"""
    path = os.path.join(CONFIG_DIR, "bank_rules.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def match_bank(subject, sender, bank_rules):
    """匹配银行"""
    text = f"{subject} {sender}".lower()
    for bank_name, rules in bank_rules.items():
        if bank_name == "default":
            continue
        for kw in rules.get("keywords", []):
            if kw.lower() in text:
                return bank_name
    return None

def decode_email(email_id):
    """解码邮件正文"""
    cmd = f"{HIMALAYA_CMD} message read {email_id} --account qq"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip() and "�" not in r.stdout:
            return r.stdout
    except:
        pass
    return decode_gbk(email_id)

def decode_gbk(email_id):
    """GBK编码邮件解码"""
    import quopri, base64
    cmd = f"{HIMALAYA_CMD} message export {email_id} --full --account qq"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        raw = r.stdout
        enc = "base64" if "Content-Transfer-Encoding: base64" in raw else "quoted-printable"
        idx = raw.find("Content-Type: text/html")
        if idx < 0:
            idx = raw.find("Content-Type: text/plain")
        if idx < 0:
            return None
        c = raw[idx:]
        sep = c.find("\n\n")
        if sep < 0:
            return None
        body = re.sub(r"------=_Part_.*", "", c[sep+2:])
        try:
            if enc == "base64":
                txt = base64.b64decode(re.sub(r"\s", "", body)).decode("gbk", errors="ignore")
            else:
                txt = quopri.decodestring(body.encode()).decode("gbk", errors="ignore")
            return re.sub(r"<[^>]+>", "\n", txt)
        except:
            return None
    except:
        return None

def download_pdf_attachment(email_id):
    """下载并解析PDF附件"""
    td = tempfile.mkdtemp(prefix="bill_")
    cmd = f"{HIMALAYA_CMD} attachment download {email_id} --account qq --downloads-dir {td}"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            for f in os.listdir(td):
                if f.lower().endswith(".pdf"):
                    pdf_path = os.path.join(td, f)
                    text = parse_pdf(pdf_path)
                    try:
                        os.remove(pdf_path)
                        os.rmdir(td)
                    except:
                        pass
                    return text
    except:
        pass
    return None

def parse_pdf(pdf_path):
    """解析PDF"""
    try:
        venv_py = "/tmp/pdfvenv/bin/python3"
        if os.path.exists(venv_py):
            script = f"import fitz; doc=fitz.open('{pdf_path}'); print(''.join(p.get_text() for p in doc))"
            r = subprocess.run([venv_py, "-c", script], capture_output=True, text=True, timeout=30)
            return r.stdout if r.returncode == 0 else ""
        else:
            r = subprocess.run(f"pdftotext '{pdf_path}' -", shell=True, capture_output=True, text=True, timeout=30)
            return r.stdout if r.returncode == 0 else ""
    except:
        return ""

def extract_bill_info(text, bank_name, bank_rules):
    """从文本提取账单信息"""
    if not text:
        return {}
    
    # 剥除 HTML 标签
    import re
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    
    rules = bank_rules.get(bank_name, bank_rules.get("default", {}))
    patterns = rules.get("amount_patterns", bank_rules["default"]["amount_patterns"])
    group_idx = rules.get("amount_group", 1)
    
    info = {}
    
    # 提取金额
    # 工商银行专用解析：从"应还款额"后面找 人民币(本位币)数字/RMB
    if bank_name == '工商':
        # 先找应还款额字段
        m = re.search(r"应还款额[\s\S]{0,500}?人民币\(本位币\)\s*(-?\d+\.\d+)/RMB", text)
        if not m:
            # 备选：直接找 数字/RMB（第一个）
            m = re.search(r"(-?\d+\.\d+)/RMB", text)
        if m:
            try:
                amount = m.group(1).replace(",", "")
                info["amount"] = str(abs(float(amount)))
            except:
                pass
    
    # 通用金额解析
    if not info.get("amount"):
        for p in patterns:
            m = re.search(p, text)
            if m:
                try:
                    amount = m.group(group_idx).replace(",", "")
                    info["amount"] = str(abs(float(amount)))
                    break
                except:
                    continue
    
    # 提取还款日
    for p in [
        r"到期还款日.*?(\d{4}年\d{1,2}月\d{1,2}日)",
        r"到期还款日[\s\S]*?(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
        r"还款日[\s\S]*?(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
    ]:
        m = re.search(p, text)
        if m:
            info["due_date"] = m.group(1).replace("年", "-").replace("月", "-").replace("日", "")
            break
    
    # 提取最低还款
    for p in [
        r"本期最低还款额.*?[¥￥]\s*([\d,]+\.?\d*)",
        r"最低还款额[\s\S]*?(\d+\.?\d+)/RMB",
    ]:
        m = re.search(p, text)
        if m:
            info["min_payment"] = m.group(1).replace(",", "")
            break
    
    # 提取账单周期
    m = re.search(r"账单周期[\s\S]*?(\d{4}[/-]\d{2}[/-]\d{2}).*?(\d{4}[/-]\d{2}[/-]\d{2})", text)
    if m:
        info["billing_cycle"] = f"{m.group(1)}~{m.group(2)}"
    
    # 提取账单月份和账单日
    # 先尝试提取完整日期的账单日（包含年月日）
    for p in [
            r"账单周期[\s\S]*?(\d{4})年(\d{1,2})月(\d{1,2})日[\s\S]*?(\d{4})年(\d{1,2})月(\d{1,2})日",
        r"账单日\s*Statement\s*Date\s*(\d{4})/(\d{2})/(\d{2})",
        r"账单日\s*Statement\s*Date\s*(\d{4})-(\d{2})-(\d{2})",
        r"账单日\s*Statement\s*Date\s*(\d{4})年(\d{1,2})月(\d{1,2})日",
        r"账单日期\s*Statement\s*Date\s*(\d{4})年(\d{1,2})月(\d{1,2})日",
        r"账单日\s+(\d{4})年(\d{1,2})月(\d{1,2})日",
        r"账单日\s+(\d{4})-(\d{2})-(\d{2})",
        r"本期账单日\s*(\d{4})-(\d{2})-(\d{2})",
        r"账单日\s*Statement\s*Closing\s*Date\s*(\d{4})-(\d{2})-(\d{2})",
        r"Statement\s*Closing\s*Date[\s\S]*?(\d{4})-(\d{2})-(\d{2})[\s\S]*?(\d{4})-(\d{2})-(\d{2})",
        r"账单日期\s*Statement\s*Date\s+到期还款日[\s\S]*?(\d{4})/(\d{2})/(\d{2})",
    ]:
        m = re.search(p, text)
        if m:
            # 根据 groups 数量选择正确的 group
            if len(m.groups()) >= 6:
                # Statement Closing Date 有两个日期，取第二个（账单日）
                info["bill_day"] = int(m.group(6))
                if "billing_month" not in info:
                    info["billing_month"] = f"{m.group(4)}-{int(m.group(5)):02d}"
            else:
                info["bill_day"] = int(m.group(3))
                if "billing_month" not in info:
                    info["billing_month"] = f"{m.group(1)}-{int(m.group(2)):02d}"
            break
    
    # 再尝试提取纯数字账单日（支持中文格式）
    if "bill_day" not in info:
        for p in [
            r"账单日[：:]\s*(\d{1,2})",
            r"账单日为(\d{1,2})日",
            r"(\d{1,2})日为您的账单日",
            r"每月(\d{1,2})日出账",
            r"账单日(\d{1,2})本期",
        ]:
            m = re.search(p, text)
            if m:
                info["bill_day"] = int(m.group(1))
                break
    
    # 如果还没提取到账单月，从账单周期推断
    if "billing_month" not in info and "billing_cycle" in info:
        try:
            cycle_end = info["billing_cycle"].split("~")[1]
            parts = cycle_end.replace("/", "-").split("-")
            if len(parts) == 3:
                info["billing_month"] = f"{parts[0]}-{int(parts[1]):02d}"
                info["bill_day"] = int(parts[2])
        except:
            pass
    
    for p in [
        r"(\d{4})年(\d{1,2})月账单",
        r"(\d{4})年(\d{1,2})月[日].*?账单",
        r"账单周期\s*(\d{4})[/-](\d{2})[/-](\d{2})",
    ]:
        m = re.search(p, text)
        if m:
            year = int(m.group(1))
            month = int(m.group(2))
            if 2020 <= year <= 2030 and 1 <= month <= 12:
                info["billing_month"] = f"{year}-{month:02d}"
                break
    
    # 如果没有提取到账单月，从到期还款日推断
    if "billing_month" not in info and "due_date" in info:
        try:
            from datetime import datetime
            due = datetime.strptime(info["due_date"][:10], "%Y-%m-%d")
            bill_month = due.month - 1
            bill_year = due.year
            if bill_month == 0:
                bill_month = 12
                bill_year -= 1
            info["billing_month"] = f"{bill_year}-{bill_month:02d}"
        except:
            pass
    
    return info

def identify_cardholder(text):
    """从邮件内容识别持卡人"""
    if not text:
        return None
    
    # 加载持卡人配置
    config_path = os.path.expanduser("~/credit-card-bill-monitor/config/cardholders.json")
    if not os.path.exists(config_path):
        return None
    
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    
    cardholders = config.get("cardholders", {})
    
    # 搜索姓名
    for name, person in cardholders.items():
        if name in text:
            return person
    
    return None

def fetch_all_emails():
    """获取所有邮件信封"""
    cmd = f"{HIMALAYA_CMD} envelope list --account qq --page-size 80 -o json"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return json.loads(r.stdout)
    except:
        pass
    return []

def sync_emails(verbose=False, force=False):
    """同步邮件"""
    init_db()
    bank_rules = load_bank_rules()
    
    # 获取邮件列表
    emails = fetch_all_emails()
    if verbose:
        print(f"获取到 {len(emails)} 封邮件")
    
    new_count = 0
    skip_count = 0
    
    for email in emails:
        email_id = email.get("id")
        
        # 跳过已存在的（除非force模式）
        if email_exists(email_id):
            if force:
                reparse_email(email_id, bank_rules, verbose)
                update_count += 1
            else:
                skip_count += 1
            continue
        
        subject = email.get("subject", "")
        sender_name = email.get("from", {}).get("name", "")
        sender_addr = email.get("from", {}).get("addr", "")
        sender = f"{sender_name} <{sender_addr}>"
        received_at = email.get("date", "")
        
        # 匹配银行
        bank = match_bank(subject, sender_name, bank_rules)
        
        # 跳过广告
        if "AD)" in subject or "好礼" in subject or "广告" in subject:
            if verbose:
                print(f"  跳过广告: {subject[:30]}")
            continue
        
        if not bank:
            if verbose:
                print(f"  未匹配银行: {subject[:30]}")
            continue
        
        # 解码邮件正文
        body_text = decode_email(email_id)
        
        # 检查是否有附件
        has_attachment = 0
        attachment_text = None
        rules = bank_rules.get(bank, {})
        if rules.get("source") == "attachment":
            has_attachment = 1
            attachment_text = download_pdf_attachment(email_id)
        
        # 提取账单信息
        text_to_parse = attachment_text if attachment_text else body_text
        bill_info = extract_bill_info(text_to_parse, bank, bank_rules)
        
        # 识别持卡人
        person = identify_cardholder(text_to_parse or body_text)
        
        # 存入数据库
        insert_email(
            email_id=email_id,
            subject=subject,
            sender=sender,
            received_at=received_at,
            bank=bank,
            person=person,
            body_text=body_text,
            has_attachment=has_attachment,
            attachment_text=attachment_text,
            parsed_amount=bill_info.get("amount"),
            parsed_due_date=bill_info.get("due_date"),
            parsed_cardholder=person,
            billing_month=bill_info.get("billing_month"),
            bill_day=bill_info.get("bill_day")
        )
        
        new_count += 1
        if verbose:
            amount = bill_info.get("amount", "未解析")
            print(f"  ✅ 新增: {bank} | {subject[:30]} | 金额: {amount}")
    
    if verbose:
        print(f"\n同步完成: 新增 {new_count}, 更新 {update_count}, 跳过 {skip_count}, 总计 {get_email_count()}")
    
    return new_count

def reparse_email(email_id, bank_rules, verbose=False):
    """重新解析已有邮件的金额"""
    conn = get_db()
    row = conn.execute("SELECT bank, body_text, attachment_text FROM emails WHERE id=?", (email_id,)).fetchone()
    if not row:
        conn.close()
        return
    
    bank, body_text, attachment_text = row[0], row[1], row[2]
    if not bank:
        conn.close()
        return
    
    rules = bank_rules.get(bank, bank_rules.get("default", {}))
    text_to_parse = attachment_text if rules.get("source") == "attachment" and attachment_text else body_text
    
    if not text_to_parse:
        conn.close()
        return
    
    bill_info = extract_bill_info(text_to_parse, bank, bank_rules)
    
    if bill_info.get("amount"):
        conn.execute("UPDATE emails SET parsed_amount=? WHERE id=?", (bill_info["amount"], email_id))
        conn.commit()
        if verbose:
            print(f"  🔄 更新: {bank} | 金额: {bill_info["amount"]}")
    
    conn.close()

if __name__ == "__main__":
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    force = "--force" in sys.argv or "-f" in sys.argv
    sync_emails(verbose=verbose, force=force)

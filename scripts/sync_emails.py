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
    
    rules = bank_rules.get(bank_name, bank_rules.get("default", {}))
    patterns = rules.get("amount_patterns", bank_rules["default"]["amount_patterns"])
    group_idx = rules.get("amount_group", 1)
    
    info = {}
    
    # 提取金额
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
        r"最低还款额[\s\S]*?(\d+\.\d+)/RMB",
    ]:
        m = re.search(p, text)
        if m:
            info["min_payment"] = m.group(1).replace(",", "")
            break
    
    # 提取账单周期
    m = re.search(r"账单周期[\s\S]*?(\d{4}[/-]\d{2}[/-]\d{2}).*?(\d{4}[/-]\d{2}[/-]\d{2})", text)
    if m:
        info["billing_cycle"] = f"{m.group(1)}~{m.group(2)}"
    
    return info

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

def sync_emails(verbose=False):
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
        
        # 跳过已存在的
        if email_exists(email_id):
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
        
        # 存入数据库
        insert_email(
            email_id=email_id,
            subject=subject,
            sender=sender,
            received_at=received_at,
            bank=bank,
            body_text=body_text,
            has_attachment=has_attachment,
            attachment_text=attachment_text,
            parsed_amount=bill_info.get("amount"),
            parsed_due_date=bill_info.get("due_date"),
            parsed_cardholder=None  # 后续匹配
        )
        
        new_count += 1
        if verbose:
            amount = bill_info.get("amount", "未解析")
            print(f"  ✅ 新增: {bank} | {subject[:30]} | 金额: {amount}")
    
    if verbose:
        print(f"\n同步完成: 新增 {new_count}, 跳过 {skip_count}, 总计 {get_email_count()}")
    
    return new_count

if __name__ == "__main__":
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    sync_emails(verbose=verbose)

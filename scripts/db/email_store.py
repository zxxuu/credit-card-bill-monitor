#!/usr/bin/env python3
"""邮件存储操作"""
import sqlite3
from datetime import datetime
from . import get_db

def insert_email(email_id, subject, sender, received_at, bank=None, person=None,
                 body_text=None, has_attachment=0, attachment_text=None,
                 parsed_amount=None, parsed_due_date=None, parsed_cardholder=None):
    """插入或更新邮件"""
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO emails 
        (id, subject, sender, received_at, bank, person, body_text, 
         has_attachment, attachment_text, parsed_amount, parsed_due_date, 
         parsed_cardholder, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (email_id, subject, sender, received_at, bank, person, body_text,
          has_attachment, attachment_text, parsed_amount, parsed_due_date,
          parsed_cardholder, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_email(email_id):
    """获取单封邮件"""
    conn = get_db()
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_emails_by_bank(bank, person=None):
    """按银行获取邮件"""
    conn = get_db()
    if person:
        rows = conn.execute(
            "SELECT * FROM emails WHERE bank = ? AND person = ? ORDER BY received_at DESC",
            (bank, person)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM emails WHERE bank = ? ORDER BY received_at DESC",
            (bank,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_latest_email(bank, person, billing_cycle=None):
    """获取某人某银行的最新邮件"""
    conn = get_db()
    sql = "SELECT * FROM emails WHERE bank = ? AND person = ? ORDER BY received_at DESC LIMIT 1"
    row = conn.execute(sql, (bank, person)).fetchone()
    conn.close()
    return dict(row) if row else None

def email_exists(email_id):
    """检查邮件是否已存在"""
    conn = get_db()
    row = conn.execute("SELECT 1 FROM emails WHERE id = ?", (email_id,)).fetchone()
    conn.close()
    return row is not None

def get_all_emails():
    """获取所有邮件"""
    conn = get_db()
    rows = conn.execute("SELECT * FROM emails ORDER BY received_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_email_count():
    """获取邮件总数"""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    conn.close()
    return count

#!/usr/bin/env python3
"""数据库初始化和操作"""
import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.expanduser("~/credit-card-bill-monitor/data/emails.db")

def get_db():
    """获取数据库连接"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化数据库表"""
    conn = get_db()
    conn.executescript("""
        -- 卡片配置（静态）
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY,
            person TEXT NOT NULL,
            bank TEXT NOT NULL,
            card_name TEXT,
            bill_day INTEGER NOT NULL,
            due_rule TEXT NOT NULL,
            grace_days INTEGER,
            notes TEXT
        );

        -- 每月账单
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY,
            card_id INTEGER REFERENCES cards(id),
            billing_cycle TEXT NOT NULL,
            billing_date TEXT,
            due_date TEXT,
            amount TEXT,
            min_payment TEXT,
            cardholder TEXT,
            confirmed INTEGER DEFAULT 0,
            status TEXT DEFAULT '未处理',
            processed_at TEXT,
            updated_at TEXT,
            UNIQUE(card_id, billing_cycle)
        );

        -- 邮件
        CREATE TABLE IF NOT EXISTS emails (
            id TEXT PRIMARY KEY,
            subject TEXT,
            sender TEXT,
            received_at TEXT,
            bank TEXT,
            person TEXT,
            body_text TEXT,
            has_attachment INTEGER DEFAULT 0,
            attachment_text TEXT,
            parsed_amount TEXT,
            parsed_due_date TEXT,
            parsed_cardholder TEXT,
            billing_month TEXT,
            bill_day INTEGER,
            created_at TEXT
        );

        -- 邮件-账单关联
        CREATE TABLE IF NOT EXISTS email_bill_links (
            email_id TEXT REFERENCES emails(id),
            bill_id INTEGER REFERENCES bills(id),
            PRIMARY KEY (email_id, bill_id)
        );

        -- 索引
        CREATE INDEX IF NOT EXISTS idx_bills_cycle ON bills(billing_cycle);
        CREATE INDEX IF NOT EXISTS idx_bills_card ON bills(card_id);
        CREATE INDEX IF NOT EXISTS idx_emails_bank ON emails(bank);
        CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_at);
    """)
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("数据库初始化完成")

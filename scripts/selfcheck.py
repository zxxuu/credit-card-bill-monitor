#!/usr/bin/env python3
"""解析器自检 —— 用当前代码重放库里的邮件，对比已人工核对的期望值。

只读，不写数据库。改 bank_rules.json 或 sync_emails.py 之后跑一遍。

用法（容器内）：
    cd /opt/data/home/credit-card-bill-monitor
    ./venv/bin/python scripts/selfcheck.py

期望值来源：2026-09-22 逐封打开正文/PDF 人工核对得出，见 .workbuddy/memory/2026-09-22.md。

v3（2026-09-22 第二轮）：
- EXPECT 的字段改为**可选**：只写关心的 key（bank / amount / due），没写的就不校验。
  这样「知道还款日、但不知道准确金额」的邮件也能加进来当回归用例。
- 新增 S 段合成用例：不依赖库内数据，直接喂字符串，覆盖
  Cloudflare 账单通知（必须当噪音）、招商/广发主题（必须匹配到银行）、
  「个体工商户」不得命中工商（v2 的误命中回归）。
- 注意：库里 id<21415 的邮件已从 QQ 邮箱服务器消失（INBOX 只留 144 封），
  它们的 body_text 是唯一可用依据，所以本脚本只读库、不发起网络请求。
"""
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, PROJ)

from scripts.sync_emails import (match_bank, extract_bill_info, is_bill_email,
                                 load_bank_rules, identify_cardholder)

DB = os.path.join(PROJ, "data", "emails.db")

# 应该被当成账单、且解析出这些值。
# 只写想校验的 key：bank / amount / due。amount=None 表示「必须解析不出金额」。
EXPECT = {
    # --- 还款日曾经踩页脚陷阱的 ---
    "21346": {"bank": "光大", "amount": 98.95,  "due": "2026-08-10"},
    "21274": {"bank": "光大", "amount": 9.60,   "due": "2026-07-11"},
    "21267": {"bank": "光大", "amount": 0.0,    "due": "2026-07-07"},   # 另一种版式：标签在前值在后
    "21347": {"bank": "交通", "amount": 2041.84, "due": "2026-08-19"},
    "21216": {"bank": "交通", "amount": 6.00,   "due": "2026-07-03"},
    "21307": {"bank": "交通", "amount": 0.21,   "due": "2026-08-03"},   # 溢缴款负数
    "21427": {"due": "2026-09-19"},   # 库内旧值 2023-12-8 是旧代码吃到页脚「自XXXX年」
    "21420": {"due": "2026-09-10"},   # 库内旧值 2022-6-25 同上
    "21399": {"due": "2026-09-05"},   # 库内旧值 2026-08-15 是「账单日」，不是到期还款日
    # --- 工商两种格式 ---
    "21248": {"bank": "工商", "amount": 120.16, "due": "2026-07-10"},
    "21249": {"bank": "工商", "amount": 0.0,    "due": "2026-07-10"},   # 有溢缴款 -> 本期应还 0
    "21401": {"bank": "工商", "amount": 0.0,    "due": "2026-09-10"},   # 库内旧值 48.9 是错的
    "21530": {"bank": "工商", "amount": 0.0,    "due": "2026-10-10"},   # 库内旧值 3.0 是错的
    "21325": {"bank": "工商", "amount": 48.90,  "due": "2026-08-10"},
    "21350": {"bank": "工商", "amount": 7.90,   "due": "2026-08-15"},
    "21400": {"bank": "工商", "amount": 3.00,   "due": "2026-09-10"},
    "21430": {"bank": "工商", "amount": 75.90,  "due": "2026-09-15"},
    "21531": {"bank": "工商", "amount": 17.34,  "due": "2026-10-10"},
    # --- 原来缺规则、整封被丢掉的银行 ---
    "21265": {"bank": "兴业", "amount": 59.49,  "due": "2026-07-09"},
    "21261": {"bank": "平安", "amount": 245.68, "due": "2026-07-05"},
    "21292": {"bank": "平安", "amount": 75.28,  "due": "2026-07-20"},
    "21243": {"bank": "邮储", "amount": 108.09, "due": "2026-07-05"},
    "21207": {"bank": "浦发", "amount": 0.0,    "due": None},           # 本期无需还款
    "21296": {"bank": "浦发", "amount": 16.01,  "due": "2026-07-26"},
    # --- 招商/广发：新版账单没有任何「本期应还金额」标签，只能按位置取 ---
    # bill_day 是「卡片 ↔ 账单邮件」的关联键（email_store.get_email_by_bill_day），
    # 错了就会导致提醒里查不到这封账单，所以一并锁死。
    "21512": {"bank": "招商", "amount": 5.92,   "due": "2026-09-27", "bill_day": 9},
    "21545": {"bank": "广发", "amount": 20.09,  "due": "2026-10-08", "bill_day": 19},
    # --- 原有银行回归（确认没改坏）---
    "21238": {"bank": "农行", "amount": 41.50,  "due": "2026-07-08"},
    "21318": {"bank": "中信", "amount": 165.51, "due": "2026-07-31"},
}

# 必须被噪音过滤掉（发票 / 营销 / 通知）
NOISE_IDS = ["21201", "21302", "21374", "21392", "21393", "21436", "21448",
             "21206", "21218", "21263", "21294"]

# 中行靠 PDF，正文里没有数字，需另行验证
BOC_PDF_EXPECT = {"21510": {"amount": 406.62, "due": "2026-09-28"}}

# 合成用例：(主题, 发件人名, 发件地址, 期望是账单?, 期望匹配到的银行)
# 中文一律用 \\uXXXX 转义写，避免文件在传输/编码环节被改成问号。
SYNTHETIC = [
    ("\u60a8\u7684\u8d26\u5355\u5df2\u53ef\u4f9b\u67e5\u770b",
     "Cloudflare", "noreply@notify.cloudflare.com", False, None),      # Cloudflare 账单通知
    ("\u62db\u5546\u94f6\u884c\u4fe1\u7528\u5361\u7535\u5b50\u8d26\u5355",
     "\u62db\u5546\u94f6\u884c\u4fe1\u7528\u5361", "ccsvc@message.cmbchina.com", True,
     "\u62db\u5546"),
    ("\u5e7f\u53d1\u4fe1\u7528\u5361 2026\u5e7409\u6708\u7535\u5b50\u8d26\u5355",
     "\u5e7f\u53d1\u94f6\u884c", "creditcard@cgbchina.com.cn", True, "\u5e7f\u53d1"),
    ("\u4e2d\u56fd\u5de5\u5546\u94f6\u884c\u5ba2\u6237\u5bf9\u8d26\u5355",
     "\u4e2d\u56fd\u5de5\u5546\u94f6\u884c", "webmaster@icbc.com.cn", True, "\u5de5\u5546"),
    # v2 回归：这些主题里含「工商」二字，但都不是账单，必须被挡掉
    ("\u60a8\u7684\u7535\u5b50\u53d1\u7968\u5df2\u5f00\u5177",
     "\u4e2a\u4f53\u5de5\u5546\u6237", "noreply@example.com", False, None),
]

# 持卡人归属期望值。
# 交通白金账户是「莎莎」的，但那份账单的**交易明细**里出现了「我的」（另一位持卡人）
# 的名字（形如「跨行自助转账还款-<我的姓名>」），
# 旧逻辑按 cardholders.json 顺序取第一个出现在正文里的名字 → 全被误判成「我的」。
PERSON_EXPECT = {
    # 白金 = 莎莎
    "21216": "莎莎", "21217": "莎莎", "21306": "莎莎", "21307": "莎莎",
    "21379": "莎莎", "21380": "莎莎", "21480": "莎莎", "21481": "莎莎",
    # 个人卡 = 我的
    "21281": "我的", "21282": "我的", "21347": "我的", "21348": "我的",
    "21426": "我的", "21427": "我的",
    # 其它行：招商顿号后写「尊敬的 <我的姓名> 先生」（名字两侧带空格），
    # 广发写「尊敬的<莎莎姓名>女士」—— 有无空格是两家的版式差异
    "21512": "我的", "21545": "莎莎",
}


def main():
    rules = load_bank_rules()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM emails")}
    conn.close()

    ok = fail = 0

    print("=" * 78)
    print("A. 噪音过滤")
    for eid in NOISE_IDS:
        r = rows.get(eid)
        if not r:
            print("  SKIP  %-8s (库里没有)" % eid)
            continue
        name = (r["sender"] or "").split("<")[0].strip()
        addr = (r["sender"] or "").split("<")[-1].rstrip(">")
        noise = not is_bill_email(r["subject"] or "", name, addr)
        if noise:
            ok += 1
        else:
            fail += 1
            print("  FAIL  %-8s 未被过滤: %s" % (eid, (r["subject"] or "")[:44]))

    print("\n" + "=" * 78)
    print("B. 字段解析（bank / amount / due，只校验 EXPECT 里写了的字段）")
    print("%-8s %-6s %-10s %-14s %s" % ("id", "bank", "amount", "due", "结果"))
    for eid, exp in sorted(EXPECT.items()):
        r = rows.get(eid)
        if not r:
            print("%-8s 库里没有" % eid)
            fail += 1
            continue
        name = (r["sender"] or "").split("<")[0].strip()
        bank = match_bank(r["subject"] or "", name, rules)
        rr = rules.get(bank or "", {})
        text = (r["attachment_text"]
                if rr.get("source") == "attachment" and r["attachment_text"]
                else r["body_text"])
        info = extract_bill_info(text, bank, rules) if bank else {}

        bad = []
        if "bank" in exp and bank != exp["bank"]:
            bad.append("bank %s!=%s" % (bank, exp["bank"]))
        got_amt = info.get("amount")
        if "amount" in exp:
            if exp["amount"] is None:
                if got_amt:
                    bad.append("amount %s (应为空)" % got_amt)
            else:
                try:
                    if abs(float(got_amt) - exp["amount"]) > 0.005:
                        bad.append("amount %s!=%s" % (got_amt, exp["amount"]))
                except (TypeError, ValueError):
                    bad.append("amount %s!=%s" % (got_amt, exp["amount"]))
        if "due" in exp and (info.get("due_date") or None) != (exp["due"] or None):
            bad.append("due %s!=%s" % (info.get("due_date"), exp["due"]))
        if "bill_day" in exp and info.get("bill_day") != exp["bill_day"]:
            bad.append("bill_day %s!=%s" % (info.get("bill_day"), exp["bill_day"]))

        if bad:
            fail += 1
            print("%-8s %-6s %-10s %-14s FAIL  %s" % (
                eid, bank, got_amt, info.get("due_date"), "; ".join(bad)))
        else:
            ok += 1
            print("%-8s %-6s %-10s %-14s ok" % (eid, bank, got_amt, info.get("due_date")))

    print("\n" + "=" * 78)
    print("C. 全库扫描：真账单里还有几封解析不出金额")
    noremain = []
    for eid, r in sorted(rows.items()):
        name = (r["sender"] or "").split("<")[0].strip()
        addr = (r["sender"] or "").split("<")[-1].rstrip(">")
        if not is_bill_email(r["subject"] or "", name, addr):
            continue
        bank = match_bank(r["subject"] or "", name, rules)
        if not bank:
            noremain.append((eid, "未匹配银行", r["subject"]))
            continue
        rr = rules.get(bank, {})
        text = (r["attachment_text"]
                if rr.get("source") == "attachment" and r["attachment_text"]
                else r["body_text"])
        info = extract_bill_info(text, bank, rules)
        if not info.get("amount"):
            noremain.append((eid, bank, r["subject"]))
    for eid, b, s in noremain:
        print("  %-8s %-8s %s" % (eid, b, (s or "")[:50]))
    print("  剩余解析不出金额: %d 封" % len(noremain))
    if noremain:
        boc = [x for x in noremain if x[1] == "中行"]
        other = [x for x in noremain if x[1] != "中行"]
        if boc:
            print("  其中中行 %d 封（预期：靠 PDF；id<21415 的邮件已从服务器消失，附件不可再取）"
                  % len(boc))
        if other:
            print("  ⚠️ 非中行仍有 %d 封，需要查" % len(other))
            fail += len(other)

    print("\n" + "=" * 78)
    print("D. 中行 PDF 期望值（仅在 attachment_text 已解析时校验）")
    for eid, exp in BOC_PDF_EXPECT.items():
        r = rows.get(eid)
        if not r:
            continue
        if not r["attachment_text"]:
            print("  %-8s 附件尚未解析（下次 sync 后校验）" % eid)
            continue
        info = extract_bill_info(r["attachment_text"], "中行", rules)
        good = (abs(float(info.get("amount") or 0) - exp["amount"]) < 0.005
                and info.get("due_date") == exp["due"])
        if good:
            ok += 1
            print("  %-8s amount=%s due=%s ok" % (eid, info.get("amount"), info.get("due_date")))
        else:
            fail += 1
            print("  %-8s amount=%s(%s) due=%s(%s) FAIL" % (
                eid, info.get("amount"), exp["amount"], info.get("due_date"), exp["due"]))

    print("\n" + "=" * 78)
    print("S. 合成用例（不依赖库内数据）")
    for subj, nm, ad, want_bill, want_bank in SYNTHETIC:
        got_bill = is_bill_email(subj, nm, ad)
        got_bank = match_bank(subj, nm, rules) if got_bill else None
        bad = []
        if got_bill != want_bill:
            bad.append("是账单? %s!=%s" % (got_bill, want_bill))
        if want_bill and got_bank != want_bank:
            bad.append("bank %s!=%s" % (got_bank, want_bank))
        if bad:
            fail += 1
            print("  FAIL  %-34s %s" % (subj[:34], "; ".join(bad)))
        else:
            ok += 1
            print("  ok    %-34s bill=%-5s bank=%s" % (subj[:34], got_bill, got_bank))

    print("\n" + "=" * 78)
    print("P. 持卡人归属（必须认正文称呼，不能被交易摘要里的名字带跑）")
    for eid, want in PERSON_EXPECT.items():
        r = rows.get(eid)
        if not r:
            print("  SKIP  %-8s (库里没有)" % eid)
            continue
        rr = rules.get(r["bank"] or "", {})
        text = (r["attachment_text"]
                if rr.get("source") == "attachment" and r["attachment_text"]
                else r["body_text"])
        got = identify_cardholder(text or "")
        if got == want:
            ok += 1
            print("  ok    %-8s person=%s" % (eid, got))
        else:
            fail += 1
            print("  FAIL  %-8s person=%s!=%s  %s" % (eid, got, want, (r["subject"] or "")[:30]))

    print("\n" + "=" * 78)
    print("RESULT: ok=%d fail=%d -> %s" % (ok, fail, "PASS" if fail == 0 else "FAIL"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

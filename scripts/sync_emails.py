#!/usr/bin/env python3
"""邮件同步脚本 - 增量拉取并解析邮件到SQLite

v2 修复（2026-09-22）：
1. match_bank 关键词改精确匹配：ASCII 关键词要求词边界（修「个体工商户」命中工商、
   以及「bocomcc」被中行 "boc" 抢走）；冲突时取最长匹配关键词。
2. 增加噪音过滤 is_bill_email：主题必须像账单，且不得是发票/营销/通知类。
3. 所有跨段通配符加长度上限：修「到期还款日.*?」跨段吃到页脚「自XXXX年X月X日起」，
   导致光大还款日错成 2022-06-25、交通错成 2023-12-8。
4. parse_pdf 改为进程内优先用 PyMuPDF（原来写死 /tmp/pdfvenv 和 pdftotext，两者都不存在，
   导致中行 6 封 PDF 附件 100% 解析失败）。
5. 修 update_count 未初始化导致 --force 必崩。
6. reparse_email 回写全部字段（原来只回写金额）。
7. 工商支持两种账单格式（有欠款 / 有溢缴款），取「本期余额」而非「上期余额」。

v3 修复（2026-09-22 第二轮，实测驱动）：
8. 关键认知：**QQ 邮箱 INBOX 只保留最近 144 封**（实测最老 id=21415），更早的邮件在
   服务器上已不存在（`himalaya message export 21308` → `cannot find message`）。
   而 fetch_all_emails 原来写死 --page-size 80，所以「老邮件永远进不了重解析循环」，
   库里的 due=2023-12-8 / 2022-6-25 全是**陈旧值**，不是规则没写对。
   → 新增 `--reparse-all`：直接遍历库里所有行，用已存的 body_text/attachment_text
     重新解析并回写，完全不依赖 IMAP 窗口。这是老邮件唯一的纠错途径。
   → fetch_all_emails 改为逐页翻到底，不再只取第一页。
9. 噪音名单补 Cloudflare：主题「您的账单已可供查看」来自
   noreply@notify.cloudflare.com，是 Cloudflare 的账单通知而非银行账单，
   原来混进候选池、只是碰巧没匹配到银行。
10. DUE_PATTERNS_COMMON 去掉 `账单日 Statement Date` 兜底 —— 它把**账单日**当**到期还款日**
    写进 due_date，会让人提前 20 天还款。宁可为空，不可给错。
11. billing_cycle 支持没有「账单周期」标签的裸日期区间（招商新版账单就是
    `2026/08/10-2026/09/09 ¥ 48,000.00...`，否则账单月永远推不出来）。
12. has_attachment 改为反映「是否真的拿到附件文本」，不再无条件置 1。
"""
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

# 每页信封数；翻页直到取不满一页为止
PAGE_SIZE = 80
MAX_PAGES = 20

# 主题里必须出现其中之一，才当成账单邮件
BILL_SUBJECT_MARKERS = ["账单", "对账单", "Statement"]
# 主题里出现其中之一，一律不是账单
NOISE_SUBJECT_MARKERS = ["发票", "数电票", "优惠券", "诈骗", "章程", "信用管家",
                         "好礼", "广告", "退订", "活动邀请"]
# 这些发件人不是银行
# cloudflare：Cloudflare 自己的「您的账单已可供查看」通知，主题含「账单」但跟信用卡无关
NOISE_SENDERS = ["标普智元", "美团电票平台", "诺诺网", "51发票",
                 "biaopu", "meituan", "nuonuo", "51fapiao",
                 "cloudflare", "notify.cloudflare.com"]


def _norm_date(s):
    """把各种日期写法归一化成 YYYY-MM-DD。

    邮件里至少出现过这三种：2026/07/11、2026-7-10、2026-08-19。
    不归一化的话下游 datetime.strptime(x, "%Y-%m-%d") 会直接抛异常。
    """
    s = (s or "").strip()
    if not s:
        return s
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def _bmon_from_received(received_at):
    """从收信时间推账单月（最后兜底）。

    账单邮件都在账单日当天或次日发出，所以收信月份就是账单月。
    这比「到期还款日减一个月」准：平安 2026-07-03 收信 / 2026-07-20 到期，
    账单月是 07，减一个月会算成 06。
    """
    m = re.match(r"(\d{4})-(\d{2})", (received_at or "").strip())
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _kw_hit(kw, text):
    """关键词命中判断。ASCII 关键词要求词边界，避免子串误命中。"""
    kw_l = kw.lower()
    if kw_l.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(kw_l) + r"(?![a-z0-9])", text) is not None
    return kw_l in text


def is_bill_email(subject, sender_name, sender_addr=""):
    """判断这封邮件是不是「银行账单」。"""
    s = subject or ""
    blob = f"{sender_name} {sender_addr}"
    if any(k.lower() in blob.lower() for k in NOISE_SENDERS):
        return False
    if not any(k in s for k in BILL_SUBJECT_MARKERS):
        return False
    if any(k in s for k in NOISE_SUBJECT_MARKERS):
        return False
    return True


def load_bank_rules():
    """加载银行规则"""
    path = os.path.join(CONFIG_DIR, "bank_rules.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def match_bank(subject, sender, bank_rules):
    """匹配银行。多个关键词命中时取最长的那个，避免顺序依赖。"""
    text = f"{subject} {sender}".lower()
    hits = []
    for bank_name, rules in bank_rules.items():
        if bank_name == "default" or bank_name.startswith("_"):
            continue
        for kw in rules.get("keywords", []):
            if _kw_hit(kw, text):
                hits.append((len(kw), bank_name))
    if not hits:
        return None
    hits.sort(key=lambda x: -x[0])
    return hits[0][1]


def decode_email(email_id):
    """解码邮件正文"""
    cmd = f"{HIMALAYA_CMD} message read {email_id} --account qq"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip() and "\ufffd" not in r.stdout:
            return r.stdout
    except Exception:
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
        except Exception:
            return None
    except Exception:
        return None


def _load_fitz():
    """拿到可用的 PDF 库。优先 pymupdf（新版包名），退回 fitz。"""
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        pass
    try:
        import fitz
        return fitz
    except ImportError:
        return None


def parse_pdf(pdf_path):
    """解析PDF

    顺序：进程内 PyMuPDF → /tmp/pdfvenv 的外部解释器 → pdftotext。
    进程内优先（项目 venv 已装 PyMuPDF），不再依赖 /tmp/pdfvenv 这个已失效的路径。
    """
    fitz = _load_fitz()
    if fitz is not None:
        try:
            with fitz.open(pdf_path) as doc:
                txt = "".join(page.get_text() for page in doc)
            if txt and txt.strip():
                return txt
        except Exception:
            pass

    venv_py = "/tmp/pdfvenv/bin/python3"
    if os.path.exists(venv_py):
        try:
            script = ("import fitz; doc=fitz.open(%r); "
                      "print(''.join(p.get_text() for p in doc))" % pdf_path)
            r = subprocess.run([venv_py, "-c", script], capture_output=True,
                               text=True, timeout=60)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        except Exception:
            pass

    try:
        r = subprocess.run(f"pdftotext '{pdf_path}' -", shell=True,
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    except Exception:
        pass
    return ""


def download_pdf_attachment(email_id):
    """下载并解析PDF附件"""
    td = tempfile.mkdtemp(prefix="bill_")
    cmd = (f"{HIMALAYA_CMD} attachment download {email_id} "
           f"--account qq --downloads-dir {td}")
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=90)
        if r.returncode == 0:
            for f in sorted(os.listdir(td)):
                if f.lower().endswith(".pdf"):
                    pdf_path = os.path.join(td, f)
                    text = parse_pdf(pdf_path)
                    try:
                        os.remove(pdf_path)
                        os.rmdir(td)
                    except Exception:
                        pass
                    if text and text.strip():
                        return text
    except Exception:
        pass
    return None


# 通用还款日兜底模式。全部带长度上限，禁止跨段吃到页脚条款。
# ⚠️ 曾经这里有一条 `账单日\s*Statement\s*Date\s*(\d{4}[-/]...)`，
#    它把「账单日」当成「到期还款日」返回，会让提醒提前约 20 天。已删除：
#    宁可 due 为空（后续用 due_rule 推算），也不能给一个错的日期。
DUE_PATTERNS_COMMON = [
    r"到期还款日\s*Payment Due Date\s*(\d{4}年\d{1,2}月\d{1,2}日)",
    r"到期还款日\s*Payment Due Date\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
    r"Payment Due Date\s*(\d{4}年\d{1,2}月\d{1,2}日)",
    r"Payment Due Date\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
    r"到期还款日[\s\S]{0,60}?(\d{4}年\d{1,2}月\d{1,2}日)",
    r"到期还款日[\s\S]{0,60}?(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
    r"还款日[\s\S]{0,40}?(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
]

MIN_PAYMENT_PATTERNS = [
    r"本期最低还款额[\s\S]{0,80}?[¥￥]\s*([\d,]+\.?\d*)",
    r"本期最低应还金额[\s\S]{0,60}?[¥￥]\s*([\d,]+\.?\d*)",
    r"最低还款额[\s\S]{0,60}?([\d,]+\.?\d+)/RMB",
]

# 裸日期区间（无「账单周期」标签），招商新版账单就长这样
CYCLE_RANGE_RE = (r"(\d{4}[/-]\d{1,2}[/-]\d{1,2})\s*[-~至]\s*"
                  r"(\d{4}[/-]\d{1,2}[/-]\d{1,2})")


def _parse_icbc_amount(text):
    """工商银行账单金额。

    两种格式：
      A（本期有欠款）：合计人民币(本位币) <本期应还款额>/RMB <最低还款额>/RMB
      B（有溢缴款）  ：合计 <上期余额>/RMB<本期收入>/RMB<本期支出>/RMB<本期余额>/RMB
      —— 第 4 个值才是本期应还款（负数表示溢缴，取绝对值）。
    只认「合计」行，不要用「上期余额」，否则有溢缴款时金额会算错。

    ⚠️ 金额可能带千分符（`1,234.56/RMB`），所以数字类统一写 `-?[\\d,]+\\.\\d+`
    并在 float() 前 `.replace(",", "")`。写成 `-?\\d+\\.\\d+` 会把 `1,234.56`
    从 `2` 开始咬，得到 `234.56` —— 静默少一位数，不报错。
    """
    m = re.search(r"合计人民币\(本位币\)\s*(-?[\d,]+\.\d+)/RMB", text)
    if m:
        return str(abs(float(m.group(1).replace(",", ""))))
    m = re.search(r"合计\s*(-?[\d,]+\.\d+)/RMB\s*(-?[\d,]+\.\d+)/RMB"
                  r"\s*(-?[\d,]+\.\d+)/RMB\s*(-?[\d,]+\.\d+)/RMB", text)
    if m:
        return str(abs(float(m.group(4).replace(",", ""))))
    return None


def extract_bill_info(text, bank_name, bank_rules):
    """从文本提取账单信息"""
    if not text:
        return {}

    # 剥除 HTML 标签
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    rules = bank_rules.get(bank_name, bank_rules.get("default", {}))
    patterns = rules.get("amount_patterns", bank_rules["default"]["amount_patterns"])
    group_idx = rules.get("amount_group", 1)

    info = {}

    # 提取金额
    if bank_name == "工商":
        amt = _parse_icbc_amount(text)
        if amt:
            info["amount"] = amt

    # 通用金额解析
    if not info.get("amount"):
        for p in patterns:
            m = re.search(p, text)
            if m:
                try:
                    amount = m.group(group_idx).replace(",", "")
                    info["amount"] = str(abs(float(amount)))
                    break
                except Exception:
                    continue

    # 提取还款日：先用该银行专属模式，再走通用兜底
    due_patterns = list(rules.get("due_patterns", [])) + DUE_PATTERNS_COMMON
    for p in due_patterns:
        m = re.search(p, text)
        if m:
            info["due_date"] = _norm_date(
                m.group(1).replace("年", "-").replace("月", "-").replace("日", ""))
            break

    # 提取最低还款
    for p in MIN_PAYMENT_PATTERNS:
        m = re.search(p, text)
        if m:
            info["min_payment"] = m.group(1).replace(",", "")
            break

    # 提取账单周期。
    # ① 先找带「账单周期」标签的；② 没有标签就接受裸日期区间
    #    （招商新版账单是 `2026/08/10-2026/09/09 ¥ 48,000.00...`，没有标签）
    m = re.search(r"账单周期[：:]?\s*" + CYCLE_RANGE_RE, text)
    if not m:
        m = re.search(CYCLE_RANGE_RE, text)
    if m:
        info["billing_cycle"] = f"{m.group(1)}~{m.group(2)}"

    # 提取账单月份和账单日
    for p in [
        r"账单周期[\s\S]{0,60}?(\d{4})年(\d{1,2})月(\d{1,2})日[\s\S]{0,30}?(\d{4})年(\d{1,2})月(\d{1,2})日",
        r"账单日\s*Statement\s*Date\s*(\d{4})/(\d{2})/(\d{2})",
        r"账单日\s*Statement\s*Date\s*(\d{4})-(\d{2})-(\d{2})",
        r"账单日\s*Statement\s*Date\s*(\d{4})年(\d{1,2})月(\d{1,2})日",
        r"账单日期\s*Statement\s*Date\s*(\d{4})年(\d{1,2})月(\d{1,2})日",
        r"账单日\s+(\d{4})年(\d{1,2})月(\d{1,2})日",
        r"账单日\s+(\d{4})-(\d{2})-(\d{2})",
        # 广发写「账单日:2026/09/19」（带冒号 + 斜杠）。少了这条会掉进下面的
        # 纯数字兜底，把「2026」的口径咬成「20」→ bill_day 错成 20，
        # 而 state.json 里卡片的 bill_day 是 19，get_email_by_bill_day 就关联不上这封账单。
        r"账单日[：:]\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})",
        r"本期账单日\s*(\d{4})-(\d{2})-(\d{2})",
        # 中行 PDF 把表格拍平后，表头和值之间隔了约 78 个字符（中英文两行表头），
        # 窗口给 60 会匹配不到 → 账单月退化成「还款日减 1」。放宽到 150/30。
        r"Statement\s*Closing\s*Date[\s\S]{0,150}?(\d{4})-(\d{2})-(\d{2})"
        r"[\s\S]{0,30}?(\d{4})-(\d{2})-(\d{2})",
    ]:
        m = re.search(p, text)
        if m:
            if len(m.groups()) >= 6:
                # 两个日期：取第二个（账单日）
                info["bill_day"] = int(m.group(6))
                if "billing_month" not in info:
                    info["billing_month"] = f"{m.group(4)}-{int(m.group(5)):02d}"
            else:
                info["bill_day"] = int(m.group(3))
                if "billing_month" not in info:
                    info["billing_month"] = f"{m.group(1)}-{int(m.group(2)):02d}"
            break

    # 再尝试提取纯数字账单日（支持中文格式）
    # ⚠️ 每个 (\d{1,2}) 都加 (?!\d)：否则「账单日:2026/09/19」会被咬成 20。
    if "bill_day" not in info:
        for p in [
            r"账单日[：:]\s*(\d{1,2})(?!\d)",
            r"账单日为(\d{1,2})日",
            r"(\d{1,2})日为您的账单日",
            r"每月(\d{1,2})日出账",
            r"账单日(\d{1,2})(?!\d)",
        ]:
            m = re.search(p, text)
            if m:
                info["bill_day"] = int(m.group(1))
                break

    # 如果还没提取到账单月，从账单周期推断（账单周期的结束日就是账单日）
    if "billing_month" not in info and "billing_cycle" in info:
        try:
            cycle_end = info["billing_cycle"].split("~")[1]
            parts = cycle_end.replace("/", "-").split("-")
            if len(parts) == 3:
                info["billing_month"] = f"{parts[0]}-{int(parts[1]):02d}"
                info["bill_day"] = int(parts[2])
        except Exception:
            pass

    for p in [
        r"(\d{4})年(\d{1,2})月账单",
        r"(\d{4})年(\d{1,2})月[日].{0,10}?账单",
        r"账单周期\s*(\d{4})[/-](\d{2})[/-](\d{2})",
    ]:
        m = re.search(p, text)
        if m:
            year = int(m.group(1))
            month = int(m.group(2))
            if 2020 <= year <= 2030 and 1 <= month <= 12:
                info["billing_month"] = f"{year}-{month:02d}"
                break

    # 注意：这里**不再**用「到期还款日减一个月」兜底推账单月。
    # 那个启发式经常算错（平安 2026-07-03 收信 / 2026-07-20 到期 → 会算成 2026-06），
    # 已改为由调用方用**收信月份**兜底，见 _bmon_from_received。

    return info


# 账单邮件的收件人称呼，形如「尊敬的<姓名>先生 / 女士」
GREETING_RE = re.compile(r"尊[敬称]的\s*([\u4e00-\u9fa5]{2,4})\s*(?:先生|女士|小姐)")


def identify_cardholder(text):
    """从邮件内容识别持卡人。

    ① 优先看正文开头的称呼「尊敬的X先生/女士」—— 这才是收件人，最可靠。
    ② 匹配不到才退化到「正文任何位置出现过名字」的旧逻辑。

    为什么必须加 ①：交通白金账户是「莎莎」的，但那份账单的**交易明细**里出现了
    「我的」（另一位持卡人）的名字，形如「跨行自助转账还款-<姓名>」，
    而 cardholders.json 里「我的」那个人的名字排在「莎莎」前面，
    旧逻辑按顺序返回第一个命中的名字 → 整封被算成「我的」。
    后果是 state.json 里「莎莎/交通/bill_day=8」这张卡用
    get_email_by_bill_day(bank, person, bill_day, billing_month) 永远查不到自己的账单，
    提醒里也就没有金额。实测 6 封（21217/21306/21307/21379/21480/21481）被误判。

    另注：cardholders.json 里那种带星号的脱敏写法（如「张**」）是死条目
    （`name in text` 永远命中不了字面星号），不要依赖它们的顺序。
    """
    if not text:
        return None

    config_path = os.path.expanduser("~/credit-card-bill-monitor/config/cardholders.json")
    if not os.path.exists(config_path):
        return None

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    cardholders = config.get("cardholders", {})

    # ① 先认称呼
    m = GREETING_RE.search(text)
    if m and m.group(1) in cardholders:
        return cardholders[m.group(1)]

    # ② 退化到旧逻辑
    for name, person in cardholders.items():
        if name in text:
            return person
    return None


def fetch_all_emails():
    """获取所有邮件信封（逐页翻到底）。

    v3：原来固定 `--page-size 80` 只取第一页。QQ 邮箱 INBOX 实测有 144 封，
    第二页还有 64 封 —— 只取第一页会让「新增邮件」漏掉一半。
    这里逐页翻，直到某页取不满 PAGE_SIZE 或没有新 id 为止。
    """
    all_envs = []
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        cmd = (f"{HIMALAYA_CMD} envelope list --account qq "
               f"--page-size {PAGE_SIZE} --page {page} -o json")
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        except Exception:
            break
        if r.returncode != 0:
            break
        try:
            data = json.loads(r.stdout)
        except Exception:
            break
        if not data:
            break
        fresh = [e for e in data if e.get("id") not in seen]
        if not fresh:
            break
        for e in fresh:
            seen.add(e.get("id"))
        all_envs.extend(fresh)
        if len(data) < PAGE_SIZE:
            break
    return all_envs


def parse_email_record(email, bank, bank_rules):
    """拉正文/附件 → 解析 → 返回入库所需字段。"""
    email_id = email.get("id")

    body_text = decode_email(email_id)

    attachment_text = None
    rules = bank_rules.get(bank, {})
    if rules.get("source") == "attachment":
        attachment_text = download_pdf_attachment(email_id)

    text_to_parse = attachment_text if attachment_text else body_text
    bill_info = extract_bill_info(text_to_parse, bank, bank_rules)
    person = identify_cardholder(text_to_parse or body_text)
    if not bill_info.get("billing_month"):
        bill_info["billing_month"] = _bmon_from_received(email.get("date", ""))

    return {
        "email_id": email_id,
        "body_text": body_text,
        # 反映「是否真的拿到了附件文本」，不再对 attachment 型银行无条件置 1
        "has_attachment": 1 if attachment_text else 0,
        "attachment_text": attachment_text,
        "bill_info": bill_info,
        "person": person,
    }


def sync_emails(verbose=False, force=False):
    """同步邮件"""
    init_db()
    bank_rules = load_bank_rules()

    emails = fetch_all_emails()
    if verbose:
        print(f"获取到 {len(emails)} 封邮件")

    new_count = 0
    skip_count = 0
    update_count = 0
    noise_count = 0
    nomatch_count = 0

    for email in emails:
        email_id = email.get("id")

        subject = email.get("subject", "")
        sender_name = email.get("from", {}).get("name", "")
        sender_addr = email.get("from", {}).get("addr", "")
        sender = f"{sender_name} <{sender_addr}>"
        received_at = email.get("date", "")

        # 噪音过滤：发票 / 营销 / 通知 一律不收（替代原来只看「广告/好礼」的写法）
        if not is_bill_email(subject, sender_name, sender_addr):
            noise_count += 1
            if verbose:
                print(f"  跳过非账单: {subject[:40]}")
            continue

        bank = match_bank(subject, sender_name, bank_rules)
        if not bank:
            nomatch_count += 1
            if verbose:
                print(f"  未匹配银行: {subject[:40]}")
            continue

        # 跳过已存在的（除非force模式）
        if email_exists(email_id):
            if force:
                if reparse_email(email_id, bank_rules, verbose):
                    update_count += 1
            else:
                skip_count += 1
            continue

        rec = parse_email_record(email, bank, bank_rules)
        bill_info = rec["bill_info"]

        insert_email(
            email_id=email_id,
            subject=subject,
            sender=sender,
            received_at=received_at,
            bank=bank,
            person=rec["person"],
            body_text=rec["body_text"],
            has_attachment=rec["has_attachment"],
            attachment_text=rec["attachment_text"],
            parsed_amount=bill_info.get("amount"),
            parsed_due_date=bill_info.get("due_date"),
            parsed_cardholder=rec["person"],
            billing_month=bill_info.get("billing_month"),
            bill_day=bill_info.get("bill_day"),
        )

        new_count += 1
        if verbose:
            amount = bill_info.get("amount", "未解析")
            print(f"  新增: {bank} | {subject[:30]} | 金额: {amount}")

    if verbose:
        print(f"\n同步完成: 新增 {new_count}, 更新 {update_count}, 跳过 {skip_count}, "
              f"噪音 {noise_count}, 未匹配 {nomatch_count}, 总计 {get_email_count()}")

    return new_count


def reparse_email(email_id, bank_rules, verbose=False):
    """重新解析已有邮件并回写全部字段（原来只回写金额）"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT bank, subject, body_text, attachment_text, received_at "
            "FROM emails WHERE id=?",
            (email_id,)).fetchone()
        if not row:
            return False

        bank, subject, body_text, attachment_text = row[0], row[1], row[2], row[3]
        received_at = row[4]
        if not bank:
            return False

        # 用最新规则重新判一次银行（规则改了以后银行归属可能变）
        sender = conn.execute("SELECT sender FROM emails WHERE id=?", (email_id,)).fetchone()[0] or ""
        sender_name = sender.split("<")[0].strip()
        new_bank = match_bank(subject or "", sender_name, bank_rules) or bank
        if not is_bill_email(subject or "", sender_name, sender):
            return False

        rules = bank_rules.get(new_bank, bank_rules.get("default", {}))
        # 附件型银行（如中行）如果上次没取到附件文本，这里补下载一次。
        # 注意：仅当邮件还在服务器上才有意义 —— QQ 邮箱 INBOX 只留最近 ~144 封，
        # 更早的邮件会返回 `cannot find message`，此时下不到附件属正常，不算错误。
        if rules.get("source") == "attachment" and not attachment_text:
            attachment_text = download_pdf_attachment(email_id)
            if attachment_text:
                conn.execute("UPDATE emails SET attachment_text=?, has_attachment=1 WHERE id=?",
                             (attachment_text, email_id))

        text_to_parse = (attachment_text
                         if rules.get("source") == "attachment" and attachment_text
                         else body_text)
        if not text_to_parse:
            return False

        bill_info = extract_bill_info(text_to_parse, new_bank, bank_rules)
        person = identify_cardholder(text_to_parse or body_text or "")
        if not bill_info.get("billing_month"):
            bill_info["billing_month"] = _bmon_from_received(received_at)

        conn.execute(
            "UPDATE emails SET bank=?, person=?, parsed_amount=?, parsed_due_date=?, "
            "parsed_cardholder=?, billing_month=?, bill_day=? WHERE id=?",
            (new_bank, person, bill_info.get("amount"), bill_info.get("due_date"),
             person, bill_info.get("billing_month"), bill_info.get("bill_day"),
             email_id))
        conn.commit()

        if verbose:
            print(f"  更新: {new_bank} | amt={bill_info.get('amount')} "
                  f"due={bill_info.get('due_date')}")
        return True
    except Exception as e:
        if verbose:
            print(f"  重解析失败 {email_id}: {e}")
        return False
    finally:
        conn.close()


def reparse_all(verbose=False):
    """用库里已存的正文/附件文本重解析**全部**邮件，不依赖 IMAP 信封窗口。

    为什么必须有这个入口：`--force` 只能重解析 fetch_all_emails() 拿到的邮件，
    而 QQ 邮箱 INBOX 实测只保留最近 144 封（最老 id=21415），更早的账单邮件
    在服务器上已经不存在了（`himalaya message export 21308` → `cannot find message`）。
    这些老邮件的 body_text / attachment_text 还留在库里，是唯一可用的纠错依据。
    """
    init_db()
    bank_rules = load_bank_rules()

    conn = get_db()
    ids = [r[0] for r in conn.execute("SELECT id FROM emails ORDER BY id").fetchall()]
    conn.close()

    ok = skipped = 0
    for eid in ids:
        if reparse_email(eid, bank_rules, verbose):
            ok += 1
        else:
            skipped += 1

    print(f"全量重解析: 成功 {ok}, 跳过 {skipped}, 库里共 {len(ids)} 封")
    return ok


def report_gaps():
    """打印库里仍解析不出金额的账单（按银行归类）。

    库里有历史遗留的噪音邮件（发票/营销，见 selfcheck 的 NOISE_IDS），
    它们本来就不该有金额，必须先排除，否则会虚报「解析不出来」。
    """
    bank_rules = load_bank_rules()
    conn = get_db()
    rows = conn.execute(
        "SELECT id, bank, subject, parsed_amount, sender FROM emails ORDER BY id"
    ).fetchall()
    conn.close()

    by_bank = {}
    noise = 0
    for r in rows:
        eid, bank, subject, amt, sender = r[0], r[1], r[2], r[3], r[4]
        name = (sender or "").split("<")[0].strip()
        addr = (sender or "").split("<")[-1].rstrip(">")
        if not is_bill_email(subject or "", name, addr):
            noise += 1
            continue
        if amt not in (None, ""):
            continue
        by_bank.setdefault(bank or "未匹配", []).append(eid)

    print("\n仍解析不出金额的账单：")
    if not by_bank:
        print("  （无）")
    for bank, ids in sorted(by_bank.items()):
        print(f"  {bank}: {len(ids)} 封  id={ids}")
    print(f"（另有 {noise} 封历史遗留噪音邮件，不参与统计）")
    return by_bank


if __name__ == "__main__":
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    force = "--force" in sys.argv or "-f" in sys.argv
    if "--reparse-all" in sys.argv:
        reparse_all(verbose=verbose)
        report_gaps()
    else:
        sync_emails(verbose=verbose, force=force)

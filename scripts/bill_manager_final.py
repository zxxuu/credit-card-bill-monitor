#!/usr/bin/env python3
"""信用卡账单管理系统 v3 - 精确解析 + PDF支持 + 区分0/失败"""
import json, os, subprocess, re, zipfile, tempfile, xml.etree.ElementTree as ET
from datetime import datetime, timedelta

HIMALAYA_CMD = os.path.expanduser("~/.local/bin/himalaya")
EXCEL_PATH = os.path.expanduser("~/credit-card-bill-monitor/credit_cards.xlsx")
STATE_FILE = os.path.expanduser("~/credit-card-bill-monitor/state.json")
CARDHOLDERS_FILE = os.path.expanduser("~/credit-card-bill-monitor/cardholders.json")

def load_cardholders():
    """加载持卡人配置"""
    if os.path.exists(CARDHOLDERS_FILE):
        with open(CARDHOLDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"cardholders": {}, "person_map": {}}

CARDHOLDERS = load_cardholders()

BANK_KEYWORDS = {
    "\u5149\u5927":["\u5149\u5927"],"\u5174\u4e1a":["\u5174\u4e1a"],"\u5e73\u5b89":["\u5e73\u5b89"],
    "\u5de5\u5546":["\u5de5\u5546"],"\u90ae\u50a8":["\u90ae\u50a8"],"\u90ae\u50a8\u65e0\u754c\u767d":["\u90ae\u50a8"],
    "\u4ea4\u901a":["\u4ea4\u901a"],"\u6d66\u53d1":["\u6d66\u53d1"],"\u62db\u5546":["\u62db\u5546"],
    "\u4e2d\u4fe1":["\u4e2d\u4fe1"],"\u6c11\u751f":["\u6c11\u751f"],"\u534e\u590f":["\u534e\u590f"],
    "\u5e7f\u53d1":["\u5e7f\u53d1"],"\u4e2d\u884c":["\u4e2d\u884c","\u4e2d\u56fd\u94f6\u884c"],
    "\u519c\u884c":["\u519c\u884c","\u519c\u4e1a\u94f6\u884c"],
    "\u519c\u884c\u6f02\u4eae\u5988\u5988":["\u519c\u884c","\u519c\u4e1a\u94f6\u884c"],
    "\u519c\u884cETC":["\u519c\u884c","\u519c\u4e1a\u94f6\u884c"],
    "\u519c\u884c\u60a0\u7136\u767d":["\u519c\u884c","\u519c\u4e1a\u94f6\u884c"],
    "\u5f20\u5bb6\u53e3":["\u5f20\u5bb6\u53e3"],
}

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f: return json.load(f)
    return {"cards": [], "last_update": None}

def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f: json.dump(s, f, ensure_ascii=False, indent=2)

def excel_date_to_str(d):
    try: return (datetime(1899, 12, 30) + timedelta(days=int(d))).strftime("%Y-%m-%d")
    except: return str(d)

def parse_excel(path):
    cards = []
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path, "r") as z:
        ss = []
        if "xl/sharedStrings.xml" in z.namelist():
            with z.open("xl/sharedStrings.xml") as f:
                for si in ET.parse(f).getroot().findall(f".//{NS}si"):
                    ss.append("".join(t.text or "" for t in si.findall(f".//{NS}t")))
        with z.open("xl/worksheets/sheet1.xml") as f:
            headers = {}
            for row in ET.parse(f).getroot().findall(f".//{NS}row"):
                rn = row.get("r"); rd = {}
                for c in row.findall(f"{NS}c"):
                    col = "".join(filter(str.isalpha, c.get("r")))
                    v = c.find(f"{NS}v"); val = v.text if v is not None else ""
                    if c.get("t") == "s": val = ss[int(val)]
                    if rn == "1": headers[col] = val
                    else: rd[headers.get(col, col)] = val
                if rn != "1" and rd: cards.append(rd)
    return cards

def get_all_emails():
    cmd = f"{HIMALAYA_CMD} envelope list --account qq --page-size 80 -o json"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if r.returncode == 0: return json.loads(r.stdout)
    except: pass
    return []

def match_email(email, person, bank):
    # 不按收件人过滤 - 所有账单都转发到同一邮箱
    # 按银行关键词 + 邮件内容中的持卡人姓名匹配
    kw = BANK_KEYWORDS.get(bank, [bank])
    subj = email.get("subject", "")
    sender = email.get("from", {}).get("name", "") + " " + email.get("from", {}).get("addr", "")
    text = f"{subj} {sender}".lower()
    if not any(k.lower() in text for k in kw): return False
    if "AD)" in subj or "\u597d\u793c" in subj: return False
    # 如果同银行有多人，需要进一步检查
    # 这里先返回True，在main函数中通过邮件内容匹配持卡人
    return True

def decode_email(email_id):
    cmd = f"{HIMALAYA_CMD} message read {email_id} --account qq"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip() and "\ufffd" not in r.stdout:
            return r.stdout
    except: pass
    return decode_gbk(email_id)

def decode_gbk(email_id):
    import quopri, base64
    cmd = f"{HIMALAYA_CMD} message export {email_id} --full --account qq"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if r.returncode != 0: return None
        raw = r.stdout
        enc = "base64" if "Content-Transfer-Encoding: base64" in raw else "quoted-printable"
        idx = raw.find("Content-Type: text/html")
        if idx < 0: idx = raw.find("Content-Type: text/plain")
        if idx < 0: return None
        c = raw[idx:]; sep = c.find("\n\n")
        if sep < 0: return None
        body = re.sub(r"------=_Part_.*", "", c[sep+2:])
        try:
            if enc == "base64": txt = base64.b64decode(re.sub(r"\s", "", body)).decode("gbk", errors="ignore")
            else: txt = quopri.decodestring(body.encode()).decode("gbk", errors="ignore")
            return re.sub(r"<[^>]+>", "\n", txt)
        except: return None
    except: return None

def extract_bill(text, bank):
    info = {"bank": bank, "amount_confirmed": False}
    # 从配置文件读取持卡人姓名
    for person_id, cfg in CARDHOLDERS.get("cardholders", {}).items():
        for name in cfg.get("names", []):
            if name in text:
                info["cardholder"] = name
                break
    # \u8fd8\u6b3e\u65e5
    for p in [r"\u5230\u671f\u8fd8\u6b3e\u65e5.*?(\d{4}\u5e74\d{1,2}\u6708\d{1,2}\u65e5)",
              r"\u5230\u671f\u8fd8\u6b3e\u65e5[\s\S]*?(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
              r"\u8fd8\u6b3e\u65e5[\s\S]*?(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
              r"RMB.*?\d{4}/\d{2}/\d{2}\s+(\d{4}/\d{2}/\d{2})"]:
        m = re.search(p, text)
        if m: info["due_date"] = m.group(1).replace("\u5e74","-").replace("\u6708","-").replace("\u65e5",""); break
    # \u5e94\u8fd8\u91d1\u989d - \u591a\u79cd\u94f6\u884c\u683c\u5f0f
    for p in [
        r"\u672c\u671f\u5e94\u8fd8\u6b3e\u603b\u989d.*?[\uffe5\xa5]\s*([\d,]+\.?\d*)",
        r"\u672c\u671f\u5e94\u8fd8\u91d1\u989d\s*[\uffe5\xa5]\s*([\d,]+\.?\d*)",
        r"\u672c\u671f\u5e94\u8fd8\u6b3e\s+[\uffe5\xa5]\s*([\d,]+\.\d+)",
        r"\u5e94\u8fd8\u6b3e\u989d[\s\S]*?(\d+\.\d+)/RMB",
        r"\u672c\u671f\u5e94\u8fd8\u6b3e\u989d.*?\u4eba\u6c11\u5e01\(CNY\)\s+(-?[\d,.]+)",
        r"New Balance\s+\u4eba\u6c11\u5e01\(CNY\)\s+(-?[\d,.]+)",
        r"\u672c\u671f\u5e94\u8fd8\u6b3e\u603b\u989d\s+CNY\s+([\d,.]+)",
        r"\u5e94\u8fd8\u6b3e\u91d1\u989d[\uffe5\xa5]?\s*([\d,]+\.?\d*)",
        r"\u672c\u671f\u8d26\u5355\u91d1\u989d[\uffe5\xa5]?\s*([\d,]+\.?\d*)",
        r"New Balance\s*(?:CNY|RMB)\s*([\d,]+\.?\d*)",
    ]:
        m = re.search(p, text)
        if m:
            a = m.group(1).replace(",", "")
            try:
                v = float(a); info["amount"] = str(abs(v)); info["amount_confirmed"] = True; break
            except: pass
    # \u5149\u5927\u7279\u6b8a\u683c\u5f0f: \u4e09\u4e2a\u91d1\u989d\u540c\u884c (\u4fe1\u7528\u989d\u5ea6, \u8d26\u5355\u91d1\u989d, \u6700\u4f4e\u8fd8\u6b3e)
    if not info.get("amount_confirmed"):
        m = re.search(r"RMB Statement Balance[\s\S]*?[\uffe5\xa5]([\d,]+\.?\d*)\s+[\uffe5\xa5]([\d,]+\.?\d*)\s+[\uffe5\xa5]([\d,]+\.?\d*)", text)
        if m:
            try:
                v = float(m.group(2).replace(",", ""))
                info["amount"] = str(abs(v)); info["amount_confirmed"] = True
            except: pass
    # \u6700\u4f4e\u8fd8\u6b3e
    for p in [r"\u672c\u671f\u6700\u4f4e\u8fd8\u6b3e\u989d.*?[\uffe5\xa5]\s*([\d,]+\.?\d*)",
              r"\u672c\u671f\u6700\u4f4e\u5e94\u8fd8\u91d1\u989d\s*[\uffe5\xa5]\s*([\d,]+\.?\d*)",
              r"\u6700\u4f4e\u8fd8\u6b3e\u989d[\s\S]*?(\d+\.\d+)/RMB",
              r"\u6700\u5c0f\u8fd8\u6b3e[\s\S]*?\u4eba\u6c11\u5e01\(CNY\)\s+([\d,.]+)",
              r"Min.*?Payment\s+CNY\s+([\d,.]+)",
              r"\u6700\u4f4e\u5e94\u8fd8\u6b3e\s+[\uffe5\xa5]\s*([\d,]+\.\d+)"]:
        m = re.search(p, text)
        if m: info["min_payment"] = m.group(1).replace(",", ""); break
    # \u8d26\u5355\u5468\u671f
    m = re.search(r"\u8d26\u5355\u5468\u671f[\s\S]*?(\d{4}[/-]\d{2}[/-]\d{2}).*?(\d{4}[/-]\d{2}[/-]\d{2})", text)
    if m: info["billing_cycle"] = f"{m.group(1)}~{m.group(2)}"
    return info

def download_pdf_attachment(email_id):
    td = tempfile.mkdtemp(prefix="bill_")
    cmd = f"{HIMALAYA_CMD} attachment download {email_id} --account qq --downloads-dir {td}"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            for f in os.listdir(td):
                if f.lower().endswith(".pdf"):
                    pdf_path = os.path.join(td, f)
                    info = parse_boc_pdf(pdf_path)
                    try: os.remove(pdf_path); os.rmdir(td)
                    except: pass
                    return info
    except: pass
    return None

def parse_boc_pdf(pdf_path):
    try:
        venv_py = "/tmp/pdfvenv/bin/python3"
        if os.path.exists(venv_py):
            script = f"import fitz; doc=fitz.open('{pdf_path}'); print(''.join(p.get_text() for p in doc))"
            r = subprocess.run([venv_py, "-c", script], capture_output=True, text=True, timeout=30)
            txt = r.stdout if r.returncode == 0 else ""
        else:
            r = subprocess.run(f"pdftotext '{pdf_path}' -", shell=True, capture_output=True, text=True, timeout=30)
            txt = r.stdout if r.returncode == 0 else ""
        if not txt: return None
        info = {}
        m = re.search(r"\u672c\u671f\u4eba\u6c11\u5e01\u6b20\u6b3e\u603b\u8ba1[\s\S]*?(\d+\.\d+)", txt)
        if m: info["amount"] = m.group(1)
        m = re.search(r"\u5230\u671f\u8fd8\u6b3e\u65e5[\s\S]*?(\d{4}-\d{2}-\d{2})", txt)
        if m: info["due_date"] = m.group(1)
        # 从配置文件读取持卡人姓名
        for person_id, cfg in CARDHOLDERS.get("cardholders", {}).items():
            for name in cfg.get("names", []):
                if name in txt:
                    info["cardholder"] = name
                    break
        return info if info else None
    except Exception as e:
        print(f"    PDF\u89e3\u6790\u5931\u8d25: {e}")
        return None

def main():
    print("\U0001f4b3 \u4fe1\u7528\u5361\u8d26\u5355\u7ba1\u7406\u7cfb\u7edf v3")
    print("=" * 60)
    cards = parse_excel(EXCEL_PATH)
    emails = get_all_emails()
    print(f"\n\U0001f4ca Excel: {len(cards)}\u5361  \U0001f4e7 \u90ae\u4ef6: {len(emails)}\u5c01\n")
    state = load_state()
    used_emails = set()  # 每封邮件只能匹配一张卡
    for card in cards:
        person = card.get("\u5217", ""); bank = card.get("\u94f6\u884c", "")
        bd = card.get("\u8d26\u5355\u65e5", ""); pd = excel_date_to_str(card.get("\u8fd8\u6b3e\u65e5", ""))
        matched = [e for e in emails if match_email(e, person, bank) and e["id"] not in used_emails]
        if not matched:
            # \u6ca1\u6709\u90ae\u4ef6\u4e5f\u8bb0\u5f55\uff0c\u7528Excel\u6570\u636e
            print(f"  \u26a0\ufe0f {person:4}|{bank:10}|\u672a\u627e\u5230\u90ae\u4ef6")
            cs = {"person":person,"bank":bank,"bill_day":bd,"pay_date":pd,
                  "amount":"","amount_confirmed":False,"min_payment":"",
                  "email_due_date":"","cardholder":"",
                  "billing_cycle":"","email_to":"",
                  "status":"\u672a\u5904\u7406","last_update":datetime.now().isoformat()}
            ex = next((c for c in state["cards"] if c["person"]==person and c["bank"]==bank and c.get("bill_day")==bd), None)
            if ex:
                if ex.get("status")=="\u5df2\u5904\u7406": cs["status"]="\u5df2\u5904\u7406"; cs["processed_at"]=ex.get("processed_at")
                ex.update(cs)
            else:
                state["cards"].append(cs)
            continue
        # 同银行多人时，通过邮件内容匹配持卡人（从配置文件读取）
        person_names = CARDHOLDERS.get("person_map", {})
        names = person_names.get(person, [])
        lat = None
        for e in matched:
            if e["id"] in used_emails:
                continue
            if len(matched) == 1:
                lat = e
                break
            # 先检查邮件正文
            text = decode_email(e["id"])
            if text and any(n in text for n in names):
                lat = e
                break
            # 邮件正文没有姓名，检查PDF附件（如中行）
            if e.get("has_attachment"):
                pi = download_pdf_attachment(e["id"])
                if pi and pi.get("cardholder"):
                    if any(n in pi["cardholder"] for n in names):
                        lat = e
                        break
        if not lat and len(matched) == 1:
            lat = matched[0]
        if not lat:
            print(f"  \u26a0\ufe0f {person:4}|{bank:10}|\u672a\u627e\u5230\u5339\u914d\u90ae\u4ef6")
            cs = {"person":person,"bank":bank,"bill_day":bd,"pay_date":pd,
                  "amount":"","amount_confirmed":False,"min_payment":"",
                  "email_due_date":"","cardholder":"",
                  "billing_cycle":"","email_to":"",
                  "status":"\u672a\u5904\u7406","last_update":datetime.now().isoformat()}
            ex = next((c for c in state["cards"] if c["person"]==person and c["bank"]==bank and c.get("bill_day")==bd), None)
            if ex:
                if ex.get("status")=="\u5df2\u5904\u7406": cs["status"]="\u5df2\u5904\u7406"; cs["processed_at"]=ex.get("processed_at")
                ex.update(cs)
            else:
                state["cards"].append(cs)
            continue
        eid = lat["id"]; to = lat["to"]["addr"]
        used_emails.add(eid)
        used_emails.add(eid)
        text = decode_email(eid)
        if not text: print(f"  \u274c {person:4}|{bank:10}|\u89e3\u7801\u5931\u8d25"); continue
        info = extract_bill(text, bank)
        # \u4e2d\u884cPDF
        if not info.get("amount_confirmed") and bank == "\u4e2d\u884c":
            pi = download_pdf_attachment(eid)
            if pi and pi.get("amount"):
                info["amount"] = pi["amount"]; info["amount_confirmed"] = True
                if pi.get("due_date"): info["due_date"] = pi["due_date"]
                if pi.get("cardholder"): info["cardholder"] = pi["cardholder"]
                print(f"    [PDF\u89e3\u6790OK]")
        a = info.get("amount", ""); ok = info.get("amount_confirmed", False)
        if ok: ad = f"\uffe5{a}" if float(a) > 0 else "\u2705\uffe50(\u65e0\u6b20\u6b3e)"
        else: ad = "\u2753\u89e3\u6790\u5931\u8d25"
        cyc = info.get("billing_cycle", "")
        print(f"  {'\u2705' if ok else '\u2753'} {person:4}|{bank:10}|{ad:>20}|{pd}|\u2192{to}")
        if cyc: print(f"    \u5468\u671f:{cyc}")
        cs = {"person":person,"bank":bank,"bill_day":bd,"pay_date":pd,"amount":a,
              "amount_confirmed":ok,"min_payment":info.get("min_payment",""),
              "email_due_date":info.get("due_date",""),"cardholder":info.get("cardholder",""),
              "billing_cycle":cyc,"email_to":to,"status":"\u672a\u5904\u7406",
              "last_update":datetime.now().isoformat()}
        ex = next((c for c in state["cards"] if c["person"]==person and c["bank"]==bank and c.get("bill_day")==bd), None)
        if ex:
            if ex.get("status")=="\u5df2\u5904\u7406": cs["status"]="\u5df2\u5904\u7406"; cs["processed_at"]=ex.get("processed_at")
            ex.update(cs)
        else: state["cards"].append(cs)
    state["last_update"] = datetime.now().isoformat(); save_state(state)
    print("\n" + "="*60 + "\n\U0001f4cb \u8d26\u5355\u6c47\u603b:\n")
    today = datetime.now().strftime("%Y-%m-%d")
    bp = {}
    for c in state["cards"]: bp.setdefault(c.get("person",""),[]).append(c)
    ta = 0
    for person, cl in bp.items():
        print(f"\U0001f464 {person}:"); t = 0
        for c in cl:
            a = c.get("amount","") or "0"; ok = c.get("amount_confirmed", False)
            pd = c.get("pay_date",""); st = c.get("status","\u672a\u5904\u7406")
            try:
                dl = (datetime.strptime(pd,"%Y-%m-%d")-datetime.strptime(today,"%Y-%m-%d")).days
                u = "\u274c\u5df2\u8fc7\u671f" if dl<0 else f"\u26a0\ufe0f{dl}\u5929" if dl<=3 else f"\u2705{dl}\u5929"
            except: u = "\u2753"
            si = "\u2705" if st=="\u5df2\u5904\u7406" else "\u23f3"
            if ok: astr = f"{a}" if float(a)>0 else "0(\u786e\u8ba4)"
            else: astr = "\u2753\u5f85\u89e3\u6790"
            print(f"  \u2022 {c['bank']:10}|\uffe5{astr:>12}|{pd}|{u}|{si}{st}")
            if ok:
                try: t += float(a)
                except: pass
        print(f"  \U0001f4b0 \u5c0f\u8ba1: \uffe5{t:.2f}"); ta += t
    print(f"\n\U0001f4b0 \u603b\u8ba1: \uffe5{ta:.2f}")

if __name__ == "__main__":
    main()

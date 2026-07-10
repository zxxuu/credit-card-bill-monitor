#!/usr/bin/env python3
"""还款日计算规则"""
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

def calc_billing_date(bill_day, today=None):
    """
    计算本期账单日
    如果今天 >= 本月账单日，返回本月账单日
    如果今天 < 本月账单日，返回上月账单日
    """
    if today is None:
        today = datetime.now()
    
    # 本月账单日
    try:
        this_month = today.replace(day=bill_day)
    except ValueError:
        # 本月没有这一天（如2月30日），取月末
        import calendar
        last_day = calendar.monthrange(today.year, today.month)[1]
        this_month = today.replace(day=min(bill_day, last_day))
    
    if today > this_month:
        return this_month
    else:
        # 上月账单日
        last_month = this_month - relativedelta(months=1)
        return last_month

def calc_due_date(bill_day, due_rule, today=None):
    """
    计算还款日
    
    规则格式：
    - "+N" : 账单日后N天
    - "N"  : 下月固定N号
    """
    if today is None:
        today = datetime.now()
    
    billing_date = calc_billing_date(bill_day, today)
    
    if due_rule.startswith('+'):
        # 账单日后N天
        days = int(due_rule[1:])
        return billing_date + timedelta(days=days)
    else:
        # 下月固定某天
        due_day = int(due_rule)
        # 还款日在账单日的下个月
        next_month = billing_date + relativedelta(months=1)
        try:
            return next_month.replace(day=due_day)
        except ValueError:
            # 下月没有这一天，取月末
            import calendar
            last_day = calendar.monthrange(next_month.year, next_month.month)[1]
            return next_month.replace(day=min(due_day, last_day))

def get_billing_cycle(bill_day, today=None):
    """获取账单周期标识，如 '2026-06'"""
    billing_date = calc_billing_date(bill_day, today)
    return billing_date.strftime("%Y-%m")

def days_until_due(due_date, today=None):
    """计算距还款日天数"""
    if today is None:
        today = datetime.now()
    if isinstance(due_date, str):
        due_date = datetime.strptime(due_date, "%Y-%m-%d")
    return (due_date.date() - today.date()).days

if __name__ == "__main__":
    # 测试
    today = datetime.now()
    print(f"今天: {today.strftime('%Y-%m-%d')}")
    print()
    
    test_cases = [
        (2, "+19", "中信"),
        (8, "3", "交通"),
        (17, "10", "工商"),
        (17, "5", "平安"),
    ]
    
    for bill_day, due_rule, bank in test_cases:
        billing = calc_billing_date(bill_day, today)
        due = calc_due_date(bill_day, due_rule, today)
        days = days_until_due(due, today)
        cycle = get_billing_cycle(bill_day, today)
        print(f"{bank}: 账单日={billing.strftime('%Y-%m-%d')}, "
              f"还款日={due.strftime('%Y-%m-%d')}, "
              f"距还款日={days}天, 周期={cycle}")

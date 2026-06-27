#!/bin/bash
cd ~/credit-card-bill-monitor
./venv/bin/python3 scripts/tg_bill_reminder.py send 2>&1

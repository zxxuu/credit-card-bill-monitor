# 💳 信用卡账单监控提醒系统

自动解析邮箱中的信用卡账单邮件，通过 Telegram Bot 推送提醒，支持交互式标记还款状态。

## ✨ 功能特性

- **多银行支持**：工商、交通、中信、光大、兴业、平安、邮储、浦发、招商、民生、华夏、广发、中行、张家口等
- **PDF 附件解析**：支持解析银行发送的 PDF 账单附件（如中国银行）
- **智能匹配**：通过 Excel 蓝图自动匹配持卡人、账单日、还款日
- **Telegram 交互**：
  - 📋 展开/折叠账单列表
  - ✅ 单独标记已还款
  - 🔄 实时刷新账单数据
  - 🔔 按需监听模式（30分钟超时自动停止）
- **状态持久化**：跨天追踪还款状态，避免重复提醒
- **定时推送**：每天自动推送待还款账单

## 📋 支持的银行

| 银行 | 解析方式 | 备注 |
|------|---------|------|
| 工商银行 | 邮件正文 | |
| 交通银行 | 邮件正文 | |
| 中信银行 | 邮件正文 | |
| 光大银行 | 邮件正文 | |
| 兴业银行 | 邮件正文 | |
| 平安银行 | 邮件正文 | |
| 邮储银行 | 邮件正文 | |
| 浦发银行 | 邮件正文 | |
| 中国银行 | PDF 附件 | 需要 PyMuPDF |
| 招商银行 | 待优化 | |
| 民生银行 | 待优化 | |
| 华夏银行 | 待优化 | |
| 广发银行 | 待优化 | |
| 张家口银行 | 待优化 | |

## 🚀 快速开始

### 1. 安装依赖

```bash
# 安装 himalaya（邮件客户端）
curl -sSL https://pimalaya.org/himalaya/install.sh | sh

# 安装 Python 依赖
pip install -r requirements.txt

# 或者使用 pip 安装可选依赖
pip install PyMuPDF openpyxl
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，填入你的配置
```

**需要配置**：
- `BILL_BOT_TOKEN`：Telegram Bot Token（通过 @BotFather 创建）
- `TELEGRAM_HOME_CHANNEL`：接收消息的 Chat ID
- `QQ_EMAIL`：QQ 邮箱地址
- `QQ_IMAP_AUTH_CODE`：QQ 邮箱 IMAP 授权码

### 3. 配置 himalaya

```bash
# 创建配置文件
mkdir -p ~/.config/himalaya
cat > ~/.config/himalaya/config.toml << 'EOF'
[accounts.qq]
default = true
display-name = "你的名字"
email = "your_email@qq.com"

backend.type = "imap"
backend.host = "imap.qq.com"
backend.port = 993
backend.encryption = "tls"
backend.auth.type = "password"
backend.auth.raw = "your_imap_auth_code"

message.send.backend.type = "none"
EOF
```

### 4. 准备 Excel 蓝图

复制示例文件并填入你的真实数据：

```bash
cp credit_cards.example.csv credit_cards.csv
# 编辑 credit_cards.csv，填入你的信用卡信息
```

CSV 文件格式：

| 列名 | 说明 | 示例 |
|------|------|------|
| 持卡人 | 持卡人标识 | user1 / user2 |
| 银行 | 银行名称 | 工商、交通、中行 |
| 账单日 | 每月账单日 | 8、18、25 |
| 还款日 | 每月还款日 | 28、15、5 |

**注意**：`credit_cards.xlsx` 包含真实数据，已被 `.gitignore` 排除，不会上传到 Git。

### 5. 运行

```bash
# 手动解析账单
python3 scripts/bill_manager_final.py

# 启动 Telegram Bot（监听模式）
python3 scripts/tg_bill_reminder.py start

# 停止监听
python3 scripts/tg_bill_reminder.py stop
```

## 📁 项目结构

```
credit-card-bill-monitor/
├── scripts/
│   ├── bill_manager_final.py   # 主解析脚本
│   └── tg_bill_reminder.py     # Telegram Bot 脚本
├── data/                       # 数据目录（自动生成）
├── credit_cards.xlsx           # Excel 蓝图（用户创建）
├── .env.example                # 环境变量模板
├── .gitignore
├── requirements.txt
└── README.md
```

## ⚙️ 工作原理

1. **邮件扫描**：通过 himalaya CLI 扫描 QQ 邮箱中的账单邮件
2. **内容解析**：使用正则表达式解析邮件正文，提取账单金额、还款日等信息
3. **PDF 解析**：对于包含 PDF 附件的邮件（如中国银行），使用 PyMuPDF 解析
4. **智能匹配**：通过 Excel 蓝图匹配持卡人、账单日、还款日
5. **状态管理**：将解析结果保存到 `state.json`，支持标记已还款
6. **Telegram 推送**：通过 Bot 推送账单提醒，支持交互式操作

## 🔧 定时任务

使用 cron 设置每天自动推送：

```bash
# 每天早上 8 点（北京时间）推送账单提醒
0 0 * * * cd ~/credit-card-bill-monitor && python3 scripts/tg_bill_reminder.py send
```

## 📝 使用说明

### Telegram Bot 命令

- **🔔 启动监听**：开始监听按钮点击事件（30分钟超时）
- **🔄 刷新**：重新扫描邮件，更新账单数据
- **⏹ 停止监听**：停止监听模式
- **📋 展开/折叠**：显示/隐藏单个账单的标记按钮
- **✅ 标记还款**：点击单个账单标记为已还款

### 状态说明

- ⏳ 未处理：尚未标记还款
- ✅ 已处理：已标记还款
- ❓ 待解析：邮件解析失败，需要人工确认

## 🐛 故障排除

### 邮件解析失败

1. 检查 himalaya 配置是否正确
2. 确认 QQ 邮箱 IMAP 服务已开启
3. 检查授权码是否正确

### PDF 解析失败

1. 确认已安装 PyMuPDF：`pip install PyMuPDF`
2. 检查 PDF 文件是否损坏

### Telegram Bot 无响应

1. 检查 Bot Token 是否正确
2. 确认 Chat ID 是否正确
3. 检查是否有其他进程在使用同一个 Bot Token

## 📄 许可证

MIT License

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

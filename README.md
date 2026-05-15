# CFMMC 期货交易复盘系统

中国期货市场监控中心（CFMMC）投资者查询系统 —— 数据抓取、结算单解析、Web 交互式复盘分析。

## 功能特性

- **数据抓取**：自动登录监控中心，下载逐日盯市 / 逐笔对冲 XLS 结算单（支持验证码自动识别）
- **智能解析**：兼容两种报表格式（逐日盯市 / 逐笔对冲），正确处理期货与期权成交汇总
- **Web 复盘平台**：Flask 前后端分离架构，浏览器中直接查看分析结果
  - 总览卡片：总盈亏（期货+期权）、分项明细、净盈亏
  - 品种盈亏纵览：按品种汇总手数/盈亏/手续费，支持排名
  - 每日累计趋势图
  - 品种详情弹窗（点击品种名）
  - TOP 盈亏成交排行

## 项目结构

```
cfmmc/
├── cfmmc.py           # 监控中心爬虫 v1（客户权益/风险度查询）
├── cfmmc_crawler.py   # 监控中心爬虫 v2（XLS 结算单下载）
├── cfmmc_web.py       # 爬虫 Web 服务封装
├── review.py          # 复盘核心引擎（解析 + 统计 + 可视化 + CLI）
├── review_web.py      # Flask Web 服务（API + 图表生成）
├── static/
│   ├── review.html    # 前端页面
│   ├── review.css     # 样式
│   └── review.js      # 前端逻辑
└── config.json        # 账户配置（需自行创建，已 gitignore）
```

## 快速开始

### 环境要求

- Python 3.8+
- 依赖包：

```bash
pip install requests ddddocr xlrd pandas matplotlib openpyxl pillow beautifulsoup4 flask
```

### 配置账户

在项目根目录创建 `config.json`：

```json
{
  "accounts": [
    {
      "fund_name": "我的基金",
      "broker": "dw",
      "account_no": "你的账号",
      "password": "你的密码"
    }
  ],
  "start_date": "20260101",
  "end_date": "20261231",
  "output_dir": "./output"
}
```

> `broker` 为期货公司缩写（如 `dw`=东吴），`output_dir` 为下载文件的存储路径。

### 启动 Web 服务

```bash
python3 review_web.py
```

打开浏览器访问 **http://localhost:8869**，在页面内登录监控中心即可开始查询复盘。

### CLI 模式

```bash
# 交互式
python review.py

# 命令行指定日期范围
python review.py --start 20260401 --end 20260513
```

## 使用说明

1. **登录**：在 Web 页面输入期货账号和验证码登录监控中心
2. **选择日期范围**：支持"今日/本周/本月/上月/全部"快捷选择或自定义
3. **查询复盘**：点击"查询复盘"，系统会：
   - 自动检测缺失的交割单并下载
   - 解析 XLS 结算单（期货成交 + 期权权利金）
   - 计算总盈亏 = 期货平仓盈亏 + 期权权利金收支
   - 渲染图表和数据表格
4. **查看详情**：点击品种名称弹出该品种的所有成交明细

## 盈亏计算说明

| 指标 | 说明 |
|------|------|
| 期货盈亏 | 逐笔对冲报表中的平仓盈亏 / 逐日盯市报表中的当日盈亏 |
| 期权盈亏 | **FIFO 开平仓匹配后的已实现盈亏**（同花顺方式）：同一合约下买方与卖方按先进先出配对，`平仓盈亏 = (平仓权金 - 开仓权金) × 手数` |
| 总盈亏 | 期货盈亏 + 期权盈亏 |
| 净盈亏 | 总盈亏 - 手续费 |

## 注意事项

- 本工具仅供个人交易复盘学习使用，请遵守相关法律法规
- 验证码识别使用 ddddocr（基于深度学习的 OCR），准确率约 90%，失败会自动重试
- 结算单文件按日期存储在 `output/` 目录下，已被 `.gitignore` 排除，不会提交到仓库

## 代码信息

- **语言**：Python 3.8+ / JavaScript (原生) / HTML5 / CSS3
- **后端框架**：Flask（RESTful API）
- **前端**：原生 JS + ECharts（图表可视化）
- **数据解析**：xlrd（XLS）、pandas（数据处理）
- **验证码识别**：ddddocr（深度学习 OCR）
- **爬虫**：requests + BeautifulSoup4

## 联系方式

如有问题或建议，欢迎联系：**wationkong@163.com**

## License

MIT

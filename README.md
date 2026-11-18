# KPW6 PDF Studio

[![CI](https://github.com/JolnJack/kpw6-pdf-studio/actions/workflows/ci.yml/badge.svg)](https://github.com/JolnJack/kpw6-pdf-studio/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/JolnJack/kpw6-pdf-studio)](https://github.com/JolnJack/kpw6-pdf-studio/releases/latest)
[![License: AGPL-3.0-only](https://img.shields.io/badge/License-AGPL--3.0--only-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%2014%2B-0f172a)](https://github.com/JolnJack/kpw6-pdf-studio/releases/latest)

专为 **Kindle Paperwhite 6（1264×1680 · 300 PPI Carta 1300）** 与同类墨水屏阅读器打造的本地离线 PDF 精排工作台。支持双栏学术论文自动切分、带图表专业书防腰斩排版、扫描件去底漂白与字迹加深、双页漫画中缝拆切，以及纯文字 PDF 流式 EPUB 重排。所有文档均在**本机离线处理**，绝不改写原始 PDF。

---

## 下载与安装（免配环境 · 开箱即用）

按你的电脑类型下载对应安装包即可，**无需安装 Python 或 Node.js**：

| 运行平台 | 直接下载（最新版） | 安装与启动方式 |
| --- | --- | --- |
| **Windows**（Intel / AMD 64 位） | [下载 `Windows.zip`](https://github.com/JolnJack/kpw6-pdf-studio/releases/latest/download/Windows.zip) | 完整解压到固定文件夹，双击 `双击启动_Kindle精排大师.bat`（保留全部文件） |
| **Mac（Apple M 系列芯片）** | [下载 `Mac-M.dmg`](https://github.com/JolnJack/kpw6-pdf-studio/releases/latest/download/Mac-M.dmg) | 打开 DMG，将 `KPW6 PDF Studio` 拖入「应用程序」后启动 |
| **Mac（Intel 芯片）** | [下载 `Mac-Intel.dmg`](https://github.com/JolnJack/kpw6-pdf-studio/releases/latest/download/Mac-Intel.dmg) | 打开 DMG，将 `KPW6 PDF Studio` 拖入「应用程序」后启动 |

📄 **[下载中文 PDF 使用说明 (`Guide.pdf`)](https://github.com/JolnJack/kpw6-pdf-studio/releases/latest/download/Guide.pdf)** · 📖 **[在线图文说明](docs/user-guide.zh-CN.md)** · 📦 **[全部版本与构建记录](https://github.com/JolnJack/kpw6-pdf-studio/releases)** · 📝 **[更新日志](CHANGELOG.md)**

> **macOS 首次打开提示**：两个 Mac 版本均要求 **macOS 14 或更新系统**（芯片类型可在「苹果菜单  → 关于本机」查看）。当前安装包采用本地签名、尚未完成 Apple Developer ID 公证；首次打开若弹出系统拦截，可在「系统设置 → 隐私与安全性」底部点击「KPW6 PDF Studio」旁的**【仍要打开】**，或在终端执行一次 `xattr -cr "/Applications/KPW6 PDF Studio.app"`。

---

## 核心特性

- **五合一智能排版模式**
  - **双栏学术论文**：自动识别通栏标题/公式/跨栏图表与左右双栏正文，侧向旋转大图自动扶正。
  - **带图表的专业书**：紧贴版芯切白边、避开图文行智能分屏，支持单页强制整页或横置放大。
  - **扫描版书籍**：纸张灰底漂白消除墨水屏抖动网格、Gamma 墨迹加深、笔画加粗修复与小角度自动纠偏。
  - **PDF 漫画 / 纯图画册**：双页连跨中缝拆切、日漫右起（RTL）/美漫左起阅读顺序与零边距满屏输出。
  - **纯文字 PDF 书籍**：自动过滤重复页眉与孤立页码、跨行/跨页断句合并、中英文章节识别与词边界换行。
- **多格式输出与分卷管理**
  - 原生导出 **矢量优先 PDF**、**固定版面 EPUB 3**、**流式可调字号 EPUB 3**、**CBZ 漫画归档**（安装 [Calibre](https://calibre-ebook.com/) 后还可直接输出 **AZW3 / MOBI**）。
  - 支持页码范围截取、单页跳过、大书按页自动分卷、自定义/智能电子封面生成，以及图表高亮索引速跳。
- **本机隔离与流畅交互**
  - 交互预览与后台导出采用**双独立子进程**隔离，长耗时全书导出期间翻页与 600 PPI 高清放大镜依然丝滑响应，且支持随时一键取消导出。
  - 服务仅绑定 `127.0.0.1` 并内置 `Host` / 同源 / 会话令牌三重防护。

---

## 快速上手

1. **启动软件**：浏览器会自动打开本机排版工作台（Mac 可随时通过顶部菜单栏「📖 KPW6精排」重新唤起或退出；Windows 使用完毕关闭网页与启动命令窗口即可）。
2. **添加 PDF**：点击左侧上传区或拖入本地 PDF（文件仅在本机处理，侧栏支持点击 `✕` 移除历史记录）。
3. **校对预览**：软件会自动推荐文档模式；利用右侧对照预览与放大镜检查公式、图表与切分顺序，必要时对特殊页单独设置“整页保留”或“跳过”。
4. **一键导出**：选择目标格式（首次处理长书建议先导出几页试读），完成后点击顶部「打开导出目录」，通过 [Send to Kindle](https://www.amazon.com/sendtokindle) 或数据线传书即可。

---

## 仓库目录结构

```text
kpw6-pdf-studio/
├── .github/workflows/       # CI 跨平台回归测试 (ci.yml) 与自动化打包发布流水线 (release.yml)
├── docs/                    # 中文买家使用说明与第三方开源组件声明
├── packaging/               # 桌面端打包工程（macOS 状态栏与 DMG、Windows 启动器、PyInstaller 配方、冒烟验证）
├── src/kpw6_studio/         # 标准 PyPA src-layout 核心源码包
│   ├── engine/              # 版面分析、扫描增强、文本重排、封面合成与 PDF/EPUB/CBZ 导出模块
│   ├── server/              # 本机回环 WSGI 服务、安全校验与 Flask API 路由
│   ├── worker/              # 预览/导出独立子进程 JSONL 协议与外部转换器进程树管理
│   ├── web/                 # 独立前端静态资源 (index.html / styles.css / app.js)
│   └── samples.py           # 内置五类演示样张生成器
├── tests/                   # Python 单元/集成测试与 Node.js 前端状态机测试套件
├── CHANGELOG.md             # 版本变更记录
├── LICENSE                  # GNU AGPL-3.0-only 许可证
└── pyproject.toml           # PEP 621 项目元数据、依赖与 CLI 入口定义
```

<details>
<summary><strong>开发者指南：源码运行、回归测试与本地构建</strong></summary>

### 1. 从源码运行（Python 3.10+）

```bash
git clone https://github.com/JolnJack/kpw6-pdf-studio.git
cd kpw6-pdf-studio
python3 -m venv .venv
source .venv/bin/activate   # Windows CMD: .venv\Scripts\activate.bat
python -m pip install -e .
kpw6-studio                 # 或: python -m kpw6_studio
```

支持参数：`--port <端口>`（默认 `8765`，`0` 为自动分配）、`--no-browser`、`--port-file <路径>`，以及环境变量 `KPW6_DATA_DIR=<自定义数据目录>`。

### 2. 运行回归测试

```bash
python -m pip install -e ".[dev]"
python -u tests/worker_protocol_probe.py
python -u -m unittest discover -s tests -v
node --test tests/preview_race.test.cjs tests/frontend_release.test.cjs
```

### 3. 本地构建桌面安装包

```bash
# 重新生成多平台图标与中文使用说明 PDF（可选）
python packaging/make_icon.py
python packaging/make_user_guide.py --output dist/Guide.pdf

# macOS（在对应 arm64 / x86_64 架构机器上执行）
python packaging/macos/build_macos.py
python packaging/macos/build_dmg.py \
  --app-zip "dist/macos/KPW6-PDF-Studio-$(uname -m).zip" \
  --output "dist/macos/KPW6-PDF-Studio-$(uname -m).dmg"

# Windows（在 64 位 Windows 上执行）
python packaging/windows/build_windows.py --zip-output dist/Windows.zip

# 对冻结后端可执行文件执行真实 HTTP 预览与多格式导出冒烟校验
python packaging/smoke_frozen.py <后端可执行文件路径>
```

推送形如 `v1.0.0` 的 Git 标签时，GitHub Actions 中的 `Release` 工作流会自动在原生 Windows、Intel Mac 与 Apple Silicon Mac 节点完成测试、构建、签名/瘦身校验并发布正式 Release。

</details>

---

## 开源许可与合规说明

本项目采用 **[GNU Affero General Public License v3.0 only (AGPL-3.0-only)](LICENSE)** 许可发布；第三方依赖组件保留各自原有许可证（详见 [docs/THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md)）。每个预构建安装包均随包附带与构建二进制严格对应的 `SOURCE.zip`、`LICENSE`、`THIRD_PARTY_LICENSES/` 与 `RELEASE_MANIFEST.json`。

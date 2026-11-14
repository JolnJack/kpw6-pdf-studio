# KPW6 PDF Studio — Kindle PDF / 学术论文 / 扫描古籍 / 漫画精排工作台

面向 **Kindle Paperwhite 6（7 英寸 · 300 PPI · Carta 1300，兼容其他主流 Kindle 与墨水屏设备）** 设计的本地离线 Web 精排工具。专治在 7 寸墨水屏看原生 PDF 时常见的**字体过小、双栏分屏阅读困难、跨栏公式/三线表被腰斩、扫描书灰底棋盘纹、以及 Send to Kindle 无书架/锁屏封面**等问题。

---

## 核心特性

### 1. 五大专属文档重排引擎
上传 PDF 后自动检测版面特征并推荐模式，同时内置 5 类合成测试样张可供开箱体验：
- **双栏学术论文（`academic_2col`）**：自动拆分为「左栏 ➜ 右栏」单栏大字重排，保护跨栏三线表/附图不被切断，横排旋转 `90°` 的大图表自动扶正满屏。
- **带图表的专业书（`tech_book`）**：裁切多余白边、自动剔除页眉与页码，保留完整代码与架构图表（支持矢量无损直通及横屏 2 倍大字模式）。
- **扫描版书籍（`scanned_book`）**：Carta 1300 去网格漂白与黑度增强、自动切除书脊装订黑边与打孔阴影、剥离浅灰水印噪点、扫描歪斜自动扶正（Deskew）。
- **PDF 漫画 / 纯图画册（`comic_manga`）**：自动识别横版跨页从中缝拆分为两屏，支持日漫「从右 ➜ 左」阅读顺序与超窄白边满屏显示。
- **纯文字 PDF 书籍（`pure_text`）**：剔除页眉页码、合并断行、识别章节目录，可生成原生可自由调字号的流式 `.epub`。

### 2. 六种电子书输出与封面点亮
- **`.EPUB`（版面精排 + 封面）**：100% 锁定精排分屏版面与图表保护，内置封面图像与 OPF/NCX 图表跳转目录，上传官方 **Send to Kindle** 后可显示书架封面与锁屏封面（无需 Calibre）。
- **`.PDF`（专享矢量无损版）**：保留矢量文字与高清排版，体积紧凑。
- **`.EPUB`（流式可调字号）**：纯文字重排，支持在 Kindle 上自由切换字体与字号。
- **`.CBZ`（无损漫画图集包）**：适合漫画与纯图画册阅读器。
- **`.AZW3` / `.MOBI`**（需本机额外安装开源软件 [Calibre](https://calibre-ebook.com/) 以调用 `ebook-convert`）。
- 支持「`.PDF` 矢量版 + `.EPUB` 带封面版」双文件一键同步导出、页码区间截取，以及按 `50 / 100 / 150 页/卷` 自动分卷（轻松满足 Send to Kindle 单文件 200 MB 上限）。

### 3. 可视化交互校对
- 左右同屏实时对比：左侧标记「通栏/大图、左栏、右栏、旋转扶正」区块，点击编号直接跳转右侧对应屏幕。
- **600 PPI 点击放大镜**：支持单屏 1:1 精看、本页多屏平铺与局部 600 PPI 超清放大校验。
- 全书 `Table` / `Figure` 自动索引跳转条 + 单页强制微调（跟随后端自动 / 强制整页竖屏 / 横置 90° 满屏 / 导出跳过此页）。
- **100% 本地离线处理**：所有 PDF 仅在本地运行，不上传任何第三方服务器。

---

## 快速本地运行（macOS / Windows / Linux）

需要 **Python 3.10+**，建议在虚拟环境中安装依赖：

```bash
git clone https://github.com/<你的用户名>/kpw6-pdf-studio.git
cd kpw6-pdf-studio

python3 -m venv .venv
# macOS / Linux:
source .venv/bin/activate
# Windows (CMD):
# .venv\Scripts\activate.bat

pip install -r requirements.txt
python app.py
```

启动后将自动寻找可用端口（默认 `http://127.0.0.1:8765`）并打开浏览器工作台。

### 常用启动参数与环境变量
- `--port <端口号>`：指定监听端口（默认从 `8765` 起自动探测可用端口）。
- `--no-browser`（或设置环境变量 `KPW6_NO_BROWSER=1`）：仅启动后台服务，不自动弹开浏览器。
- `--port-file <路径>`：将实际绑定端口写入文件，供桌面启动器读取。
- `KPW6_DATA_DIR=<路径>`：自定义 `uploads/` 与 `outputs/` 数据根目录（PyInstaller 打包状态下默认写入 `~/Documents/KPW6_PDF_Studio`）。

---

## 打包为独立桌面应用

### 1. GitHub Actions 云端构建 Windows 独立 EXE
仓库内置 [.github/workflows/build_windows_exe.yml](.github/workflows/build_windows_exe.yml)，推送至 GitHub 后在 **`Actions -> Build Windows Standalone EXE -> Run workflow`** 手动触发，即可在云端生成免 Python 环境的 `KindleKPW6Studio-Win64` 构建产物。

### 2. macOS 本地构建 `.app`（Apple Silicon / 本地环境）
```bash
# 1. 生成应用图标 (.png / .ico / .icns)
python packaging/make_icon.py

# 2. 使用 PyInstaller 构建独立后端服务
pip install pyinstaller
pyinstaller --noconfirm --clean KPW6StudioServer.spec

# 3. 编译原生 Cocoa 菜单栏启动器（配合 .app 目录结构使用）
swiftc -O packaging/macos_tray.swift -o KPW6StudioLauncher -framework Cocoa
```

---

## 项目结构

```text
kpw6-pdf-studio/
├── app.py                              # Flask 本地工作台、前端界面与 API 路由
├── pdf_engine.py                       # 版面切分、扫描去污纠偏、EPUB/PDF/CBZ 导出引擎
├── sample_generator.py                 # 5 类内置演示样张生成器
├── requirements.txt                    # 核心 Python 依赖 (Flask / PyMuPDF / Pillow / NumPy)
├── KPW6StudioServer.spec               # PyInstaller 打包配置
├── packaging/
│   ├── make_icon.py                    # 图标生成脚本 (.png / .ico / .icns)
│   ├── macos_tray.swift                # macOS AppKit 状态栏启动器
│   ├── icon_1024.png / app_icon.ico / AppIcon.icns
└── .github/workflows/
    └── build_windows_exe.yml           # Windows PyInstaller 云端打包工作流
```

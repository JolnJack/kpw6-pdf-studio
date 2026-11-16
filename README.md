# KPW6 PDF Studio

**Kindle PDF 排版工具**，用于裁剪白边、放大正文、拆分双栏论文、处理扫描件和漫画，并将纯文字 PDF 转成可调字号的 EPUB。面向 Kindle Paperwhite 6 与类似墨水屏设备，处理在本机执行，原始 PDF 不会被改写。

## 下载与安装

按电脑类型下载一个安装包即可，**无需安装 Python 或 Node.js**。

| 你的电脑 | 下载 | 安装与启动 |
| --- | --- | --- |
| Windows（Intel / AMD，64 位） | [Windows 版](https://github.com/JolnJack/kpw6-pdf-studio/releases/download/v1.0.0-rc.1/Windows.zip) | 完整解压，双击 `双击启动_Kindle精排大师.bat`，保留全部文件 |
| Intel Mac | [Intel Mac 版](https://github.com/JolnJack/kpw6-pdf-studio/releases/download/v1.0.0-rc.1/Mac-Intel.dmg) | 打开 DMG，拖入“应用程序”，再从“应用程序”启动 |
| M 系列 Mac | [M 系列 Mac 版](https://github.com/JolnJack/kpw6-pdf-studio/releases/download/v1.0.0-rc.1/Mac-M.dmg) | 打开 DMG，拖入“应用程序”，再从“应用程序”启动 |

Mac 芯片类型可在“苹果菜单 → 关于本机”查看。**两个 Mac 版本均需要 macOS 14 或更新系统。**

[PDF 使用说明](https://github.com/JolnJack/kpw6-pdf-studio/releases/download/v1.0.0-rc.1/Guide.pdf) · [在线使用说明](docs/使用说明.md) · [版本与下载页](https://github.com/JolnJack/kpw6-pdf-studio/releases/tag/v1.0.0-rc.1)

当前版本为 **v1.0.0-rc.1 预发布版**。Mac 包尚未完成 Developer ID 签名及 Apple 公证，可能被系统阻止打开；遇到提示请记录报错并反馈，软件不要求关闭系统安全功能。

## 第一次使用

1. 启动软件，等待浏览器自动打开排版工作台。这是软件的本机操作界面。
2. 点击上传区域或拖入 PDF，再选择对应的文档类型。
3. 使用默认参数查看预览，检查正文、图表、方向和阅读顺序。
4. 选择 PDF、EPUB 等导出格式，点击“一键导出”。首次可先导出几页试读。
5. 导出完成后，点击“打开导出目录”找到文件，再按设备支持的方式传书。

预览和导出时请保持软件运行。这里的“上传”仅将文件交给本机软件处理，不会上传到本软件的云端。使用 Send to Kindle 等外部服务传书时，文件会另行交给相应服务。

Mac 可通过顶部菜单栏的“📖 KPW6精排”重新打开工作台或退出软件。Windows 请在导出完成后关闭网页和启动时出现的命令窗口。

## 处理与导出

- **双栏论文**：分析通栏与左右栏，分屏放大；按文字方向处理侧向图表。
- **图表书**：裁剪空白、分析切分位置，支持单页强制保留和横置。
- **扫描件**：背景处理、墨迹加深、笔画加粗、锐化和小角度纠偏。
- **漫画**：跨页拆分、左右阅读顺序与页边距设置。
- **纯文字**：跨行/跨页拼接、中文及英文章节识别，导出流式 EPUB 或重新排版的 PDF。
- 导出 **PDF、版面 EPUB、流式 EPUB、CBZ**，可同时生成 PDF 与 EPUB。
- **AZW3/MOBI** 需要另行安装 [Calibre](https://calibre-ebook.com/)，安装包不包含 Calibre。
- 支持页码范围、跳过页面、分卷和图表索引。每次导出使用独立目录，同名输入或重复导出不会覆盖旧结果。

PDF 分析和重排包含启发式判断。请用左右预览校对正文、公式、图表、方向和阅读顺序；必要时使用强制整页、关闭自动页眉页脚处理或调整参数。模糊印刷连字符会保留，明确的软连字符可合并，避免擅自改变正文词义。

原生 PDF 的可见批注及已填写表单会烘焙到内存副本中以保留外观；生成的阅读文件不保留原表单的可编辑性或原有数字签名效力。PDF 通道优先保留矢量，扫描件及显式增强选项使用图像路径。原始文件始终保留。

“无封面”会省略独立封面。流式 EPUB 中标记为强制整页/横置的页面以图像保留版面，前后正文仍可调整字号。

设备显示、Send to Kindle 转换、书架/锁屏封面和具体固件兼容性需要在实际设备上确认。按页数分卷不等于文件大小保证；云传前请检查每卷大小。

<details>
<summary>开发者说明：源码运行、测试与构建</summary>

以下内容面向需要运行源码或自行构建的开发者。使用上方安装包无需执行这些命令。

### 从源码运行

需要 Python 3.10 或更新版本。推荐单独虚拟环境。

macOS / Linux：

```bash
git clone https://github.com/JolnJack/kpw6-pdf-studio.git
cd kpw6-pdf-studio
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Windows CMD：

```bat
git clone https://github.com/JolnJack/kpw6-pdf-studio.git
cd kpw6-pdf-studio
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r requirements.txt
python app.py
```

启动后打开本机浏览器。服务只监听 `127.0.0.1`，默认从端口 8765 开始寻找空闲端口。

- `--port <端口>`：指定首选端口；`0` 自动选择空闲端口。
- `--no-browser` 或 `KPW6_NO_BROWSER=1`：不自动打开浏览器。
- `--port-file <路径>`：写入实际监听端口，供桌面启动器使用。
- `KPW6_DATA_DIR=<路径>`：自定义数据目录；包含 `uploads/`、`outputs/` 和演示样张。
- 源码运行默认使用项目目录；冻结应用默认使用 `~/Documents/KPW6_PDF_Studio`，目录创建失败时尝试 `~/.kpw6_pdf_studio`。

上传的 PDF 会先校验；损坏、需要密码或空文档不会进入历史列表。自定义封面会完整解码校验，最多2500万像素。旧版本遗留损坏 PDF 会被忽略而不删除。上传和导出文件保留在本地，可从对应目录手动管理。

HTTP 线程不直接调用 MuPDF。文档操作由一个独立子进程的主线程依次执行；进度查询仍可响应。退出会清理 PDF 子进程和本应用启动的转换器；处理进程意外退出后，可在下一次请求中重建。

### 回归验证

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
# 仅测试前端状态需要 Node.js；应用本身无需 Node.js
node --test tests/preview_race.test.cjs tests/frontend_release.test.cjs
```

测试使用临时合成文件。已安装 Calibre 时会验证实际 AZW3/MOBI 转换，否则跳过该可选项。CI 在 Windows、macOS、Linux 的 Python 3.10/3.12 上运行回归。

正式校验 EPUB 可使用 [EPUBCheck](https://github.com/w3c/epubcheck/releases)。打包后的 HTTP/独立处理进程验证：

```bash
python packaging/smoke_frozen.py <后端可执行文件路径>
```

### 构建 Windows 应用

Windows 上安装开发依赖后：

```bash
python packaging/make_icon.py
python packaging/build_windows.py
```

默认输出 `dist/Release/`，保持目录完整，双击其中的启动 BAT。脚本指向 `KindleKPW6Studio/KindleKPW6Studio.exe`，依赖位于同一子目录。

也可在 GitHub **Actions → Build Windows Standalone EXE → Run workflow** 构建。工作流先运行测试，再装配、实际启动冻结 EXE 并通过 HTTP 校验预览及 PDF/EPUB/CBZ 导出，最后提供构建产物。这些产物不是自动发布的正式 Release。

### 构建 macOS 应用

在目标架构的 Mac 上安装开发依赖：

```bash
python packaging/make_icon.py
python packaging/build_macos.py
```

默认输出 `dist/macos/KPW6-PDF-Studio-<架构>.zip`，内含完整的 `KPW6 PDF Studio.app`。脚本在本地临时目录装配 `Info.plist`、Cocoa 启动器、完整后端依赖、图标、对应源码及许可证，完成 ad-hoc 签名，并再次解压校验签名。最低系统版本依据实际 Python/依赖 wheel 的部署要求生成。

构建脚本输出 ZIP；上方下载区的 DMG 是由已验证应用另行封装的安装映像。

建议解压到本地目录使用；部分同步目录会附加 Finder 元数据，影响应用签名校验。ZIP不携带这些扩展属性。

可用 `--work-dir` 与 `--output-dir` 指定隔离构建目录；输出目标应为空。Intel 与 Apple Silicon 应分别在相应环境构建和验收。也可手动运行 Actions 中的 `Build macOS Apps` 工作流，在两种原生架构上构建，并检查解压后的签名、依赖架构、预览和导出。

**本地 ad-hoc 签名不等于 Developer ID 签名或 Apple 公证。** 面向外部用户分发前，仍需完成相应签名、公证、首次启动和退出验收；本项目不要求用户关闭系统安全功能。

</details>

## 许可与对应源码

项目所有者已选择 **AGPL-3.0-only** 开源分发，全文见 [LICENSE](LICENSE)。第三方组件保留各自许可，说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

构建脚本随包提供：

- `LICENSE`：项目许可全文。
- `SOURCE.zip` 与 `SOURCE_CODE.md`：本次构建使用的项目源码和获取说明。
- `THIRD_PARTY_LICENSES/`：实际打包依赖 wheel 中的版权、许可和供应商告知材料，以及 Python 许可。
- `RELEASE_MANIFEST.json`：源码哈希、基线提交、工作区状态、依赖版本和许可文件清单。

冻结配方排除不使用的可选布局/ML组件及其私有依赖，不将它们列为已打包运行组件。改变配方、依赖或分发方式后，应重新检查实际安装包的材料。开发工作区未提交时，清单会明确标记，不能把基线提交当成本次源码快照。

## 发布验收边界

自动测试和本地构建不能替代设备与平台验收。发布前请确认实际 Windows 包、macOS 完整应用、对应架构/系统版本、退出清理，以及目标 Kindle/Send to Kindle 行为。已知修复与验收证据见仓库测试和构建工作流；不作“任何 PDF 都不会出错”的保证。

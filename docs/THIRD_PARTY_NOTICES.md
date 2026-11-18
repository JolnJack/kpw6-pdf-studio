# Third-party software / 第三方软件告知

KPW6 PDF Studio is distributed under **AGPL-3.0-only**, as authorized by the
project owner. The full project license is in `LICENSE`. Dependencies retain
their own copyright and license terms; the project license does not replace them.

本项目采用 AGPL 第3版（仅该版本）开源分发。实际安装包提供对应项目源码
`SOURCE.zip`、源码取得说明 `SOURCE_CODE.md`、构建清单 `RELEASE_MANIFEST.json`
以及从实际安装依赖中收集的 `THIRD_PARTY_LICENSES/`。源码仓库：
[JolnJack/kpw6-pdf-studio](https://github.com/JolnJack/kpw6-pdf-studio)。

## Principal runtime components

| Component | Upstream terms and source |
|---|---|
| PyMuPDF / MuPDF | [AGPL / Artifex licensing](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright), [source](https://github.com/pymupdf/PyMuPDF). This distribution uses the AGPL route, not an assumed commercial license. Relevant library/copyright producer notices are retained in generated PDFs. |
| Flask, Werkzeug, Jinja, Click, ItsDangerous, MarkupSafe, Blinker | Their respective BSD-style or other upstream licenses, copied from the installed wheels. [Pallets projects](https://palletsprojects.com/). |
| Pillow and bundled image codecs | [Pillow licensing](https://github.com/python-pillow/Pillow/blob/main/LICENSE); wheel legal directories include additional codec notices. |
| NumPy and bundled numerical libraries | [NumPy license](https://github.com/numpy/numpy/blob/main/LICENSE.txt) and notices shipped in the installed wheel. |
| CPython | [Python licensing](https://docs.python.org/3/license.html); the interpreter's `LICENSE.txt` is included in binary distributions. |

Build tools such as [PyInstaller](https://pyinstaller.org/en/stable/license.html)
have their own terms. Its application-distribution exception does not replace
the licenses of bundled dependencies.

Calibre is **optional and not bundled**. AZW3/MOBI export invokes a separately
installed `ebook-convert`; see [Calibre source and license](https://github.com/kovidgoyal/calibre).
Its presence is not required for PDF, EPUB, or CBZ export.

The packaging scripts resolve the installed runtime dependency closure and copy
the actual wheel license, copyright and vendor-notice files, preserving bytes and
relative paths. The release manifest records versions and copied filenames.
It also lists distributions without detected legal files so omissions can be
reviewed before distributing a new dependency combination. The project/source
archive excludes private uploads, exports, virtual environments and credentials.

不应把本说明当作某个未构建安装包的许可证清单；请同时查看该安装包中的实际
构建清单和第三方许可目录。若改变依赖或分发方式，应重新核对相应条款。

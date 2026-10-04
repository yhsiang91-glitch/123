# PDF to TXT

把 PDF 轉成純文字（TXT）的命令列工具，對繁體中文／CJK 文件特別處理，並可批次、平行轉換整個資料夾。

## 安裝

需要 Python 3.9 以上。

```bash
pip install .                    # 安裝後可直接使用 pdf-to-txt 指令
# 或只裝相依套件，直接執行 python pdf_to_txt.py ...
pip install "pypdf[crypto]"
```

`pypdf[crypto]` 中的 `cryptography` 用來開啟 AES 加密的 PDF；沒有它時，部分「僅限制複製／列印」的 PDF 會無法讀取。

## 使用方式

```bash
pdf-to-txt report.pdf                    # 輸出 report.txt（與 PDF 同資料夾）
pdf-to-txt a.pdf b.pdf -o out/           # 輸出 out/a.txt、out/b.txt
pdf-to-txt report.pdf -o 結果.txt        # 指定輸出檔（僅限單一 PDF）
pdf-to-txt ./pdfs                        # 遞迴轉換整個資料夾，.txt 放在各 PDF 旁邊
pdf-to-txt ./pdfs -o ./txts              # 保留子目錄結構，輸出到 ./txts
pdf-to-txt book.pdf -p 1-3,10- -o -      # 只取指定頁，文字輸出到 stdout
pdf-to-txt locked.pdf --password 密碼     # 加密 PDF（也可用環境變數 PDF_PASSWORD）
```

### 選項

| 選項 | 說明 |
| --- | --- |
| `-o, --output OUT` | 輸出檔（僅單一 PDF）、輸出資料夾，或 `-` 表示輸出到 stdout。省略時，`NAME.txt` 寫在各 PDF 旁邊 |
| `-p, --pages SPEC` | 只轉換指定頁（從 1 起算），例如 `1-3,5,8-`、`-3` |
| `--mode {auto,plain,layout}` | 擷取模式，見下方說明（預設 `auto`） |
| `--page-sep {blank,ff,marker,none}` | 頁與頁之間：空白行（預設）、換頁字元 `\f`（空白頁也保留位置）、`===== Page N =====` 標記、或僅換行 |
| `--password PW` | 加密 PDF 的密碼，預設讀取環境變數 `PDF_PASSWORD` |
| `-e, --encoding ENC` | 輸出編碼，預設 `utf-8`；可用 `utf-8-sig`（帶 BOM）、`big5`、`gb18030` 等。無法編碼的字元會寫成 `?` 並顯示警告 |
| `-j, --jobs N` | 平行處理的行程數，預設為 CPU 核心數（最多 8）；`1` 表示不平行 |
| `--skip-existing` | 輸出檔已存在就略過（預設是覆蓋） |
| `--no-recursive` | 不進入子資料夾 |
| `-q, --quiet` | 只顯示警告與錯誤 |
| `-V, --version` | 顯示版本 |

進度與訊息都輸出到 stderr，stdout 只在 `-o -` 時輸出文字，因此可以安心接管線。

### 擷取模式

- `auto`（預設）：依 PDF 內的閱讀順序擷取；若某一頁被拆成「每行一個字」（常見於逐字繪製的中文 PDF），自動改用版面模式重新擷取，並清理多餘空白行與 CJK 字元之間的空格。
- `plain`：永遠依 PDF 內的內容順序。
- `layout`：盡量保留視覺版面（表格、欄位對齊）。多欄文件可能會左右欄交錯。

### 結束碼

| 碼 | 意義 |
| --- | --- |
| 0 | 全部成功（或已略過） |
| 1 | 至少一個檔案失敗、找不到輸入、或沒有任何 PDF |
| 2 | 參數錯誤 |
| 3 | pypdf 無法使用 |
| 130 | 被 Ctrl-C 中斷 |

批次轉換時，單一檔案失敗不會中斷其餘檔案，結尾會列出成功／略過／失敗的數量。

## 注意事項

- **掃描檔（純圖片 PDF）沒有文字層**，無法擷取。這類檔案會被回報為失敗（`no extractable text`），且不會寫出空的 TXT 檔，也不會蓋掉既有輸出。請先用 OCR 工具處理，例如 `ocrmypdf -l chi_tra input.pdf output.pdf`。
- 輸出檔以暫存檔寫入再改名，中途失敗不會留下殘缺檔案或毀損先前的輸出。
- 輸出路徑若是 `.pdf` 或與輸入檔相同會被拒絕，不會覆蓋原始 PDF。
- 遞迴搜尋會忽略 `.git` 這類隱藏資料夾，並以不分大小寫的方式比對 `.pdf`（`.PDF` 也會處理）。
- 大型 PDF 會被切成數段由多個行程同時處理（約 64 頁以上），內嵌字型的中文 PDF 特別受惠。

### 與 0.1 版的差異

舊版的第二個位置參數是輸出路徑（`pdf_to_txt.py in.pdf out.txt`、`pdf_to_txt.py ./pdfs ./txts`）。這種寫法在多個輸入時會造成誤判（曾可能覆蓋第二個 PDF），因此改為明確的 `-o`：

```bash
pdf-to-txt in.pdf -o out.txt
pdf-to-txt ./pdfs -o ./txts
```

## 開發與測試

```bash
pip install -e ".[test]"
pytest
```

測試用的 PDF 全部在執行時以 reportlab 動態產生，不需要任何二進位測試檔。也可以作為函式庫使用：

```python
from pdf_to_txt import pdf_to_text

text = pdf_to_text("report.pdf", pages="1-3", mode="auto")
```

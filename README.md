# PDF to TXT Converter

一個簡單的 Python 工具，可將 PDF 檔案轉換為純文字 (TXT) 檔。

## 安裝

```bash
pip install -r requirements.txt
```

## 使用方式

轉換單一檔案：

```bash
python pdf_to_txt.py input.pdf
python pdf_to_txt.py input.pdf output.txt
```

批次轉換整個資料夾（會保留子目錄結構）：

```bash
python pdf_to_txt.py ./pdfs ./txts
```

## 說明

- 優先使用 `pypdf`，若未安裝則退回 `PyPDF2`。
- 以 UTF-8 編碼輸出。
- 對於影像型 PDF（掃描檔）無法擷取文字，需要 OCR 工具（例如 `pytesseract`）。

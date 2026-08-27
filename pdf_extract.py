# -*- coding: utf-8 -*-
"""简历 PDF 文本提取（pdfplumber）。扫描件/图片型 PDF 返回空串，由上层提示需 OCR。"""
import io


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """从 PDF 字节提取纯文本；扫描件/图片型 PDF 会返回空串。"""
    import pdfplumber
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        parts = [(p.extract_text() or "") for p in pdf.pages]
    return "\n".join(parts).strip()

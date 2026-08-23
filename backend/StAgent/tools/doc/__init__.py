"""Document processing tools for StAgent.

This module provides tools for reading and processing various document formats:
- PDF files (PyMuPDF)
- Word documents (python-docx)
- Excel spreadsheets (openpyxl)
- Image OCR (PaddleOCR)
- Document classification (rule-based and LLM)
- CSV/TSV processing
- Markdown parsing and conversion
- HTML extraction and conversion
- Config files (JSON/YAML/TOML)
- Archive management (zip/tar)
- Text file processing with encoding detection
"""

from StAgent.tools.doc.archive_manager import ArchiveManagerTool
from StAgent.tools.doc.config_file_tool import ConfigFileTool
from StAgent.tools.doc.csv_processor import CSVProcessorTool
from StAgent.tools.doc.doc_classifier import DocClassifierTool
from StAgent.tools.doc.excel_reader import ExcelReaderTool
from StAgent.tools.doc.html_processor import HTMLProcessorTool
from StAgent.tools.doc.image_ocr import ImageOCRTool
from StAgent.tools.doc.knowledge_search import KnowledgeSearchTool
from StAgent.tools.doc.markdown_processor import MarkdownProcessorTool
from StAgent.tools.doc.pdf_reader import PDFReaderTool
from StAgent.tools.doc.pdf_research import (
    CitationExtractorTool,
    PDFStructureTool,
    PDFTranslateTool,
    SectionSummarizerTool,
)
from StAgent.tools.doc.text_processor import TextFileProcessorTool
from StAgent.tools.doc.word_reader import WordReaderTool

__all__ = [
    "PDFReaderTool",
    "PDFStructureTool",
    "CitationExtractorTool",
    "SectionSummarizerTool",
    "PDFTranslateTool",
    "WordReaderTool",
    "ExcelReaderTool",
    "ImageOCRTool",
    "DocClassifierTool",
    "CSVProcessorTool",
    "MarkdownProcessorTool",
    "HTMLProcessorTool",
    "ConfigFileTool",
    "ArchiveManagerTool",
    "TextFileProcessorTool",
    "KnowledgeSearchTool",
]

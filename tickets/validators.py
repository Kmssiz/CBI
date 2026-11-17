"""
Ticket attachments: images, PDF, Word and Excel files.

The extension alone proves nothing, so the first bytes (and, for Office files, the zip layout) are
checked too. Macro-enabled documents (.docm/.xlsm, or any file carrying a VBA project), web pages and
SVG images are refused on purpose.
"""
import os
import zipfile

from django.core.exceptions import ValidationError

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
WORD_EXTENSIONS = {".doc", ".docx"}
EXCEL_EXTENSIONS = {".xls", ".xlsx", ".csv"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf"} | WORD_EXTENSIONS | EXCEL_EXTENSIONS

# What the file input offers in the browser's picker.
ACCEPT_ATTRIBUTE = ",".join(sorted(ALLOWED_EXTENSIONS))
FORMATS_HELP = "Images (JPG, PNG, GIF, WEBP), PDF, Word (DOC, DOCX) ou Excel (XLS, XLSX, CSV). Taille max : 10 Mo."

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"          # .doc / .xls
_OOXML_FOLDER = {".docx": "word/", ".xlsx": "xl/"}


def extension_of(name: str) -> str:
    return os.path.splitext(name or "")[1].lower()


def is_image_name(name: str) -> bool:
    return extension_of(name) in IMAGE_EXTENSIONS


def kind_of(name: str) -> str:
    """'image' | 'pdf' | 'word' | 'excel' | 'file': picks the icon shown for an attachment."""
    ext = extension_of(name)
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in WORD_EXTENSIONS:
        return "word"
    if ext in EXCEL_EXTENSIONS:
        return "excel"
    return "file"


def _head(upload, size: int = 4096) -> bytes:
    upload.seek(0)
    data = upload.read(size)
    upload.seek(0)
    return data


def _check_image(upload, ext):
    head = _head(upload, 16)
    signatures = {
        ".jpg": head.startswith(b"\xff\xd8\xff"), ".jpeg": head.startswith(b"\xff\xd8\xff"),
        ".png": head.startswith(b"\x89PNG\r\n\x1a\n"), ".gif": head[:4] == b"GIF8",
        ".webp": head[:4] == b"RIFF" and head[8:12] == b"WEBP",
    }
    return signatures[ext]


def _check_ooxml(upload, ext) -> bool:
    try:
        upload.seek(0)
        with zipfile.ZipFile(upload) as archive:
            names = archive.namelist()
        upload.seek(0)
    except zipfile.BadZipFile:
        return False
    if "[Content_Types].xml" not in names or not any(n.startswith(_OOXML_FOLDER[ext]) for n in names):
        return False
    return not any(n.lower().endswith("vbaproject.bin") for n in names)     # no macros


def _check_ole(upload) -> bool:
    """A genuine OLE file (.doc / .xls) that holds no VBA macro project (stream names are UTF-16)."""
    upload.seek(0)
    data = upload.read()
    upload.seek(0)
    if not data.startswith(_OLE):
        return False
    return not any(marker in data for marker in (
        "_VBA_PROJECT".encode("utf-16-le"), "VBA".encode("utf-16-le") + b"\x00\x00", b"_VBA_PROJECT",
    ))


def _check_csv(upload) -> bool:
    data = _head(upload)
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            data.decode("cp1252")
        except UnicodeDecodeError:
            return False
    return True


def validate_ticket_attachment(upload):
    """Raise ValidationError (French message) unless `upload` is an allowed, genuine file."""
    ext = extension_of(upload.name)
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError("Format non accepté. " + FORMATS_HELP)
    if upload.size > MAX_ATTACHMENT_BYTES:
        raise ValidationError("Le fichier ne doit pas dépasser 10 Mo.")

    if ext in IMAGE_EXTENSIONS:
        genuine = _check_image(upload, ext)
    elif ext == ".pdf":
        genuine = b"%PDF-" in _head(upload, 1024)
    elif ext in (".docx", ".xlsx"):
        genuine = _check_ooxml(upload, ext)
    elif ext in (".doc", ".xls"):
        genuine = _check_ole(upload)
    else:
        genuine = _check_csv(upload)
    if not genuine:
        raise ValidationError("Le contenu du fichier ne correspond pas à son format.")
    return upload

"""
drive_service.py - אינטגרציה מול Google Drive API.

מה הקובץ עושה:
    עוטף את כל הפעולות מול תיקיית החוזים (MITVE TOOLS - NECHASIM - CONTRACTS)
    שנמצאת ב-Shared Drive: רשימת קבצים (כולל md5Checksum ל-Delta Sync),
    העלאה ב-Chunks, הורדה לתיקייה זמנית (/tmp), ייצוא Google Docs ל-PDF ומחיקה.

איך זה עובד:
    ההזדהות מתבצעת עם Service Account (contracts-bot). מאחר שהתיקייה ב-Shared Drive,
    כל קריאה ל-API נשלחת עם supportsAllDrives=True (ובחיפוש גם includeItemsFromAllDrives=True),
    אחרת Google מחזירה "File not found" גם כשיש הרשאה.

תרומה לחיסכון בטוקנים / זיכרון ב-Render:
    - list_files מחזיר רק את השדות ההכרחיים (fields=...) כולל md5Checksum, שמאפשר
      לשירות ה-RAG לדלג על קבצים שלא השתנו - כלומר לא לשלם שוב על OCR ו-Embeddings.
    - ההורדה וההעלאה מתבצעות ב-Chunks של 5MB ישירות מ/אל הדיסק, כך שקובץ סרוק
      של עשרות MB לעולם לא נטען כולו לזיכרון (קריטי ב-Instance של 512MB ב-Render).
"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import re
import uuid
from typing import Any, Dict, List, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from backend.config import Settings

logger = logging.getLogger(__name__)

# הרשאה מלאה לדרייב - נדרשת להעלאה ולמחיקה, לא רק לקריאה
SCOPES = ["https://www.googleapis.com/auth/drive"]

# גודל Chunk להעלאה/הורדה: 5MB (חייב להיות כפולה של 256KB לפי דרישות Google)
CHUNK_SIZE = 5 * 1024 * 1024

# ניסיונות חוזרים אוטומטיים (עם Backoff אקספוננציאלי של ספריית Google) לשגיאות
# זמניות בלבד: 429, 5xx ושגיאות רשת. שגיאות הרשאה (403/404) לא נשלחות שוב.
NUM_RETRIES = 3

# השדות היחידים שאנחנו מבקשים מה-API - פחות נתונים ברשת ובזיכרון
FILE_FIELDS = "id, name, mimeType, md5Checksum, modifiedTime, size, webViewLink"

# סוגי קבצים שהמערכת יודעת לפענח ולאנדקס
SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/tiff",
    "image/webp",
    "application/vnd.google-apps.document",  # Google Docs - מיוצא ל-PDF לפני פיענוח
}

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# סוגי קבצים שמותר להשתמש בהם כתבנית Word (Google Docs מיוצא ל-docx בהורדה)
TEMPLATE_MIME_TYPES = {DOCX_MIME, GOOGLE_DOC_MIME}
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveServiceError(RuntimeError):
    """שגיאה ידידותית בעברית שמוצגת למשתמש כשהפעולה מול הדרייב נכשלת."""


class DriveService:
    """
    שירות יחיד לכל הפעולות מול Google Drive.

    המופע נוצר פעם אחת בעליית השרת ומחזיק לקוח API אחד (חיבור HTTP אחד שמשותף
    לכל הבקשות), במקום ליצור לקוח חדש בכל קריאה.
    """

    def __init__(self, settings: Settings):
        """
        יוצר לקוח Drive v3 מאומת באמצעות ה-Service Account.

        cache_discovery=False מונע אזהרות ושמירת קבצי Cache מיותרים על הדיסק.
        """
        self.settings = settings
        self.folder_id = settings.DRIVE_CONTRACTS_FOLDER_ID
        creds = service_account.Credentials.from_service_account_info(
            settings.google_credentials_info(), scopes=SCOPES
        )
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._generated_folder_id: Optional[str] = None

    # ------------------------------------------------------------------
    # רשימת קבצים
    # ------------------------------------------------------------------
    def list_files(self, folder_id: Optional[str] = None, only_supported: bool = True) -> List[Dict[str, Any]]:
        """
        מחזיר את כל הקבצים בתיקייה (ללא קבצים שבאשפה), כולל md5Checksum.

        - עובר על כל העמודים (pagination) עד שאין nextPageToken.
        - only_supported=True מסנן החוצה תיקיות וסוגי קבצים שאין לנו מפענח עבורם,
          כדי שהסנכרון לא ינסה (ויבזבז זמן) על קבצי Excel, וידאו וכו'.
        - ל-Google Docs אין md5Checksum, ולכן משתמשים ב-modifiedTime כ"טביעת אצבע".
        """
        folder_id = folder_id or self.folder_id
        query = f"'{folder_id}' in parents and trashed = false"
        files: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        try:
            while True:
                resp = (
                    self._service.files()
                    .list(
                        q=query,
                        fields=f"nextPageToken, files({FILE_FIELDS})",
                        pageSize=200,
                        pageToken=page_token,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                        corpora="allDrives",
                        orderBy="modifiedTime desc",
                    )
                    .execute(num_retries=NUM_RETRIES)
                )
                for f in resp.get("files", []):
                    if only_supported and f.get("mimeType") not in SUPPORTED_MIME_TYPES:
                        continue
                    # טביעת אצבע אחידה לכל סוגי הקבצים - משמשת את ה-Delta Sync
                    f["fingerprint"] = f.get("md5Checksum") or f"mtime:{f.get('modifiedTime')}"
                    files.append(f)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as e:
            raise self._friendly_error(e, "רשימת הקבצים") from e
        return files

    def get_file(self, file_id: str) -> Dict[str, Any]:
        """מחזיר את המטא-דאטה של קובץ בודד (משמש לאחר העלאה, כדי לקבל md5)."""
        try:
            f = (
                self._service.files()
                .get(fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True)
                .execute(num_retries=NUM_RETRIES)
            )
            f["fingerprint"] = f.get("md5Checksum") or f"mtime:{f.get('modifiedTime')}"
            return f
        except HttpError as e:
            raise self._friendly_error(e, "שליפת פרטי הקובץ") from e

    # ------------------------------------------------------------------
    # העלאה ב-Chunks
    # ------------------------------------------------------------------
    def upload_file(self, local_path: str, name: str, mime_type: Optional[str] = None,
                    parent_id: Optional[str] = None) -> Dict[str, Any]:
        """
        מעלה קובץ מהדיסק המקומי לתיקיית החוזים בהעלאה מתחדשת (Resumable) ב-Chunks.

        איך זה עובד:
            MediaFileUpload קורא מהדיסק 5MB בכל פעם, ו-next_chunk() שולח אותם אחד-אחד.
            כך צריכת הזיכרון קבועה (~5MB) ללא תלות בגודל הקובץ, ואם החיבור נופל
            באמצע - ההעלאה ממשיכה מהנקודה האחרונה ולא מתחילה מחדש.
        """
        mime_type = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        metadata = {"name": name, "parents": [parent_id or self.folder_id]}
        media = MediaFileUpload(local_path, mimetype=mime_type, chunksize=CHUNK_SIZE, resumable=True)
        try:
            request = self._service.files().create(
                body=metadata, media_body=media, fields=FILE_FIELDS, supportsAllDrives=True
            )
            response = None
            while response is None:
                status, response = request.next_chunk(num_retries=NUM_RETRIES)
                if status:
                    logger.info("העלאה %s: %d%%", name, int(status.progress() * 100))
        except HttpError as e:
            raise self._friendly_error(e, "העלאת הקובץ") from e
        finally:
            # MediaFileUpload מחזיק את הקובץ פתוח; ב-Windows זה חוסם את מחיקת הקובץ הזמני
            fd = getattr(media, "_fd", None)
            if fd is not None:
                fd.close()
        response["fingerprint"] = response.get("md5Checksum") or f"mtime:{response.get('modifiedTime')}"
        return response

    def upload_generated_doc(self, local_path: str, name: str) -> Optional[Dict[str, Any]]:
        """
        מגבה מסמך Word שהסוכן יצר לתת-התיקייה generated בדרייב.

        הדיסק של Render הוא זמני (נמחק בכל Deploy/Restart), ולכן הגיבוי בדרייב
        מבטיח שהמסמך לא ילך לאיבוד. כשל בגיבוי לא מפיל את יצירת המסמך.
        """
        try:
            folder_id = self._get_or_create_generated_folder()
            return self.upload_file(
                local_path,
                name,
                mime_type=DOCX_MIME,
                parent_id=folder_id,
            )
        except Exception as e:  # noqa: BLE001 - גיבוי הוא "Best effort"
            logger.warning("גיבוי המסמך לדרייב נכשל: %s", e)
            return None

    def _get_or_create_generated_folder(self) -> str:
        """מאתר (או יוצר פעם אחת) את תת-התיקייה generated ושומר את המזהה שלה בזיכרון."""
        if self._generated_folder_id:
            return self._generated_folder_id
        name = self.settings.GENERATED_DRIVE_SUBFOLDER
        q = (f"'{self.folder_id}' in parents and name = '{name}' "
             f"and mimeType = '{FOLDER_MIME}' and trashed = false")
        resp = self._service.files().list(
            q=q, fields="files(id)", supportsAllDrives=True,
            includeItemsFromAllDrives=True, corpora="allDrives",
        ).execute(num_retries=NUM_RETRIES)
        if resp.get("files"):
            self._generated_folder_id = resp["files"][0]["id"]
        else:
            folder = self._service.files().create(
                body={"name": name, "mimeType": FOLDER_MIME, "parents": [self.folder_id]},
                fields="id", supportsAllDrives=True,
            ).execute(num_retries=NUM_RETRIES)
            self._generated_folder_id = folder["id"]
        return self._generated_folder_id

    # ------------------------------------------------------------------
    # הורדה ל-/tmp
    # ------------------------------------------------------------------
    def download_to_tmp(self, file: Dict[str, Any]) -> str:
        """
        מוריד קובץ לתיקייה הזמנית ומחזיר את הנתיב המקומי.

        - קבצי Google Docs אינם קבצים בינאריים, ולכן מיוצאים ל-PDF (export_media).
        - שאר הקבצים מורדים כמו שהם (get_media).
        - MediaIoBaseDownload כותב ישירות לקובץ פתוח על הדיסק, Chunk אחר Chunk,
          כך שהקובץ לא נטען כולו לזיכרון.
        באחריות הקורא למחוק את הקובץ בסיום (rag_service עושה זאת ב-finally).
        """
        file_id = file["id"]
        is_gdoc = file.get("mimeType") == GOOGLE_DOC_MIME
        ext = ".pdf" if is_gdoc else (os.path.splitext(file.get("name", ""))[1] or "")
        if is_gdoc:
            request = self._service.files().export_media(fileId=file_id, mimeType="application/pdf")
        else:
            request = self._service.files().get_media(fileId=file_id, supportsAllDrives=True)
        return self._download(request, ext, "הורדת הקובץ")

    def _download(self, request: Any, ext: str, action: str) -> str:
        """
        מוריד בקשת get_media/export_media לקובץ זמני בשם אקראי ומחזיר את הנתיב.

        MediaIoBaseDownload כותב ישירות לדיסק, Chunk אחר Chunk. קובץ חלקי (הורדה
        שנכשלה באמצע) נמחק מיד, כדי לא להשאיר זבל ב-/tmp המוגבל של Render.
        """
        safe_name = re.sub(r"[^\w.\-]", "_", f"{uuid.uuid4().hex}{ext}")
        local_path = os.path.join(self.settings.TMP_DIR, safe_name)
        try:
            with io.FileIO(local_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=CHUNK_SIZE)
                done = False
                while not done:
                    _, done = downloader.next_chunk(num_retries=NUM_RETRIES)
        except Exception as e:
            if os.path.exists(local_path):
                os.remove(local_path)
            if isinstance(e, HttpError):
                raise self._friendly_error(e, action) from e
            raise
        return local_path

    # ------------------------------------------------------------------
    # תבניות Word (תיקיית התבניות בדרייב)
    # ------------------------------------------------------------------
    def list_templates(self) -> List[Dict[str, Any]]:
        """
        רשימת תבניות ה-Word בתיקיית התבניות (DRIVE_TEMPLATES_FOLDER_ID).

        נכללים רק קבצי docx ו-Google Docs (שמיוצאים ל-docx בהורדה). התיקייה צריכה
        להיות משותפת עם חשבון השירות, בדיוק כמו תיקיית החוזים.
        """
        folder = self.settings.DRIVE_TEMPLATES_FOLDER_ID
        if not folder:
            return []
        return [f for f in self.list_files(folder_id=folder, only_supported=False)
                if f.get("mimeType") in TEMPLATE_MIME_TYPES]

    def download_template(self, file_id: str) -> str:
        """
        מוריד תבנית Word לקובץ זמני (.docx) ומחזיר את הנתיב. באחריות הקורא למחוק.

        - Google Docs מיוצא לפורמט docx (export_media), כך שאפשר לנהל תבניות גם
          ישירות ב-Google Docs.
        - כל סוג קובץ אחר (PDF, ‎.doc ישן וכו') נדחה עם הודעה ברורה - docxtpl
          עובד רק עם docx.
        """
        meta = self.get_file(file_id)
        mime = meta.get("mimeType")
        if mime == GOOGLE_DOC_MIME:
            request = self._service.files().export_media(fileId=file_id, mimeType=DOCX_MIME)
        elif mime == DOCX_MIME:
            request = self._service.files().get_media(fileId=file_id, supportsAllDrives=True)
        else:
            raise DriveServiceError(f"התבנית '{meta.get('name')}' חייבת להיות קובץ Word (.docx) או Google Doc.")
        return self._download(request, ".docx", "הורדת התבנית")

    # ------------------------------------------------------------------
    # מחיקה
    # ------------------------------------------------------------------
    def delete_file(self, file_id: str) -> None:
        """
        מעביר קובץ לאשפה (trashed=True) במקום מחיקה לצמיתות.

        ב-Shared Drive, מחיקה לצמיתות דורשת הרשאת Manager, והעברה לאשפה מאפשרת
        שחזור במקרה של טעות - בטוח יותר עבור מסמכים משפטיים.
        """
        try:
            self._service.files().update(
                fileId=file_id, body={"trashed": True}, supportsAllDrives=True
            ).execute(num_retries=NUM_RETRIES)
        except HttpError as e:
            raise self._friendly_error(e, "מחיקת הקובץ") from e

    # ------------------------------------------------------------------
    # עזר
    # ------------------------------------------------------------------
    @staticmethod
    def _friendly_error(e: HttpError, action: str) -> DriveServiceError:
        """
        ממיר שגיאת HTTP של Google להודעה ברורה בעברית.

        המקרים הנפוצים: ה-Service Account לא חבר ב-Shared Drive (404/403),
        או שהתיקייה ב-My Drive רגיל ואין לחשבון השירות מכסת אחסון (storageQuotaExceeded).
        """
        text = str(e)
        if "storageQuotaExceeded" in text:
            msg = (f"{action} נכשלה: לחשבון השירות אין מכסת אחסון. "
                   "ודא שהתיקייה נמצאת ב-Shared Drive ושחשבון השירות חבר בו.")
        elif e.resp is not None and e.resp.status in (403, 404):
            msg = (f"{action} נכשלה: אין הרשאה או שהקובץ לא נמצא. "
                   "ודא שחשבון השירות הוא חבר ב-Shared Drive בתפקיד Content Manager.")
        else:
            msg = f"{action} נכשלה: {text[:300]}"
        logger.error(msg)
        return DriveServiceError(msg)

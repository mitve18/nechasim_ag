"""
locustfile.py - בדיקת עומסים (Load Testing) לשרת ה-FastAPI עם Locust.

מה הקובץ עושה:
    מדמה משתמשים שפותחים את הממשק ומשתמשים בו, אבל רק מול נתיבים בטוחים
    לקריאה בלבד: בלי צ'אט/אנליזה (עולים טוקנים ב-OpenAI) ובלי העלאה, מחיקה,
    סנכרון או ייצוא מסמכים (כותבים לדרייב/לאינדקס/לדיסק).

התקנה (פעם אחת, רק בסביבת פיתוח - לא בייצור):
    pip install -r requirements-dev.txt

הרצה מול השרת המקומי (http://127.0.0.1:8000) - ברירת המחדל:
    1. בטרמינל אחד:  uvicorn backend.main:app --port 8000
    2. בטרמינל שני, עם ממשק Web של Locust:
           locust
       ואז לפתוח http://localhost:8089, לבחור מספר משתמשים וקצב, ו-Start.
    3. או בלי ממשק (Headless) - 20 משתמשים, 2 חדשים בשנייה, למשך 2 דקות:
           locust --headless -u 20 -r 2 -t 2m --host http://127.0.0.1:8000

הרצה מול שרת הייצור (https://mitve-contracts-agent.onrender.com):
    locust --headless -u 5 -r 1 -t 1m --host https://mitve-contracts-agent.onrender.com

    זהירות בייצור:
    - השרת הוא Worker יחיד על Instance של 512MB ומשרת משתמשים אמיתיים -
      התחל עם מעט משתמשים (-u) ותמיד הגבל זמן (-t).
    - כל קריאה ל-/api/files פונה ל-Google Drive API ונספרת במכסת המשתמש המחובר.
    - אם השרת "נרדם", הבקשה הראשונה תהיה איטית במיוחד - אל תסיק ממנה מסקנות.

שמירת התוצאות לקבצי CSV (results_stats.csv וכו'): הוסף --csv results לכל פקודה.
"""

import random
import uuid

from locust import HttpUser, between, task

REPORT_KINDS = ("expiring", "financial", "status")


class ContractsUser(HttpUser):
    """משתמש טיפוסי: טוען את הממשק, ואז בודק סטטוס, דוחות מהירים ורשימת חוזים."""

    # ברירת מחדל מקומית - לייצור חייבים לבחור במפורש עם --host
    host = "http://127.0.0.1:8000"
    wait_time = between(1, 5)

    def on_start(self) -> None:
        """טעינת הדף - אותן בקשות ש-app.js שולח בעלייה."""
        # מזהה שיחה בפורמט של newSessionId() ב-app.js (UUID בלי מקפים)
        self.session_id = uuid.uuid4().hex
        self.client.get("/", name="/ (UI)")
        self.client.get("/styles.css", name="/ (static)")
        self.client.get("/app.js", name="/ (static)")
        self.session_messages()
        self.list_files()

    def _get_json(self, path: str, key: str, name: str | None = None) -> None:
        """GET שנכשל אם הסטטוס אינו 200 או שחסר המפתח הצפוי ב-JSON."""
        with self.client.get(path, name=name or path, catch_response=True) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            try:
                data = resp.json()
            except ValueError:
                resp.failure("התשובה אינה JSON")
                return
            if key not in data:
                resp.failure(f"חסר המפתח '{key}' בתשובה")

    @task(4)
    def sync_status(self) -> None:
        """סטטוס הסנכרון האחרון (זיכרון בלבד)."""
        self._get_json("/api/sync/status", "last_sync")

    @task(3)
    def report(self) -> None:
        """כפתורי הדוחות המהירים - SQL ישיר, 0 טוקנים."""
        kind = random.choice(REPORT_KINDS)
        self._get_json(f"/api/reports/{kind}", "rows", name="/api/reports/[kind]")

    @task(2)
    def session_messages(self) -> None:
        """שחזור שיחה אחרי רענון (קריאה מ-Postgres)."""
        self._get_json(
            f"/api/sessions/{self.session_id}/messages",
            "messages",
            name="/api/sessions/[id]/messages",
        )

    @task(1)
    def list_files(self) -> None:
        """רשימת החוזים - Google Drive API + Postgres, היקרה מבין הבקשות."""
        self._get_json("/api/files", "files")

    @task(1)
    def health(self) -> None:
        """בדיקת חיים (כמו ה-Health Check של Render)."""
        self._get_json("/health", "status")

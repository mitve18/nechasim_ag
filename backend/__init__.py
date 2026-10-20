"""
חבילת ה-Backend של סוכן החוזים וההסכמים - יחידת נכסים.

החבילה כוללת:
- config.py   : טעינת הגדרות ומשתני סביבה.
- agent.py    : הגדרת הסוכן היחיד (Agno + gpt-4o).
- tools.py    : מחלקת הכלים היחידה של הסוכן (ContractsToolkit).
- main.py     : שרת FastAPI, נתיבי API והגשת הממשק.
- services/   : שירותי תשתית (Google Drive, RAG/PgVector).
"""

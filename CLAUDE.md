# CLAUDE.md — מערכת העץ הארגוני של ארקיע (arkia-org)

הקשר לכל מי (או מה) שנכנס לפתח כאן — כולל Claude Code בדפדפן.

## מה זה
מערכת ווב לבניית **עץ ארגוני שבונה את עצמו**: כל מנהל ממלא רק את הכפופים הישירים
לו, ולכל כפוף שהוא מנהל נוצר קישור-קסם אישי (token) לשליחה בוואטסאפ. העץ מתפשט
מלמעלה למטה עד העלים. עברית RTL. **הוצא כמודול עצמאי מ-`arkia-pricing`** (פרויקט אחות).

## סטאק ומבנה
- **Python 3.11 + FastAPI + SQLite + Jinja2**. פורט 8020 (מקומית).
- `app/main.py` — כל ה-routes וה-API (התחברות, אדמין, דף מילוי ציבורי, ייצוא).
- `app/services/org.py` — שכבת הנתונים של העץ (**הליבה** — יצירה/הוספה/עריכה/השחלה/מחיקה).
- `app/services/org_export.py` — ייצוא דף HTML עצמאי לשיתוף.
- `app/templates/` — `base.html`, `login.html`, `org_admin.html` (התרשים + עורך), `org_fill.html` (דף מנהל).
- `app/static/css/app.css` — סגנון בסיס. `app/auth.py` — התחברות. `app/db.py` — סכימת SQLite.
- `data/org.db` — ה-DB (לא במאגר; קיים רק בשרת/מקומית).

## איך זה רץ
- **מקומית:** `python -m uvicorn app.main:app --port 8020` (או `run.bat`). משתמשים מ-`ARKIA_USERS` או `users.txt`.

## מודל הנתונים
טבלה אחת `org_nodes`: עץ עם `parent_id` + `token` סודי לכל צומת, `is_manager`,
`status` (pending/filled), שם/תפקיד/טלפון/מחלקה. שורש = ראש מחלקה (`parent_id IS NULL`).

## אבטחה — לשים לב
- דף המילוי `/org/fill/{token}` ו-`/org/api/public/*` פתוחים **בלי התחברות** — הגישה
  מבוססת אך ורק על ה-`token` הסודי. הטוקן מגביל עריכה **לכפופים הישירים של אותו צומת בלבד**.
- כל שאר `/org/api/*` והמסך הראשי דורשים session (התחברות).
- **בייצוא ה-HTML לשיתוף לא נכללים טוקנים וטלפונים** — רק שמות ותפקידים.

## סודות — לא במאגר, לא לגעת
`.gitignore` מוציא: `data/` (DB + `secret_key.txt`), `users.txt`, `.env`
(`SECRET_KEY`, `SESSION_HTTPS_ONLY`, `ARKIA_USERS`). **אין להכניס סודות לקוד.**

## מודול החזרי הוצאות וריכוז אשראי (`/exp`)
מערכת שנייה באותה אפליקציה, אותו `look & feel`. עובד פותח דוח חודשי (החזר הוצאות
או ריכוז אשראי), מצלם חשבונית → OCR ממלא ספק+סכום → בוחר סיווג. "הפק טופס" מפיק
PDF ממותג ושולח למנהל מאשר בקישור-קסם (כמו העץ); ריכוז אשראי מרוכז ומיוצא ללא אישור.
כל דוח ניתן גם לשליחה חופשית למייל.
- **קבצים:** `app/services/expense.py` (שכבת הנתונים — דוחות/שורות/הגדרות),
  `ocr.py` (Google Vision דרך `urllib`, אופציונלי — נופל להזנה ידנית),
  `mailer.py` (SMTP+STARTTLS דרך OCI Email Delivery; ללא הגדרה נשמר ל-`data/outbox/`),
  `pdf.py` (fpdf2 + python-bidi, גופן `app/static/fonts/DejaVuSans*.ttf` — עמוד ריכוז
  ואז עמוד ממוספר לכל חשבונית), `passkey.py` (WebAuthn — **תלות אופציונלית**).
  Templates: `exp_home`, `exp_report`, `exp_approve`, `exp_admin`, `exp_account`.
- **טבלאות:** `exp_profiles` (מחלקה/מייל לכל משתמש), `exp_categories`, `exp_approvers`,
  `exp_departments`, `exp_reports`, `exp_lines`, `exp_webauthn`. סטטוסים:
  `draft→pending→approved/rejected` (החזר) / `compiled` (אשראי).
- **אבטחה:** מסך ההגדרות `/exp/admin` וכל `/exp/api/settings/*` — אדמין בלבד.
  מסך האישור `/exp/approve/{token}` ו-`/exp/api/public/*` — טוקן בלבד, בלי טוקנים/מיילים
  בתצוגה. שאר `/exp/api/*` דורש session, ומוגבל לבעל הדוח (או אדמין).
- **הגדרות ב-`.env`** (ראה `.env.example`): `APP_BASE_URL`, `SMTP_*`, `GOOGLE_VISION_API_KEY`,
  `WEBAUTHN_RP_ID/ORIGIN`. הכל אופציונלי — האפליקציה רצה גם בלי אף אחד מהם.

## פרויקט אחות
`rancohen22-eng/arkia-pricing` — מערכת התמחיר של ארקיע, שממנה הוצא המודול הזה.

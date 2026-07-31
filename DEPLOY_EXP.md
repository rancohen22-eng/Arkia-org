# פריסת שירות ההוצאות (arkia-exp) — מנותק מהעץ הארגוני

מודול החזרי ההוצאות רץ כ**שירות עצמאי**, על **אותו שרת** כמו העץ הארגוני והתמחיר,
בתיקייה נפרדת `/opt/arkia-exp`, כשירות systemd נפרד (`arkia-exp`) על פורט `8021`,
מאחורי Caddy (פורט ציבורי `8091`). יש לו **בסיס נתונים משלו** (`data/exp.db`)
ו**רשימת משתמשים משלו** — אין לו שום גישה לנתוני העץ הארגוני. אותו repo, קוד משותף,
נקודת כניסה נפרדת (`app.exp_app:app`) שמגישה רק את `/exp`.

> **דרישה מקדימה:** הקוד של מודול ההוצאות צריך להיות בענף שאותו תפרוס. אם עדיין לא
> מוזג ל-`main`, שכפל בשלב 1 את ענף הפיתוח (`-b claude/expense-reimbursement-system-aa0a79`),
> או מזג אותו ל-`main` תחילה. הפריסה האוטומטית (שלב 4) עובדת מ-`main`.

## 0. סודות ב-GitHub (חד-פעמי)
אין צורך בחדשים — שירות ההוצאות משתמש **באותם שלושה secrets** של העץ/התמחיר:
`SERVER_SSH_KEY`, `SERVER_HOST`, `SERVER_USER`.

## 1. הקמה חד-פעמית על השרת (SSH)
```bash
# תיקייה + קוד (repo פרטי — יבקש שם משתמש + Personal Access Token)
sudo mkdir -p /opt/arkia-exp && sudo chown $USER:$USER /opt/arkia-exp
git clone https://github.com/rancohen22-eng/Arkia-org.git /opt/arkia-exp
cd /opt/arkia-exp
# אם מודול ההוצאות עדיין לא ב-main, החלף לענף שמכיל אותו:
#   git checkout claude/expense-reimbursement-system-aa0a79

# סביבת פייתון (uv, כמו בשאר השירותים)
export PATH=$HOME/.local/bin:$PATH
uv venv .venv
VIRTUAL_ENV=/opt/arkia-exp/.venv uv pip install -r requirements.txt

# .env — מפתח סשן משלו + משתמשים משלו (נפרדים מהעץ!)
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # -> SECRET_KEY
nano .env    # SECRET_KEY=..., ARKIA_USERS=משתמשי-ההוצאות, ADMIN_USERS=..., SESSION_HTTPS_ONLY=0
             # (אין צורך לגעת ב-ARKIA_DB_PATH — יחידת ה-systemd קובעת data/exp.db)
             # אופציונלי: APP_BASE_URL=http://<PUBLIC_IP>:8091 לבניית לינקי אישור במיילים

# בעלות (כך ש-rsync של הפריסה וגם השירות יעבדו)
sudo chown -R ubuntu:ubuntu /opt/arkia-exp
```

> **משתמשים נפרדים:** ה-`ARKIA_USERS` (או `users.txt`) שכאן שייכים אך ורק לשירות
> ההוצאות. אפשר אותם משתמשים כמו במקומות אחרים או אחרים לגמרי — זו רשימה עצמאית.

## 2. שירות systemd
```bash
cd /opt/arkia-exp
sudo cp deploy/arkia-exp.service /etc/systemd/system/arkia-exp.service
sudo systemctl daemon-reload && sudo systemctl enable --now arkia-exp
systemctl is-active arkia-exp && curl -sS http://127.0.0.1:8021/health
# אפשר לפריסה האוטומטית להריץ restart בלי סיסמה:
echo "$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart arkia-exp" | sudo tee /etc/sudoers.d/arkia-exp
```
יחידת השירות כבר מגדירה `ARKIA_DB_PATH=/opt/arkia-exp/data/exp.db`, כך ש-DB ההוצאות
נוצר בתיקייה הזו ולעולם לא נוגע ב-`data/org.db` של העץ.

## 3. Caddy + פורט (אין דומיין → פורט נפרד 8091)
```bash
# הוסף את הבלוק מ-deploy/Caddyfile.exp.snippet לסוף /etc/caddy/Caddyfile:
sudo nano /etc/caddy/Caddyfile     # הדבק את בלוק ה-:8091
sudo systemctl reload caddy
# פתח פורט 8091 — ב-Oracle Security List (Ingress TCP 8091) וגם ב-VM:
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8091 -j ACCEPT
sudo netfilter-persistent save
```
אחרי זה שירות ההוצאות זמין ב: **http://\<PUBLIC_IP\>:8091** (עמוד הבית `/exp`).

> כשיהיה דומיין/סאב-דומיין — עדיף בלוק `expenses.example.com { reverse_proxy 127.0.0.1:8021 }`
> ב-Caddyfile (HTTPS אוטומטי) במקום הפורט. אז גם כדאי להגדיר `SESSION_HTTPS_ONLY=1`
> ו-`APP_BASE_URL=https://expenses.example.com` ב-`.env`.

## 4. פריסה אוטומטית
מרגע שההקמה החד-פעמית הושלמה, **כל דחיפה ל-`main`** מריצה את `.github/workflows/deploy-exp.yml`:
מסנכרן את הקוד ל-`/opt/arkia-exp`, מתקין תלויות ומריץ `systemctl restart arkia-exp`.
(עד שההקמה מבוצעת — שלב זה ייכשל, וזה צפוי.)

## סיכום ההפרדה בין השירותים
| | עץ ארגוני | הוצאות (מנותק) |
|---|---|---|
| נקודת כניסה | `app.main:app` | `app.exp_app:app` |
| תיקייה | `/opt/arkia-org` | `/opt/arkia-exp` |
| שירות systemd | `arkia-org` | `arkia-exp` |
| פורט פנימי | 8020 | 8021 |
| פורט Caddy ציבורי | 8090 | 8091 |
| בסיס נתונים | `data/org.db` | `data/exp.db` |
| משתמשים | `.env`/`users.txt` שלו | `.env`/`users.txt` שלו |
| עוגיית סשן | `arkia_org_session` | `arkia_exp_session` |

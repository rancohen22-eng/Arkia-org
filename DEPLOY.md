# פריסת arkia-org לאותו שרת Oracle (לצד התמחיר)

arkia-org רץ על **אותו שרת** כמו מערכת התמחיר, בתיקייה נפרדת `/opt/arkia-org`,
כשירות systemd נפרד (`arkia-org`) על פורט `8020`, מאחורי Caddy. הפריסה אוטומטית:
**כל דחיפה ל-`main` מפריסה לבד** דרך GitHub Actions.

## 0. סודות ב-GitHub (חד-פעמי)
ב-`Settings → Secrets and variables → Actions` של המאגר **Arkia-org**, הוסף את
אותם שלושה secrets שיש כבר בתמחיר (אותם ערכים):
`SERVER_SSH_KEY`, `SERVER_HOST`, `SERVER_USER`.

## 1. הקמה חד-פעמית על השרת (SSH)
```bash
# תיקייה + קוד (repo פרטי — יבקש שם משתמש + Personal Access Token)
sudo mkdir -p /opt/arkia-org && sudo chown $USER:$USER /opt/arkia-org
git clone https://github.com/rancohen22-eng/Arkia-org.git /opt/arkia-org
cd /opt/arkia-org

# סביבת פייתון (uv, כמו בתמחיר)
export PATH=$HOME/.local/bin:$PATH
uv venv .venv
VIRTUAL_ENV=/opt/arkia-org/.venv uv pip install -r requirements.txt

# .env — צור מפתח והדבק ב-SECRET_KEY; הגדר משתמש/סיסמה לאדמין
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # -> SECRET_KEY
nano .env          # SECRET_KEY=..., ARKIA_USERS=ranc:הסיסמה-שלך, SESSION_HTTPS_ONLY=0

# בעלות (כך ש-rsync של הפריסה וגם השירות יעבדו — התאם למשתמש שהתמחיר משתמש בו)
sudo chown -R ubuntu:ubuntu /opt/arkia-org
```

## 2. העברת העץ הקיים מהתמחיר (חד-פעמי, לפני הסרה מהתמחיר)
```bash
cd /opt/arkia-org
.venv/bin/python migrate_from_pricing.py            # יבש — מציג כמה צמתים
.venv/bin/python migrate_from_pricing.py --commit   # מעביר בפועל (שומר ids/טוקנים)
```

## 3. שירות systemd
```bash
sudo cp deploy/arkia-org.service /etc/systemd/system/arkia-org.service
sudo systemctl daemon-reload && sudo systemctl enable --now arkia-org
systemctl is-active arkia-org && curl -sS http://127.0.0.1:8020/health
# אפשר לפריסה האוטומטית להריץ restart בלי סיסמה:
echo "$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart arkia-org" | sudo tee /etc/sudoers.d/arkia-org
```

## 4. Caddy + פורט (אין דומיין → פורט נפרד)
```bash
# הוסף את הבלוק מ-deploy/Caddyfile.snippet לסוף /etc/caddy/Caddyfile:
sudo nano /etc/caddy/Caddyfile     # הדבק את בלוק ה-:8090
sudo systemctl reload caddy
# פתח פורט 8090 — ב-Oracle Security List (Ingress TCP 8090) וגם ב-VM:
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8090 -j ACCEPT
sudo netfilter-persistent save
```
אחרי זה המערכת זמינה ב: **http://\<PUBLIC_IP\>:8090**

> כשיהיה דומיין/סאב-דומיין — עדיף בלוק `org.example.com { reverse_proxy 127.0.0.1:8020 }`
> ב-Caddyfile (HTTPS אוטומטי) במקום הפורט.

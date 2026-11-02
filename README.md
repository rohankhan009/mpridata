# RC Lookup Telegram Bot — Hosting Guide

Ek hi file `rc_bot.py`. Koi web app / MongoDB nahi chahiye. Saara data ek local
SQLite file `rc_bot.db` mein save hota hai (admins, API config, user RC limits,
forward channel). Bas bot token do aur chala do.

Folder ke andar:
- `rc_bot.py`        — poora bot (single file)
- `requirements.txt` — sirf `requests`
- `Procfile`         — Railway/Heroku ke liye (`worker: python rc_bot.py`)
- `runtime.txt`      — python version hint
- `.env.example`     — env variables ka sample

## Env variables
| Variable | Zaroori? | Matlab |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ haan | @BotFather se mila token |
| `SUPER_ADMINS` | optional | permanent admin chat id(s), comma se. Default `8922842091` |
| `RELAY_PREFIX` | optional | `https://r.jina.ai/` — datacenter IP block bypass. Agar aapka server IP API ko allowed hai to **khaali** kar do (direct = bahut fast) |
| `MAX_WORKERS` | optional | jitna bada, utna fast. Default `6`. Bada VPS → `15`-`20` |
| `JINA_API_KEY` | optional | bade bulk ke liye relay ka rate limit badhane ko |
| `RC_DB` | optional | sqlite file path, default `rc_bot.db` |

---

## OPTION A — Railway (sabse easy, no server knowledge)
1. https://railway.app pe login (GitHub se).
2. Is `railway_bot` folder ko ek GitHub repo mein daalo (ya Railway pe "Deploy from local").
3. Railway → **New Project → Deploy from GitHub repo** → ye repo choose karo.
4. Railway khud `requirements.txt` install karega aur `Procfile` ka `worker` chala dega.
5. Project → **Variables** tab → add karo:
   - `TELEGRAM_BOT_TOKEN` = aapka token
   - (baaki optional; `RELAY_PREFIX=https://r.jina.ai/` rehne do)
6. Deploy hote hi bot live. Logs mein `Bot live: <username>` dikhega.

> ⚠️ Railway pe SQLite file redeploy pe reset ho sakti hai. Admins to `SUPER_ADMINS`
> se auto aa jaate hain, par user RC limits persist rakhne ke liye Railway ka
> **Volume** `/app` (ya jahan `rc_bot.db` banti hai) pe mount kar do, ya `RC_DB`
> ko volume path pe set karo (jaise `/data/rc_bot.db`).

---

## OPTION B — VPS (Ubuntu) — zyada control + fast
```bash
# 1. python + files
sudo apt update && sudo apt install -y python3 python3-pip
cd ~ && mkdir rcbot && cd rcbot
# (rc_bot.py aur requirements.txt yahan copy karo)

pip3 install -r requirements.txt

# 2. env set karo
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export RELAY_PREFIX="https://r.jina.ai/"   # IP allowed ho to hata do
export MAX_WORKERS="15"                      # bada VPS = zyada

# 3. test chalao
python3 rc_bot.py
```

### 24/7 chalane ke liye (systemd service)
`/etc/systemd/system/rcbot.service`:
```ini
[Unit]
Description=RC Lookup Bot
After=network.target

[Service]
WorkingDirectory=/root/rcbot
Environment=TELEGRAM_BOT_TOKEN=123456:ABC...
Environment=RELAY_PREFIX=https://r.jina.ai/
Environment=MAX_WORKERS=15
ExecStart=/usr/bin/python3 /root/rcbot/rc_bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rcbot
sudo journalctl -u rcbot -f     # logs
```

---

## Speed note (aapka sawaal)
- **Haan — bada VPS = fast.** `MAX_WORKERS` badha do (15-20). Har worker ek
  gaadi parallel mein check karta hai, to jitne zyada workers, utni jaldi
  numbers milenge.
- Sabse fast: agar aapke VPS ka IP API ko **allowed** ho, to `RELAY_PREFIX`
  khaali kar do (relay hat jayega, direct call = fastest). Agar direct pe
  "0 found" aaye to relay wapas laga do.

## Pehli baar setup (Telegram pe)
1. Bot pe `/admin` bhejo → aap admin ban jaoge (ya `SUPER_ADMINS` se auto).
2. `/apilist` → default **Paanel Shop** API dikhegi. Doosri API ke liye `/setapi`.
3. Kisi client ko unlock: `/setlimit <uska_chat_id> 500`.
4. Result file apne channel mein chahiye: bot ko channel mein admin banao,
   phir `/setchannel -100xxxxxxxxxx`.
5. Normal use: `/start` → gaadi series → kitne number chahiye.

---

## 🛠️ Bot reply nahi kar raha? (troubleshooting)
1. **Variables tab check karo** — `TELEGRAM_BOT_TOKEN` zaroor set hona chahiye.
   Yeh #1 wajah hai. Set karne ke baad **Redeploy**.
2. **Deploy Logs dekho** (service → Deployments → View Logs). Line dikhni chahiye:
   `Bot live: <username> | relay: ... | workers: ...`
   - Agar `FATAL: TELEGRAM_BOT_TOKEN env var is NOT set` → token daalo.
   - Agar `getUpdates not ok: 409 Conflict ...` → **same token kahin aur bhi chal
     raha hai** (purana deploy / aapka PC / doosra host). Baaki sab band karo —
     ek token sirf EK jagah chalega.
3. Token sahi hai? `getMe failed` aaye to @BotFather se token dubara copy karo.
4. `railway.json` is zip mein hai — Railway isse `python rc_bot.py` auto start
   karega. Phir bhi na chale to service → Settings → **Start Command** mein
   `python rc_bot.py` daal do.


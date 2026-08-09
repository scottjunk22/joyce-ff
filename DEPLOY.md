# Deploying stevejoyceff.com

This gets the league site live on the internet at your domain. Two paths —
**PythonAnywhere** (recommended: cheapest, SQLite-friendly, simplest) or
**Render**. You do the account/payment steps; everything else is prepared.

**What you need:** the domain (✅ Porkbun), a host account (~$5/mo), and the code
in a **GitHub repo** (private is fine) so the host can pull it.

> First, push this project to GitHub (once):
> ```bash
> gh repo create joyce-ff --private --source . --push
> ```
> or create a repo on github.com and `git remote add origin … && git push -u origin main`.

---

## Option A — PythonAnywhere (recommended)

1. **Sign up** at pythonanywhere.com and upgrade to the **"Hacker" $5/mo** plan
   (needed for a custom domain + a daily scheduled task).
2. **Get the code + deps.** Open a *Bash console*:
   ```bash
   git clone https://github.com/<you>/joyce-ff.git
   cd joyce-ff
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements-prod.txt
   ```
3. **Create the database** (real season, or the demo to show people):
   ```bash
   .venv/bin/python manage.py league-init      # real: 22 empty teams
   #   …or…  .venv/bin/python manage.py demo-seed   # populated demo
   ```
4. **Add the web app.** *Web* tab → *Add a new web app* → *Manual configuration*
   → *Python 3.12*. Then set:
   - **Source code:** `/home/<you>/joyce-ff`
   - **Virtualenv:** `/home/<you>/joyce-ff/.venv`
   - **WSGI configuration file** (click it, replace contents with):
     ```python
     import os, sys
     path = "/home/<you>/joyce-ff"
     if path not in sys.path: sys.path.insert(0, path)
     os.environ["JOYCE_DB_PATH"] = path + "/data/league.sqlite"
     os.environ["SECRET_KEY"] = "<paste a long random string>"
     from wsgi import app as application
     ```
5. **Reload** the web app (green button). It's now live at
   `https://<you>.pythonanywhere.com` — check that first.
6. **Custom domain.** *Web* tab → *Add a custom domain* → enter
   `www.stevejoyceff.com`. PythonAnywhere shows a **CNAME target** like
   `webapp-1234.pythonanywhere.com`. Then in **Porkbun → DNS**:
   - `CNAME`  host `www`  → the PythonAnywhere target
   - `ALIAS`  host `` (blank = apex)  → the same target *(Porkbun supports ALIAS
     at the apex; this makes stevejoyceff.com work too)*
   DNS takes a few minutes to an hour. Enable the free HTTPS in the Web tab.
7. **Weekly auto-scoring.** *Tasks* tab → add a **daily** task:
   ```bash
   cd /home/<you>/joyce-ff && .venv/bin/python manage.py run-current
   ```
   It scores each league week automatically once that week's NFL games finish.
   (Optional second daily task: `… manage.py sync` to cross-check your dad's site.)

---

## Option B — Render

1. **New → Web Service**, connect the GitHub repo.
   - **Build:** `pip install -r requirements-prod.txt`
   - **Start:** `gunicorn wsgi:app`
   - **Python version:** 3.12 (set env `PYTHON_VERSION=3.12.10`)
2. **Add a Disk** (Settings → Disks) mounted at `/data` so SQLite persists, and
   set env vars: `JOYCE_DB_PATH=/data/league.sqlite`, `SECRET_KEY=<random>`.
3. **Create the DB** — open the service *Shell*: `python manage.py demo-seed`
   (or `league-init`).
4. **Custom domain** (Settings → Custom Domains) → add `stevejoyceff.com` and
   `www`. Render gives you DNS records; add them in **Porkbun** (an `ALIAS`/`A`
   for the apex and a `CNAME` for `www`, exactly as Render specifies).
5. **Weekly auto-scoring:** add a **Render Cron Job** (daily) running
   `python manage.py run-current` against the same repo + disk.

---

## After it's live
- **Onboard teams:** as commissioner, set each team's `team_number`, `draft_slot`,
  `manager_names`, and passcode (a passcode-set admin screen is on the to-do
  list; until then set them in a console via `joyce_ff.league.auth`).
- **Set `SECRET_KEY`** to a long random value (done above) and keep it secret.
- **Back up** the SQLite file periodically — it's the whole league. On
  PythonAnywhere: a scheduled `cp data/league.sqlite data/backups/league-$(date +\%F).sqlite`.
- **Passcodes** are stored hashed; the site should be served over **HTTPS**
  (both hosts give free certs) so they're encrypted in transit.

## Local check before deploying
```bash
JOYCE_DB_PATH=data/league.sqlite ./.venv/Scripts/python.exe -m gunicorn wsgi:app   # (Linux/mac)
```
On Windows just use `python manage.py serve` — gunicorn is Linux-only.

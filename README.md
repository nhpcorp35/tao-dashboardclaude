# TAO Subnet Dashboard

A Flask-based dashboard that reads your Bittensor wallet's staked subnet positions via the Taostats API and displays live alpha prices, 7-day changes, and KPI signals.

## Files

```
tao-dashboard/
├── app.py              # Flask backend / API proxy
├── requirements.txt    # Python dependencies
├── Procfile            # Railway process definition
├── static/
│   └── index.html      # Dashboard frontend
└── README.md
```

## Deploy to Railway (recommended)

### 1. Push to GitHub
```bash
cd tao-dashboard
git init
git add .
git commit -m "initial commit"
# create a new repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/tao-dashboard.git
git push -u origin main
```

### 2. Deploy on Railway
1. Go to [railway.app](https://railway.app) and sign in with GitHub
2. Click **New Project → Deploy from GitHub repo**
3. Select your `tao-dashboard` repo
4. Railway auto-detects Python and uses the `Procfile`

### 3. Set environment variables in Railway
In your Railway project → **Variables**, add:

| Key | Value |
|-----|-------|
| `TAOSTATS_API_KEY` | your taostats API key (rotate after setup) |
| `COLDKEY` | `5Cexeg7deNSTzsqMKuBmvc9JHGHymuL4SdjAA9Jw4eeHUphb` |

> **Important:** Remove the hardcoded API key from `app.py` before pushing to GitHub — the env vars above will handle it.

### 4. Done
Railway gives you a public URL like `https://tao-dashboard-production.up.railway.app`

## Run locally (optional)
```bash
pip install -r requirements.txt
export TAOSTATS_API_KEY="your-key-here"
export COLDKEY="5Cexeg7deNSTzsqMKuBmvc9JHGHymuL4SdjAA9Jw4eeHUphb"
python app.py
# open http://localhost:5000
```

## API endpoints
- `GET /` — serves the dashboard
- `GET /api/wallet` — your staked positions from Taostats
- `GET /api/subnet/<netuid>` — alpha price + 7d change for a subnet
- `GET /api/subnets` — all subnet data

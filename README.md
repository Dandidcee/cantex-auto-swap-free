# Sojuu Bot — Cantex V2

Automated swap bot for [Cantex DEX](https://cantex.io) on the **Canton Network Mainnet**.

Supports multi-account trading, dynamic pair resolution, network fee guard, slippage protection, and a daily **free swap window** that auto-executes at 00:00 UTC (07:00 WIB).

---

## Requirements

- Python **3.11+**
- `cantex_sdk` (private package — provided separately)
- API key file(s) for each account

---

## Installation

### 1. Clone / Download the project

```bash
git clone <repo-url>
cd Cantex_Sojuu_V2
```

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it:

- **Windows:**
  ```powershell
  .venv\Scripts\activate
  ```
- **Linux / macOS:**
  ```bash
  source .venv/bin/activate
  ```

### 3. Install dependencies

```bash
pip install flask flask-cors
pip install cantex_sdk   # install from the provided wheel or source
```

### 4. Place API key files

Each account needs its own API key file inside the `secrets/` folder:

```
secrets/
  api_key_Account1.txt
  api_key_Account2.txt
  ...
```

The filename format is: `api_key_<account_name>.txt`

---

## First-Time Setup

Run the bot for the first time to create admin credentials:

```bash
python server.py
```

You will be prompted to set a **username** and **password** for the web dashboard. Credentials are saved to `auth.json` and never need to be entered again.

---

## Running the Bot

```bash
python server.py
```

The web dashboard is available at:

```
http://localhost:5001
```

---

## Project Structure

```
Cantex_Sojuu_V2/
├── server.py          # Main bot engine + Flask API
├── home.html          # Web dashboard frontend
├── config.json        # Bot settings (auto-generated)
├── accounts.json      # Saved accounts (auto-generated)
├── auth.json          # Admin credentials (auto-generated on first run)
├── swap_log.jsonl     # Swap history log (append-only)
└── secrets/
    └── api_key_<name>.txt   # One file per account
```

---

## Configuration

Settings are managed from the **Settings tab** in the web dashboard. All values are saved to `config.json`.

| Key | Default | Description |
|-----|---------|-------------|
| `base_url` | `https://api.cantex.io` | Cantex API endpoint |
| `swap_amount_a` | `100.0` | Amount of token A to sell per swap |
| `use_full_b` | `true` | Use full token B balance for swap back |
| `swap_amount_b` | `100.0` | Fixed token B amount (if `use_full_b` is false) |
| `max_network_fee` | `0.15` | Max allowed network fee before waiting |
| `fee_check_interval` | `5` | Seconds between fee re-checks when fee is too high |
| `delay_a_to_b` | `30` | Seconds to wait between A→B and B→A swap |
| `delay_per_loop` | `600` | Seconds to wait at the end of each full round |
| `max_slippage` | `0.05` | Max allowed slippage (5%) |
| `confirm_timeout` | `120.0` | Seconds to wait for swap confirmation |
| `free_swap_enabled` | `true` | Enable the daily free swap window |
| `free_swap_count` | `3` | Number of free rounds to run at 00:00 UTC |

---

## Supported Trading Pairs

| Label | Token A | Token B |
|-------|---------|---------|
| `CC/USDCx` | Amulet (CC) | USDCx |
| `cBTC/USDCx` | cBTC | USDCx |

---

## Adding Accounts

From the dashboard → **Accounts** tab → **Add Account**, or via API:

```bash
curl -X POST http://localhost:5001/api/accounts/add \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Account1",
    "operator_key": "YOUR_OPERATOR_HEX_KEY",
    "trading_key": "YOUR_TRADING_HEX_KEY",
    "pair": "CC/USDCx"
  }'
```

---

## Fee Guard Behavior

When the network fee exceeds `max_network_fee`:

1. The account enters **`waiting_fee`** status
2. The bot polls for a new quote every `fee_check_interval` seconds
3. A random **0–8 second jitter** is applied per account to avoid all accounts hitting the API simultaneously
4. Once fee drops below threshold, the swap proceeds

---

## Free Swap Window

Cantex resets **3 free swaps** every day at **00:00 UTC (07:00 WIB)**.

When the bot detects a new UTC day:
- It immediately executes `free_swap_count` full rounds (A→B then B→A) **without fee checking**
- Each free swap is logged with `"note": "FREE_SWAP"` in `swap_log.jsonl`
- After all free rounds complete, the normal loop resumes

---

## Demo Mode

If `cantex_sdk` is not installed, the bot runs in **Demo Mode** — simulating swap activity with fake balances. The dashboard is fully functional and can be used to test the UI.

---

## API Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `POST` | `/api/login` | — | Login and get session cookie |
| `POST` | `/api/logout` | ✓ | Logout |
| `GET` | `/api/auth-status` | — | Check login status |
| `GET` | `/api/settings` | — | Get current config |
| `POST` | `/api/settings` | ✓ | Update config |
| `GET` | `/api/accounts` | — | List all accounts |
| `POST` | `/api/accounts/add` | ✓ | Add a new account |
| `POST` | `/api/accounts/remove` | ✓ | Remove an account |
| `POST` | `/api/bot/start` | ✓ | Start all bots |
| `POST` | `/api/bot/stop` | ✓ | Stop all bots |
| `POST` | `/api/bot/restart` | ✓ | Restart all bots |
| `POST` | `/api/bot/pause` | ✓ | Pause account(s) |
| `POST` | `/api/bot/resume` | ✓ | Resume account(s) |
| `GET` | `/api/bot/status` | — | Bot running status |
| `GET` | `/api/swaplogs` | — | Paginated swap history |
| `GET` | `/api/stats` | — | Global stats |
| `GET` | `/api/uptime` | — | Server uptime |
| `GET` | `/api/pairs` | — | Supported pairs |
| `GET` | `/api/stream` | — | SSE live updates |

---

## Logs

All swaps are appended to `swap_log.jsonl` as JSON lines:

```jsonc
{
  "acc": "Account1",
  "pair": "CC/USDCx",
  "action": "Amulet→USDCx",
  "amount": "100",
  "out": "14.52",
  "fee": "0.12",
  "price": "0.145",
  "status": "SUCCESS",
  "note": "FREE_SWAP",   // only on free swaps
  "utc": "2026-04-11T00:00:05.123456+00:00"
}
```

---

## Notes

- The bot uses **intent-based trading** only. `create_trading_account()` is decommissioned by Cantex.
- Session TTL is **8 hours**. After expiry, you will be redirected to the login page.
- Operator and trading keys are stored locally in `accounts.json`. Keep this file secure.

---

© 2026 Sojuu Community

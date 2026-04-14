# mcscrap

Automatic voting bot for Czech/Slovak Minecraft server listing sites.

**Supported sites:** MinecraftServery, CzechCraft, MinecraftList, CraftList

## How it works

Each run has two phases:

1. **Check** — lightweight HTTP/API call per site to see if voting is available
2. **Vote** — shared Chromium browser with NopeCHA extension solves captchas automatically

The main loop runs every 2 hours indefinitely.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Python 3.14 (uv handles this automatically)
- Xvfb — for running the browser invisibly without a real display
- A [NopeCHA](https://nopecha.com/) API key

> **Windows users:** use WSL. Xvfb is Linux-only and required for headless operation.

### Install uv

**Linux/WSL/macOS:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows:**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

After installation, restart your terminal so `uv` is available on `PATH`.

### Install Xvfb (Debian/Ubuntu/WSL)

```bash
sudo apt install xvfb
```

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/mrkvic/mcscrap.git
cd mcscrap
```

### 2. Install dependencies

```bash
uv sync
```

### 3. Install Playwright's Chromium browser

```bash
uv run patchright install chromium
```

### 4. Install the NopeCHA extension

Download and unpack the [NopeCHA Chrome extension](https://github.com/nopecha/nopecha-chrome) into:

```
extensions/nopecha/
```

The directory must contain `manifest.json`.

### 5. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

```env
NOPECHA_API_KEY=your_key_here   # NopeCHA API key
NICK=YourNickHere               # Your Minecraft username
DEBUG=false                     # Set to true to show the browser on your real display
CAPTCHA_TIMEOUT_MS=30000        # How long to wait for captcha solve (ms), default 30000
HTTP_TIMEOUT_S=15               # HTTP request timeout (seconds), default 15
```

Also update the server slugs in the `sites` list in `main.py` if you're voting for a different server.

## Running

```bash
uv run python main.py
```

The bot runs in an infinite loop, voting every 2 hours.

## Debug mode

Set `DEBUG=true` in `.env` to open the browser on your real display instead of Xvfb.
Useful for seeing what's happening when something breaks.

# Kotak Neo Algo Dashboard

A high-performance Desktop Trading Dashboard for the Kotak Neo platform, built with Python and PySide6 (Qt). This application provides a robust user interface for algorithmic and manual trading, offering real-time data visualization, order management, and strategy execution.

## Features

* **Modern GUI**: Dark-themed, responsive user interface using PySide6.
* **Authentication**: Secure login via TOTP (Time-based One-Time Password) with support for multiple environments (Prod, Stg, Dev).
* **Watchlist**: Real-time tracking of symbols across multiple exchanges (NSE, BSE, MCX) with segments (CM, FO).
* **Scrip Search**: Advanced symbol search with segment filtering and option chain details (Expiry, Strike, Option Type).
* **Order Management**:
  * Place Orders (Limit, Market, SL, SL-M).
  * Product types: NRML, MIS, CNC, CO, BO.
  * **Quick Order Window**: Floating "Always-on-Top" window for rapid order entry.
  * **Load Lot Size**: Helper to fetch and auto-fill valid lot sizes for F&O contracts.
* **Order Book & Positions**: View real-time order status and open positions with P&L.

## Prerequisites

* **Python 3.10+**
* **Kotak Neo API Account**: You need a valid account and Consumer Key from the Kotak Neo API portal.

## Installation

1. **Clone the repository**:

    ```bash
    git clone <repository-url>
    cd Kotak_Algo_Dashboard
    ```

2. **Create a Virtual Environment** (Recommended):

    ```bash
    python -m venv .venv
    # Windows
    .\.venv\Scripts\activate
    # Linux/Mac
    source .venv/bin/activate
    ```

3. **Install Dependencies**:

    ```bash
    pip install -r requirements.txt
    ```

    *Note: The `neo_api_client` is installed directly from the official GitHub repository.*

## Configuration

The application stores local configuration (like your Consumer Key and Environment) in a `config.json` file. This file is auto-created/updated when you login.

To pre-configure, you can create a `config.json` in the root directory:

```json
{
    "consumer_key": "YOUR_CONSUMER_KEY_HERE",
    "environment": "prod",
    "mobile": "+919876543210",
    "ucc": "YOUR_UCC",
    "mpin": "YOUR_MPIN"
}
```

> [!IMPORTANT]
> **Phone Number Format**: The `mobile` field must be in the format `+919XXXXXXXXX`. It must include the `+91` country code.

```

**Note:** `config.json` is ignored by Git to protect your credentials.

## Usage

### 1. Running the Application
Run the main script:
```bash
python kotak_dahboard.py
```

*(Note: filename is `kotak_dahboard.py` currently)*

### 2. Authentication

1. Go to the **Authentication** tab.
2. Enter your **Consumer Key**, **Mobile Number** (Format: `+919XXXXXXXXX`), **UCC** (User Id), and **TOTP** (from an authenticator app like Google Authenticator).
3. Click **Verify TOTP**.
4. Once verified, enter your **MPIN** and click **Login**.

### 3. Trading

* **Watchlist**: Add symbols to track real-time prices.
* **Place Order**: Use the main "Place Order" tab for detailed order entry.
* **Quick Order**: Click the "Quick Order" button in the top bar to open a small, floating window for fast execution while monitoring other screens.

## Directory Structure

* `app/`: Core application logic (API wrappers, reusable components).
* `resources/`: UI resources (themes, icons).
* `kotak_dahboard.py`: Main entry point for the application.

## Disclaimer

**Risk Warning**: Trading in financial markets involves significant risk and may not be suitable for all investors. This software is provided for educational and automated trading purposes. The developers are not responsible for any financial losses incurred while using this software. Always test strategies in a controlled environment before trading with real capital.

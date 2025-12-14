"""
app/api_client.py

Thin wrapper around the Kotak Neo SDK (neo_api_client).  The wrapper
normalizes responses and stores tokens where needed.

Notes:
- The real SDK's method names may vary slightly; this wrapper
  calls the most common method names as seen in the SDK README.
- Adjust method names if your installed SDK uses different names.
"""

import traceback
import contextlib
import io

# import the real SDK if available; otherwise provide helpful error messages
try:
    from neo_api_client import NeoAPI
except Exception as e:
    NeoAPI = None  # Caller should catch this and inform user


class NeoWrapper:
    """
    Simple wrapper for neo_api_client.NeoAPI.
    - The constructor expects an already-created NeoAPI instance or kwargs to create one.
    """

    def __init__(self, client: "NeoAPI" = None, *, environment=None, consumer_key=None):
        if client is not None:
            self.client = client
        else:
            if NeoAPI is None:
                raise RuntimeError(
                    "neo_api_client package not found. Install the SDK: "
                    "pip install git+https://github.com/Kotak-Neo/Kotak-neo-api-v2"
                )
            # create a client with minimal args - adapt if your SDK requires different args
            self.client = NeoAPI(environment=(environment or "prod"),
                                  access_token=None,
                                  neo_fin_key=None,
                                  consumer_key=consumer_key)

        # tokens and simple state
        self.trade_token = None
        self.session_token = None

    # --- Authentication helpers ---
    def totp_login(self, mobile_number: str, ucc: str, totp: str):
        """
        Call SDK totp_login (uses TOTP from authenticator app).
        Returns SDK response or raises exception on failure.
        """
        try:
            # totp_login requires mobile, ucc, and totp
            resp = self.client.totp_login(mobile_number=mobile_number, ucc=ucc, totp=totp)
            
            # Verify if view_token was set
            if not self.client.configuration.view_token:
                raise RuntimeError(f"Login failed (No view_token). Response: {resp}")
                
            return resp
        except Exception as e:
            raise RuntimeError(f"totp_login failed: {e}")

    def totp_validate(self, mpin: str):
        """
        Validate TOTP (actually MPIN) and store trade/session token if returned.
        """
        try:
            # totp_validate only takes mpin
            resp = self.client.totp_validate(mpin=mpin)
            
            # Verify if edit_token (session token) was set
            if not self.client.configuration.edit_token:
                raise RuntimeError(f"Validation failed (No edit_token). Response: {resp}")

            # common keys used by SDKs — adjust according to actual SDK response
            self.trade_token = resp.get("trade_token") or resp.get("session_token") or self.trade_token
            self.session_token = resp.get("session_token") or self.session_token
            return resp
        except Exception as e:
            # include traceback to help debugging
            tb = traceback.format_exc()
            raise RuntimeError(f"totp_validate failed: {e}\n{tb}")

    # --- Market / Master helpers ---
    def search_scrip(self, exchange_segment: str, symbol: str):
        """
        Search master scrips.
        SDK signature: search_scrip(exchange_segment, symbol=..., ...)
        """
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return self.client.search_scrip(exchange_segment=exchange_segment, symbol=symbol)
        except Exception as e:
            raise RuntimeError(f"search_scrip failed: {e}")

    def scrip_master(self, exchange_segment: str):
        """
        Get full scrip master.
        """
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return self.client.scrip_master(exchange_segment=exchange_segment)
        except Exception as e:
            raise RuntimeError(f"scrip_master failed: {e}")

    def get_quote(self, instrument_tokens: list, quote_type: str = None):
        """
        Get quotes. SDK uses 'quotes' method and expects instrument_tokens.
        """
        try:
            # SDK signature: quotes(instrument_tokens=..., quote_type=...)
            # instrument_tokens should be a list of dicts: [{'instrument_token': '...', 'exchange_segment': '...'}]
            # OR just a list of tokens? The SDK docstring says "List", but usually it's a list of dicts for multi-exchange.
            # Let's assume the caller passes what the SDK needs.
            return self.client.quotes(instrument_tokens=instrument_tokens, quote_type=quote_type)
        except Exception as e:
            raise RuntimeError(f"get_quote(s) failed: {e}")

    def get_limits(self, segment="ALL", exchange="ALL", product="ALL"):
        """
        Get limits/margins.
        """
        try:
            return self.client.limits(segment=segment, exchange=exchange, product=product)
        except Exception as e:
            raise RuntimeError(f"get_limits failed: {e}")

    # --- Orders / Trading ---
    def place_order(self, **kwargs):
        """
        Place an order. kwargs should follow SDK expected keys:
        e.g. exchange_segment, trading_symbol, transaction_type, product,
              order_type, quantity, price, trigger_price, amo, validity
        """
        try:
            if hasattr(self.client, "place_order"):
                return self.client.place_order(**kwargs)
            # some SDKs require different method name
            if hasattr(self.client, "order_place") or hasattr(self.client, "order"):
                # try common alternatives
                if hasattr(self.client, "order_place"):
                    return self.client.order_place(**kwargs)
                return self.client.order(**kwargs)
            raise RuntimeError("SDK has no place_order method.")
        except Exception as e:
            raise RuntimeError(f"place_order failed: {e}")

    def cancel_order(self, order_id: str, **kwargs):
        """
        Cancel an order by ID.
        """
        try:
            if hasattr(self.client, "cancel_order"):
                return self.client.cancel_order(order_id=order_id, **kwargs)
            if hasattr(self.client, "order_cancel"):
                return self.client.order_cancel(order_id=order_id, **kwargs)
            raise RuntimeError("SDK has no cancel_order method.")
        except Exception as e:
            raise RuntimeError(f"cancel_order failed: {e}")

    def modify_order(self, order_id: str, **kwargs):
        """
        Modify an order.
        """
        try:
            if hasattr(self.client, "modify_order"):
                return self.client.modify_order(order_id=order_id, **kwargs)
            if hasattr(self.client, "order_modify"):
                return self.client.order_modify(order_id=order_id, **kwargs)
            raise RuntimeError("SDK has no modify_order method.")
        except Exception as e:
            raise RuntimeError(f"modify_order failed: {e}")

    def get_orders(self, **kwargs):
        """
        Return orders / order book. SDK uses 'order_report'.
        """
        try:
            return self.client.order_report()
        except Exception as e:
            raise RuntimeError(f"get_orders failed: {e}")

    def get_positions(self):
        """
        Return positions/holdings. SDK uses 'positions'.
        """
        try:
            return self.client.positions()
        except Exception as e:
            raise RuntimeError(f"get_positions failed: {e}")

    def get_margin(self):
        """
        Return margin/funds info.
        """
        try:
            if hasattr(self.client, "get_margin"):
                return self.client.get_margin()
            if hasattr(self.client, "margin"):
                return self.client.margin()
            raise RuntimeError("SDK has no get_margin method.")
        except Exception as e:
            raise RuntimeError(f"get_margin failed: {e}")

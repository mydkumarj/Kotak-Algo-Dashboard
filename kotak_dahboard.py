# app/main.py
import sys
import threading
import json
import time
import requests
import csv
import io
import contextlib
from pathlib import Path
from functools import partial

from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QListWidget, QTabWidget,
    QFormLayout, QComboBox, QSpinBox, QCheckBox, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QDialog, QDialogButtonBox, QCompleter, QDoubleSpinBox, QGroupBox
)
from PySide6.QtGui import QIcon, QColor, QFont
from PySide6.QtCore import Qt, Slot, QStringListModel, QTimer, Signal, QObject

# SDK import (from their repo)
try:
    from neo_api_client import NeoAPI
except Exception as e:
    NeoAPI = None

from app.config import ConfigManager
from app.api_client import NeoWrapper


if getattr(sys, 'frozen', False):
    # Running as compiled exe
    APP_DIR = Path(sys._MEIPASS)
else:
    # Running as script
    APP_DIR = Path(__file__).resolve().parent

class WorkerSignals(QObject):
    market_data_received = Signal(dict)
    login_success = Signal()
    status_update = Signal(str)
    search_completed = Signal(object, str, str) # result, input_text, exchange

class FloatingOrderWindow(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Quick Order")
        self.resize(280, 350)
        
        # Apply theme from main window if possible
        if self.main_window.styleSheet():
            self.setStyleSheet(self.main_window.styleSheet())
            
        self._build_ui()
        
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(5)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Symbol Search
        self.symbol_edit = QLineEdit()
        self.symbol_edit.setPlaceholderText("Search Symbol (e.g. NIFTY)")
        layout.addWidget(QLabel("Symbol:"))
        layout.addWidget(self.symbol_edit)
        
        # Completer
        self.completer = QCompleter(self)
        self.model = QStringListModel()
        self.completer.setModel(self.model)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchContains)
        self.symbol_edit.setCompleter(self.completer)
        self.symbol_edit.textEdited.connect(self._on_symbol_edit)
        self.completer.activated.connect(self._on_symbol_selected)
        
        # Exchange & Product
        row1 = QHBoxLayout()
        self.exchange = QComboBox()
        self.exchange.addItems(["nse_fo", "nse_cm", "bse_fo", "bse_cm", "mcx_fo"])
        self.product = QComboBox()
        self.product.addItems(["NRML", "MIS", "CNC"])
        row1.addWidget(self.exchange)
        row1.addWidget(self.product)
        layout.addLayout(row1)
        
        # Type & Transaction
        row2 = QHBoxLayout()
        self.order_type = QComboBox()
        self.order_type.addItems(["L", "MKT", "SL", "SL-M"])
        self.trans_type = QComboBox()
        self.trans_type.addItems(["B", "S"])
        row2.addWidget(self.order_type)
        row2.addWidget(self.trans_type)
        layout.addLayout(row2)
        
        # Quantity
        qty_layout = QHBoxLayout()
        self.qty = QSpinBox()
        self.qty.setRange(1, 1000000)
        # Removed fixed width to match column width of previous row
        
        self.load_lot_btn = QPushButton("Load Lot")
        # Removed fixed width to match column width of previous row
        self.load_lot_btn.clicked.connect(self._manual_load_lot)
        
        qty_layout.addWidget(self.qty)
        qty_layout.addWidget(self.load_lot_btn)
        
        layout.addWidget(QLabel("Quantity:"))
        layout.addLayout(qty_layout)
        
        # Price
        self.price = QLineEdit()
        self.price.setPlaceholderText("Price (0 for MKT)")
        layout.addWidget(QLabel("Price:"))
        layout.addWidget(self.price)
        
        # Trigger
        self.trigger = QLineEdit()
        self.trigger.setPlaceholderText("Trigger Price")
        layout.addWidget(QLabel("Trigger:"))
        layout.addWidget(self.trigger)
        
        # Place Button
        self.place_btn = QPushButton("PLACE ORDER")
        self.place_btn.setStyleSheet("background-color: #007bff; color: white; font-weight: bold; padding: 5px;")
        self.place_btn.clicked.connect(self.on_place_order)
        layout.addWidget(self.place_btn)
        
        # Update color on transaction type change
        self.trans_type.currentTextChanged.connect(self._update_btn_color)
        
        layout.addStretch()
        
    def _update_btn_color(self, text):
        if text == "B":
            self.place_btn.setStyleSheet("background-color: #007bff; color: white; font-weight: bold; padding: 5px;")
        else:
            self.place_btn.setStyleSheet("background-color: #ff4444; color: white; font-weight: bold; padding: 5px;")

    def _on_symbol_edit(self, text):
        if len(text) < 2: return
        # Reuse Main Window Search Logic
        # We pass our own model/completer/widget to populate our dropdown
        self.main_window._do_symbol_search(text, target_model=self.model, target_completer=self.completer, source_widget=self.symbol_edit)

    def _on_symbol_selected(self, text):
        self._check_and_load_lot(text)

    def _manual_load_lot(self):
        self._check_and_load_lot(self.symbol_edit.text(), force=True)

    def _check_and_load_lot(self, text, force=False):
        if not text: return
        ex_seg = self.exchange.currentText()
        qty = self.main_window._get_lot_size_value(ex_seg, text)
        
        if qty > 1:
            self.qty.setValue(qty)
            self.main_window._set_status(f"Quick Order: Lot size set to {qty} for {text}")
        else:
            # Not found
            if force or (not hasattr(self.main_window, "lot_size_cache") or ex_seg not in self.main_window.lot_size_cache):
                 # Trigger load
                 self.main_window._set_status(f"Loading master for {ex_seg}...")
                 threading.Thread(target=self.main_window._load_master, args=(ex_seg,), daemon=True).start()

    def on_place_order(self):
        if not self.main_window.wrapper:
            QMessageBox.warning(self, "Error", "Login in Main Window first.")
            return
            
        sym = self.symbol_edit.text().strip()
        if not sym:
            QMessageBox.warning(self, "Error", "Symbol required.")
            return
            
        # Auto-correct lot size if 1 (Safety)
        if self.qty.value() == 1:
             ex_seg = self.exchange.currentText()
             lot = self.main_window._get_lot_size_value(ex_seg, sym)
             if lot > 1: self.qty.setValue(lot)

        try:
            res = self.main_window.wrapper.place_order(
                exchange_segment=self.exchange.currentText(),
                product=self.product.currentText(),
                price=self.price.text().strip() or "0",
                order_type=self.order_type.currentText(),
                quantity=str(self.qty.value()),
                validity="DAY",
                trading_symbol=sym,
                transaction_type=self.trans_type.currentText(),
                amo="NO",
                trigger_price=self.trigger.text().strip() or "0"
            )
            QMessageBox.information(self, "Order Placed", str(res))
            self.main_window._set_status(f"Quick Order: {res}")
        except Exception as e:
            QMessageBox.critical(self, "Failed", str(e))

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kotak Neo — Trading Dashboard")
        self.setMinimumSize(1000, 700)
        
        self.signals = WorkerSignals()
        self.signals.market_data_received.connect(self.update_watchlist_item)
        self.signals.login_success.connect(self.on_login_success)
        self.signals.status_update.connect(self._set_status)
        self.signals.search_completed.connect(self.on_search_completed)

        self.config = ConfigManager(APP_DIR / "config.json")
        self.neo = None
        self.wrapper = None

        
        self.watchlist_data = [] # List of dicts: {symbol, token, segment, exchange}
        self.token_row_map = {} # Map token -> row index for fast updates

        self._build_ui()
        self._apply_saved_config()
        self._load_watchlist()
        
        # Default to Dark Theme
        qss = (APP_DIR / "resources" / "themes" / "dark.qss")
        if qss.exists():
            self.setStyleSheet(qss.read_text())

    def _build_ui(self):
        # central widget + tabs
        central = QWidget()
        self.setCentralWidget(central)
        v = QVBoxLayout()
        central.setLayout(v)

        # top-row: status + theme toggle
        top = QHBoxLayout()
        self.status_label = QLabel("Not connected")
        top.addWidget(self.status_label)
        top.addStretch()
        self.theme_btn = QPushButton("Toggle Theme")
        self.theme_btn.clicked.connect(self.toggle_theme)
        top.addWidget(self.theme_btn)
        
        # Load Lot Size Button
        self.load_lots_btn = QPushButton("Load Lot Size")
        self.load_lots_btn.setToolTip("Load lot sizes for NSE_FO, BSE_FO, MCX_FO")
        self.load_lots_btn.clicked.connect(self.load_all_lots)
        top.addWidget(self.load_lots_btn)
        
        # Quick Order Button
        self.quick_order_btn = QPushButton("Quick Order")
        self.quick_order_btn.setStyleSheet("background-color: #28a745; color: white; font-weight: bold;")
        self.quick_order_btn.clicked.connect(self.open_floating_window)
        top.addWidget(self.quick_order_btn)
        
        v.addLayout(top)

        # tabs
        self.tabs = QTabWidget()
        v.addWidget(self.tabs)

        self._build_auth_tab()
        self._build_watchlist_tab()
        self._build_quotes_tab()
        self._build_order_tab()
        self._build_orders_tab()
        self._build_positions_tab()
        self._build_funds_tab()

        self._build_logs_tab()

    def _build_auth_tab(self):
        t = QWidget(); layout = QFormLayout(); t.setLayout(layout)
        self.consumer_key_edit = QLineEdit()
        self.env_combo = QComboBox()
        self.env_combo.addItems(["prod", "stg", "dev"])
        self.mobile_edit = QLineEdit()
        self.ucc_edit = QLineEdit()
        self.totp_edit = QLineEdit()
        self.mpin_edit = QLineEdit()
        self.totp_btn = QPushButton("Verify TOTP (Step 1)")
        self.totp_btn.clicked.connect(self.on_totp_login)
        self.totp_validate_btn = QPushButton("Login (Validate TOTP)")
        self.totp_validate_btn.clicked.connect(self.on_totp_validate)

        layout.addRow("Consumer Key:", self.consumer_key_edit)
        layout.addRow("Environment:", self.env_combo)
        layout.addRow("Mobile (+91...):", self.mobile_edit)
        layout.addRow("UCC:", self.ucc_edit)
        layout.addRow("TOTP (from app):", self.totp_edit)
        layout.addRow(self.totp_btn, None)
        layout.addRow("MPIN (for final):", self.mpin_edit)
        layout.addRow(self.totp_validate_btn, None)

        self.tabs.addTab(t, "Authentication")

    def _build_watchlist_tab(self):
        t = QWidget(); layout = QVBoxLayout(); t.setLayout(layout)
        
        # Input Row
        input_row = QHBoxLayout()
        self.wl_exchange = QComboBox()
        self.wl_exchange.addItems(["nse_cm", "bse_cm", "nse_fo", "bse_fo", "mcx_fo", "cde_fo"])
        
        self.wl_symbol = QLineEdit()
        self.wl_symbol.setPlaceholderText("Search Symbol")
        # Reuse completer logic if possible, or create new one
        self.wl_completer = QCompleter(self)
        self.wl_model = QStringListModel()
        self.wl_completer.setModel(self.wl_model)
        self.wl_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.wl_completer.setFilterMode(Qt.MatchContains)
        self.wl_symbol.setCompleter(self.wl_completer)
        self.wl_symbol.textEdited.connect(self._on_wl_symbol_edit)
        
        self.wl_add_btn = QPushButton("Add to Watchlist")
        self.wl_add_btn.clicked.connect(self.add_to_watchlist)
        
        input_row.addWidget(self.wl_exchange)
        input_row.addWidget(self.wl_symbol)
        input_row.addWidget(self.wl_add_btn)
        layout.addLayout(input_row)
        
        # Table
        self.wl_table = QTableWidget()
        self.wl_table.setColumnCount(6)
        self.wl_table.setHorizontalHeaderLabels(["Symbol", "LTP", "Change", "% Change", "Segment", "Token"])
        self.wl_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.wl_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.wl_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.wl_table)
        
        # Actions
        action_row = QHBoxLayout()
        self.wl_refresh_btn = QPushButton("Refresh Watchlist")
        self.wl_refresh_btn.clicked.connect(self.refresh_watchlist)
        self.wl_remove_btn = QPushButton("Remove Selected")
        self.wl_remove_btn.clicked.connect(self.remove_from_watchlist)
        
        action_row.addWidget(self.wl_refresh_btn)
        action_row.addWidget(self.wl_remove_btn)
        layout.addLayout(action_row)
        
        self.tabs.addTab(t, "Watchlist")

    def _build_quotes_tab(self):
        t = QWidget(); layout = QVBoxLayout(); t.setLayout(layout)
        search_row = QHBoxLayout()
        self.scrip_exchange = QComboBox()
        self.scrip_exchange.addItems(["nse_cm", "bse_cm", "nse_fo", "bse_fo", "mcx_fo", "cde_fo"])
        self.scrip_search = QLineEdit()
        self.scrip_search.setPlaceholderText("Symbol (e.g. RELIANCE)")
        self.search_btn = QPushButton("Find scrip")
        self.search_btn.clicked.connect(self.search_scrip)
        search_row.addWidget(self.scrip_exchange)
        search_row.addWidget(self.scrip_search)
        search_row.addWidget(self.search_btn)
        layout.addLayout(search_row)
        self.scrip_results = QTableWidget()
        self.scrip_results.setColumnCount(6)
        self.scrip_results.setHorizontalHeaderLabels(["Symbol", "Segment", "Token", "Expiry", "Strike", "Option Type"])
        self.scrip_results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.scrip_results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.scrip_results.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.scrip_results)
        data_row = QHBoxLayout()
        self.quote_btn = QPushButton("Get Quote (get_quotes)")
        self.quote_btn.clicked.connect(self.get_quote)
        data_row.addWidget(self.quote_btn)
        self.tabs.addTab(t, "Scrip / Quotes")
        t.layout().addWidget(self.scrip_results)
        t.layout().addLayout(data_row)

    def _build_order_tab(self):
        t = QWidget(); layout = QFormLayout(); t.setLayout(layout)
        self.trading_symbol = QLineEdit()
        
        # Autocomplete setup
        self.symbol_completer = QCompleter(self)
        self.symbol_model = QStringListModel()
        self.symbol_completer.setModel(self.symbol_model)
        self.symbol_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.symbol_completer.setFilterMode(Qt.MatchContains)
        self.trading_symbol.setCompleter(self.symbol_completer)
        self.trading_symbol.textEdited.connect(self._on_symbol_edit)
        # Connect selection signal
        self.symbol_completer.activated.connect(self._on_symbol_selected)
        # Fallback: Check lot size when user leaves the field
        self.trading_symbol.editingFinished.connect(self._check_lot_size)

        self.exchange_segment = QComboBox()
        self.exchange_segment.addItems(["nse_cm", "bse_cm", "nse_fo", "bse_fo", "mcx_fo", "cde_fo"])
        self.transaction_type = QComboBox(); self.transaction_type.addItems(["B", "S"])
        self.product = QComboBox(); self.product.addItems(["NRML", "CNC", "MIS", "CO", "BO", "MTF"])
        self.order_type = QComboBox(); self.order_type.addItems(["L", "MKT", "SL", "SL-M"])
        self.quantity = QSpinBox(); self.quantity.setRange(1, 1000000)
        self.load_lot_btn = QPushButton("Load Lot")
        self.load_lot_btn.setToolTip("Fetch Lot Size from Master")
        self.load_lot_btn.clicked.connect(lambda: self._check_lot_size(force=True))

        self.price_edit = QLineEdit()
        self.trigger_edit = QLineEdit()
        self.amo_checkbox = QCheckBox("AMO")
        self.place_btn = QPushButton("Place Order")
        self.place_btn.clicked.connect(self.on_place_order)

        layout.addRow("Trading Symbol:", self.trading_symbol)
        layout.addRow("Exchange Segment:", self.exchange_segment)
        layout.addRow("Transaction Type:", self.transaction_type)
        layout.addRow("Product:", self.product)
        layout.addRow("Order Type:", self.order_type)
        
        # Quantity Row with Button
        qty_layout = QHBoxLayout()
        qty_layout.addWidget(self.quantity)
        qty_layout.addWidget(self.load_lot_btn)
        layout.addRow("Quantity:", qty_layout)

        layout.addRow("Price:", self.price_edit)
        layout.addRow("Trigger Price:", self.trigger_edit)
        layout.addRow(self.amo_checkbox)
        layout.addRow(self.place_btn)
        
        # Auto-update on segment change
        self.exchange_segment.currentTextChanged.connect(lambda: self._check_lot_size())

        self.tabs.addTab(t, "Place Order")

    def _build_orders_tab(self):
        t = QWidget(); layout = QVBoxLayout(); t.setLayout(layout)
        btn_row = QHBoxLayout()
        self.refresh_orders_btn = QPushButton("Refresh Orders")
        self.refresh_orders_btn.clicked.connect(self.refresh_orders)
        btn_row.addWidget(self.refresh_orders_btn)
        layout.addLayout(btn_row)
        self.orders_list = QTableWidget()
        self.orders_list.setColumnCount(7)
        self.orders_list.setHorizontalHeaderLabels(["Order ID", "Symbol", "Type", "Status", "Qty", "Price", "Actions"])
        self.orders_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.orders_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.orders_list.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.orders_list)
        self.tabs.addTab(t, "Orders / Book")

    def _build_positions_tab(self):
        t = QWidget(); layout = QVBoxLayout(); t.setLayout(layout)
        row = QHBoxLayout()
        self.refresh_positions_btn = QPushButton("Refresh Positions")
        self.refresh_positions_btn.clicked.connect(self.refresh_positions)
        row.addWidget(self.refresh_positions_btn)
        self.close_all_btn = QPushButton("Close All Positions")
        self.close_all_btn.setStyleSheet("background-color: #ff4444; color: white; font-weight: bold;")
        self.close_all_btn.clicked.connect(self.close_all_positions)
        row.addWidget(self.close_all_btn)
        t.layout().addLayout(row)
        self.positions_table = QTableWidget()
        self.positions_table.setColumnCount(7)
        self.positions_table.setHorizontalHeaderLabels(["Symbol", "Type", "Qty", "Avg Price", "Mkt Price", "P&L", "Actions"])
        self.positions_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.positions_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.positions_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        t.layout().addWidget(self.positions_table)
        self.tabs.addTab(t, "Positions / Holdings")

    def _build_funds_tab(self):
         # Placeholder if missing
         pass



    def _build_logs_tab(self):
        t = QWidget(); layout = QVBoxLayout(); t.setLayout(layout)
        self.logs = QTextEdit(); self.logs.setReadOnly(True)
        t.layout().addWidget(self.logs)
        self.tabs.addTab(t, "Logs")

    def _apply_saved_config(self):
        # load saved consumer key & env
        cfg = self.config.read()
        if cfg.get("consumer_key"):
            self.consumer_key_edit.setText(cfg["consumer_key"])
        if cfg.get("environment"):
            idx = self.env_combo.findText(cfg["environment"])
            if idx >= 0: self.env_combo.setCurrentIndex(idx)
        if cfg.get("mobile"):
            self.mobile_edit.setText(cfg["mobile"])
        if cfg.get("ucc"):
            self.ucc_edit.setText(cfg["ucc"])
        if cfg.get("mpin"):
            self.mpin_edit.setText(cfg["mpin"])

    def _set_status(self, txt):
        self.status_label.setText(txt)
        msg = f"[{time.strftime('%H:%M:%S')}] {txt}"
        self.logs.append(msg)
        print(msg)

    def toggle_theme(self):
        # simple theme toggle
        if self.styleSheet():
            self.setStyleSheet("")
        else:
            qss = (APP_DIR / "resources" / "themes" / "dark.qss")
            if qss.exists():
                self.setStyleSheet(qss.read_text())

    def load_all_lots(self):
        self._set_status("Starting bulk load of lot sizes for NSE_FO, BSE_FO, MCX_FO...")
        def worker():
            for ex in ["nse_fo", "bse_fo", "mcx_fo"]:
                self._load_master(ex)
        threading.Thread(target=worker, daemon=True).start()


    @Slot()
    def on_totp_login(self):
        consumer = self.consumer_key_edit.text().strip()
        mobile = self.mobile_edit.text().strip()
        ucc = self.ucc_edit.text().strip()
        totp = self.totp_edit.text().strip()
        env = self.env_combo.currentText()

        if not consumer or not mobile or not ucc or not totp:
            QMessageBox.warning(self, "Missing", "Provide consumer key, mobile, UCC and TOTP.")
            return

        # init client
        self.neo = NeoAPI(environment=env, access_token=None, neo_fin_key=None, consumer_key=consumer)
        
        # Set WebSocket Callbacks
        self.neo.on_message = self.on_stream_message
        self.neo.on_error = self.on_stream_error
        self.neo.on_close = self.on_stream_close
        self.neo.on_open = self.on_stream_open
        
        self.wrapper = NeoWrapper(self.neo)
        self.config.update({
            "consumer_key": consumer, 
            "environment": env,
            "mobile": mobile,
            "ucc": ucc
        })

        self._set_status("Verifying TOTP...")

        def do_totp():
            try:
                resp = self.wrapper.totp_login(mobile_number=mobile, ucc=ucc, totp=totp)
                self._set_status("TOTP verified. Now enter MPIN and Login.")
            except Exception as e:
                self._set_status(f"TOTP verification failed: {e}")

        threading.Thread(target=do_totp, daemon=True).start()

    @Slot()
    def on_totp_validate(self):
        if not self.wrapper:
            QMessageBox.warning(self, "Not initialized", "Call Verify TOTP first.")
            return
        mpin = self.mpin_edit.text().strip()
        if not mpin:
            QMessageBox.warning(self, "Missing", "Provide MPIN.")
            return

        self._set_status("Validating MPIN...")

        def do_validate():
            try:
                # totp_validate returns session token / trade token; wrapper stores internally
                self.wrapper.totp_validate(mpin=mpin)
                # Persist mpin if successful
                self.config.update({"mpin": mpin})
                self.signals.login_success.emit()
            except Exception as e:
                self.signals.status_update.emit(f"Login failed: {e}")

        threading.Thread(target=do_validate, daemon=True).start()

    @Slot()
    def on_login_success(self):
        self._set_status("Authenticated Successfully! You can now use the other tabs.")
        self.subscribe_watchlist()

    @Slot()
    def search_scrip(self):
        term = self.scrip_search.text().strip().lower()
        exch = self.scrip_exchange.currentText()
        if not term or not self.wrapper:
            QMessageBox.warning(self, "Missing", "Enter search term and authenticate.")
            return
        self.scrip_results.setRowCount(0)
        try:
            hits = self.wrapper.search_scrip(exchange_segment=exch, symbol=term)
            if isinstance(hits, list):
                self.scrip_results.setRowCount(len(hits))
                for i, h in enumerate(hits):
                    # Extract fields with fallbacks
                    sym = h.get('pTrdSymbol') or h.get('trading_symbol') or h.get('pSymbolName') or "Unknown"
                    seg = h.get('pExchSeg') or h.get('segment') or h.get('exchange_segment') or "Unknown"
                    tok = h.get('pSymbol') or h.get('pScripRefKey') or h.get('token') or h.get('instrument_token') or "Unknown"
                    expiry = h.get('pExpiryDate') or "-"
                    strike = h.get('dStrikePrice;') or h.get('dStrikePrice') or 0
                    try:
                        val = float(strike) / 100.0
                        if val.is_integer():
                            strike = int(val)
                        else:
                            strike = val
                    except:
                        pass
                    otype = h.get('pOptionType') or "-"

                    self.scrip_results.setItem(i, 0, QTableWidgetItem(str(sym)))
                    self.scrip_results.setItem(i, 1, QTableWidgetItem(str(seg)))
                    self.scrip_results.setItem(i, 2, QTableWidgetItem(str(tok)))
                    self.scrip_results.setItem(i, 3, QTableWidgetItem(str(expiry)))
                    self.scrip_results.setItem(i, 4, QTableWidgetItem(str(strike)))
                    self.scrip_results.setItem(i, 5, QTableWidgetItem(str(otype)))
                
                self._set_status(f"Found {len(hits)} scrip(s).")
            else:
                 self._set_status(f"Search result: {hits}")
                 QMessageBox.information(self, "Search Result", str(hits))
        except Exception as e:
            self._set_status(f"Master scrip search failed: {e}")
            QMessageBox.critical(self, "Search Failed", str(e))

    @Slot()
    def get_quote(self):
        row = self.scrip_results.currentRow()
        if row < 0:
            QMessageBox.warning(self, "Select scrip", "Choose scrip from results.")
            return
        
        try:
            # Columns: 0=Symbol, 1=Segment, 2=Token
            exch_seg = self.scrip_results.item(row, 1).text()
            token_str = self.scrip_results.item(row, 2).text()
            
            payload = [
                {"instrument_token": token_str, "exchange_segment": exch_seg}
            ]
            
            q = self.wrapper.get_quote(instrument_tokens=payload)
            # Handle list/dict response
            data = q
            if isinstance(q, list) and len(q) > 0:
                data = q[0]
            
            self._show_quote_dialog(data)
        except Exception as e:
            self._set_status(f"get_quote failed: {e}")
            QMessageBox.critical(self, "Quote Failed", str(e))

    def _on_symbol_edit(self, text):
        if not self.wrapper or len(text) < 2: 
            return
        
        # Debounce/Throttle: Cancel previous timer if exists
        if hasattr(self, "_search_timer") and self._search_timer.isActive():
            self._search_timer.stop()
        
        # Start new timer
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(lambda: self._do_symbol_search(text))
        self._search_timer.start(500) # 500ms debounce

    def _do_symbol_search(self, text, target_model=None, target_completer=None, source_widget=None):
        ex_seg = self.exchange_segment.currentText()
        if source_widget == self.wl_symbol:
            ex_seg = self.wl_exchange.currentText()
            
        # Initialize cache if needed
        if not hasattr(self, "symbol_cache"):
            self.symbol_cache = {}
        if not hasattr(self, "_downloading"):
            self._downloading = set()
            
        def fetch():
            self._load_master(ex_seg)
            try:
                # Filter from cache (if loaded)
                full_list = self.symbol_cache.get(ex_seg, [])
                if not full_list: return

                # Smart filter: Prioritize starts-with
                upper_text = text.upper()
                starts_with = []
                contains = []
                
                for s in full_list:
                    s_upper = s.upper()
                    if s_upper.startswith(upper_text):
                        starts_with.append(s)
                    elif upper_text in s_upper:
                        contains.append(s)
                
                # Combine and limit
                filtered = (starts_with + contains)[:50]
                
                # Update model on main thread AND force popup
                # Use default model/completer if not provided
                tm = target_model if target_model else self.symbol_model
                tc = target_completer if target_completer else self.symbol_completer
                sw = source_widget if source_widget else self.trading_symbol
                
                QTimer.singleShot(0, lambda: self._update_completer(filtered, tm, tc, sw))
                
            except Exception as e:
                print(f"Search failed: {e}")

        threading.Thread(target=fetch, daemon=True).start()

    def _load_master(self, ex_seg):
        try:
            # Check cache
            if hasattr(self, "symbol_cache") and ex_seg in self.symbol_cache:
                return

            if not hasattr(self, "_downloading"):
                self._downloading = set()

            if ex_seg in self._downloading:
                return
            
            self._downloading.add(ex_seg)
            # Notify user about download
            QTimer.singleShot(0, lambda: self._set_status(f"Downloading symbol master for {ex_seg}... Please wait."))
            
            try:
                # Suppress library debug prints
                with contextlib.redirect_stdout(io.StringIO()):
                    res = self.wrapper.scrip_master(exchange_segment=ex_seg)
                
                # Initialize lot cache if needed
                if not hasattr(self, "lot_size_cache"):
                    self.lot_size_cache = {}
                if ex_seg not in self.lot_size_cache:
                    self.lot_size_cache[ex_seg] = {}

                syms = []
                if isinstance(res, str) and res.startswith("http"):
                    # It's a CSV URL
                    r = requests.get(res)
                    r.raise_for_status()
                    
                    # Parse CSV
                    f = io.StringIO(r.text)
                    reader = csv.DictReader(f)
                    for row in reader:
                        s = row.get('pTrdSymbol') or row.get('pSymbol') or row.get('pSymbolName')
                        # Lot size keys: lLotSize, iLotSize, iBoardLotQty
                        lot = row.get('lLotSize') or row.get('iLotSize') or row.get('iBoardLotQty') or "1"
                        if s: 
                            syms.append(s)
                            self.lot_size_cache[ex_seg][s] = lot
                        
                elif isinstance(res, list):
                    # Extract symbols
                    for x in res:
                        s = x.get('pTrdSymbol') or x.get('trading_symbol')
                        lot = x.get('lLotSize') or x.get('iLotSize') or x.get('iBoardLotQty') or "1"
                        if s:
                            syms.append(s)
                            self.lot_size_cache[ex_seg][s] = lot
                
                if syms:
                    syms = sorted(list(set(syms)))
                    if not hasattr(self, "symbol_cache"):
                        self.symbol_cache = {}
                    self.symbol_cache[ex_seg] = syms
                    QTimer.singleShot(0, lambda: self._set_status(f"Loaded {len(syms)} symbols for {ex_seg}."))
                else:
                    QTimer.singleShot(0, lambda: self._set_status(f"Failed to load symbols for {ex_seg}."))
            finally:
                self._downloading.discard(ex_seg)
        except Exception as e:
            print(f"Master load failed: {e}")
            if ex_seg in self._downloading: self._downloading.discard(ex_seg)



    def _update_completer(self, items, model, completer, widget):
        model.setStringList(items)
        
        # Force popup
        if items:
            completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
            if widget.hasFocus():
                completer.complete()
        else:
            completer.popup().hide()

    def _on_symbol_selected(self, text):
        self._check_lot_size(text)

    def _get_lot_size_value(self, ex_seg, symbol):
        if not symbol: return 1
        symbol_upper = symbol.upper()
        if hasattr(self, "lot_size_cache") and ex_seg in self.lot_size_cache:
            lot = self.lot_size_cache[ex_seg].get(symbol_upper)
            if lot:
                try:
                    return int(lot)
                except: pass
        return 1

    def _check_lot_size(self, text=None, force=False):
        if not text:
            text = self.trading_symbol.text().strip()
            
        ex_seg = self.exchange_segment.currentText()
        qty = self._get_lot_size_value(ex_seg, text)
        
        if qty > 1:
            self.quantity.setValue(qty)
            self._set_status(f"Quantity set to {qty} (Lot Size) for {text}")
        elif force:
             if hasattr(self, "lot_size_cache") and ex_seg in self.lot_size_cache:
                 self._set_status(f"Lot size not found for {text} in {ex_seg}.")
             else:
                 self._set_status(f"Master not loaded for {ex_seg}. Loading now...")
                 threading.Thread(target=self._load_master, args=(ex_seg,), daemon=True).start()

    def _show_quote_dialog(self, data):
        if not data: return
        dlg = QDialog(self)
        dlg.setWindowTitle("Quote Details")
        l = QVBoxLayout(dlg)
        

        
        # Flatten OHLC if present
        if "ohlc" in data and isinstance(data["ohlc"], dict):
            ohlc = data["ohlc"]
            data["open"] = ohlc.get("open", data.get("open"))
            data["high"] = ohlc.get("high", data.get("high"))
            data["low"] = ohlc.get("low", data.get("low"))
            data["close"] = ohlc.get("close", data.get("close"))

        # Table
        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["Field", "Value"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        
        # Key mapping for robustness
        key_map = {
            "trading_symbol": ["trading_symbol", "trdSym", "symbol", "display_symbol"],
            "last_price": ["last_price", "ltp", "last_traded_price", "lp"],
            "volume": ["volume", "vol", "v", "volume_traded", "last_volume"],
            "average_price": ["average_price", "avg_price", "average_traded_price", "atp", "avg_cost"],
            "high": ["high", "h", "high_price_day"],
            "low": ["low", "l", "low_price_day"],
            "open": ["open", "o", "open_price_day"],
            "close": ["close", "c", "prev_close_price", "close_price"],
            "change": ["change", "net_change", "absolute_change", "ch"],
            "net_change_percentage": ["net_change_percentage", "pch", "percent_change", "pc", "per_change"]
        }

        # Filter fields
        keys = list(key_map.keys())
        row = 0
        table.setRowCount(len(keys))
        for k in keys:
            # Try all possible keys
            val = "N/A"
            for possible_key in key_map[k]:
                if possible_key in data:
                    val = data[possible_key]
                    break
            
            table.setItem(row, 0, QTableWidgetItem(k))
            table.setItem(row, 1, QTableWidgetItem(str(val)))
            row += 1
            
        l.addWidget(table)
        dlg.exec()

    @Slot()
    def on_place_order(self):
        if not self.wrapper:
            QMessageBox.warning(self, "Authenticate", "Login first.")
            return

        # Gather inputs
        payload = {
            "exchange_segment": self.exchange_segment.currentText(),
            "trading_symbol": self.trading_symbol.text().strip(),
            "transaction_type": self.transaction_type.currentText(),
            "product": self.product.currentText(),
            "order_type": self.order_type.currentText(),
            "quantity": str(self.quantity.value()),
            "price": self.price_edit.text().strip() or "0",
            "trigger_price": self.trigger_edit.text().strip() or "0",
            "amo": "YES" if self.amo_checkbox.isChecked() else "NO",
            "validity": "DAY"
        }
        
        # Validation
        if not payload['trading_symbol']:
            QMessageBox.warning(self, "Error", "Symbol required.")
            return

        # Safety: If quantity is 1, try to fetch lot size
        # Safety: If quantity is 1, try to fetch lot size
        if str(payload['quantity']) == "1":
            ex_seg = payload['exchange_segment']
            sym = payload['trading_symbol'].upper() # Ensure upper case
            if hasattr(self, "lot_size_cache") and ex_seg in self.lot_size_cache:
                lot = self.lot_size_cache[ex_seg].get(sym)
                if lot:
                    payload['quantity'] = str(lot)
                    self._set_status(f"Auto-corrected quantity to {lot} for {sym}")
        
        def do_place():
            try:
                res = self.wrapper.place_order(**payload)
                # Use invokeMethod or signals for thread safety in real app, 
                # but for now we just update status (might be unsafe but works for simple text)
                # Better: print to stdout which is captured or use a signal.
                # Here we will just use print for safety if thread issue, 
                # but _set_status updates UI so it should be on main thread.
                # However, existing code used threading. Let's keep it simple.
                # Wait, updating UI from thread is bad. 
                # But the original code did it. I will stick to the pattern but be careful.
                print(f"Order response: {res}") 
                # We can't easily update UI from here without signals.
                # Let's just run it synchronously for debugging to avoid thread issues hiding errors.
            except Exception as e:
                print(f"Place order failed: {e}")

        # Safety: If quantity is 1, try to fetch lot size
        # Safety: If quantity is 1, try to fetch lot size
        if str(payload['quantity']) == "1":
            ex_seg = payload['exchange_segment']
            sym = payload['trading_symbol'].upper() # Ensure upper case
            if hasattr(self, "lot_size_cache") and ex_seg in self.lot_size_cache:
                lot = self.lot_size_cache[ex_seg].get(sym)
                if lot:
                    payload['quantity'] = str(lot)
                    print(f"DEBUG: Auto-corrected quantity to {lot} for {sym}")

        # Run synchronously for debug
        try:
            res = self.wrapper.place_order(**payload)
            self._set_status(f"Order placed: {res}")
            QMessageBox.information(self, "Order Placed", str(res))
        except Exception as e:
            self._set_status(f"Order placement failed: {e}")
            QMessageBox.critical(self, "Order Failed", str(e))

    @Slot()
    def refresh_orders(self):
        if not self.wrapper:
            QMessageBox.warning(self, "Authenticate", "Login first.")
            return
        try:
            orders_resp = self.wrapper.get_orders()
            # Response might be {"data": [...]} or just [...]
            orders = []
            if isinstance(orders_resp, dict):
                orders = orders_resp.get("data", [])
            elif isinstance(orders_resp, list):
                orders = orders_resp
            
            self.orders_list.setRowCount(0)
            if not orders:
                self._set_status("No orders found.")
                return

            self.orders_list.setRowCount(len(orders))
            for i, o in enumerate(orders):
                # Try common keys
                oid = o.get('nOrdNo') or o.get('order_id') or o.get('id') or "Unknown"
                ttype = o.get('trnsTp') or o.get('transaction_type') or o.get('side') or "Unknown"
                status = o.get('ordSt') or o.get('status') or "Unknown"
                sym = o.get('trdSym') or o.get('trading_symbol') or "Unknown"
                qty = o.get('qty') or o.get('quantity') or "0"
                price = o.get('price') or "0"
                
                self.orders_list.setItem(i, 0, QTableWidgetItem(str(oid)))
                self.orders_list.setItem(i, 1, QTableWidgetItem(str(sym)))
                self.orders_list.setItem(i, 2, QTableWidgetItem(str(ttype)))
                
                status_item = QTableWidgetItem(str(status))
                # Highlight "Open" or "Pending" status
                st_lower = str(status).lower()
                if "open" in st_lower or "pending" in st_lower or "trig" in st_lower:
                    status_item.setForeground(Qt.green)
                    f = status_item.font()
                    f.setBold(True)
                    status_item.setFont(f)
                
                self.orders_list.setItem(i, 3, status_item)
                self.orders_list.setItem(i, 4, QTableWidgetItem(str(qty)))
                self.orders_list.setItem(i, 5, QTableWidgetItem(str(price)))

                # Actions
                btn_widget = QWidget()
                l = QHBoxLayout(btn_widget)
                l.setContentsMargins(2, 2, 2, 2)
                mod_btn = QPushButton("Mod")
                can_btn = QPushButton("Can")
                mod_btn.clicked.connect(partial(self._modify_order_dialog, o))
                can_btn.clicked.connect(partial(self._cancel_single_order, oid))
                l.addWidget(mod_btn)
                l.addWidget(can_btn)
                self.orders_list.setCellWidget(i, 6, btn_widget)

            self._set_status(f"Orders refreshed. Count: {len(orders)}")
        except Exception as e:
            self._set_status(f"get_orders failed: {e}")
            QMessageBox.critical(self, "Orders Failed", str(e))

    def _cancel_single_order(self, order_id):
        if not self.wrapper: return
        order_id = str(order_id) # Ensure string
        ret = QMessageBox.question(self, "Cancel Order", f"Cancel Order {order_id}?", QMessageBox.Yes | QMessageBox.No)
        if ret != QMessageBox.Yes: return
        
        try:
            res = self.wrapper.cancel_order(order_id=order_id, amo="NO")
            self._set_status(f"Cancel result: {res}")
            QMessageBox.information(self, "Cancelled", str(res))
            self.refresh_orders()
        except Exception as e:
            self._set_status(f"Cancel failed: {e}")
            QMessageBox.critical(self, "Cancel Failed", str(e))

    def _modify_order_dialog(self, order_data):
        if not self.wrapper: return
        oid = order_data.get('nOrdNo') or order_data.get('order_id')
        if not oid:
            QMessageBox.warning(self, "Error", "No Order ID found.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Modify Order {oid}")
        form = QFormLayout(dlg)
        
        qty_spin = QSpinBox()
        qty_spin.setRange(1, 1000000)
        curr_qty = order_data.get('qty') or order_data.get('quantity') or "0"
        if str(curr_qty).isdigit(): qty_spin.setValue(int(curr_qty))
        
        price_edit = QLineEdit()
        curr_price = order_data.get('price') or "0"
        price_edit.setText(str(curr_price))
        
        form.addRow("New Quantity:", qty_spin)
        form.addRow("New Price:", price_edit)
        
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        
        if dlg.exec() == QDialog.Accepted:
            new_qty = str(qty_spin.value())
            new_price = price_edit.text().strip()
            
            # Extract other required fields from order_data
            # Keys based on user logs: prcTp=order_type, vldt=validity, prod=product, 
            # trdSym=trading_symbol, exSeg=exchange_segment, tok=instrument_token, trnsTp=transaction_type
            otype = order_data.get('prcTp') or "L"
            validity = order_data.get('vldt') or "DAY"
            prod = order_data.get('prod')
            sym = order_data.get('trdSym')
            ex_seg = order_data.get('exSeg')
            tok = order_data.get('tok')
            trans_type = order_data.get('trnsTp')

            try:
                # Pass all arguments required by NeoAPI.modify_order
                res = self.wrapper.modify_order(
                    order_id=str(oid),
                    price=new_price,
                    quantity=new_qty,
                    order_type=otype,
                    validity=validity,
                    product=prod,
                    trading_symbol=sym,
                    exchange_segment=ex_seg,
                    instrument_token=tok,
                    transaction_type=trans_type,
                    amo="NO"
                )
                self._set_status(f"Modify result: {res}")
                QMessageBox.information(self, "Modified", str(res))
                self.refresh_orders()
            except Exception as e:
                self._set_status(f"Modify failed: {e}")
                QMessageBox.critical(self, "Modify Failed", str(e))

    @Slot()
    def refresh_positions(self):
        try:
            pos_resp = self.wrapper.get_positions()
            positions = []
            if isinstance(pos_resp, dict):
                positions = pos_resp.get("data", [])
            elif isinstance(pos_resp, list):
                positions = pos_resp
            
            self.positions_table.setRowCount(0)
            if not positions:
                self._set_status("No positions found.")
                return

            # Prepare for LTP fetch
            quotes_payload = []
            pos_calcs = []

            for p in positions:
                # Keys: flBuyQty, flSellQty, cfBuyQty, cfSellQty, buyAmt, sellAmt, ...
                # Safely get values (handle string/int/float)
                def get_f(k): return float(p.get(k, 0) or 0)
                def get_i(k): return int(float(p.get(k, 0) or 0)) # handle "100.0" string

                fl_buy_qty = get_i('flBuyQty')
                fl_sell_qty = get_i('flSellQty')
                cf_buy_qty = get_i('cfBuyQty')
                cf_sell_qty = get_i('cfSellQty')
                
                buy_amt = get_f('buyAmt')
                sell_amt = get_f('sellAmt')
                cf_buy_amt = get_f('cfBuyAmt')
                cf_sell_amt = get_f('cfSellAmt')

                net_qty = (fl_buy_qty + cf_buy_qty) - (fl_sell_qty + cf_sell_qty)
                
                total_buy_val = buy_amt + cf_buy_amt
                total_sell_val = sell_amt + cf_sell_amt
                
                # Avg Price (Net Price)
                avg_prc = 0.0
                if net_qty != 0:
                    # If Long: (Total Buy Val - Total Sell Val) / Net Qty? 
                    # No, that mixes realized P&L.
                    # Simple Avg: 
                    # If Net > 0 (Long): Avg = Total Buy Val / Total Buy Qty (Approx)
                    # This is tricky without per-trade data.
                    # Let's use "Break-even" price logic if possible, or just Buy Avg for Longs.
                    # Standard approach:
                    # Buy Avg = (buy_amt + cf_buy_amt) / (fl_buy_qty + cf_buy_qty) if denom > 0
                    # Sell Avg = (sell_amt + cf_sell_amt) / (fl_sell_qty + cf_sell_qty) if denom > 0
                    
                    if net_qty > 0:
                        tot_b_qty = fl_buy_qty + cf_buy_qty
                        if tot_b_qty > 0: avg_prc = total_buy_val / tot_b_qty
                    else:
                        tot_s_qty = fl_sell_qty + cf_sell_qty
                        if tot_s_qty > 0: avg_prc = total_sell_val / tot_s_qty
                
                sym = p.get('trdSym') or p.get('trading_symbol') or "Unknown"
                ex_seg = p.get('exSeg') or "nse_cm"
                tok = p.get('tok') or "0"
                
                # Add to payload for LTP
                if tok != "0":
                    quotes_payload.append({"instrument_token": str(tok), "exchange_segment": ex_seg})
                
                pos_calcs.append({
                    "sym": sym,
                    "net_qty": net_qty,
                    "avg_prc": avg_prc,
                    "buy_val": total_buy_val,
                    "sell_val": total_sell_val,
                    "tok": str(tok),
                    "raw": p
                })

            # Fetch LTPs
            ltp_map = {}
            if quotes_payload:
                try:
                    # Batch fetch might fail if too many? SDK usually handles it.
                    # If fails, we continue with LTP=0
                    q_resp = self.wrapper.get_quote(instrument_tokens=quotes_payload)
                    
                    # q_resp structure: {'message': '...', 'data': [...]} or just [...]
                    q_data = []
                    if isinstance(q_resp, dict):
                        q_data = q_resp.get('data', [])
                        if not q_data and 'instrument_token' in q_resp: # Single dict response?
                             q_data = [q_resp]
                        if not q_data and 'exchange_token' in q_resp: # Check for exchange_token too
                             q_data = [q_resp]
                    elif isinstance(q_resp, list):
                        q_data = q_resp
                    
                    for q in q_data:
                        # q keys: instrument_token, last_price, ...
                        # Response might use 'exchange_token' instead of 'instrument_token'
                        t = str(q.get('instrument_token') or q.get('exchange_token') or '')
                        
                        # Try multiple keys for LTP
                        lp = float(q.get('last_price') or q.get('ltp') or q.get('last_traded_price') or 0)
                        if t: ltp_map[t] = lp
                except Exception as e:
                    print(f"LTP fetch failed: {e}")

            # Populate Table
            self.positions_table.setRowCount(len(pos_calcs))
            for i, pc in enumerate(pos_calcs):
                qty = pc['net_qty']
                sym = pc['sym']
                avg = pc['avg_prc']
                tok = pc['tok']
                ltp = ltp_map.get(tok, 0.0)
                
                # P&L = (Sell Val - Buy Val) + (Net Qty * LTP)
                # This formula accounts for Realized + Unrealized
                pnl = (pc['sell_val'] - pc['buy_val']) + (qty * ltp)
                
                ttype = "BUY" if qty > 0 else "SELL"
                if qty == 0: ttype = "CLOSED"
                
                self.positions_table.setItem(i, 0, QTableWidgetItem(str(sym)))
                self.positions_table.setItem(i, 1, QTableWidgetItem(str(ttype)))
                self.positions_table.setItem(i, 2, QTableWidgetItem(str(qty)))
                self.positions_table.setItem(i, 3, QTableWidgetItem(f"{avg:.2f}"))
                self.positions_table.setItem(i, 4, QTableWidgetItem(f"{ltp:.2f}"))
                
                pnl_item = QTableWidgetItem(f"{pnl:.2f}")
                if pnl >= 0: pnl_item.setForeground(Qt.green)
                else: pnl_item.setForeground(Qt.red)
                self.positions_table.setItem(i, 5, pnl_item)

                # Exit Action
                btn_widget = QWidget()
                l = QHBoxLayout(btn_widget)
                l.setContentsMargins(2, 2, 2, 2)
                exit_btn = QPushButton("Exit")
                exit_btn.setStyleSheet("background-color: #ff4444; color: white;")
                # Pass raw position data but update quantity to net_qty for exit logic
                # We need to be careful: _exit_single_position uses 'fldQty' from raw data?
                # No, we should update it to use our calculated Net Qty.
                # Let's pass a modified dict or handle it in _exit_single_position
                # Better: Pass the calculated net_qty to the exit function
                # But _exit_single_position expects a dict.
                # Let's update the raw dict with 'calculated_qty'
                pc['raw']['calculated_qty'] = abs(qty)
                pc['raw']['calculated_type'] = "B" if qty > 0 else "S"
                
                exit_btn.clicked.connect(partial(self._exit_single_position, pc['raw']))
                if qty == 0: exit_btn.setEnabled(False)
                l.addWidget(exit_btn)
                self.positions_table.setCellWidget(i, 6, btn_widget)

            self._set_status(f"Positions refreshed. Count: {len(pos_calcs)}")
        except Exception as e:
            self._set_status(f"get_positions failed: {e}")
            import traceback
            traceback.print_exc()

    def _exit_single_position(self, pos_data):
        if not self.wrapper: return
        
        sym = pos_data.get('trdSym') or pos_data.get('trading_symbol')
        # Use calculated qty if available, else fallback
        qty = pos_data.get('calculated_qty') 
        if qty is None:
             qty = pos_data.get('fldQty') or pos_data.get('quantity') or "0"
        
        curr_type = pos_data.get('calculated_type')
        if curr_type is None:
             curr_type = pos_data.get('trnsTp') or pos_data.get('type') # B or S
        
        if not sym or not curr_type:
            QMessageBox.warning(self, "Error", "Invalid position data.")
            return

        # Determine exit transaction type
        exit_trans_type = "S" if curr_type == "B" else "B"
        
        ret = QMessageBox.question(self, "Exit Position", f"Exit {sym} ({qty} qty)?", QMessageBox.Yes | QMessageBox.No)
        if ret != QMessageBox.Yes: return

        # Need other details for place_order
        # Assuming we can get them from pos_data or use defaults/mappings
        # We need: exchange_segment, product, order_type='MKT', validity='DAY'
        # pos_data keys might be 'exSeg', 'prod'
        ex_seg = pos_data.get('exSeg') or pos_data.get('exchange_segment')
        prod = pos_data.get('prod') or pos_data.get('product')
        tok = pos_data.get('tok') or pos_data.get('instrument_token')
        
        if not ex_seg or not prod:
             QMessageBox.warning(self, "Error", "Missing exchange/product info in position.")
             return

        try:
            res = self.wrapper.place_order(
                exchange_segment=ex_seg,
                product=prod,
                price="0", # Market order
                order_type="MKT",
                quantity=str(qty),
                validity="DAY",
                trading_symbol=sym,
                transaction_type=exit_trans_type,
                amo="NO"
            )
            self._set_status(f"Exit result: {res}")
            QMessageBox.information(self, "Exited", str(res))
            self.refresh_positions()
        except Exception as e:
            self._set_status(f"Exit failed: {e}")
            QMessageBox.critical(self, "Exit Failed", str(e))

    def close_all_positions(self):
        if not self.wrapper: return
        
        ret = QMessageBox.question(self, "Close All", "Are you sure you want to CLOSE ALL positions?", QMessageBox.Yes | QMessageBox.No)
        if ret != QMessageBox.Yes: return
        
        try:
            pos_resp = self.wrapper.get_positions()
            positions = []
            if isinstance(pos_resp, dict):
                positions = pos_resp.get("data", [])
            elif isinstance(pos_resp, list):
                positions = pos_resp
            
            if not positions:
                self._set_status("No positions to close.")
                return

            count = 0
            errors = []
            for p in positions:
                # Calculate Net Qty
                def get_i(k): return int(float(p.get(k, 0) or 0))
                fl_buy_qty = get_i('flBuyQty')
                fl_sell_qty = get_i('flSellQty')
                cf_buy_qty = get_i('cfBuyQty')
                cf_sell_qty = get_i('cfSellQty')
                
                net_qty = (fl_buy_qty + cf_buy_qty) - (fl_sell_qty + cf_sell_qty)
                
                if net_qty == 0: continue

                sym = p.get('trdSym') or p.get('trading_symbol')
                ex_seg = p.get('exSeg') or p.get('exchange_segment')
                prod = p.get('prod') or p.get('product')
                
                if not (sym and ex_seg and prod):
                    continue
                
                # Determine Exit Side
                # If Net Qty > 0 (Long) -> Sell
                # If Net Qty < 0 (Short) -> Buy
                exit_trans_type = "S" if net_qty > 0 else "B"
                qty_to_close = abs(net_qty)

                try:
                    self.wrapper.place_order(
                        exchange_segment=ex_seg,
                        product=prod,
                        price="0",
                        order_type="MKT",
                        quantity=str(qty_to_close),
                        validity="DAY",
                        trading_symbol=sym,
                        transaction_type=exit_trans_type,
                        amo="NO"
                    )
                    count += 1
                except Exception as e:
                    errors.append(f"{sym}: {e}")
            
            msg = f"Closed {count} positions."
            if errors:
                msg += f"\nErrors: {errors}"
            
            self._set_status(msg)
            QMessageBox.information(self, "Close All Result", msg)
            self.refresh_positions()
            
        except Exception as e:
             self._set_status(f"Close All failed: {e}")
             QMessageBox.critical(self, "Close All Failed", str(e))

    def _build_funds_tab(self):
        w = QWidget()
        l = QVBoxLayout(w)
        
        # Controls
        h = QHBoxLayout()
        btn = QPushButton("Refresh Funds")
        btn.clicked.connect(self.refresh_funds)
        h.addWidget(btn)
        h.addStretch()
        l.addLayout(h)
        
        # Table
        self.funds_table = QTableWidget()
        self.funds_table.setColumnCount(2)
        self.funds_table.setHorizontalHeaderLabels(["Field", "Value"])
        self.funds_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.funds_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        l.addWidget(self.funds_table)
        
        self.tabs.addTab(w, "Funds")

    def refresh_funds(self):
        if not self.wrapper:
            QMessageBox.warning(self, "Error", "Login first.")
            return
            
        try:
            # Fetch limits
            resp = self.wrapper.get_limits()
            # resp structure: {'data': {...}, ...} or just {...}
            
            data = {}
            if isinstance(resp, dict):
                if 'data' in resp:
                    data = resp['data']
                else:
                    data = resp
            elif isinstance(resp, list) and len(resp) > 0:
                data = resp[0]
            
            if not data:
                self._set_status("No funds data received.")
                return

            # Flatten and display
            items = []
            for k, v in data.items():
                if isinstance(v, dict):
                    for sub_k, sub_v in v.items():
                        items.append((f"{k} - {sub_k}", str(sub_v)))
                else:
                    items.append((k, str(v)))
            
            self.funds_table.setRowCount(len(items))
            for i, (k, v) in enumerate(items):
                self.funds_table.setItem(i, 0, QTableWidgetItem(str(k)))
                self.funds_table.setItem(i, 1, QTableWidgetItem(str(v)))
            
            self._set_status("Funds refreshed.")
            
        except Exception as e:
            self._set_status(f"refresh_funds failed: {e}")
            QMessageBox.critical(self, "Funds Failed", str(e))

    # --- Watchlist Logic ---
    def _on_wl_symbol_edit(self, text):
        # Re-use the search logic but target the watchlist completer
        if len(text) < 3: return
        self._do_symbol_search(text, target_model=self.wl_model, target_completer=self.wl_completer, source_widget=self.wl_symbol)

    def add_to_watchlist(self):
        txt = self.wl_symbol.text().strip()
        ex_seg = self.wl_exchange.currentText()
        
        if not txt: return
        
        # Check if master loaded
        if not hasattr(self, "symbol_cache") or ex_seg not in self.symbol_cache:
             self._set_status(f"Master not loaded for {ex_seg}. Searching will load it.")
             # We proceed anyway, search_scrip handles it? 
             # Actually wrapper.search_scrip calls API, doesn't need local master if API handles it.
             # But if we rely on local cache for something else... 
             # Let's just proceed.

        self._set_status(f"Searching for {txt}...")
        self.wl_add_btn.setEnabled(False) # Disable button

        def do_search():
            try:
                res = self.wrapper.search_scrip(exchange_segment=ex_seg, symbol=txt)
                self.signals.search_completed.emit(res, txt, ex_seg)
            except Exception as e:
                self.signals.status_update.emit(f"Search failed: {e}")
                self.signals.search_completed.emit(None, txt, ex_seg)

        threading.Thread(target=do_search, daemon=True).start()

    @Slot(object, str, str)
    def on_search_completed(self, res, txt, ex_seg):
        self.wl_add_btn.setEnabled(True) # Re-enable button
        
        if res and isinstance(res, list) and len(res) > 0:
            # Find exact match or take first
            target = None
            for r in res:
                if r.get('pTrdSymbol', '').upper() == txt.upper() or r.get('trading_symbol', '').upper() == txt.upper():
                    target = r
                    break
            if not target: target = res[0]
            
            sym = target.get('pTrdSymbol') or target.get('trading_symbol')
            token = str(target.get('pSymbol') or target.get('instrument_token') or '')
            
            # Add to data
            item = {"symbol": sym, "token": token, "segment": ex_seg}
            
            # Check duplicate
            for x in self.watchlist_data:
                if x['token'] == token and x['segment'] == ex_seg:
                    self._set_status("Already in watchlist")
                    return

            self.watchlist_data.append(item)
            self._save_watchlist()
            self._render_watchlist()
            self.wl_symbol.clear()
            self._set_status(f"Added {sym}")
            
            # Auto-subscribe (Subscribe to ALL to ensure cumulative)
            self.subscribe_watchlist()
            
            # Fetch initial snapshot for this item (Async)
            def fetch_initial():
                try:
                    q_res = self.wrapper.get_quote(instrument_tokens=[{"instrument_token": token, "exchange_segment": ex_seg}])
                    if q_res:
                        self.signals.market_data_received.emit(q_res[0])
                except Exception as e:
                    print(f"Initial fetch failed: {e}")
            
            threading.Thread(target=fetch_initial, daemon=True).start()
            
        else:
            self._set_status("Symbol not found via API search")

    def remove_from_watchlist(self):
        row = self.wl_table.currentRow()
        if row >= 0:
            self.watchlist_data.pop(row)
            self._save_watchlist()
            self._render_watchlist()

    def _save_watchlist(self):
        try:
            with open(APP_DIR / "watchlist.json", "w") as f:
                json.dump(self.watchlist_data, f)
        except Exception as e:
            print(f"Save WL failed: {e}")

    def _load_watchlist(self):
        p = APP_DIR / "watchlist.json"
        if p.exists():
            try:
                with open(p, "r") as f:
                    self.watchlist_data = json.load(f)
                self._render_watchlist()
            except Exception as e:
                print(f"Load WL failed: {e}")

    def _render_watchlist(self):
        self.wl_table.setRowCount(len(self.watchlist_data))
        self.wl_table.setColumnCount(8)
        self.wl_table.setHorizontalHeaderLabels(["Symbol", "LTP", "Side", "Product", "Type", "Qty", "Price", "Action"])
        
        # Resize Symbol Column
        header = self.wl_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        
        # Set fixed widths for other columns
        self.wl_table.setColumnWidth(1, 80)  # LTP
        self.wl_table.setColumnWidth(2, 60)  # Side
        self.wl_table.setColumnWidth(3, 60)  # Product
        self.wl_table.setColumnWidth(4, 60)  # Type
        self.wl_table.setColumnWidth(5, 60)  # Qty
        self.wl_table.setColumnWidth(6, 80)  # Price
        self.wl_table.setColumnWidth(7, 60)  # Action
        
        self.token_row_map.clear()
        
        for i, item in enumerate(self.watchlist_data):
            sym_item = QTableWidgetItem(str(item.get('symbol')))
            # Store hidden data
            sym_item.setData(Qt.UserRole, item.get('token'))
            sym_item.setData(Qt.UserRole + 1, item.get('segment'))
            self.wl_table.setItem(i, 0, sym_item)
            
            # LTP
            self.wl_table.setItem(i, 1, QTableWidgetItem(str(item.get('ltp', '-'))))
            
            # Side Combo
            side_combo = QComboBox()
            side_combo.addItems(["BUY", "SELL"])
            self.wl_table.setCellWidget(i, 2, side_combo)
            
            # Product Combo
            prod_combo = QComboBox()
            prod_combo.addItems(["MIS", "NRML", "CNC"])
            prod_combo.setCurrentText("NRML")
            self.wl_table.setCellWidget(i, 3, prod_combo)
            
            # Type Combo
            type_combo = QComboBox()
            type_combo.addItems(["MKT", "L"])
            self.wl_table.setCellWidget(i, 4, type_combo)
            
            # Qty SpinBox
            qty_spin = QSpinBox()
            qty_spin.setRange(1, 1000000)
            # Auto-Lot Size
            lot = self._get_lot_size_value(item.get('segment'), item.get('symbol'))
            qty_spin.setValue(lot)
            self.wl_table.setCellWidget(i, 5, qty_spin)
            
            # Price SpinBox
            price_spin = QDoubleSpinBox()
            price_spin.setRange(0, 1000000)
            price_spin.setDecimals(2)
            price_spin.setValue(0)
            price_spin.setEnabled(False) # Default MKT -> Disabled
            self.wl_table.setCellWidget(i, 6, price_spin)
            
            # Connect Type -> Price Enable/Disable
            # Use a helper to capture widgets
            def update_price_state(idx=i, tc=type_combo, ps=price_spin):
                if tc.currentText() == "MKT":
                    ps.setEnabled(False)
                    ps.setValue(0)
                else:
                    ps.setEnabled(True)
            
            type_combo.currentTextChanged.connect(update_price_state)
            
            # Action Button
            btn = QPushButton("Place")
            btn.setStyleSheet("background-color: #007bff; color: white; font-weight: bold;")
            btn.clicked.connect(partial(self.place_order_from_row, i))
            self.wl_table.setCellWidget(i, 7, btn)
            
            # Map token to row
            self.token_row_map[str(item.get('token'))] = i

    def place_order_from_row(self, row):
        try:
            # Get data
            sym_item = self.wl_table.item(row, 0)
            token = sym_item.data(Qt.UserRole)
            segment = sym_item.data(Qt.UserRole + 1)
            symbol = sym_item.text()
            
            side = self.wl_table.cellWidget(row, 2).currentText()
            product = self.wl_table.cellWidget(row, 3).currentText()
            order_type = self.wl_table.cellWidget(row, 4).currentText()
            qty = self.wl_table.cellWidget(row, 5).value()
            price = self.wl_table.cellWidget(row, 6).value()
            
            if order_type == "MKT":
                price = 0
            
            if not self.wrapper:
                QMessageBox.warning(self, "Error", "Not logged in")
                return
                
            # Confirm?
            # reply = QMessageBox.question(self, "Confirm Order", f"{side} {qty} {symbol} @ {price if price > 0 else 'MKT'}", QMessageBox.Yes | QMessageBox.No)
            # if reply == QMessageBox.No: return
            
            self._set_status(f"Placing {side} {qty} {symbol}...")
            
            # Run in thread
            def do_place():
                try:
                    # Need trading_symbol, exchange_segment, etc.
                    # We have symbol and segment.
                    # transaction_type: B/S -> BUY/SELL? SDK expects "B" or "BUY"?
                    # Usually SDK expects "B" or "S" or "BUY"/"SELL". Let's check place_order usage.
                    # In on_place_order, we use "B" or "S".
                    
                    ttype = "B" if side == "BUY" else "S"
                    
                    # We need to ensure we pass correct args.
                    # place_order(exchange_segment=..., trading_symbol=..., quantity=..., price=..., order_type=..., product=..., transaction_type=..., validity="DAY")
                    
                    res = self.wrapper.place_order(
                        exchange_segment=segment,
                        trading_symbol=symbol,
                        quantity=str(qty),
                        price=str(price),
                        order_type=order_type,
                        product=product,
                        transaction_type=ttype,
                        validity="DAY",
                        amo="NO"
                    )
                    self.signals.status_update.emit(f"Order Placed: {res}")
                except Exception as e:
                    self.signals.status_update.emit(f"Order Failed: {e}")
            
            threading.Thread(target=do_place, daemon=True).start()
            
        except Exception as e:
            self._set_status(f"Error reading row: {e}")

    def refresh_watchlist(self):
        if not self.watchlist_data: return
        if not self.wrapper: 
            self._set_status("Not logged in")
            return
            
        # Group by segment? get_quote takes list of {instrument_token, exchange_segment}
        payload = []
        for item in self.watchlist_data:
            payload.append({"instrument_token": item['token'], "exchange_segment": item['segment']})
            
        try:
            # API might have limit on number of tokens. Assuming list is small (<50)
            res = self.wrapper.get_quote(instrument_tokens=payload)
            # Response is list of dicts
            if res and isinstance(res, list):
                # Map back to watchlist data
                # We need to match by token
                lookup = {str(x.get('instrument_token')): x for x in res}
                
                for item in self.watchlist_data:
                    tok = str(item['token'])
                    if tok in lookup:
                        q = lookup[tok]
                        # Robust key lookup
                        def get_val(keys):
                            for k in keys:
                                if k in q: return q[k]
                            return '-'

                        item['ltp'] = get_val(["last_price", "ltp", "last_traded_price", "lp"])
                        item['change'] = get_val(["change", "net_change", "absolute_change", "ch"])
                        item['p_change'] = get_val(["net_change_percentage", "pch", "percent_change", "pc", "per_change"])
                
                self._render_watchlist()
                self._set_status("Watchlist updated")
                
                # Subscribe to all
                self.subscribe_watchlist()
                
        except Exception as e:
            self._set_status(f"WL Refresh failed: {e}")

    def subscribe_watchlist(self):
        if not self.neo: return
        
        def do_subscribe():
            tokens = []
            # Accessing self.watchlist_data should be safe if only reading, 
            # but ideally we should copy it or lock it. 
            # For this simple app, reading is likely fine.
            for item in self.watchlist_data:
                tokens.append({"instrument_token": str(item['token']), "exchange_segment": item['segment']})
            
            if tokens:
                try:
                    self.neo.subscribe(instrument_tokens=tokens)
                    # Use signal for status update
                    self.signals.status_update.emit(f"Subscribed to {len(tokens)} symbols for live updates.")
                except Exception as e:
                    print(f"Subscribe failed: {e}")

        threading.Thread(target=do_subscribe, daemon=True).start()

    # --- WebSocket Callbacks ---
    def on_stream_message(self, message):
        # This runs on a background thread
        # Message structure depends on API. Usually list of dicts or single dict.
        # Example: {'data': [{'instrument_token': '...', 'last_price': ...}]}
        try:
            # Parse if string
            if isinstance(message, str):
                message = json.loads(message)
            
            # Extract data list
            data_list = []
            if isinstance(message, list):
                data_list = message
            elif isinstance(message, dict):
                if 'data' in message:
                    d = message['data']
                    if isinstance(d, list): data_list = d
                    else: data_list = [d]
                else:
                    data_list = [message]
            
            for d in data_list:
                if not isinstance(d, dict): continue
                # Emit signal for each update
                self.signals.market_data_received.emit(d)
                
                # Update Strategy Manager
                if hasattr(self, 'strategy_manager'):
                    self.strategy_manager.on_market_data(d)
                
        except Exception as e:
            print(f"Stream parse error: {e}")

    def on_stream_error(self, error):
        print(f"Stream Error: {error}")

    def on_stream_close(self, message):
        print(f"Stream Closed: {message}")

    def on_stream_open(self, message):
        print(f"Stream Opened: {message}")

    @Slot()
    def update_watchlist_item(self, data):
        # Runs on Main Thread
        # data keys: instrument_token (or tk), last_price (or ltp), change (or ch), etc.
        
        # Robust key extraction
        def get_val(keys, default=None):
            for k in keys:
                if k in data: return data[k]
            return default

        token = str(get_val(['instrument_token', 'tk', 'token']))
        if not token: return
        
        ltp = get_val(['last_price', 'ltp', 'lp'])
        change = get_val(['change', 'ch', 'net_change'])
        p_change = get_val(['net_change_percentage', 'pc', 'pch'])
        
        # Fast lookup
        if token in self.token_row_map:
            row = self.token_row_map[token]
            
            # Update LTP
            if ltp is not None:
                old_item = self.wl_table.item(row, 1)
                # Check if item exists (it should)
                if old_item:
                    try:
                        old_text = old_item.text().replace(',', '')
                        old_val = float(old_text) if old_text.replace('.','',1).isdigit() else 0.0
                    except: old_val = 0.0
                    
                    try:
                        new_val = float(ltp)
                    except: new_val = 0.0
                    
                    ltp_item = QTableWidgetItem(str(ltp))
                    
                    # Flash color
                    if new_val > old_val:
                        ltp_item.setForeground(QColor("green"))
                    elif new_val < old_val:
                        ltp_item.setForeground(QColor("red"))
                    else:
                        ltp_item.setForeground(old_item.foreground())
                        
                    self.wl_table.setItem(row, 1, ltp_item)
            
            # Update Change
            # if change is not None:
            #    self.wl_table.setItem(row, 2, QTableWidgetItem(str(change)))
            
            # Update % Change
            # if p_change is not None:
            #    self.wl_table.setItem(row, 3, QTableWidgetItem(str(p_change)))

    def open_floating_window(self):
        if not hasattr(self, "floating_window") or self.floating_window is None:
            self.floating_window = FloatingOrderWindow(self)
        
        self.floating_window.show()
        self.floating_window.raise_()
        self.floating_window.activateWindow()

def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

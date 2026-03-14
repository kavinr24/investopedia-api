from __future__ import annotations

import pathlib
from dataclasses import dataclass, field, asdict
from typing import Literal
import re

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

BASE_URL = "https://www.investopedia.com/simulator"
SESSION_FILE = pathlib.Path(__file__).parent / "session.json"
LOGIN_URL = f"{BASE_URL}/portfolio"


class SessionExpiredError(Exception):
    pass


@dataclass
class Holding:
    symbol: str
    description: str
    current_price: float
    purchase_price: float
    quantity: int | float
    total_value: float
    todays_change: float | None = None
    gain_loss: float | None = None


@dataclass
class Portfolio:
    account_value: float
    buying_power: float
    cash_balance: float
    annual_return_pct: float | None
    todays_change: float | None
    holdings: list[Holding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class TradeAPI:
    def __init__(
        self,
        headless: bool = True,
        session_path: str | pathlib.Path = SESSION_FILE,
    ) -> None:
        self._headless = headless
        self._session_path = pathlib.Path(session_path)
        self._pw = sync_playwright().start()
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._connect()

    def _connect(self) -> None:
        if not self._session_path.exists():
            raise FileNotFoundError(
                f"Session file not found at {self._session_path}. "
                "Run auth_setup.py first to create one."
            )
        self._browser = self._pw.chromium.launch(headless=self._headless)
        self._context = self._browser.new_context(
            storage_state=str(self._session_path),
        )
        self._page = self._context.new_page()
        self._validate_session()

    @staticmethod
    def _is_on_auth_page(page: Page) -> bool:
        host = page.evaluate("() => window.location.hostname").lower()
        return host.startswith("auth.") or "login" in host

    def _validate_session(self) -> None:
        self._page.goto(LOGIN_URL, wait_until="domcontentloaded")
        self._page.wait_for_timeout(2000)
        if self._is_on_auth_page(self._page):
            self.close()
            raise SessionExpiredError(
                "Session has expired. Re-run auth_setup.py to log in again."
            )

    def close(self) -> None:
        if self._context:
            self._context.close()
            self._context = None
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._pw:
            self._pw.stop()
            self._pw = None

    def __enter__(self) -> "TradeAPI":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _ensure_page(self) -> Page:
        if self._page is None or self._page.is_closed():
            raise RuntimeError("Browser page is not available.")
        return self._page

    @staticmethod
    def _parse_money(text: str) -> float:
        cleaned = text.replace("$", "").replace(",", "").replace("+", "").strip()
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]
        return float(cleaned)

    @staticmethod
    def _parse_pct(text: str) -> float:
        cleaned = text.replace("%", "").replace(",", "").replace("+", "").strip()
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]
        return float(cleaned)

    @staticmethod
    def _parse_number(text: str) -> float:
        return float(text.replace(",", "").strip())

    def get_portfolio(self) -> Portfolio:
        page = self._ensure_page()
        page.goto(f"{BASE_URL}/portfolio", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        if self._is_on_auth_page(page):
            raise SessionExpiredError(
                "Session has expired. Re-run auth_setup.py to log in again."
            )

        account_value = self._scrape_summary_value(page, "Account Value")
        buying_power = self._scrape_summary_value(page, "Buying Power")
        cash_balance = self._scrape_summary_value(page, "Cash")
        annual_return_pct = self._try_scrape_pct(page, "Annual Return")
        todays_change = self._try_scrape_summary_value(page, "Today's Change")

        holdings: list[Holding] = []

        table = page.locator("table").first
        if table.count():
            rows = table.locator("tbody tr")
            for i in range(rows.count()):
                row = rows.nth(i)
                cells = row.locator("td")
                cell_count = cells.count()
                if cell_count < 7:
                    continue

                texts = [cells.nth(c).inner_text().strip() for c in range(cell_count)]

                try:
                    change_text = texts[3].split("\n")[0].strip()
                    gain_text = texts[7].split("\n")[0].strip() if cell_count > 7 else None

                    holding = Holding(
                        symbol=texts[0].split("\n")[0].strip(),
                        description=texts[1].strip(),
                        current_price=self._parse_money(texts[2]),
                        purchase_price=self._parse_money(texts[4]),
                        quantity=self._parse_number(texts[5]),
                        total_value=self._parse_money(texts[6]),
                        todays_change=self._parse_money(change_text) if "$" in change_text else None,
                        gain_loss=self._parse_money(gain_text) if gain_text and "$" in gain_text else None,
                    )
                    holdings.append(holding)
                except (ValueError, IndexError):
                    continue

        return Portfolio(
            account_value=account_value,
            buying_power=buying_power,
            cash_balance=cash_balance,
            annual_return_pct=annual_return_pct,
            todays_change=todays_change,
            holdings=holdings,
        )

    def _scrape_summary_value(self, page: Page, label: str) -> float:
        locator = page.get_by_text(label).first
        if locator.count():
            parent = locator.locator(".. >> span, .. >> p, .. >> div").first
            container = locator.locator("..").first
            text = container.inner_text()
            for line in text.split("\n"):
                line = line.strip()
                if "$" in line and label not in line:
                    return self._parse_money(line)
                if "$" in line and label in line:
                    parts = line.split("$", 1)
                    if len(parts) == 2:
                        return self._parse_money("$" + parts[1])
        raise ValueError(f"Could not find summary value for '{label}'.")

    def _try_scrape_summary_value(self, page: Page, label: str) -> float | None:
        try:
            return self._scrape_summary_value(page, label)
        except ValueError:
            return None

    def _try_scrape_pct(self, page: Page, label: str) -> float | None:
        try:
            locator = page.get_by_text(label).first
            if locator.count():
                container = locator.locator("..").first
                text = container.inner_text()
                for line in text.split("\n"):
                    line = line.strip()
                    if "%" in line and label not in line:
                        return self._parse_pct(line)
        except (ValueError, Exception):
            pass
        return None

    def _set_text_input(self, locator, value: str) -> None:
        locator.click(force=True)
        locator.fill("")
        locator.fill(value)
        locator.press("Tab")
        try:
            current = locator.input_value().strip()
        except Exception:
            current = ""
        if current != value:
            locator.evaluate(
                """
                (el, val) => {
                    el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                }
                """,
                value,
            )

    def _select_dropdown_option(self, page: Page, input_locator, option_text: str) -> None:
        input_locator.click(force=True)
        input_locator.fill("")
        input_locator.fill(option_text)
        page.wait_for_timeout(250)
        option = page.get_by_text(re.compile(rf"^{re.escape(option_text)}$", re.IGNORECASE)).first
        if option.count():
            option.click()
        else:
            input_locator.press("Enter")
        page.wait_for_timeout(200)

    def place_order(
        self,
        symbol: str,
        qty: int,
        side: Literal["buy", "sell"],
        order_type: Literal["Market", "Limit", "Stop"] = "Market",
        limit_stop_price: float | None = None,
    ) -> dict:
        if order_type in ("Limit", "Stop") and limit_stop_price is None:
            raise ValueError(f"limit_stop_price is required for {order_type} orders.")

        page = self._ensure_page()
        page.goto(f"{BASE_URL}/trade/stocks", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        if self._is_on_auth_page(page):
            raise SessionExpiredError(
                "Session has expired. Re-run auth_setup.py to log in again."
            )

        symbol_input = page.get_by_placeholder("Look up Symbol/Company Name").first
        symbol_input.click()
        symbol_input.fill(symbol.upper())
        page.wait_for_timeout(1500)

        suggestion = page.locator("[class*='suggestion'], [class*='lookup'] li, [class*='result'] li, [role='option']").first
        if suggestion.count():
            suggestion.click()
        else:
            symbol_input.press("Enter")
        page.wait_for_timeout(500)

        action_input = (
            page.locator("input[aria-label='action']")
            .or_(page.get_by_label("Action"))
            .first
        )
        self._select_dropdown_option(page, action_input, side.capitalize())

        qty_input = (
            page.locator("input[aria-label='quantity']")
            .or_(page.get_by_label("Quantity"))
            .or_(page.locator("input[type='number']"))
            .first
        )
        self._set_text_input(qty_input, str(qty))

        order_input = (
            page.locator("input[aria-label='order-type']")
            .or_(page.get_by_label("Order Type"))
            .first
        )
        self._select_dropdown_option(page, order_input, order_type)

        if limit_stop_price is not None:
            price_input = (
                page.get_by_label("Price")
                .or_(page.get_by_label("Limit Price"))
                .or_(page.get_by_label("Stop Price"))
                .or_(page.locator("input[name='price']"))
                .first
            )
            price_input.click()
            price_input.fill(str(limit_stop_price))

        preview_btn = page.get_by_role("button", name="Preview Order").first
        preview_btn.click()
        page.wait_for_timeout(2000)

        submit_btn = (
            page.get_by_role("button", name="Submit Order")
            .or_(page.get_by_role("button", name="Submit"))
            .or_(page.get_by_role("button", name="Confirm"))
            .first
        )
        submit_btn.click()
        page.wait_for_timeout(2000)

        confirmation = self._scrape_confirmation(page)
        return confirmation

    def _scrape_confirmation(self, page: Page) -> dict:
        result: dict = {"status": "submitted"}

        success_locator = page.get_by_text("success").or_(
            page.get_by_text("Order Confirmation")
        ).or_(page.get_by_text("has been placed")).first

        if success_locator.count():
            container = success_locator.locator("..").first
            result["message"] = container.inner_text().strip()[:500]
            result["status"] = "confirmed"
        else:
            error_locator = page.get_by_text("error").or_(
                page.get_by_text("insufficient")
            ).or_(page.get_by_text("not available")).first
            if error_locator.count():
                result["status"] = "error"
                result["message"] = error_locator.inner_text().strip()[:500]
            else:
                result["message"] = page.locator("main").first.inner_text().strip()[:500] if page.locator("main").count() else ""

        return result

    @staticmethod
    def login_and_save_session(
        session_path: str | pathlib.Path = SESSION_FILE,
    ) -> None:
        session_path = pathlib.Path(session_path)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded")

            print(
                "\n[*] A browser window has opened.\n"
                "    1. Enter your email address.\n"
                "    2. Check your inbox for the login code.\n"
                "    3. Enter the code in the browser.\n"
                "    4. Wait until you see the Portfolio page.\n"
            )

            while True:
                page.wait_for_timeout(2000)
                host = page.evaluate("() => window.location.hostname").lower()
                path = page.evaluate("() => window.location.pathname").lower()
                if (
                    "investopedia.com" in host
                    and not host.startswith("auth.")
                    and "/simulator/" in path
                ):
                    break

            page.wait_for_timeout(3000)
            context.storage_state(path=str(session_path))
            print(f"[+] Session saved to {session_path}")

            browser.close()

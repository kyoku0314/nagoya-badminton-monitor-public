"""Fallback query using a real Chromium browser, without logging in or booking."""
from playwright.sync_api import sync_playwright

HOME = "https://www.net.city.nagoya.jp/sporec/index.html"
SEARCH = "https://www.net.city.nagoya.jp/cgi-bin/sp/sps04001"

class BrowserSession:
    def __init__(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.context = self.browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo",
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")

    def close(self):
        self.browser.close()
        self.playwright.stop()

    def query(self, day, fields):
        page = self.context.new_page()
        try:
            page.set_default_timeout(12000)
            page.goto(HOME, wait_until="domcontentloaded", timeout=15000)
            # Prefer the actual menu button visible on the homepage.
            link = page.get_by_role("link", name="利用日ごとの検索・予約")
            if not link.count():
                link = page.locator('a:has(img[alt*="利用日ごと"]), a[href*="sps04001"]')
            if link.count():
                link.first.click(timeout=15000)
            else:
                print("Homepage button not found; opening known search URL", flush=True)
                page.goto(SEARCH, wait_until="domcontentloaded", timeout=15000)
            page.locator('form[name="user"]').wait_for(timeout=12000)
            # The site's markup contains multiple forms; the visible date
            # selects are not necessarily descendants of form[name="user"].
            # Resolve visible controls globally rather than by form ancestry.
            for name in ("syumoku", "month", "day", "joken", "kyoyo1", "kyoyo2", "chiiki"):
                value = fields.get(name)
                if value is None:
                    continue
                candidates = page.locator(f'select[name="{name}"], input[name="{name}"]')
                visible = candidates.filter(visible=True)
                if not visible.count():
                    if name == "kyoyo2":
                        continue
                    raise RuntimeError(f"Visible control missing: {name}; DOM matches={candidates.count()}")
                element = visible.first
                tag = element.evaluate("(el) => el.tagName.toLowerCase()")
                kind = element.get_attribute("type")
                if tag == "select":
                    element.select_option(value=str(value), timeout=5000)
                elif kind in ("radio", "checkbox"):
                    option = page.locator(f'input[name="{name}"][value="{value}"]').filter(visible=True)
                    if not option.count():
                        raise RuntimeError(f"Option missing: {name}={value}")
                    option.first.check(timeout=5000)
                else:
                    element.fill(str(value), timeout=5000)
            selected = page.evaluate("""() => Object.fromEntries(
                ['syumoku','month','day','joken','kyoyo1','kyoyo2','chiiki'].map(n => {
                    const items = Array.from(document.querySelectorAll('[name="' + n + '"]'));
                    const el = items.find(e => e.getClientRects().length && e.type !== 'hidden');
                    return [n, el?.type === 'radio'
                        ? items.find(e => e.checked)?.value ?? null : el?.value ?? null];
                })
            )""")
            print(f"Browser form selections: {selected}", flush=True)
            for name in ("syumoku", "month", "day", "kyoyo1", "chiiki"):
                if selected.get(name) != fields.get(name):
                    raise RuntimeError(f"Selection mismatch: {name}={selected.get(name)} expected {fields.get(name)}")
            submit = page.locator('form[name="user"] input[name="button"]')
            if submit.count():
                submit.first.click(timeout=12000)
            else:
                page.evaluate("""() => document.forms['user'].submit()""")
            # A click can trigger a delayed navigation. Wait for the actual
            # results marker, not merely the previous page's load state.
            page.get_by_text("空き検索結果", exact=False).first.wait_for(
                state="attached", timeout=15000
            )
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            return page.content()
        except Exception:
            if page is not None:
                try:
                    page.screenshot(path="browser_failure.png", full_page=True, timeout=5000)
                    from pathlib import Path
                    Path("browser_failure.html").write_text(page.content(), encoding="utf-8")
                except Exception as capture_error:
                    print(f"Could not capture browser failure: {capture_error}", flush=True)
            raise
        finally:
            page.close()

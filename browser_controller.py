from playwright.sync_api import sync_playwright


class BrowserController:

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def start(self):
        self.playwright = sync_playwright().start()

        self.browser = self.playwright.chromium.launch(
            headless=False
        )

        self.context = self.browser.new_context()

        self.page = self.context.new_page()

    def open(self, url):
        self.page.goto(url)

    def get_title(self):
        return self.page.title()

    def get_url(self):
        return self.page.url

    def new_tab(self):
        self.page = self.context.new_page()

    def close_tab(self):
        self.page.close()

    def stop(self):
        self.browser.close()
        self.playwright.stop()

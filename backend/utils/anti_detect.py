"""Anti-detection utilities for Playwright browser automation."""

import random

STEALTH_JS = """
() => {
    // Override navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Override navigator.plugins
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const plugins = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' },
            ];
            plugins.length = 3;
            return plugins;
        }
    });

    // Override navigator.languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['pl-PL', 'pl', 'en-US', 'en']
    });

    // Override navigator.platform
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

    // Override navigator.hardwareConcurrency
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

    // Override navigator.deviceMemory
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

    // Override chrome runtime
    window.chrome = {
        runtime: { connect: () => {}, sendMessage: () => {} },
        loadTimes: () => ({}),
        csi: () => ({})
    };

    // Override permissions query
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);

    // Spoof WebGL vendor/renderer
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
        return getParameter.call(this, parameter);
    };

    // Remove Playwright/automation traces
    delete window.__playwright;
    delete window.__pw_manual;
    delete window.__PW_inspect;
}
"""

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 720},
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
]


def get_random_viewport():
    return random.choice(VIEWPORTS)


def get_random_user_agent():
    return random.choice(USER_AGENTS)


async def human_delay(page, min_ms=500, max_ms=2000):
    """Simulate human-like delay."""
    delay = random.randint(min_ms, max_ms)
    await page.wait_for_timeout(delay)


async def human_scroll(page):
    """Simulate human-like scrolling behavior."""
    scroll_amount = random.randint(200, 600)
    await page.mouse.wheel(0, scroll_amount)
    await human_delay(page, 300, 800)


async def human_mouse_move(page):
    """Simulate random mouse movement."""
    x = random.randint(100, 1200)
    y = random.randint(100, 700)
    await page.mouse.move(x, y, steps=random.randint(5, 15))
    await human_delay(page, 100, 300)

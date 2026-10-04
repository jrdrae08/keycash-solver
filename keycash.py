# language: Python, file: keycash.py
# target: Python 3.10+, selenium 4.15+
# pip install selenium
#
# CLI usage (local):
#   python keycash.py                 # headless, 30 min
#   python keycash.py --duration 3600
#   python keycash.py --show
#
# Web service (Render):
#   starts an HTTP server; a GET / or /run triggers one solver pass
"""
KeyCash solver. Runs as a CLI or as a Render web service.
On Render, cron-job.org pings the service URL to trigger runs.
"""

import argparse
import os
import random
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

DEFAULT_USER = os.environ.get("KEYCASH_USER", "")
DEFAULT_PASS = os.environ.get("KEYCASH_PASS", "")

DEFAULT_DURATION = int(os.environ.get("SOLVER_DURATION", "1800"))


# =========================================================
# SOLVER BODY — injected into every page. String only; Python never runs it.
# =========================================================

SOLVER_JS = r"""
(function () {
  'use strict';

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const rand  = (min, max) => Math.random() * (max - min) + min;
  const randInt = (min, max) => Math.floor(rand(min, max + 1));

  function setNativeValue(el, value) {
    const proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, value);
  }

  async function humanType(el, text, wpm) {
    wpm = wpm || 62;
    el.focus();
    await sleep(rand(120, 320));
    const mean = 60000 / (wpm * 5);
    for (const ch of text) {
      setNativeValue(el, el.value + ch);
      el.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
      el.dispatchEvent(new InputEvent('input', { data: ch, inputType: 'insertText', bubbles: true }));
      el.dispatchEvent(new KeyboardEvent('keyup', { key: ch, bubbles: true }));
      await sleep(Math.max(40, mean * (0.6 + Math.random() * 0.8)));
    }
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  async function humanClick(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const x = r.left + r.width  * rand(0.35, 0.65);
    const y = r.top  + r.height * rand(0.35, 0.65);
    const base = { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 };
    el.dispatchEvent(new MouseEvent('mouseover', base));
    el.dispatchEvent(new MouseEvent('mousemove', base));
    el.dispatchEvent(new MouseEvent('mousedown', base));
    await sleep(rand(40, 90));
    el.dispatchEvent(new MouseEvent('mouseup', base));
    el.dispatchEvent(new MouseEvent('click', base));
    return true;
  }

  function hardClear(el) {
    if (!el) return;
    el.focus();
    setNativeValue(el, '');
    el.dispatchEvent(new InputEvent('input', { inputType: 'deleteContentBackward', bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function splitGraphemes(s) {
    if (typeof Intl !== 'undefined' && Intl.Segmenter) {
      const seg = new Intl.Segmenter('en', { granularity: 'grapheme' });
      return [...seg.segment(s)].map(x => x.segment);
    }
    return [...s];
  }
  const isEmoji = (g) => /\p{Extended_Pictographic}/u.test(g);

  async function clickNext(input, re) {
    re = re || /next|submit|verify|continue/i;
    const scope = (input && (input.closest('form') || input.closest('div')?.parentElement)) || document;
    let btn = [...scope.querySelectorAll('button')].find(b => re.test((b.id || '') + ' ' + (b.textContent || '').trim()));
    if (!btn) btn = [...document.querySelectorAll('button')].find(b => re.test((b.id || '') + ' ' + (b.textContent || '').trim()));
    if (btn) return humanClick(btn);
    return false;
  }

  let startClicked = false;
  function tryClickStart() {
    const btn = document.getElementById('start-btn');
    if (!btn) { if (startClicked) startClicked = false; return; }
    if (startClicked) return;
    const r = btn.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    console.log('[start] clicking #start-btn');
    btn.click();
    startClicked = true;
  }
  setInterval(function () { if (document.body) tryClickStart(); }, 500);

  function readEquation(el) {
    const t = (el && el.textContent || '').trim();
    const m = t.match(/^(\d+)\s*([+\-*/×÷])\s*(\d+)\s*=\s*\?$/);
    return m ? { a: parseInt(m[1], 10), op: m[2], b: parseInt(m[3], 10) } : null;
  }
  function isTextChallenge(el) {
    const t = (el && el.textContent || '').trim();
    if (!t) return false;
    if (readEquation(el)) return false;
    if ([...splitGraphemes(t)].some(isEmoji)) return false;
    return /^[A-Z0-9]+$/i.test(t);
  }
  function isEmojiChallenge(el) {
    const t = (el && el.textContent || '').trim();
    return t ? [...splitGraphemes(t)].some(isEmoji) : false;
  }

  async function captchaText() {
    const d = document.getElementById('custom-captcha-display');
    const i = document.getElementById('custom-captcha-answer');
    const s = document.getElementById('custom-captcha-submit');
    if (!d || !i || !isTextChallenge(d)) return false;
    const raw = d.textContent.trim();
    hardClear(i);
    await humanType(i, raw.toUpperCase(), rand(48, 72));
    await sleep(rand(280, 620));
    if (s) await humanClick(s); else await clickNext(i);
    console.log('[captcha][text] typed: ' + raw);
    return true;
  }

  async function captchaEmoji() {
    const d = document.getElementById('custom-captcha-display');
    const i = document.getElementById('custom-captcha-answer');
    if (!d || !i || !isEmojiChallenge(d)) return false;
    const counts = new Map();
    const walker = document.createTreeWalker(d, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => {
        const p = n.parentElement;
        if (!p) return NodeFilter.FILTER_REJECT;
        const t = p.tagName;
        if (t === 'INPUT' || t === 'TEXTAREA' || t === 'BUTTON') return NodeFilter.FILTER_REJECT;
        return isEmoji(n.nodeValue || '') ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    let n;
    while ((n = walker.nextNode())) {
      for (const g of splitGraphemes(n.nodeValue)) {
        if (!isEmoji(g)) continue;
        counts.set(g, (counts.get(g) || 0) + 1);
      }
    }
    const total = [...counts.values()].reduce((s, k) => s + k, 0);
    if (total === 0) return false;
    hardClear(i);
    await sleep(rand(150, 350));
    await humanType(i, String(total), rand(40, 65));
    await sleep(rand(220, 520));
    await clickNext(i);
    console.log('[captcha][emoji] total: ' + total);
    return true;
  }

  async function captchaMath() {
    const d = document.getElementById('custom-captcha-display');
    const i = document.getElementById('custom-captcha-answer');
    if (!i) return false;
    let eq = readEquation(d);
    if (!eq) {
      const nodes = [...document.querySelectorAll('p, div, span, label, h1, h2, h3')];
      const line = nodes.find(x => /^\s*\d+\s*[+\-*/×÷]\s*\d+\s*=\s*\?\s*$/.test(x.textContent) && x.children.length === 0);
      if (!line) return false;
      eq = readEquation(line);
      if (!eq) return false;
    }
    const { a, op, b } = eq;
    let r;
    switch (op) {
      case '+': r = a + b; break;
      case '-': r = a - b; break;
      case '*': case '×': r = a * b; break;
      case '/': case '÷': r = b === 0 ? NaN : a / b; break;
      default: return false;
    }
    if (!Number.isFinite(r)) return false;
    hardClear(i);
    await sleep(rand(180, 400));
    await humanType(i, String(r), rand(42, 68));
    await clickNext(i);
    console.log('[captcha][math] ' + a + ' ' + op + ' ' + b + ' -> ' + r);
    return true;
  }

  let cBusy = false;
  async function captchaTick() {
    if (cBusy) return;
    cBusy = true;
    try {
      if (await captchaText())  { cBusy = false; return; }
      if (await captchaEmoji()) { cBusy = false; return; }
      if (await captchaMath())  { cBusy = false; return; }
    } catch (e) {}
    cBusy = false;
  }
  function attachObserver() {
    if (!document.body) { setTimeout(attachObserver, 50); return; }
    let t = null;
    new MutationObserver(() => {
      if (cBusy) return;
      clearTimeout(t);
      t = setTimeout(captchaTick, 400);
    }).observe(document.body, { childList: true, subtree: true });
    console.log('[captcha] observer attached');
  }
  attachObserver();
  setInterval(captchaTick, randInt(1500, 2600));

  function startMathSolver() {
    console.log('[Auto-Solver] Math solver initialized.');
    const hud = document.createElement('div');
    hud.style = 'position:fixed;top:10px;left:10px;padding:10px;background:rgba(0,0,0,0.8);color:#0f0;z-index:9999;font-family:monospace;border:1px solid #0f0;pointer-events:none;';
    (function attachHud() {
      const root = document.documentElement || document.body;
      if (!root) { setTimeout(attachHud, 50); return; }
      root.appendChild(hud);
    })();

    function solve(res) {
      if (!res || !res.success) return;
      if (document.body && document.body.innerText.includes('Failed to load question')) return;
      if (res.total !== undefined) {
        hud.innerHTML = 'TOTAL: ' + res.total + '<br>SOLVES: ' + res.math_solves;
        return;
      }
      if (res.a !== undefined && res.b !== undefined) {
        const a = parseFloat(res.a), b = parseFloat(res.b);
        let ans;
        switch (res.op) {
          case '-': ans = a - b; break;
          case 'x': case '×': ans = a * b; break;
          case '/': case '÷': ans = a / b; break;
          default:  ans = a + b; break;
        }
        console.log('[Auto-Solver] ' + a + ' ' + res.op + ' ' + b + ' = ' + ans);
        setTimeout(() => {
          const buttons = document.querySelectorAll('button, .option, .choice');
          for (const btn of buttons) {
            if (btn.innerText.trim() === String(ans) && !btn.disabled) {
              btn.click();
              console.log('[Auto-Solver] clicked: ' + ans);
              break;
            }
          }
        }, 400);
      }
    }

    const oldOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function () {
      this.addEventListener('load', function () {
        try { solve(JSON.parse(this.responseText)); } catch (e) {}
      });
      return oldOpen.apply(this, arguments);
    };
    const oldFetch = window.fetch;
    window.fetch = function () {
      return oldFetch.apply(this, arguments).then(r => {
        r.clone().text().then(t => { try { solve(JSON.parse(t)); } catch (e) {} });
        return r;
      });
    };

    setInterval(() => {
      ['result-modal', 'warning-modal'].forEach(id => {
        const m = document.getElementById(id);
        if (!m) return;
        if (m.classList.contains('hidden') || !m.classList.contains('flex')) return;
        const again = m.querySelector('a[href*="start-game=math"]');
        if (again) { console.log('[Auto-Solver] play again'); again.click(); }
      });
    }, 700);
  }

  const p = new URLSearchParams(window.location.search);
  const mode  = p.get('c');
  const start = p.get('start-game');
  if (start === 'math' || mode === 'math') {
    console.log('[KeyCash] Math mode detected.');
    startMathSolver();
  }
  captchaTick();
})();
"""


# =========================================================
# SELENIUM + SOLVER LOGIC
# =========================================================

SOLVER_TAGS = ("[KeyCash]", "[Auto-Solver]", "[captcha]", "[start]")


def build_driver(show: bool):
    opts = Options()
    if not show:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--log-level=3")
    opts.add_argument("--silent")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": SOLVER_JS},
    )
    return driver


def drain_console(driver):
    try:
        for entry in driver.get_log("browser"):
            msg = (entry.get("message") or "").strip()
            if not msg:
                continue
            if not any(tag in msg for tag in SOLVER_TAGS):
                continue
            if '"' in msg:
                a = msg.find('"')
                b = msg.rfind('"')
                if a != -1 and b > a:
                    msg = msg[a + 1:b]
            print(f"[page] {msg}", flush=True)
    except Exception:
        pass


def human_type(el, text, mean=0.10, sd=0.04):
    el.clear()
    for ch in text:
        el.send_keys(ch)
        time.sleep(max(0.03, random.gauss(mean, sd)))


def login(driver, wait, user, password):
    driver.get("https://keycash.pro/")
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='identifier']")))
    human_type(driver.find_element(By.CSS_SELECTOR, "input[name='identifier']"), user)
    time.sleep(random.uniform(0.2, 0.5))
    human_type(driver.find_element(By.CSS_SELECTOR, "input[name='password']"), password)
    time.sleep(random.uniform(0.4, 0.9))
    driver.find_element(By.CSS_SELECTOR, "button[name='login']").click()

    def gone(d):
        return not d.find_elements(By.CSS_SELECTOR, "input[name='identifier']")
    try:
        wait.until(gone)
    except TimeoutException:
        raise SystemExit("login failed — check credentials")
    print("[cli] login complete", flush=True)


def on_login_form(driver):
    return bool(driver.find_elements(By.CSS_SELECTOR, "input[name='identifier']"))


def goto_math(driver, wait, user, password):
    driver.get("https://keycash.pro/?c=games")
    time.sleep(2.0)

    if on_login_form(driver):
        print("[cli] hub asked for login — re-authenticating", flush=True)
        login(driver, wait, user, password)
        driver.get("https://keycash.pro/?c=games")
        time.sleep(2.0)

    anchors = driver.find_elements(By.CSS_SELECTOR, "a[href*='start-game=math']")
    if anchors:
        print("[cli] math card found — clicking", flush=True)
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", anchors[0])
            time.sleep(random.uniform(0.4, 0.9))
            anchors[0].click()
        except Exception:
            driver.get("https://keycash.pro/?start-game=math")
    else:
        print("[cli] no math card — going direct", flush=True)
        driver.get("https://keycash.pro/?start-game=math")

    time.sleep(2.0)
    if "start-game=math" not in driver.current_url:
        driver.get("https://keycash.pro/?start-game=math")
        time.sleep(2.0)
    print(f"[cli] landed on {driver.current_url}", flush=True)


def run_solver_once(user, password, duration, show=False):
    """One full pass: login, hub -> math, run for duration seconds, quit."""
    print(f"[run] starting solver (duration={duration}s, headless={not show})", flush=True)
    driver = build_driver(show)
    wait = WebDriverWait(driver, 20)
    try:
        driver.get("https://keycash.pro/")
        time.sleep(2.0)
        if on_login_form(driver):
            login(driver, wait, user, password)
        else:
            print("[cli] session reused", flush=True)

        goto_math(driver, wait, user, password)
        print(f"[cli] solver running for {duration}s", flush=True)

        deadline = time.time() + duration
        last_drain = 0
        while time.time() < deadline:
            if time.time() - last_drain > 1.0:
                drain_console(driver)
                last_drain = time.time()
            time.sleep(0.5)
        drain_console(driver)
        print("[cli] duration reached, exiting", flush=True)
    except Exception as e:
        print(f"[run] solver error: {e}", flush=True)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        print("[run] solver stopped", flush=True)


# =========================================================
# WEB SERVICE (Render) — ping triggers one run
# =========================================================

solver_running = False
solver_lock = threading.Lock()


def run_solver_threaded():
    global solver_running
    with solver_lock:
        if solver_running:
            print("[web] solver already running — ignoring ping", flush=True)
            return
        solver_running = True
    try:
        run_solver_once(
            DEFAULT_USER,
            DEFAULT_PASS,
            DEFAULT_DURATION,
            show=False,
        )
    finally:
        with solver_lock:
            solver_running = False


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK\n")

        if self.path.startswith("/run") or self.path == "/":
            print(f"[web] ping received: {self.path}", flush=True)
            threading.Thread(target=run_solver_threaded, daemon=True).start()
        else:
            # health check path — just respond, don't trigger
            pass

    def log_message(self, fmt, *args):
        # silence the default per-request logging
        return


def run_web_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[web] listening on 0.0.0.0:{port}", flush=True)
    print("[web] GET / or /run to trigger a solver pass", flush=True)
    server.serve_forever()


# =========================================================
# ENTRY POINT
# =========================================================


def parse_args():
    ap = argparse.ArgumentParser(description="KeyCash headless solver")
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--pass", dest="password", default=DEFAULT_PASS)
    ap.add_argument("--duration", type=int, default=DEFAULT_DURATION)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--serve", action="store_true",
                    help="run as a web service (Render mode)")
    return ap.parse_args()


def main():
    args = parse_args()

    # Render sets PORT — if it's present, we're in web-service mode
    if args.serve or os.environ.get("PORT"):
        print("[web] starting in web-service mode", flush=True)
        run_web_server()
        return

    # CLI mode — run once, exit
    if not args.user or not args.password:
        print("[cli] error: no credentials. Set KEYCASH_USER and KEYCASH_PASS, "
              "or pass --user and --pass.", flush=True)
        sys.exit(1)
    run_solver_once(args.user, args.password, args.duration, show=args.show)


if __name__ == "__main__":
    main()
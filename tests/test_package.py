"""End-to-end tests for the Asyntai AI Assistant Espanso package.

Each test takes the command from package.yml and runs it the way Espanso's
shell extension does (espanso-render/src/extension/shell.rs):

    Windows      powershell -Command <cmd>     (CREATE_NO_WINDOW, like Espanso)
    Linux, Mac   bash -c / zsh -c / sh -c <cmd>

with CONFIG and the ESPANSO_* variables in the environment, against a local
Asyntai server (ASYNTAI_BASE_URL, localhost only).

    python tests/test_package.py --base http://127.0.0.1:8019 --shell powershell
    python3 tests/test_package.py --base http://127.0.0.1:8019 --shell bash

The server must know two keys: a Starter key and a Free-plan key (see --key and
--free-key). Its chat stub must answer with the text the rig in this session
returns (a heading, bold, two links, Czech text, an emoji, and "You asked: <q>").
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.join(os.path.dirname(HERE), "package")
NAME = "asyntai-ai-assistant"

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("pip install pyyaml")


def load_commands():
    with open(os.path.join(PACKAGE, "package.yml"), encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    out = {}
    for match in doc["matches"]:
        shell_vars = [v for v in match["vars"] if v["type"] == "shell"]
        assert len(shell_vars) == 1, match["trigger"]
        out[match["trigger"]] = (match, shell_vars[0]["params"]["cmd"])
    return out


class Env:
    """A fake Espanso config dir with the package installed, and a fake home."""

    def __init__(self, shell):
        self.shell = shell
        self.root = tempfile.mkdtemp(prefix="asyntai_espanso_")
        self.config = os.path.join(self.root, "espanso config")  # a space, like macOS
        target = os.path.join(self.config, "match", "packages", NAME)
        shutil.copytree(PACKAGE, target)
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)

    def key_file(self):
        if self.shell == "powershell":
            return os.path.join(self.home, "asyntai", "espanso-api-key")
        return os.path.join(self.home, ".config", "asyntai", "espanso-api-key")

    def run(self, cmd, base, **espanso_vars):
        env = dict(os.environ)
        for k in ("ASYNTAI_API_KEY", "ESPANSO_FORM1_QUESTION", "ESPANSO_FORM1_KEY", "ESPANSO_CLIP"):
            env.pop(k, None)
        env["CONFIG"] = self.config
        env["ASYNTAI_BASE_URL"] = base
        if self.shell == "powershell":
            env["APPDATA"] = self.home
        else:
            env["HOME"] = self.home
            env.pop("XDG_CONFIG_HOME", None)
        for k, v in espanso_vars.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v

        if self.shell == "powershell":
            argv = ["powershell", "-Command", cmd]
            flags = 0x08000000  # CREATE_NO_WINDOW, as Espanso sets it
        else:
            argv = [self.shell, "-c", cmd]
            flags = 0
        p = subprocess.run(argv, env=env, capture_output=True, timeout=180, creationflags=flags)
        stdout = p.stdout.decode("utf-8")  # Espanso: String::from_utf8_lossy
        return p.returncode, stdout.strip(), p.stderr.decode("utf-8", "replace").strip()


def redirect_trap(code):
    """Two local servers: one answers every request with a redirect to the
    other, the other counts what reaches it. Returns (base_url, hits, stop)."""
    hits = []

    class Catch(BaseHTTPRequestHandler):
        def _any(self):
            hits.append((self.command, self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"success": true, "response": "STOLEN", "websites": []}')
        do_GET = do_POST = _any

        def log_message(self, *a):
            pass

    catch = HTTPServer(("127.0.0.1", 0), Catch)

    class Redirect(BaseHTTPRequestHandler):
        def _any(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            self.send_response(code)
            self.send_header("Location", "http://127.0.0.1:%d/steal" % catch.server_port)
            self.send_header("Content-Length", "0")
            self.end_headers()
        do_GET = do_POST = _any

        def log_message(self, *a):
            pass

    redirect = HTTPServer(("127.0.0.1", 0), Redirect)
    for srv in (catch, redirect):
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop():
        for srv in (catch, redirect):
            srv.shutdown()

    return "http://127.0.0.1:%d" % redirect.server_port, hits, stop


PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  ->  " + detail))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--shell", required=True)
    ap.add_argument("--key", default="esp_test_key_starter_123456")
    ap.add_argument("--free-key", default="esp_test_key_free_123456")
    ap.add_argument("--domain", default="espanso-shop.example")
    ap.add_argument("--log", default="", help="rig_calls.jsonl to check what the API received")
    a = ap.parse_args()

    cmds = load_commands()
    check("three triggers", sorted(cmds) == [":ask", ":asyntaikey", ":clipask"], str(sorted(cmds)))
    ask_cmd, clip_cmd, key_cmd = cmds[":ask"][1], cmds[":clipask"][1], cmds[":asyntaikey"][1]

    e = Env(a.shell)
    tag = "[%s] " % a.shell

    def ok(code, err):
        return code == 0 and err == ""

    # 1. ask before any key
    code, out, err = e.run(ask_cmd, a.base, ESPANSO_FORM1_QUESTION="Hello")
    check(tag + "no key -> setup hint", out == "Asyntai: add your API key first. Type :asyntaikey in an empty text field.", out)
    check(tag + "no key -> exit 0, no stderr", ok(code, err), "%s %s" % (code, err))

    # 2. key: wrong key is refused and not saved
    code, out, err = e.run(key_cmd, a.base, ESPANSO_FORM1_KEY="  wrong-key  ")
    check(tag + "wrong key refused", out.startswith("Asyntai: this is not a valid API key."), out)
    check(tag + "wrong key not saved", not os.path.exists(e.key_file()))
    check(tag + "wrong key -> exit 0, no stderr", ok(code, err), "%s %s" % (code, err))

    # 3. key: Free plan is refused with the server's own text, not saved
    code, out, err = e.run(key_cmd, a.base, ESPANSO_FORM1_KEY=a.free_key)
    check(tag + "free plan refused", out.startswith("Asyntai: API access requires a paid plan"), out)
    check(tag + "free key not saved", not os.path.exists(e.key_file()))

    # 4. key: empty
    code, out, err = e.run(key_cmd, a.base, ESPANSO_FORM1_KEY="   ")
    check(tag + "empty key", out == "Asyntai: the API key is empty. Type :asyntaikey and paste your key.", out)

    # 5. key: valid key saved, names the primary website
    code, out, err = e.run(key_cmd, a.base, ESPANSO_FORM1_KEY=" %s \n" % a.key)
    check(tag + "valid key saved message", out == "Asyntai API key saved. Your chatbot for %s is ready: type :ask to ask it." % a.domain, out)
    saved = open(e.key_file(), encoding="utf-8").read() if os.path.exists(e.key_file()) else None
    check(tag + "key file holds the trimmed key", saved == a.key, repr(saved))
    if a.shell != "powershell" and os.name != "nt":
        mode = oct(os.stat(e.key_file()).st_mode & 0o777)
        check(tag + "key file is 0600", mode == "0o600", mode)
    check(tag + "valid key -> exit 0, no stderr", ok(code, err), "%s %s" % (code, err))

    # 6. ask: hostile characters reach the API unchanged, answer made plain
    question = 'Can I return it? "quoted" \'single\' $HOME `whoami` %PATH% $(id) ; & | < > ü ž 😀'
    before = sum(1 for _ in open(a.log, encoding="utf-8")) if a.log and os.path.exists(a.log) else 0
    code, out, err = e.run(ask_cmd, a.base, ESPANSO_FORM1_QUESTION=question)
    check(tag + "ask -> exit 0, no stderr", ok(code, err), "%s %s" % (code, err))
    check(tag + "question reached the API unchanged", out.endswith("You asked: " + question), out[-200:])
    check(tag + "heading marks removed", "##" not in out and out.startswith("Returns"), out[:60])
    check(tag + "bold removed", "**" not in out and "within 30 days." in out, out)
    check(tag + "link as text (url)", "return policy (https://espanso-shop.example/returns)" in out, out)
    check(tag + "mailto link as its label", "write to support@espanso-shop.example." in out and "mailto:" not in out, out)
    check(tag + "Czech + emoji intact", "Příliš žluťoučký kůň úpěl ďábelské ódy 😀" in out, out)
    check(tag + "special chars in answer intact", '"quotes" & <tags> $HOME `tick` %PATH%' in out, out)
    check(tag + "no triple blank lines", "\n\n\n" not in out, repr(out))
    check(tag + "no CR characters", "\r" not in out, repr(out[:80]))
    if a.log and os.path.exists(a.log):
        rows = [json.loads(l) for l in open(a.log, encoding="utf-8")][before:]
        check(tag + "one API call per ask", len(rows) == 1, str(len(rows)))
        if rows:
            check(tag + "session marked espanso_ask_", rows[-1]["session_key"].startswith("api_espanso_ask_"), rows[-1]["session_key"])
            check(tag + "primary website used", rows[-1]["website"] == a.domain, rows[-1]["website"])

    # 7. two asks use two sessions
    if a.log and os.path.exists(a.log):
        e.run(ask_cmd, a.base, ESPANSO_FORM1_QUESTION="first")
        e.run(ask_cmd, a.base, ESPANSO_FORM1_QUESTION="second")
        rows = [json.loads(l) for l in open(a.log, encoding="utf-8")]
        check(tag + "each ask is a new session", rows[-1]["session_key"] != rows[-2]["session_key"])

    # 8. clip: multi-line clipboard text
    clip = "Hi,\r\nwhat is your return window?\nThanks, Jana"
    code, out, err = e.run(clip_cmd, a.base, ESPANSO_CLIP=clip)
    check(tag + "clip -> answer", "You asked: Hi," in out and "Thanks, Jana" in out, out[-120:])
    check(tag + "clip -> exit 0, no stderr", ok(code, err), "%s %s" % (code, err))

    # 9. empty question / empty clipboard
    code, out, err = e.run(ask_cmd, a.base, ESPANSO_FORM1_QUESTION="  \n ")
    check(tag + "empty question", out == "Asyntai: the question is empty. Type :ask and write a question.", out)
    code, out, err = e.run(clip_cmd, a.base, ESPANSO_CLIP=None)
    check(tag + "empty clipboard", out == "Asyntai: the question is empty. Type :ask and write a question.", out)

    # 10. server errors become readable text
    code, out, err = e.run(ask_cmd, a.base, ESPANSO_FORM1_QUESTION="LIMIT")
    check(tag + "429 limit message", out == "Asyntai: Message limit reached. Please upgrade your plan.", out)
    code, out, err = e.run(ask_cmd, a.base, ESPANSO_FORM1_QUESTION="BROKEN")
    check(tag + "503 message", out == "Asyntai: AI service error: model down", out)

    # 11. a key that stops working
    code, out, err = e.run(ask_cmd, a.base, ESPANSO_FORM1_QUESTION="hi", ASYNTAI_API_KEY="revoked-key")
    check(tag + "revoked key message", out == "Asyntai: this API key no longer works. Type :asyntaikey and paste a new key.", out)

    # 12. ASYNTAI_API_KEY overrides the saved file
    code, out, err = e.run(ask_cmd, a.base, ESPANSO_FORM1_QUESTION="env key", ASYNTAI_API_KEY=a.key)
    check(tag + "env key works", out.endswith("You asked: env key"), out[-60:])

    # 13. server down
    code, out, err = e.run(ask_cmd, "http://127.0.0.1:9", ESPANSO_FORM1_QUESTION="hi")
    check(tag + "server down message", out == "Asyntai: no answer from asyntai.com. Check your internet connection and try again.", out)
    check(tag + "server down -> exit 0, no stderr", ok(code, err), "%s %s" % (code, err))

    # 14. redirects are never followed: no question, no key to another host
    for code in (307, 302):
        base, hits, stop = redirect_trap(code)
        code_, out, err = e.run(ask_cmd, base, ESPANSO_FORM1_QUESTION="secret question", ASYNTAI_API_KEY=a.key)
        check(tag + "%d on ask -> error text" % code, out == "Asyntai: the server answered with status %d." % code, out)
        code_, out, err = e.run(key_cmd, base, ESPANSO_FORM1_KEY="redirect-test-key")
        check(tag + "%d on key check -> error text" % code, out == "Asyntai: the server answered with status %d." % code, out)
        check(tag + "%d -> nothing reached the other host" % code, hits == [], str(hits))
        stop()
    check(tag + "redirect key not saved", open(e.key_file(), encoding="utf-8").read() == a.key)

    shutil.rmtree(e.root, ignore_errors=True)
    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

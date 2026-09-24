# -*- coding: utf-8 -*-
"""Почтовые серверы-заглушки для тестов рассылки.

Настоящие SMTP и IMAP, но на локальном адресе и в памяти: проверяется
сам разговор по протоколу — вход, отправка, поиск и чтение писем, — а
не наши догадки о нём. Внешняя сеть не нужна.
"""
import base64
import socketserver
import threading


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeSMTP:
    """Принимает письма и складывает их в self.sent как (от, кому, текст)."""

    def __init__(self, user="me@test.local", password="secret",
                 reject_rcpt=(), spam=False):
        self.user, self.password = user, password
        self.reject_rcpt = {r.lower() for r in reject_rcpt}
        self.spam = spam
        self.sent = []
        self.logins = 0
        outer = self

        class H(socketserver.StreamRequestHandler):
            def say(self, line):
                self.wfile.write((line + "\r\n").encode())

            def handle(self):
                self.say("220 fake ESMTP")
                authed, frm, rcpt = False, "", []
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    up = line.upper()
                    if up.startswith(("EHLO", "HELO")):
                        self.wfile.write(b"250-fake\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
                    elif up.startswith("AUTH PLAIN"):
                        parts = line.split(" ", 2)
                        blob = parts[2] if len(parts) > 2 else ""
                        if not blob:
                            self.say("334 ")
                            blob = self.rfile.readline().decode().strip()
                        _, u, p = base64.b64decode(blob).decode().split("\0")
                        if u == outer.user and p == outer.password:
                            authed = True
                            outer.logins += 1
                            self.say("235 ok")
                        else:
                            self.say("535 5.7.8 authentication failed")
                    elif up.startswith("AUTH LOGIN"):
                        self.say("334 VXNlcm5hbWU6")
                        u = base64.b64decode(self.rfile.readline().strip()).decode()
                        self.say("334 UGFzc3dvcmQ6")
                        p = base64.b64decode(self.rfile.readline().strip()).decode()
                        if u == outer.user and p == outer.password:
                            authed = True
                            outer.logins += 1
                            self.say("235 ok")
                        else:
                            self.say("535 5.7.8 authentication failed")
                    elif up.startswith("MAIL FROM"):
                        if not authed:
                            self.say("530 auth required")
                            continue
                        frm, rcpt = line.split(":", 1)[1].strip(" <>").split(">")[0], []
                        self.say("250 ok")
                    elif up.startswith("RCPT TO"):
                        addr = line.split(":", 1)[1].strip().strip("<>").lower()
                        if addr in outer.reject_rcpt:
                            self.say("550 5.1.1 user unknown")
                        else:
                            rcpt.append(addr)
                            self.say("250 ok")
                    elif up == "DATA":
                        self.say("354 go")
                        buf = []
                        while True:
                            ln = self.rfile.readline()
                            if ln in (b".\r\n", b".\n", b""):
                                break
                            buf.append(ln[1:] if ln.startswith(b"..") else ln)
                        if outer.spam:
                            self.say("554 5.7.1 Message rejected under suspicion of SPAM")
                        else:
                            outer.sent.append((frm, list(rcpt), b"".join(buf)))
                            self.say("250 queued")
                    elif up in ("RSET", "NOOP"):
                        self.say("250 ok")
                    elif up == "QUIT":
                        self.say("221 bye")
                        return
                    else:
                        self.say("502 not implemented")

        self.server = _Server(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeIMAP:
    """Ящик «Входящие» в памяти: self.add(письмо в байтах) кладёт письмо."""

    def __init__(self, user="me@test.local", password="secret", validity=777):
        self.user, self.password, self.validity = user, password, validity
        self.box = []          # [(uid, bytes)]
        self.next_uid = 1
        self.seen_flags_changed = False
        outer = self

        class H(socketserver.StreamRequestHandler):
            def say(self, line):
                self.wfile.write((line + "\r\n").encode())

            def handle(self):
                self.say("* OK fake IMAP4rev1 ready")
                authed = False
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    tag, _, rest = line.partition(" ")
                    cmd, _, args = rest.partition(" ")
                    cmd = cmd.upper()
                    if cmd == "CAPABILITY":
                        self.say("* CAPABILITY IMAP4rev1 AUTH=PLAIN")
                        self.say("%s OK done" % tag)
                    elif cmd == "LOGIN":
                        u, p = [x.strip('"') for x in args.split(" ", 1)]
                        if u == outer.user and p == outer.password:
                            authed = True
                            self.say("%s OK logged in" % tag)
                        else:
                            self.say("%s NO [AUTHENTICATIONFAILED] Invalid credentials" % tag)
                    elif cmd in ("SELECT", "EXAMINE"):
                        if not authed:
                            self.say("%s BAD not authenticated" % tag)
                            continue
                        self.say("* %d EXISTS" % len(outer.box))
                        self.say("* OK [UIDVALIDITY %d] ok" % outer.validity)
                        self.say("%s OK [READ-ONLY] done" % tag)
                    elif cmd == "UID":
                        sub, _, a = args.partition(" ")
                        sub = sub.upper()
                        if sub == "SEARCH":
                            uids = [u for u, _ in outer.box]
                            parts = a.split()
                            if parts and parts[0].upper() == "UID":
                                lo = int(parts[1].split(":")[0])
                                hit = [u for u in uids if u >= lo]
                                # Как у настоящих серверов: «n:*» при пустом
                                # хвосте возвращает последнее письмо.
                                if not hit and uids:
                                    hit = [uids[-1]]
                                uids = hit
                            self.say("* SEARCH %s" % " ".join(map(str, uids)))
                            self.say("%s OK done" % tag)
                        elif sub == "FETCH":
                            uid = int(a.split(" ", 1)[0])
                            if "PEEK" not in a.upper():
                                outer.seen_flags_changed = True
                            for i, (u, data) in enumerate(outer.box, 1):
                                if u == uid:
                                    self.wfile.write(
                                        b"* %d FETCH (UID %d BODY[] {%d}\r\n"
                                        % (i, u, len(data)) + data + b")\r\n")
                            self.say("%s OK done" % tag)
                        else:
                            self.say("%s BAD unknown" % tag)
                    elif cmd == "LOGOUT":
                        self.say("* BYE")
                        self.say("%s OK bye" % tag)
                        return
                    elif cmd == "NOOP":
                        self.say("%s OK" % tag)
                    else:
                        self.say("%s BAD unknown command" % tag)

        self.server = _Server(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        self.box.append((self.next_uid, data))
        self.next_uid += 1

    def close(self):
        self.server.shutdown()
        self.server.server_close()

# -*- coding: utf-8 -*-
"""Проверка почтовых адресов: есть ли домен, и принимает ли он адрес.

Про честность результата. Проверка по SMTP — не приговор, а оценка:

  • половина доменов настроена как catch-all и отвечает «да» на любой
    адрес. Такой домен надо распознать заранее — иначе валидатор радостно
    подтвердит все двенадцать кандидатов подряд;
  • крупные почтовые сервисы (Яндекс, Mail.ru, Google) отвечают уклончиво
    или греют отправителя — их ответ «не знаю» честнее трактовать как
    «не знаю», а не как «адрес плохой»;
  • слишком быстрый перебор с одного IP приводит к блокировке. Паузы
    здесь не вежливость, а условие работоспособности.

Поэтому результат — одно из: ok, catch_all, bad, unknown.
"""
import random
import smtplib
import socket
import string
import time

# Домены, чей SMTP не даёт полезного ответа. Спрашивать их бессмысленно:
# получим «unknown» после трёх секунд ожидания.
OPAQUE = {"yandex.ru", "ya.ru", "mail.ru", "bk.ru", "inbox.ru", "list.ru",
          "gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
          "icloud.com", "me.com", "rambler.ru", "internet.ru"}

_mx_cache = {}


def mx_hosts(domain):
    """Почтовые серверы домена. Без dnspython откатываемся на A-запись."""
    domain = (domain or "").lower().strip()
    if not domain:
        return []
    if domain in _mx_cache:
        return _mx_cache[domain]
    hosts = []
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=6)
        hosts = [str(r.exchange).rstrip(".") for r in
                 sorted(answers, key=lambda r: r.preference)]
    except Exception:
        # Нет dnspython или нет MX — пробуем сам домен: у мелких хостингов
        # почта часто висит на том же адресе, что и сайт.
        try:
            socket.getaddrinfo(domain, 25)
            hosts = [domain]
        except Exception:
            hosts = []
    _mx_cache[domain] = hosts
    return hosts


def _probe(host, domain, addresses, timeout=8, sender="verify@example.com"):
    """Один сеанс SMTP на несколько адресов сразу.

    Открывать соединение на каждый адрес — верный способ попасть в
    чёрный список: десять коннектов подряд с одного IP выглядят как
    перебор, чем они, строго говоря, и являются.
    """
    out = {}
    try:
        srv = smtplib.SMTP(timeout=timeout)
        srv.connect(host, 25)
        srv.helo("example.com")
        srv.mail(sender)
        for addr in addresses:
            try:
                code, _ = srv.rcpt(addr)
            except Exception:
                code = 0
            out[addr] = code
            time.sleep(0.4)
        try:
            srv.quit()
        except Exception:
            pass
    except Exception:
        return {}
    return out


def _random_local(domain):
    tail = "".join(random.choice(string.ascii_lowercase) for _ in range(14))
    return "zz%s@%s" % (tail, domain)


def check(addresses, timeout=8):
    """Проверить пачку адресов одного домена.

    Возвращает {адрес: 'ok'|'catch_all'|'bad'|'unknown'}.
    """
    addresses = [a.lower().strip() for a in addresses if a and "@" in a]
    if not addresses:
        return {}
    domain = addresses[0].split("@")[1]
    if domain in OPAQUE:
        return {a: "unknown" for a in addresses}

    hosts = mx_hosts(domain)
    if not hosts:
        # Нет почтовых серверов — писать некуда, и это как раз надёжный
        # отрицательный ответ, в отличие от молчания конкретного адреса.
        return {a: "bad" for a in addresses}

    canary = _random_local(domain)
    codes = _probe(hosts[0], domain, [canary] + addresses, timeout=timeout)
    if not codes:
        return {a: "unknown" for a in addresses}

    catch_all = 200 <= codes.get(canary, 0) < 300
    out = {}
    for a in addresses:
        code = codes.get(a, 0)
        if catch_all:
            out[a] = "catch_all"
        elif 200 <= code < 300:
            out[a] = "ok"
        elif 500 <= code < 600:
            out[a] = "bad"
        else:
            out[a] = "unknown"
    return out

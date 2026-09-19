# -*- coding: utf-8 -*-
"""Точка входа.

Программа — это локальный Flask плюс нативное окно поверх него. Выбор в
пользу такой связки, а не Qt: интерфейс здесь в основном таблицы и формы,
на HTML они пишутся быстрее, а окно pywebview выглядит как обычное
приложение — пользователь не догадывается, что внутри веб.

Порт выбирается свободный и слушается только на 127.0.0.1: фиксированный
номер рано или поздно окажется занят чужой программой, а внешний интерфейс
открыл бы базу всей локальной сети.
"""
import os
import socket
import sys
import threading
import time

from app import db, settings, worker
from app.web import create_app


def _quiet_console():
    """Под pythonw консоли нет, и sys.stdout равен None.

    Любой print — в том числе из Flask и requests — тогда падает с
    AttributeError, и программа закрывается молча, ничего не показав.
    Поэтому вывод уходит в файл рядом с базой: если что-то сломается,
    человеку будет что прислать, а окна с чёрной консолью он не увидит.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = os.path.join(settings.data_dir(), "navodka.log")
    try:
        stream = open(path, "a", encoding="utf-8", buffering=1)
    except Exception:
        stream = open(os.devnull, "w")
    sys.stdout = sys.stderr = stream
    print("\n=== запуск %s ===" % time.strftime("%Y-%m-%d %H:%M:%S"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until_up(port, timeout=15):
    """Открывать окно раньше, чем сервер ответит, нельзя: получим пустую
    страницу с ошибкой соединения вместо интерфейса."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.12)
    return False


def main():
    _quiet_console()
    # Как часто потоки уступают друг другу общую блокировку.
    #
    # По умолчанию поток держит её пять миллисекунд, и этого достаточно,
    # чтобы фоновый обход, занятый разбором разметки, заметно подвешивал
    # окно: оно рисуется в главном потоке и ждёт своей очереди. Две
    # миллисекунды делают переключения чаще; на скорости обхода это не
    # сказывается — он всё равно ждёт чужие серверы.
    try:
        sys.setswitchinterval(0.002)
    except Exception:
        pass
    db.init()
    worker.start()

    port = free_port()
    app = create_app()
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, threaded=True,
                               debug=False, use_reloader=False),
        daemon=True).start()

    if not wait_until_up(port):
        print("Не удалось запустить внутренний сервер", file=sys.stderr)
        return 1

    url = "http://127.0.0.1:%d/" % port
    if _open_window(url):
        return 0
    return _open_browser(url)


def _open_window(url):
    """Нативное окно. False — значит, не вышло, и надо открыть браузер.

    Отдельная ветка на случай «библиотека есть, а окно не создаётся»: на
    Windows 10 без среды WebView2 pywebview импортируется нормально и
    падает только на старте, а на Linux — при отсутствии GTK или Qt.
    Раньше программа в этот момент просто закрывалась, и под pythonw это
    выглядело как «ничего не произошло».
    """
    try:
        import webview
    except Exception as e:
        print("Окно недоступно (%s)" % e)
        return False
    try:
        webview.create_window(settings.APP_NAME, url, width=1280, height=860,
                              min_size=(940, 620))
        webview.start()
        return True
    except Exception as e:
        print("Окно не открылось (%s) — перехожу в браузер" % e)
        return False


def _open_browser(url):
    """Запасной путь: интерфейс в браузере, сервер живёт до закрытия окна.

    Хуже нативного окна ровно одним: браузер придётся закрыть вручную. Но
    это несравнимо лучше, чем программа, которая не открывается вовсе.
    """
    import webbrowser
    print("Интерфейс: %s" % url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

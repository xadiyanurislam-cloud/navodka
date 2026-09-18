# -*- coding: utf-8 -*-
"""Подготовка окружения и запуск.

Почему это здесь, а не в bat-файле. Первый вариант запуска был написан
целиком на cmd, и это оказалось ошибкой: русский текст внутри bat зависит
от кодовой страницы консоли, кавычки в вызове PowerShell экранируются
по-разному в разных версиях Windows, а ошибка в любой строке проявляется
как мгновенно закрывшееся чёрное окно без единого слова.

Теперь bat-файл — пятнадцать строк на латинице, которые ищут Python и
передают управление сюда. Всё остальное на Python: он одинаково работает
на любой Windows, умеет в Unicode и может внятно сказать, что пошло не
так.
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(ROOT, ".venv")
IS_WIN = os.name == "nt"


# Всё, что печатается, дублируется в файл рядом с программой. Окно
# консоли закрывается вместе с ошибкой, и без записи человеку нечего
# прислать, кроме слов «не работает».
_LOG = None


def say(text=""):
    """Печать, которая не падает на кириллице в старой консоли."""
    try:
        print(text)
    except UnicodeEncodeError:
        try:
            sys.stdout.buffer.write((text + "\n").encode("utf-8", "replace"))
        except Exception:
            pass
    except Exception:
        pass
    try:
        sys.stdout.flush()
    except Exception:
        pass
    if _LOG:
        try:
            _LOG.write(text + "\n")
            _LOG.flush()
        except Exception:
            pass


def venv_python(windowed=False):
    if IS_WIN:
        name = "pythonw.exe" if windowed else "python.exe"
        return os.path.join(VENV, "Scripts", name)
    return os.path.join(VENV, "bin", "python")


def ensure_venv():
    if os.path.exists(venv_python()):
        return True
    say("Первый запуск: готовлю окружение. Это займёт минуту-две.")
    try:
        subprocess.check_call([sys.executable, "-m", "venv", VENV])
    except Exception as e:
        say("")
        say("Не удалось создать окружение: %s" % e)
        say("Обычно это значит, что Python установлен без модуля venv.")
        say("Переустановите его с python.org.")
        return False
    return True


def ensure_packages():
    """Ставим библиотеки, только если список изменился.

    Сравниваем содержимое файла, а не дату: дата меняется от любого
    копирования папки, и установка запускалась бы каждый раз.
    """
    req = os.path.join(ROOT, "requirements.txt")
    stamp = os.path.join(VENV, "requirements.stamp")
    try:
        with open(req, "rb") as f:
            want = f.read()
    except Exception:
        say("Рядом с программой нет requirements.txt — архив распакован не целиком.")
        return False
    if os.path.exists(stamp):
        with open(stamp, "rb") as f:
            if f.read() == want:
                return True

    say("Устанавливаю библиотеки...")
    py = venv_python()
    for args in (["-m", "pip", "install", "--upgrade", "pip"],
                 ["-m", "pip", "install", "-r", req]):
        try:
            subprocess.check_call([py] + args + ["--quiet", "--disable-pip-version-check"])
        except Exception as e:
            say("")
            say("Библиотеки не установились: %s" % e)
            say("Чаще всего дело в отсутствии интернета или в корпоративном прокси.")
            return False
    with open(stamp, "wb") as f:
        f.write(want)
    return True


def make_shortcut():
    """Ярлык на рабочем столе — один раз.

    Через отдельный файл .ps1, а не через powershell -Command: путь с
    кириллицей и пробелами, пропущенный через кавычки cmd, ломается на
    ровном месте, и отладить это в чужой системе невозможно.
    """
    if not IS_WIN:
        return
    done = os.path.join(VENV, "shortcut.done")
    if os.path.exists(done):
        return
    bat = os.path.join(ROOT, "Navodka.bat")
    ico = os.path.join(ROOT, "build", "icon.ico")
    ps = os.path.join(VENV, "shortcut.ps1")
    script = (
        "$d = [Environment]::GetFolderPath('Desktop')\n"
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        "(Join-Path $d 'Наводка.lnk'))\n"
        "$s.TargetPath = '%s'\n"
        "$s.WorkingDirectory = '%s'\n"
        "$s.IconLocation = '%s'\n"
        "$s.Description = 'Поиск B2B-лидов'\n"
        "$s.Save()\n" % (bat, ROOT, ico))
    try:
        # BOM обязателен: без него PowerShell читает файл в системной
        # кодировке и кириллица в имени ярлыка превращается в мусор.
        with open(ps, "w", encoding="utf-8-sig") as f:
            f.write(script)
        subprocess.check_call(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        say("Ярлык «Наводка» добавлен на рабочий стол.")
    except Exception:
        # Ярлык — удобство, а не условие работы. Политики PowerShell в
        # корпоративной сети часто запрещают запуск скриптов, и падать
        # из-за этого нельзя.
        say("Ярлык создать не удалось — запускайте Navodka.bat из этой папки.")
    with open(done, "w") as f:
        f.write("1")


def launch():
    """Запуск и проверка, что программа действительно открылась.

    Без проверки самый частый исход выглядит так: окно мигнуло и
    закрылось, а почему — неизвестно. Теперь при неудаче показываем
    последние строки журнала прямо здесь.
    """
    main = os.path.join(ROOT, "main.py")
    py = venv_python(windowed=IS_WIN)
    if not os.path.exists(py):
        py = venv_python()

    say("Запускаю...")
    kwargs = {"cwd": ROOT}
    if IS_WIN:
        # DETACHED_PROCESS | CREATE_NO_WINDOW: программа живёт своей
        # жизнью после закрытия этого окна и не тащит за собой консоль.
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen([py, main], **kwargs)
    except (OSError, ValueError):
        # Отдельные сборки Windows не принимают эту пару флагов. Лучше
        # запустить с консолью, чем не запустить вовсе.
        kwargs.pop("creationflags", None)
        proc = subprocess.Popen([py, main], **kwargs)

    for _ in range(12):
        time.sleep(0.5)
        if proc.poll() is not None:
            break
    if proc.poll() is None:
        say("Готово — программа открылась.")
        return True

    say("")
    say("Программа закрылась сразу после запуска. Последние строки журнала:")
    say("")
    log = os.path.join(_data_dir(), "navodka.log")
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            for line in f.readlines()[-25:]:
                say("   " + line.rstrip())
        say("")
        say("Журнал целиком: %s" % log)
    except Exception:
        say("   журнал не найден (%s)" % log)
    return False


def _data_dir():
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "navodka")


def main():
    global _LOG
    try:
        _LOG = open(os.path.join(ROOT, "launcher.log"), "w", encoding="utf-8")
        _LOG.write("=== %s ===\nPython: %s\n%s\n\n"
                   % (time.strftime("%Y-%m-%d %H:%M:%S"), sys.version,
                      sys.executable))
    except Exception:
        _LOG = None

    # Версию печатаем первой строкой. После обновления это единственный
    # способ убедиться, что запускается новая программа, а не старая копия
    # из соседней папки.
    try:
        sys.path.insert(0, ROOT)
        from app import settings as _s
        say("Наводка %s — %s" % (_s.VERSION, _s.BUILD))
        say("Папка: %s" % ROOT)
        say("")
    except Exception:
        pass

    if sys.version_info < (3, 9):
        say("Нужен Python 3.9 или новее, а установлен %d.%d."
            % sys.version_info[:2])
        return 1
    os.chdir(ROOT)
    if not ensure_venv():
        return 1
    if not ensure_packages():
        return 1
    make_shortcut()
    return 0 if launch() else 1


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        # Голый traceback в чужой консоли читается плохо, а закрывшееся
        # окно не читается вовсе. Показываем и то и другое: понятную
        # строку сверху и подробности для разбора ниже.
        import traceback
        say("")
        say("Запуск прервался из-за ошибки. Подробности:")
        say("")
        details = traceback.format_exc()
        for line in details.strip().splitlines():
            say("   " + line)
        say("")
        say("Эти строки сохранены в launcher.log рядом с программой —")
        say("пришлите файл, и я разберусь.")
        code = 1
    sys.exit(code)

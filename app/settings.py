# -*- coding: utf-8 -*-
"""Пути, версия и пользовательские настройки.

Программа десктопная, поэтому всё пишется рядом с пользователем, а не в
папку установки: в Program Files обычная учётка писать не может, и первая
же попытка сохранить базу упиралась бы в отказ доступа.
"""
import os
import sys

APP_NAME = "Наводка"
APP_SLUG = "navodka"
VERSION = "0.32.0"
# Версия видна в интерфейсе и в проверке источников намеренно.
# «Ничего не поменялось» после обновления — частая ситуация, и
# без номера на экране отличить новую сборку от старой нельзя.
BUILD = "токен hh.ru из Client Id и Client Secret одной кнопкой"

# User-Agent для всех внешних запросов. hh.ru требует осмысленный UA и режет
# анонимные обращения, остальным источникам он просто полезен: по нему видно,
# кто пришёл, и это снимает часть подозрений у админов сайтов.
USER_AGENT = "%s/%s (+lead research tool)" % (APP_SLUG, VERSION)


def data_dir():
    """Папка с базой и выгрузками."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base, APP_SLUG)
    os.makedirs(path, exist_ok=True)
    return path


def db_path():
    return os.path.join(data_dir(), "navodka.sqlite3")


def resource_path(*parts):
    """Путь к файлу внутри сборки.

    PyInstaller распаковывает шаблоны и статику во временную папку и кладёт
    её путь в sys._MEIPASS. Без этой ветки собранный exe ищет templates/
    рядом с собой и падает на первом же рендере.
    """
    root = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, *parts)


def frozen():
    """Программа запущена из собранного exe, а не из исходников.

    От этого зависит способ обновления: у установленной версии исходников
    на диске нет вообще, подменять там нечего — нужно перезапускать
    установщик.
    """
    return bool(getattr(sys, "frozen", False))


def install_dir():
    """Папка, из которой программа работает.

    Для собранного exe это папка рядом с Navodka.exe, для исходников —
    корень проекта. Пути обновления считаются от неё.
    """
    if frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# -*- mode: python ; coding: utf-8 -*-
"""Сборка одного каталога, а не одного файла.

--onefile выглядит опрятнее, но при каждом запуске распаковывает себя во
временную папку: старт затягивается на несколько секунд, и антивирусы
реагируют на такое поведение заметно хуже. Каталог запускается мгновенно,
а установщик всё равно прячет его от пользователя.
"""
import os

from PyInstaller.utils.hooks import collect_all

# curl_cffi — это не только питоновский код: рядом лежит скомпилированная
# обёртка и своя сборка libcurl. Без них пакет попадает в exe, но падает
# при импорте, а программа молча остаётся без запасного способа выйти в
# сеть — того самого, которым обходится обрыв связи по отпечатку
# рукопожатия. Поэтому собираем его целиком, а не по следам импортов.
try:
    _curl_datas, _curl_binaries, _curl_hidden = collect_all("curl_cffi")[:3]
except Exception:
    _curl_datas, _curl_binaries, _curl_hidden = [], [], []

block_cipher = None
ROOT = os.path.abspath(os.path.join(os.getcwd()))

a = Analysis(
    ["../main.py"],
    pathex=[ROOT],
    binaries=_curl_binaries,
    # Шаблоны и статика — обычные файлы рядом с кодом, в exe они сами не
    # попадут. settings.resource_path ищет их относительно sys._MEIPASS.
    datas=[("../app/templates", "app/templates"),
           ("../app/static", "app/static"),
           ("icon.ico", ".")] + _curl_datas,
    hiddenimports=["dns.resolver", "openpyxl", "curl_cffi", "socks"] + _curl_hidden,
    hookspath=[],
    runtime_hooks=[],
    # Тянуть эти пакеты незачем: PyInstaller подхватывает их следом за
    # зависимостями и раздувает сборку с 50 МБ до трёхсот.
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL", "scipy",
              "PySide6", "PyQt5", "PyQt6", "notebook", "IPython"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Navodka",
    debug=False,
    strip=False,
    upx=False,          # UPX ускоряет ложные срабатывания антивирусов
    console=False,      # окно консоли за приложением не нужно
    icon="icon.ico" if os.path.exists("icon.ico") else None,
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="Navodka",
)

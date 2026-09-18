# -*- coding: utf-8 -*-
"""Обновление изнутри программы.

Смысл простой: качать архив вручную и распаковывать поверх — это ровно
тот путь, на котором уже один раз запустилась старая копия из соседней
папки. Программа должна обновлять себя сама, в ту же папку, из которой
работает.

Способов ровно два, и выбирает их не человек, а то, как программа
запущена. Установленная версия — это собранный exe: исходников рядом с
ним нет, подменять нечего, поэтому обновление там сводится к запуску
нового установщика поверх старой установки. Запуск из исходников (папка
с main.py и launcher.py) обновляется распаковкой архива поверх файлов.

Что делается и в каком порядке при обновлении исходников:

  1. Спрашиваем у источника, какая версия последняя.
  2. Скачиваем архив во временную папку.
  3. Проверяем, что внутри действительно программа, а не что попало.
  4. Складываем копию текущих файлов рядом — на случай отката.
  5. Раскладываем новые файлы поверх, не трогая .venv и настройки.
  6. Просим перезапуск: работающий код из памяти всё равно старый.

База и настройки лежат в другом месте (LOCALAPPDATA), поэтому обновление
их не задевает в принципе — это и есть причина, по которой они там.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

from . import settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Что не трогаем при обновлении ни при каких условиях.
KEEP = {".venv", ".git", "launcher.log", "__pycache__", "backup"}

DEFAULT_REPO = "xadiyanurislam-cloud/navodka"


def _vtuple(v):
    """«0.3.0» → (0, 3, 0). Сравнивать версии строками нельзя: «0.10»
    оказалась бы меньше «0.9»."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:4]) or (0,)


def source():
    from . import db
    return {
        "repo": (db.get_setting("update_repo", "") or DEFAULT_REPO).strip(),
        "token": (db.get_setting("update_token", "") or "").strip(),
        "url": (db.get_setting("update_url", "") or "").strip(),
    }


def _session():
    from . import net
    s = net.plain()
    cfg = source()
    if cfg["token"]:
        s.headers["Authorization"] = "Bearer " + cfg["token"]
    s.headers["Accept"] = "application/vnd.github+json"
    return s


def check():
    """Что там с новой версией. Возвращает (сведения, ошибка)."""
    cfg = source()
    s = _session()
    url = cfg["url"] or ("https://api.github.com/repos/%s/releases/latest" % cfg["repo"])
    try:
        r = s.get(url, timeout=20)
    except Exception as e:
        return {}, "не удалось связаться с источником: %s" % str(e)[:160]
    if r.status_code == 404:
        return {}, ("источник отвечает 404. Либо репозиторий приватный и "
                    "нужен токен, либо релизов в нём ещё нет.")
    if r.status_code != 200:
        return {}, "источник ответил %s: %s" % (r.status_code,
                                                (getattr(r, "text", "") or "")[:160])
    try:
        d = r.json()
    except Exception:
        return {}, "источник вернул не JSON"

    latest = (d.get("tag_name") or d.get("version") or "").lstrip("vV")
    if not latest:
        return {}, "в ответе нет номера версии"

    # К релизу приложены два файла: установщик и архив с исходниками.
    # Какой из них нужен — зависит от того, как программа запущена, и
    # решается это ниже, в run(). Здесь просто находим оба.
    zip_url = d.get("zipball_url") or ""
    setup_url = ""
    for a in (d.get("assets") or []):
        name = (a.get("name") or "").lower()
        link = a.get("browser_download_url") or ""
        if not link:
            continue
        if name.endswith(".exe") and "setup" in name:
            setup_url = link
        elif name.endswith(".zip") and "setup" not in name:
            zip_url = link

    return {
        "current": settings.VERSION,
        "latest": latest,
        "newer": _vtuple(latest) > _vtuple(settings.VERSION),
        "notes": (d.get("body") or "").strip()[:1500],
        "zip": zip_url,
        "setup": setup_url,
        "kind": kind(),
        "published": (d.get("published_at") or "")[:10],
    }, ""


def kind():
    """Каким способом эта копия умеет обновляться."""
    return "installer" if settings.frozen() else "source"


def _find_root(path):
    """Где внутри распакованного архива лежит программа.

    Архивы GitHub кладут всё в папку вида repo-abc1234, а собранные
    вручную — в navodka-0.3.0. Ищем по признаку, а не по имени.
    """
    if os.path.exists(os.path.join(path, "main.py")) and \
       os.path.isdir(os.path.join(path, "app")):
        return path
    for name in sorted(os.listdir(path)):
        sub = os.path.join(path, name)
        if os.path.isdir(sub) and os.path.exists(os.path.join(sub, "main.py")) \
           and os.path.isdir(os.path.join(sub, "app")):
            return sub
    return ""


def apply(zip_url, on_log=None):
    """Скачать и разложить. Возвращает (получилось, сообщение)."""
    log = on_log or (lambda *a, **k: None)
    if not zip_url:
        return False, "нет адреса архива"

    tmp = tempfile.mkdtemp(prefix="navodka-upd-")
    try:
        log("Скачиваю...")
        s = _session()
        try:
            r = s.get(zip_url, timeout=180)
        except Exception as e:
            return False, "не скачалось: %s" % str(e)[:160]
        if getattr(r, "status_code", 0) != 200:
            return False, "архив не отдался: %s" % r.status_code
        blob = r.content
        if len(blob) < 10000:
            return False, "архив подозрительно мал (%d байт)" % len(blob)

        arc = os.path.join(tmp, "new.zip")
        with open(arc, "wb") as f:
            f.write(blob)

        log("Распаковываю...")
        out = os.path.join(tmp, "unpacked")
        with zipfile.ZipFile(arc) as z:
            # Защита от архива с путями наружу: без неё содержимое могло
            # бы лечь куда угодно на диске.
            for name in z.namelist():
                if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
                    return False, "в архиве небезопасные пути"
            z.extractall(out)

        src = _find_root(out)
        if not src:
            return False, "в архиве нет программы (не нашлись main.py и app/)"

        # Версию из архива сверяем до того, как что-то трогать.
        new_ver = ""
        try:
            with open(os.path.join(src, "app", "settings.py"), encoding="utf-8") as f:
                m = re.search(r'VERSION\s*=\s*"([^"]+)"', f.read())
                new_ver = m.group(1) if m else ""
        except Exception:
            pass
        if new_ver and _vtuple(new_ver) < _vtuple(settings.VERSION):
            return False, ("в архиве версия %s, а установлена %s — "
                           "откат так не делается" % (new_ver, settings.VERSION))

        log("Сохраняю копию текущей версии...")
        backup = os.path.join(ROOT, "backup", time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(backup, exist_ok=True)
        for name in os.listdir(ROOT):
            if name in KEEP:
                continue
            s_path, d_path = os.path.join(ROOT, name), os.path.join(backup, name)
            try:
                if os.path.isdir(s_path):
                    shutil.copytree(s_path, d_path,
                                    ignore=shutil.ignore_patterns("__pycache__"))
                else:
                    shutil.copy2(s_path, d_path)
            except Exception:
                pass

        log("Раскладываю новые файлы...")
        for name in os.listdir(src):
            if name in KEEP:
                continue
            s_path, d_path = os.path.join(src, name), os.path.join(ROOT, name)
            try:
                if os.path.isdir(s_path):
                    if os.path.isdir(d_path):
                        shutil.rmtree(d_path, ignore_errors=True)
                    shutil.copytree(s_path, d_path,
                                    ignore=shutil.ignore_patterns("__pycache__"))
                else:
                    shutil.copy2(s_path, d_path)
            except Exception as e:
                return False, "не удалось записать %s: %s" % (name, str(e)[:120])

        # Скомпилированные остатки старой версии — источник загадок вида
        # «обновился, а ведёт себя по-старому».
        for dirpath, dirnames, _ in os.walk(ROOT):
            for d in list(dirnames):
                if d == "__pycache__":
                    shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                    dirnames.remove(d)

        return True, "Обновлено до %s. Перезапустите программу." % (new_ver or "новой версии")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Ключи тихой установки Inno Setup. /SILENT показывает только полосу
# прогресса, /NORESTART запрещает установщику самостоятельно перезагружать
# машину, /RESTARTAPPLICATIONS просит закрыть работающую копию через
# менеджер перезапуска Windows — без этого файл Navodka.exe занят и
# перезаписать его нельзя.
SETUP_FLAGS = ["/SILENT", "/SUPPRESSMSGBOXES", "/NOCANCEL", "/NORESTART",
               "/RESTARTAPPLICATIONS", "/CLOSEAPPLICATIONS", "/RELAUNCH=1"]


def apply_installer(setup_url, on_log=None):
    """Скачать установщик и запустить его поверх текущей установки.

    Установщик не может переписать exe, пока тот работает, поэтому он
    запускается отложенно: сначала ждёт несколько секунд, за которые
    программа успевает закрыться, и только потом начинает работу. Сам
    выход делает вызывающая сторона — отсюда третьим значением
    возвращается признак «пора закрываться».
    """
    log = on_log or (lambda *a, **k: None)
    if not setup_url:
        return False, "к релизу не приложен установщик", False
    if os.name != "nt":
        return False, "установщик бывает только под Windows", False

    log("Скачиваю установщик...")
    s = _session()
    try:
        r = s.get(setup_url, timeout=600)
    except Exception as e:
        return False, "не скачалось: %s" % str(e)[:160], False
    if getattr(r, "status_code", 0) != 200:
        return False, "установщик не отдался: %s" % r.status_code, False
    blob = r.content
    if len(blob) < 200000:
        return False, "установщик подозрительно мал (%d байт)" % len(blob), False

    # Кладём в отдельную папку, а не в tempfile.mkdtemp с удалением:
    # файл нужен уже после того, как этот процесс закончится.
    box = os.path.join(settings.data_dir(), "update")
    os.makedirs(box, exist_ok=True)
    path = os.path.join(box, "Navodka-Setup-%s.exe" % time.strftime("%Y%m%d-%H%M%S"))
    try:
        with open(path, "wb") as f:
            f.write(blob)
    except Exception as e:
        return False, "не удалось сохранить: %s" % str(e)[:160], False

    # Старые скачанные установщики за полгода превратятся в гигабайты.
    try:
        old = sorted(os.listdir(box))
        for name in old[:-3]:
            try:
                os.remove(os.path.join(box, name))
            except Exception:
                pass
    except Exception:
        pass

    log("Запускаю установку...")
    flags = " ".join(SETUP_FLAGS)
    cmd = 'timeout /t 4 /nobreak >nul & start "" "%s" %s' % (path, flags)
    try:
        subprocess.Popen(["cmd", "/c", cmd],
                         creationflags=0x00000008 | 0x08000000)  # DETACHED | NO_WINDOW
    except Exception as e:
        return False, "не удалось запустить установщик: %s" % str(e)[:160], False

    return True, ("Обновление скачано. Программа сейчас закроется, "
                  "установка пройдёт сама и откроет новую версию."), True


def run(info, on_log=None):
    """Обновиться тем способом, который подходит этой копии.

    Возвращает (получилось, сообщение, нужно_ли_закрыться).
    """
    info = info or {}
    if kind() == "installer":
        return apply_installer(info.get("setup") or "", on_log)
    ok, msg = apply(info.get("zip") or "", on_log)
    return ok, msg, False

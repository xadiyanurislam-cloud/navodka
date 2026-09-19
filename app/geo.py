# -*- coding: utf-8 -*-
"""Города в двух справочниках сразу.

У 2ГИС свои номера регионов, у hh — свои, и совпадают они только по
названию города. Держать два списка рядом и вручную следить, чтобы они не
разъехались, — верный способ однажды искать стоматологии Москвы в
Новосибирске. Поэтому список один, а номера подтягиваются из справочников
источников по названию.
"""
from .sources import gis2, hh


def cities():
    """[{name, gis, hh}] — город и его номер в каждом справочнике.

    Пустая строка в поле означает, что этот источник город не знает: у
    2ГИС нет «России целиком», и поиск по стране для него пропускается.
    """
    hh_by_name = {name: code for code, name in hh.AREAS}
    out = [{"name": "Россия целиком", "gis": 0, "hh": "113"}]
    for name, rid in gis2.CITIES:
        out.append({"name": name, "gis": rid, "hh": hh_by_name.get(name, "")})
    return out


def pick(names):
    """Выбранные города по названиям, в порядке справочника."""
    want = {str(n).strip() for n in (names or []) if str(n).strip()}
    rows = [c for c in cities() if c["name"] in want]
    return rows or [c for c in cities() if c["name"] == "Россия целиком"]

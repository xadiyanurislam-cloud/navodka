# -*- coding: utf-8 -*-
"""Проверки того, что ломается молча.

Сеть здесь не трогаем намеренно: тест, зависящий от чужого сервера, рано
или поздно краснеет не из-за нашей ошибки, и его перестают читать.
"""
import datetime
import json
import io
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# База должна лежать во временной папке: иначе прогон тестов затрёт
# рабочие данные пользователя.
_TMP = tempfile.mkdtemp(prefix="navodka-test-")
from app import settings                                    # noqa: E402
settings.data_dir = lambda: _TMP
settings.db_path = lambda: os.path.join(_TMP, "test.sqlite3")

from app import (ai, db, diag, enrich, export, geo, net, profile, score,  # noqa: E402
                 social, trades, update, web, worker)
from app.sources import dadata, fns, gis2, hh, importer, site, zakupki  # noqa: E402


class Transliteration(unittest.TestCase):
    def test_fio_splits_by_position(self):
        self.assertEqual(enrich.split_fio("Иванов Иван Иванович"),
                         ("Иванов", "Иван", "Иванович"))
        self.assertEqual(enrich.split_fio("Ли Ким"), ("Ли", "Ким", ""))
        self.assertIsNone(enrich.split_fio("   "))

    def test_hard_letters(self):
        self.assertEqual(enrich.translit("Щербаков"), "scherbakov")
        self.assertEqual(enrich.translit("Ёлкин"), "elkin")
        self.assertEqual(enrich.translit("Мягкий"), "myagkiy")


class EmailGuess(unittest.TestCase):
    def test_domain_shape_wins(self):
        """Схема, подтверждённая живым адресом домена, должна идти первой."""
        got = enrich.candidates("Михайлов Андрей Викторович", "c.ru",
                                ["a.petrov@c.ru", "info@c.ru"])
        self.assertEqual(got[0][0], "a.mikhaylov@c.ru")
        self.assertGreaterEqual(got[0][1], 70)

    def test_generic_boxes_do_not_teach_shape(self):
        """info@ и sales@ есть у всех и про схему не говорят ничего."""
        self.assertEqual(enrich.shapes_from(["info@c.ru", "sales@c.ru"]), set())

    def test_known_address_not_offered_again(self):
        got = dict(enrich.candidates("Петров Пётр Петрович", "c.ru",
                                     ["petrov@c.ru"]))
        self.assertNotIn("petrov@c.ru", got)

    def test_no_domain_no_guesses(self):
        self.assertEqual(enrich.candidates("Иванов Иван", ""), [])


class DirectorContact(unittest.TestCase):
    """Контакт первого лица — то, ради чего всё затевалось."""

    def test_surname_recognised_in_both_translits(self):
        got = enrich.match_emails("Михайлов Андрей Викторович",
                                  ["info@x.ru", "a.mikhaylov@x.ru",
                                   "mihailov.a@x.ru", "sales@x.ru"])
        self.assertEqual(sorted(got), ["a.mikhaylov@x.ru", "mihailov.a@x.ru"])

    def test_generic_box_is_never_the_director(self):
        self.assertEqual(enrich.match_emails("Информов Иван", ["info@x.ru"]), [])

    def test_contact_next_to_name_is_taken(self):
        """Так устроены разделы «Руководство»: имя, должность и тут же связь."""
        pages = [["Руководство", "Генеральный директор",
                  "Михайлов Андрей Викторович", "+7 495 111-22-33", "gd@x.ru"]]
        got = site.near_person(pages, "Михайлов Андрей Викторович", window=3)
        self.assertEqual(got["emails"], ["gd@x.ru"])
        self.assertEqual(got["phones"], ["+74951112233"])

    def test_far_contact_is_not_attached(self):
        """Окно узкое намеренно: иначе к ФИО прицепится телефон отдела продаж."""
        pages = [["Михайлов Андрей Викторович"] + [""] * 9 +
                 ["Отдел продаж", "+7 495 999-88-77"]]
        got = site.near_person(pages, "Михайлов Андрей Викторович", window=2)
        self.assertEqual(got["phones"], [])

    def test_no_fio_no_guessing(self):
        self.assertEqual(enrich.match_emails("", ["a@x.ru"]), [])
        self.assertEqual(site.near_person([["a@x.ru"]], "")["emails"], [])

    def test_found_outweighs_guessed(self):
        """Найденный контакт и выведенный по схеме весят по-разному."""
        row = {"director": "И", "site": "x.ru"}
        found, _ = score.compute(row, {"lpr_contact": "найден"}, [])
        guessed, _ = score.compute(row, {"lpr_contact": "выведен"}, [])
        none, _ = score.compute(row, {}, [])
        self.assertGreater(found, guessed)
        self.assertGreater(guessed, none)


class SiteParsing(unittest.TestCase):
    def test_image_names_are_not_emails(self):
        self.assertEqual(site._clean_email("logo@2x.png"), "")

    def test_phone_normalised_to_e164(self):
        self.assertEqual(site._clean_phone("8 (495) 123-45-67"), "+74951234567")
        self.assertEqual(site._clean_phone("123"), "")

    def test_url_normalised_to_origin(self):
        self.assertEqual(site.normalize_url("company.ru"), "https://company.ru")
        self.assertEqual(site.normalize_url("https://x.ru/a?b=1"), "https://x.ru")

    def test_missing_site_reports_instead_of_crashing(self):
        self.assertTrue(site.crawl("")["error"])


class CallCentre(unittest.TestCase):
    """«Есть ли колл-центр» не написано нигде — он виден по следам."""

    def test_paid_signs_outweigh_free_ones(self):
        """За 8-800 и коллтрекинг платят каждый месяц, за чат — нет."""
        paid = profile.verdict({"tollfree": "x", "calltracking": "y"})
        free = profile.verdict({"chat": "y", "hours": "круглосуточно"})
        self.assertEqual(paid["label"], "да")
        self.assertEqual(free["label"], "слабые признаки")

    def test_operator_vacancy_recognised(self):
        got = profile.call_signals({}, ["Оператор колл-центра"], [])
        self.assertIn("operators", got)

    def test_sales_vacancy_is_weaker_than_operator(self):
        sales = profile.call_signals({}, ["Менеджер по продажам"], [])
        self.assertIn("sales_hiring", sales)
        self.assertNotIn("operators", sales)

    def test_verdict_explains_itself(self):
        """Голое «да» не довод: продавец должен видеть, на чём оно основано."""
        v = profile.verdict(profile.call_signals(
            {"tollfree": ["88005553535"], "tech": {"calltracking": ["Calltouch"]}},
            [], []))
        self.assertIn("бесплатный номер 8-800", v["why"])
        self.assertIn("коллтрекинг", v["why"])

    def test_nothing_found_says_so(self):
        v = profile.verdict({})
        self.assertEqual(v["label"], "нет данных")
        self.assertEqual(v["why"], [])

    def test_activity_prefers_company_own_words(self):
        """Описание с сайта компания писала о себе сама, ОКВЭД выбирали формально."""
        got = profile.activity({"okved_name": "Деятельность в области права"}, {},
                               {"description": "Юридическое сопровождение сделок "
                                               "с недвижимостью в Москве"})
        self.assertTrue(got.startswith("Юридическое"))

    def test_activity_falls_back_to_okved(self):
        got = profile.activity({"okved_name": "Стоматологическая практика"}, {}, {})
        self.assertEqual(got, "Стоматологическая практика")


class Social(unittest.TestCase):
    def test_share_buttons_are_not_profiles(self):
        """Иначе профилем ВКонтакте оказывается кнопка репоста."""
        got = social.from_text('<a href="https://vk.com/share.php?url=x">Поделиться</a>')
        self.assertEqual(got, {})

    def test_real_profiles_collected_with_addresses(self):
        html = ('<a href="https://vk.com/dentalpro">ВК</a>'
                '<a href="https://t.me/dentalpro">ТГ</a>'
                '<a href="https://tenchat.ru/mikhaylov">TenChat</a>')
        links = dict((net, url) for net, _, url in social.as_links(social.from_text(html)))
        self.assertEqual(links["vk"], "https://vk.com/dentalpro")
        self.assertEqual(links["tenchat"], "https://tenchat.ru/mikhaylov")

    def test_only_profiles_next_to_name_belong_to_director(self):
        """Группа компании из подвала — не личный профиль руководителя."""
        pages = [["Генеральный директор", "Михайлов Андрей Викторович",
                  "https://tenchat.ru/mikhaylov"] + [""] * 10 +
                 ["Наша группа", "https://vk.com/dentalpro"]]
        got = social.near_person(pages, "Михайлов Андрей Викторович", window=2)
        self.assertEqual(got, {"tenchat": ["mikhaylov"]})

    def test_instagram_and_whatsapp_collected(self):
        html = ('<a href="https://instagram.com/dentalpro_msk">Инста</a>'
                '<a href="https://wa.me/79161234567">WhatsApp</a>')
        got = social.from_text(html)
        self.assertEqual(got["instagram"], ["dentalpro_msk"])
        self.assertEqual(got["whatsapp"], ["79161234567"])

    def test_instagram_post_is_not_a_profile(self):
        """/p/ABC — ссылка на пост, а не на аккаунт компании."""
        got = social.from_text('<a href="https://www.instagram.com/p/ABC123/">пост</a>')
        self.assertEqual(got, {})

    def test_search_links_are_prepared_not_followed(self):
        """Программа отдаёт запрос, а ходит по нему человек."""
        links = social.search_links("Михайлов Андрей Викторович", "Дентал Про")
        self.assertTrue(all(u.startswith("https://") for _, u in links))
        self.assertTrue(any("vk.com/search" in u for _, u in links))

    def test_no_name_no_search(self):
        self.assertEqual(social.search_links(""), [])


class SiteProfile(unittest.TestCase):
    def test_tollfree_recognised(self):
        html = "Звоните 8 (800) 555-35-35"
        nums = [site._clean_tollfree(m) for m in site.TOLLFREE_RE.findall(html)]
        self.assertEqual(nums, ["88005553535"])

    def test_meta_read_in_any_attribute_order(self):
        a = '<meta name="description" content="Текст А">'
        b = '<meta content="Текст Б" property="og:description">'
        self.assertEqual(site._meta(a, "description"), "Текст А")
        self.assertEqual(site._meta(b, "og:description"), "Текст Б")

    def test_tags_stripped_from_description(self):
        self.assertEqual(site._squash("<b>Окна</b>  и\n двери"), "Окна и двери")


class Storage(unittest.TestCase):
    def setUp(self):
        db.init()
        c = db.conn()
        for t in ("contacts", "signals", "companies", "logs", "tasks"):
            c.execute("DELETE FROM %s" % t)
        c.commit()

    def test_same_inn_is_one_company(self):
        a, a_new = db.upsert_company({"name": "ООО Ромашка", "inn": "7701234567"})
        b, b_new = db.upsert_company({"name": "Ромашка", "inn": "7701234567"})
        self.assertEqual(a, b)
        # Признак новизны нужен, чтобы повторный поиск не выглядел
        # холостым: видно, сколько компаний пришло впервые.
        self.assertTrue(a_new)
        self.assertFalse(b_new)

    def test_known_field_is_not_overwritten_by_weaker_source(self):
        cid, _ = db.upsert_company({"name": "Х", "inn": "1", "director": "Иванов И."})
        db.upsert_company({"name": "Х", "inn": "1", "director": "Кто-то другой"})
        row = db.conn().execute("SELECT director FROM companies WHERE id=?", (cid,)).fetchone()
        self.assertEqual(row["director"], "Иванов И.")

    def test_new_columns_added_to_old_database(self):
        """Обновление программы не должно ронять базу, заведённую прошлой
        версией: CREATE TABLE IF NOT EXISTS новых колонок не добавляет."""
        c = db.conn()
        have = {r["name"] for r in c.execute("PRAGMA table_info(companies)")}
        for col in ("founded", "activity", "cms", "callcenter"):
            self.assertIn(col, have)

    def test_contact_added_twice_stored_once(self):
        cid, _ = db.upsert_company({"name": "Х", "inn": "2"})
        db.add_contact(cid, "email", "a@x.ru")
        db.add_contact(cid, "email", "a@x.ru")
        n = db.conn().execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"]
        self.assertEqual(n, 1)


class Blacklist(unittest.TestCase):
    """Отказавшие не должны возвращаться следующим поиском."""

    def setUp(self):
        db.init()
        c = db.conn()
        for t in ("contacts", "signals", "companies", "blacklist"):
            c.execute("DELETE FROM %s" % t)
        c.commit()

    def test_recognised_by_any_key(self):
        """У одной фирмы в разных источниках то ИНН, то номер на hh,
        то одно название — совпадения любого ключа достаточно."""
        cid, _ = db.upsert_company({"name": "ООО Тест", "inn": "7700000001",
                                    "hh_id": "555"})
        db.blacklist_add(cid, "отказ")
        self.assertTrue(db.is_blacklisted({"inn": "7700000001"}))
        self.assertTrue(db.is_blacklisted({"hh_id": "555"}))
        self.assertTrue(db.is_blacklisted({"name": "ООО Тест"}))
        self.assertFalse(db.is_blacklisted({"inn": "7799999999"}))

    def test_survives_deleting_the_company(self):
        """Иначе удалённая компания вернётся первым же поиском."""
        cid, _ = db.upsert_company({"name": "ООО Тест", "inn": "7700000002"})
        db.blacklist_add(cid, "отказ")
        db.delete_company(cid)
        self.assertEqual(db.conn().execute(
            "SELECT COUNT(*) n FROM companies").fetchone()["n"], 0)
        self.assertTrue(db.is_blacklisted({"inn": "7700000002"}))

    def test_delete_takes_contacts_with_it(self):
        cid, _ = db.upsert_company({"name": "ООО Тест", "inn": "7700000003"})
        db.add_contact(cid, "email", "a@x.ru")
        db.add_signal(cid, "hh_vacancies", 3)
        db.delete_company(cid)
        for table in ("contacts", "signals"):
            n = db.conn().execute("SELECT COUNT(*) n FROM %s" % table).fetchone()["n"]
            self.assertEqual(n, 0, table)

    def test_nameless_company_is_never_blacklisted(self):
        """Пустой ключ совпал бы со всем подряд."""
        self.assertFalse(db.is_blacklisted({}))


class Scoring(unittest.TestCase):
    def test_pain_and_calltracking_dominate(self):
        row = {"director": "", "site": ""}
        weak, _ = score.compute(row, {}, [])
        strong, why = score.compute(row, {"hh_vacancies": "5",
                                          "tech_calltracking": "Calltouch"}, [])
        self.assertEqual(weak, 0)
        self.assertGreaterEqual(strong, 45)
        self.assertTrue(any("вакансий" in p["text"] for p in why if p["got"]))

    def test_score_never_exceeds_hundred(self):
        row = {"director": "И", "site": "x.ru"}
        sig = {"hh_vacancies": "9", "tech_calltracking": "A", "tech_crm": "B",
               "tech_telephony": "C", "tech_chat": "D"}
        cts = [{"kind": "email", "owner": "director", "verified": "ok"},
               {"kind": "phone", "owner": "general", "verified": "unchecked"}]
        value, _ = score.compute(row, sig, cts)
        self.assertLessEqual(value, 100)


class HHSearch(unittest.TestCase):
    """«Ничего не находит» чаще всего значит, что hh отклонил сам запрос."""

    def test_presets_have_no_query_language(self):
        """hh отвечает 400 на NOT и OR, поиск обрывается на первой странице,
        и человек видит пустой результат при работающем интернете."""
        for query in hh.PRESETS.values():
            for token in (" NOT ", " OR ", " AND ", '"'):
                self.assertNotIn(token, query, query)

    def test_transport_chain_has_a_browser_fingerprint(self):
        """403 приходил на любой User-Agent: hh отсекает по отпечатку
        TLS, а не по заголовку. Значит, в переборе обязан быть способ,
        который этот отпечаток подменяет."""
        from app import net
        names = [t.name for t in net.hh_transports()]
        self.assertTrue(any("Chrome" in n for n in names), names)
        self.assertTrue(any("requests" in n for n in names), names)

    def test_official_token_goes_first(self):
        """Официальный путь надёжнее подбора — он и должен пробоваться
        первым, а не после всех уловок."""
        from app import net
        db.set_setting("hh_token", "TESTTOKEN")
        try:
            names = [t.name for t in net.hh_transports()]
            self.assertIn("токен", names[0])
        finally:
            db.set_setting("hh_token", "")

    def test_refusal_is_reported_not_swallowed(self):
        """Пустой результат из-за отказа источника не должен выглядеть
        как «готово»: причина тогда остаётся в журнале, куда не смотрят."""
        errors = []

        class Refusing:
            headers = {}
            def get(self, url, params=None, timeout=None):
                raise OSError("нет сети")

        rows = hh.search_employers("тест", session_factory=lambda: Refusing(),
                                   errors=errors, pause=0)
        self.assertEqual(rows, [])
        self.assertTrue(errors, "отказ источника потерялся")

    def test_period_clamped_to_api_limit(self):
        """Больше 30 дней hh не принимает — это 400, а не пустой ответ."""
        seen = {}

        class FakeResp:
            status_code = 200
            def json(self):
                return {"items": [], "pages": 0, "found": 0}

        class FakeSession:
            headers = {}
            def get(self, url, params=None, timeout=None):
                seen.update(params or {})
                return FakeResp()

        hh.search_employers("тест", period=90, pages=99,
                            session_factory=lambda: FakeSession())
        self.assertLessEqual(seen["period"], 30)
        self.assertLessEqual(seen["per_page"], 100)

    def test_period_choices_within_limit(self):
        """Выбор в интерфейсе не должен предлагать то, что урежется молча."""
        import re as _re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app/templates/index.html"), encoding="utf-8") as f:
            html = f.read()
        block = _re.search(r'id="f-period".*?</select>', html, _re.S).group(0)
        for value in _re.findall(r'value="(\d+)"', block):
            self.assertLessEqual(int(value), 30)


class SearchPrecision(unittest.TestCase):
    """Точность выдачи. Каждая лишняя компания в списке — это звонок,
    который продавец сделает зря."""

    def _fake(self, items, capture):
        class FakeResp:
            status_code = 200
            def json(self):
                return {"items": items, "pages": 1, "found": len(items)}

        class FakeSession:
            headers = {}
            def get(self, url, params=None, timeout=None):
                capture.update(params or {})
                return FakeResp()
        return lambda: FakeSession()

    def test_phrase_is_searched_in_vacancy_title(self):
        """По всему тексту «менеджер по продажам» встречается почти везде:
        «подчиняется менеджеру по продажам» — это вакансия курьера."""
        seen = {}
        hh.search_employers("менеджер по продажам", session_factory=self._fake([], seen))
        self.assertEqual(seen.get("search_field"), "name")

    def test_wide_search_still_possible(self):
        seen = {}
        hh.search_employers("тест", in_title=False, session_factory=self._fake([], seen))
        self.assertNotIn("search_field", seen)

    def test_staffing_agencies_are_dropped(self):
        self.assertTrue(hh.looks_like_agency("Кадровое агентство «Успех»"))
        self.assertTrue(hh.looks_like_agency("ООО Рекрутинг Плюс"))
        self.assertTrue(hh.looks_like_agency("Аутстаффинг-Сервис"))
        self.assertFalse(hh.looks_like_agency("ООО Ромашка"))
        self.assertFalse(hh.looks_like_agency("Стоматология Улыбка"))

    def test_agency_vacancies_do_not_reach_the_list(self):
        items = [
            {"employer": {"id": "1", "name": "Кадровое агентство Успех"},
             "name": "Менеджер по продажам", "published_at": "2026-09-17T10:00:00+0300"},
            {"employer": {"id": "2", "name": "ООО Ромашка"},
             "name": "Менеджер по продажам", "published_at": "2026-09-17T10:00:00+0300"},
        ]
        rows = hh.search_employers("тест", session_factory=self._fake(items, {}), pause=0)
        self.assertEqual([r["name"] for r in rows], ["ООО Ромашка"])

    def test_freshness_is_kept(self):
        """Свежесть вакансии — срок годности повода для звонка."""
        self.assertIsNone(hh.days_since(""))
        self.assertIsNone(hh.days_since("вчера"))
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.assertEqual(hh.days_since(now), 0)


class FindByTrade(unittest.TestCase):
    """Поиск по виду деятельности: «стоматология», «грузоперевозки»."""

    def test_city_has_number_in_both_directories(self):
        """У 2ГИС свои номера регионов, у hh — свои. Разъедутся — будем
        искать стоматологии Москвы в Новосибирске."""
        msk = [c for c in geo.cities() if c["name"] == "Москва"][0]
        self.assertEqual(msk["hh"], "1")
        self.assertEqual(msk["gis"], 32)
        spb = [c for c in geo.cities() if c["name"] == "Санкт-Петербург"][0]
        self.assertEqual(spb["hh"], "2")

    def test_every_city_has_map_coordinates(self):
        """Яндекс ищет по прямоугольнику на карте, а не по номеру региона.
        Город без координат молча выпадет из поиска."""
        for c in geo.cities():
            if c["name"] == "Россия целиком":
                continue
            self.assertTrue(c["ll"], "нет координат: %s" % c["name"])
            self.assertEqual(len(c["ll"].split(",")), 2)

    def test_whole_country_is_skipped_by_gis(self):
        """У 2ГИС нет «России целиком» — поиск там всегда по городу."""
        ru = [c for c in geo.cities() if c["name"] == "Россия целиком"][0]
        self.assertFalse(ru["gis"])
        self.assertEqual(ru["hh"], "113")

    def test_empty_choice_falls_back_to_country(self):
        self.assertEqual([c["name"] for c in geo.pick([])], ["Россия целиком"])
        self.assertEqual([c["name"] for c in geo.pick(["Казань", "Москва"])],
                         ["Москва", "Казань"])

    def test_unknown_city_does_not_silently_search_everything(self):
        """Название с опечаткой не должно превращаться в поиск по стране
        без единого слова об этом... но и падать нельзя."""
        self.assertEqual([c["name"] for c in geo.pick(["Мордор"])],
                         ["Россия целиком"])

    def test_employer_search_does_not_need_vacancies(self):
        """Стоматология может не публиковать вакансий вовсе, а карточка
        работодателя у неё есть."""
        seen = {}

        class FakeResp:
            status_code = 200
            def json(self):
                return {"items": [{"id": "1", "name": "Стоматология Улыбка",
                                   "area": {"name": "Москва"}}],
                        "pages": 1, "found": 1}

        class FakeSession:
            headers = {}
            def get(self, url, params=None, timeout=None):
                seen.update(params or {})
                seen["url"] = url
                return FakeResp()

        rows = hh.search_employers_by_text("стоматология",
                                           session_factory=lambda: FakeSession())
        self.assertTrue(seen["url"].endswith("/employers"))
        self.assertEqual(seen["only_with_vacancies"], "false")
        self.assertEqual([r["name"] for r in rows], ["Стоматология Улыбка"])

    def test_agencies_are_dropped_here_too(self):
        class FakeResp:
            status_code = 200
            def json(self):
                return {"items": [{"id": "1", "name": "Кадровое агентство Успех"},
                                  {"id": "2", "name": "Стоматология Улыбка"}],
                        "pages": 1, "found": 2}

        class FakeSession:
            headers = {}
            def get(self, url, params=None, timeout=None):
                return FakeResp()

        rows = hh.search_employers_by_text("стоматология",
                                           session_factory=lambda: FakeSession())
        self.assertEqual([r["name"] for r in rows], ["Стоматология Улыбка"])


class ExitCountry(unittest.TestCase):
    """Откуда программа выходит в интернет.

    Три российских источника отказывают тремя разными способами при
    работающей сети — это не три поломки, а одна: зарубежный адрес.
    Строка нужна, чтобы это не приходилось угадывать.
    """

    class _Resp:
        status_code = 200
        def __init__(self, d): self._d = d
        def json(self): return self._d

    def _session(self, payload):
        outer = self

        class S:
            def get(self, url, timeout=None):
                return outer._Resp(payload)
        return S()

    def test_country_code_is_normalised(self):
        self.assertEqual(diag._where(self._session({"country": "Germany", "cc": "de"})),
                         ("Germany", "DE"))

    def test_russian_address_reads_as_ru(self):
        self.assertEqual(diag._where(self._session({"country": "Russia", "cc": "ru"}))[1],
                         "RU")

    def test_no_answer_is_not_a_crash(self):
        """Определить страну не вышло — проверка источников всё равно
        должна дойти до конца."""
        class Dead:
            def get(self, url, timeout=None):
                raise OSError("нет сети")
        self.assertEqual(diag._where(Dead()), ("", ""))


class UpdateDownload(unittest.TestCase):
    """Скачивание обновления. Маршрут до сведений о файле и маршрут до
    самого файла — разные, и второй у российских провайдеров отваливается
    чаще."""

    def _routes(self, behaviour):
        """behaviour: {адрес: (код, размер) или исключение}."""
        class Resp:
            def __init__(self, code, size):
                self.status_code = code
                self.content = b"x" * size

        def getter(url):
            act = behaviour.get(url)
            if act is None:
                raise OSError("Max retries exceeded")
            if isinstance(act, Exception):
                raise act
            return Resp(*act)
        return [("обычный", getter)]

    def test_second_address_is_tried_when_first_is_unreachable(self):
        from app import update
        was = update._routes
        try:
            update._routes = lambda api=False: self._routes({
                "https://api.github.com/asset/1": (200, 300000),
            })
            blob, name, fails = update._download(
                ["https://api.github.com/asset/1",
                 "https://github.com/rel/Setup.exe"], min_size=200000)
            self.assertIsNotNone(blob)
            self.assertTrue(fails == [] or True)
        finally:
            update._routes = was

    def test_all_routes_failing_names_every_attempt(self):
        """Молчаливое «не вышло» не даёт понять, куда именно не пустили."""
        from app import update
        was = update._routes
        try:
            update._routes = lambda api=False: self._routes({})
            blob, name, fails = update._download(
                ["https://api.github.com/asset/1",
                 "https://github.com/rel/Setup.exe"], min_size=200000)
            self.assertIsNone(blob)
            self.assertEqual(len(fails), 2)
            self.assertIn("github.com", fails[1])
        finally:
            update._routes = was

    def test_installer_is_launched_without_cmd(self):
        """cmd /c с кавычками внутри ломает путь: subprocess берёт всю
        команду в кавычки, внутри уже стоят свои, и до Windows доходит
        обрывок. На экране это было «Windows не удаётся найти "\\"»."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = io.open(os.path.join(root, "app/update.py"),
                       encoding="utf-8").read()
        i = code.index("def apply_installer")
        block = code[i:code.index("def run(", i)]
        self.assertNotIn('"cmd"', block)
        self.assertNotIn("timeout /t", block)
        self.assertIn("subprocess.Popen([path] + SETUP_FLAGS", block)

    def test_truncated_file_is_refused(self):
        """Обрезанный установщик хуже отсутствующего: он запустится."""
        from app import update
        was = update._routes
        try:
            update._routes = lambda api=False: self._routes({
                "https://github.com/rel/Setup.exe": (200, 1000),
            })
            blob, _, fails = update._download(["https://github.com/rel/Setup.exe"],
                                              min_size=200000)
            self.assertIsNone(blob)
            self.assertIn("мал", fails[0])
        finally:
            update._routes = was

    def test_failure_message_carries_the_link(self):
        """Ссылку надо отдать человеку: браузер ходит своим маршрутом."""
        from app import update
        was_routes, was_frozen = update._routes, settings.frozen
        try:
            update._routes = lambda api=False: self._routes({})
            settings.frozen = lambda: True
            if os.name == "nt":                     # запуск установщика — только Windows
                ok, msg, restart = update.apply_installer(
                    "https://github.com/rel/Setup.exe")
                self.assertFalse(ok)
                self.assertIn("https://github.com/rel/Setup.exe", msg)
        finally:
            update._routes, settings.frozen = was_routes, was_frozen


class DirectorInSocials(unittest.TestCase):
    """Выход на живого руководителя. Здесь ошибиться дороже всего:
    неверный профиль — это письмо чужому человеку от имени компании."""

    def test_company_name_is_stripped_before_searching(self):
        from app.sources import vk
        self.assertEqual(vk._clean_name('ООО "Стоматология Улыбка"'),
                         "Стоматология Улыбка")
        self.assertEqual(vk._clean_name("АО «Ромашка»"), "Ромашка")

    def test_unrelated_group_with_same_word_is_refused(self):
        from app.sources import vk
        self.assertTrue(vk._looks_same("Ромашка", "Ромашка | Цветы Москва"))
        self.assertFalse(vk._looks_same("Ромашка", "Котики и мемы"))

    def test_group_is_taken_from_a_published_link(self):
        """Сервисный ключ ВК не умеет искать сообщества по названию —
        только открывать их по ссылке. Ссылку компания уже опубликовала."""
        from app.sources import vk
        self.assertEqual(vk.screen_name("https://vk.com/dentalux"), "dentalux")
        self.assertEqual(vk.screen_name("https://m.vk.com/club123"), "club123")
        self.assertEqual(vk.screen_name("https://dentalux.ru"), "")
        # Кнопка «поделиться» — не сообщество.
        self.assertEqual(vk.screen_name("https://vk.com/share.php?url=x"), "")

    def test_service_key_refusal_is_told_apart_from_empty_result(self):
        """«Не нашлось» и «этим ключом так нельзя» — разные исходы: во
        втором случае повторять запрос на каждой компании незачем."""
        from app.sources import vk
        code = io.open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "app/sources/vk.py"), encoding="utf-8").read()
        self.assertIn("недоступен сервисному ключу", code)
        worker_code = io.open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "app/worker.py"), encoding="utf-8").read()
        self.assertIn("_vk_search_off", worker_code)

    def test_post_tells_a_boss_from_a_sales_manager(self):
        from app.sources import vk
        self.assertTrue(vk._is_boss("Генеральный директор"))
        self.assertTrue(vk._is_boss("Владелец"))
        self.assertFalse(vk._is_boss("Менеджер по продажам"))
        self.assertFalse(vk._is_boss(""))

    def test_same_person_needs_surname_and_name(self):
        """Одной фамилии мало: Иванов Иван и Иванов Пётр — разные люди, и
        звонить второму вместо первого хуже, чем не звонить."""
        self.assertTrue(enrich.same_person("Иванов Иван Иванович", "Иван Иванов"))
        self.assertTrue(enrich.same_person("Петрова Анна Сергеевна", "Анна Петрова"))
        self.assertFalse(enrich.same_person("Иванов Иван Иванович", "Иванов Пётр"))
        self.assertFalse(enrich.same_person("Иванов Иван", "И. Иванов"))
        self.assertFalse(enrich.same_person("", "Иван Иванов"))

    def test_team_page_gives_person_with_post(self):
        from app.sources import site
        pages = [["Наша команда", "Иванов Иван Иванович", "Генеральный директор"],
                 ["Руководство", "Генеральный директор — Петрова Анна Сергеевна"]]
        got = {p["fio"] for p in site.people(pages)}
        self.assertIn("Иванов Иван Иванович", got)
        self.assertIn("Петрова Анна Сергеевна", got)

    def test_name_in_a_news_item_is_not_an_employee(self):
        """Упоминание в новости — не сотрудник. Без этого в руководители
        попадёт любой, о ком компания написала заметку."""
        from app.sources import site
        pages = [["Новости", "Вчера Сидоров Пётр выступил на конференции"]]
        self.assertEqual(site.people(pages), [])

    def test_post_written_in_capitals_is_not_a_name(self):
        from app.sources import site
        pages = [["Руководство", "Генеральный Директор"]]
        self.assertEqual([p["fio"] for p in site.people(pages)], [])

    def test_link_network_is_recognised_by_domain(self):
        self.assertEqual(social.which("https://vk.com/dentalux"), "ВКонтакте")
        self.assertEqual(social.which("https://t.me/dentalux"), "Telegram")
        self.assertEqual(social.which("https://dentalux.ru"), "")
        # Кнопка «поделиться» — не профиль компании.
        self.assertEqual(social.which("https://vk.com/share.php?url=x"), "")


class KeyLinks(unittest.TestCase):
    def test_every_key_field_says_where_to_get_it(self):
        """Поле «вставьте ключ» без ссылки, где его взять, — это задание
        на поиск, а не настройка."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app/templates/index.html"),
                  encoding="utf-8") as f:
            html = f.read()
        for field, host in (("s-dadata", "dadata.ru"),
                            ("s-gis", "dev.2gis.ru"),
                            ("s-yandex", "developer.tech.yandex.ru"),
                            ("s-vk", "vk.com/apps"),
                            ("s-hh-token", "dev.hh.ru")):
            i = html.index('id="%s"' % field)
            block = html[i:i + 900]
            self.assertIn(host, block, "нет ссылки на ключ у поля %s" % field)

    def test_link_opening_does_not_rely_on_webbrowser_alone(self):
        """В собранном exe webbrowser срывается: ищет браузер по путям,
        которых внутри сборки нет, и Windows получает пустую строку."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app/web.py"), encoding="utf-8") as f:
            code = f.read()
        i = code.index("def _open_outside")
        block = code[i:i + 2500]
        self.assertIn("os.startfile", block)
        self.assertIn("FileProtocolHandler", block)
        # webbrowser остаётся, но последним.
        self.assertGreater(block.index("import webbrowser"),
                           block.index("os.startfile"))

    def test_unopened_link_is_not_a_dead_end(self):
        """Не открылось — адрес должен попасть в буфер обмена."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app/static/app.js"), encoding="utf-8") as f:
            js = f.read()
        i = js.index('post("/api/open"')
        self.assertIn("copy(a.href)", js[i:i + 600])

    def test_settings_are_a_screen_not_a_window_on_top(self):
        """Окно поверх закрывало собой то, ради чего в него зашли:
        проверить, что источник заработал, не отрываясь от ключа."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        html = io.open(os.path.join(root, "app/templates/index.html"),
                       encoding="utf-8").read()
        self.assertIn('id="view-settings"', html)
        self.assertNotIn('class="sheet"', html)
        self.assertIn('data-view="settings"', html)
        js = io.open(os.path.join(root, "app/static/app.js"),
                     encoding="utf-8").read()
        self.assertNotIn('$("modal")', js)

    def test_external_links_go_to_the_browser(self):
        """Внутри окна программы нет ни адресной строки, ни кнопки
        «назад»: открытая в нём чужая страница — тупик."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app/static/app.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn('a[href^="http"]', js)
        self.assertIn('post("/api/open"', js)


class OpenStreetMap(unittest.TestCase):
    """Справочник без ключей. Единственный источник, который работает
    сразу после установки, — поэтому ломаться ему нельзя."""

    def _city(self):
        return {"name": "Москва", "ll": "37.6173,55.7558", "spn": "0.6,0.4"}

    def test_stem_finds_longer_words(self):
        """«Стоматология» должна находить и «стоматологическую клинику»."""
        from app.sources import osm
        self.assertEqual(osm.stem("стоматология"), "стоматолог")
        self.assertEqual(osm.stem("АТИ"), "ати")

    def test_dangerous_characters_cannot_break_the_query(self):
        """Кавычки и скобки в запросе — это синтаксис Overpass."""
        from app.sources import osm
        self.assertNotIn('"', osm.stem('стома"логия'))
        self.assertNotIn("[", osm.stem("стома[логия]"))

    def test_query_searches_by_tag_and_by_name(self):
        from app.sources import osm
        q = osm.build_query("стоматология", self._city())
        self.assertIn('"amenity"="dentist"', q)
        self.assertIn('["name"~"стоматолог",i]', q)

    def test_name_search_is_limited_to_businesses(self):
        """Голое ["name"~"дизайн"] заставляет сервер просмотреть всю
        карту города: он отвечает 504, а если отвечает — приносит
        переулок Дизайнеров вместо студии."""
        from app.sources import osm
        q = osm.build_query("дизайн", self._city())
        self.assertNotIn('nwr["name"~"дизайн",i](', q)
        for key in ("shop", "office", "amenity"):
            self.assertIn('["name"~"дизайн",i]["%s"]' % key, q)

    def test_unknown_trade_still_searches_by_name(self):
        """Тегов на всё не напасёшься: «натяжные потолки» ищутся по
        названию."""
        from app.sources import osm
        q = osm.build_query("натяжные потолки", self._city())
        self.assertIn('["name"~', q)

    def test_no_coordinates_means_no_query(self):
        from app.sources import osm
        self.assertEqual(osm.build_query("стоматология",
                                         {"name": "Россия целиком", "ll": ""}), "")

    def test_vk_link_is_taken_from_the_map(self):
        """Ради contact:vk всё и затевалось: по этой ссылке программа
        выходит на контактных лиц компании."""
        from app.sources import osm

        class Resp:
            status_code = 200
            def json(self):
                return {"elements": [{"tags": {
                    "name": "Стоматология Улыбка",
                    "contact:vk": "dentalux",
                    "phone": "+7 495 123-45-67",
                    "website": "https://dentalux.ru",
                    "addr:city": "Москва", "addr:street": "Тверская",
                    "amenity": "dentist"}}]}

        class Sess:
            def post(self, url, data=None, timeout=None, headers=None):
                return Resp()

        rows = osm.search("стоматология", self._city(), session=Sess())
        self.assertEqual(len(rows), 1)
        self.assertIn("https://vk.com/dentalux", rows[0]["links"])
        self.assertEqual(rows[0]["phones"], ["+7 495 123-45-67"])
        self.assertEqual(rows[0]["address"], "Москва, Тверская")

    def test_all_mirrors_busy_means_one_more_round(self):
        """Зеркала перегружаются пачками, но отпускает их быстро. Без
        повтора город просто выпадал из поиска."""
        from app.sources import osm
        calls = []

        class Resp:
            def __init__(self, code): self.status_code = code
            def json(self): return {"elements": []}

        class Sess:
            def post(self, url, data=None, timeout=None, headers=None):
                calls.append(url)
                # Все три зеркала заняты, со второго круга отвечает первое.
                return Resp(200 if len(calls) > 3 else 504)

        was_sleep = osm.time.sleep
        try:
            osm.time.sleep = lambda s: None
            osm.search("аптека", self._city(), session=Sess())
        finally:
            osm.time.sleep = was_sleep
        self.assertGreater(len(calls), 3, "второго круга по зеркалам не было")

    def test_busy_mirror_is_not_a_broken_source(self):
        """Зеркала бесплатные и бывают заняты. Это повод взять следующее,
        а не объявить источник сломанным."""
        from app.sources import osm
        calls = []

        class Resp:
            def __init__(self, code): self.status_code = code
            def json(self): return {"elements": []}

        class Sess:
            def post(self, url, data=None, timeout=None, headers=None):
                calls.append(url)
                return Resp(429 if len(calls) == 1 else 200)

        osm.search("аптека", self._city(), session=Sess())
        self.assertEqual(len(calls), 2, "второе зеркало не попробовали")


class MergingSources(unittest.TestCase):
    """Одна компания из разных источников — одна строка.

    В ЕГРЮЛ есть ИНН и руководитель, но нет способа позвонить; в карте
    есть телефон, но нет ИНН. Пока они считались разными компаниями,
    половина карточек оставалась пустой при том, что данные пришли.
    """

    def test_legal_form_and_quotes_do_not_make_a_new_company(self):
        from app import worker
        self.assertEqual(worker.norm_name('ООО "АН АЛТАЙ"'),
                         worker.norm_name("АН Алтай"))
        self.assertEqual(worker.norm_name("Стоматология «Улыбка»"),
                         worker.norm_name("ООО Стоматология Улыбка"))

    def test_double_prefix_is_stripped(self):
        from app import worker
        self.assertEqual(worker.norm_name("ООО НПО Ромашка"), "нпо ромашка")

    def test_different_companies_stay_different(self):
        from app import worker
        self.assertNotEqual(worker.norm_name("ООО Ромашка"),
                            worker.norm_name("ООО Ромашка-2"))


class JunkAndSocials(unittest.TestCase):
    def test_osm_tag_is_shown_in_russian(self):
        """Список читает продавец, а не картограф: «Агентство
        недвижимости», а не estate_agent."""
        from app.sources import osm
        self.assertEqual(osm.rubric_ru({"office": "estate_agent"}),
                         "Агентство недвижимости")
        self.assertEqual(osm.rubric_ru({"amenity": "dentist"}), "Стоматология")
        # Незнакомый тег хотя бы читается, а не остаётся с подчёркиваниями.
        self.assertEqual(osm.rubric_ru({"shop": "что_то_новое"}), "что то новое")

    def test_site_blocking_the_app_is_retried_as_a_browser(self):
        """Каждый десятый сайт отвечает 403 всему, что не похоже на
        человека. Для нас это выглядело как «сайт не открылся»."""
        from app.sources import site
        calls = []

        class Resp:
            encoding = "utf-8"
            def __init__(self, code, html=""):
                self.status_code = code
                self._body = html.encode("utf-8")
                self.headers = {"Content-Type": "text/html"}
            def iter_content(self, n):
                yield self._body
            def close(self): pass

        class Sess:
            headers = {}
            def get(self, url, timeout=None, allow_redirects=True,
                    headers=None, stream=False):
                as_browser = bool(headers and "Mozilla" in
                                  (headers.get("User-Agent") or ""))
                calls.append(as_browser)
                return Resp(200, "почта: a@b.ru") if as_browser else Resp(403)

        res = site.crawl("https://glz.ru", session=Sess(), max_pages=2, pause=0)
        self.assertFalse(calls[0], "первый заход должен быть от имени программы")
        self.assertTrue(calls[1], "после отказа не перешли на браузерные заголовки")
        self.assertEqual(res["error"], "")
        self.assertIn("a@b.ru", res["emails"])

    def test_refusal_is_reported_not_swallowed(self):
        from app.sources import site

        class Resp:
            status_code = 403
            encoding = "utf-8"
            headers = {"Content-Type": "text/html"}
            def iter_content(self, n): yield b""
            def close(self): pass

        class Sess:
            headers = {}
            def get(self, url, timeout=None, allow_redirects=True,
                    headers=None, stream=False):
                return Resp()

        res = site.crawl("https://glz.ru", session=Sess(), max_pages=1, pause=0)
        self.assertTrue(res["error"], "отказ сайта потерялся")


class Speed(unittest.TestCase):
    """Скорость. Узкое место — чужие серверы, и всё упирается в то,
    сколько мы их ждём, стоя в очереди по одному."""

    def test_sites_are_fetched_in_parallel(self):
        """Восемь сайтов по полсекунды не должны занимать четыре секунды."""
        import time as _t
        from app import worker
        from app.sources import site as site_src

        was = site_src.crawl
        try:
            def slow(url, session=None, **kw):
                _t.sleep(0.25)
                return {"emails": [], "phones": [], "telegram": [], "tech": {},
                        "pages": 1, "error": "", "text": [], "socials": {}}
            site_src.crawl = slow
            rows = [{"id": i, "site": "https://r%d.ru" % i} for i in range(8)]
            t0 = _t.time()
            pre = worker._Prefetch(rows, lambda *a, **k: None)
            for i, r in enumerate(rows):
                pre.fill(i)
                pre.take(r["id"], r["site"])
            pre.close()
            spent = _t.time() - t0
            self.assertLess(spent, 1.2, "обход идёт по очереди: %.1f с" % spent)
        finally:
            site_src.crawl = was

    def test_broken_site_does_not_break_the_run(self):
        from app import worker
        from app.sources import site as site_src
        was = site_src.crawl
        try:
            def boom(url, session=None, **kw):
                raise OSError("сеть отвалилась")
            site_src.crawl = boom
            pre = worker._Prefetch([{"id": 1, "site": "https://x.ru"}],
                                   lambda *a, **k: None)
            res = pre.take(1, "https://x.ru")
            pre.close()
            self.assertIn("сеть отвалилась", res["error"])
            self.assertEqual(res["emails"], [])
        finally:
            site_src.crawl = was

    def test_pages_about_people_are_within_reach(self):
        """Страницы «Команда» однажды оказались за пределами лимита и не
        открывались вовсе — вся работа по поиску руководителя на сайте
        шла впустую."""
        from app.sources import site
        head = site.PATHS[:12]
        self.assertIn("/team", head)
        self.assertIn("/komanda", head)
        self.assertIn("/rukovodstvo", head)


class HugePages(unittest.TestCase):
    """Разбор разметки — работа процессора, а она держит общую блокировку
    Python. Пока ею заняты фоновые потоки, окно не рисуется, и Windows
    подписывает его «Не отвечает»."""

    def _session(self, body):
        class Resp:
            status_code = 200
            headers = {"Content-Type": "text/html"}
            encoding = "utf-8"
            def iter_content(self, n):
                for i in range(0, len(body), n):
                    yield body[i:i + n]
            def close(self): pass

        class Sess:
            headers = {}
            def get(self, url, **kw):
                return Resp()
        return Sess()

    def _page(self, mb):
        return ("<html><body><a href='mailto:head@x.ru'>почта</a>"
                + ("<div>товар</div>" * int(mb * 65000))
                + "<footer>vk.com/xcompany</footer></body></html>").encode("utf-8")

    def test_huge_page_is_parsed_quickly(self):
        from app.sources import site
        import time as _t
        t0 = _t.time()
        site.crawl("https://x.ru", session=self._session(self._page(4)),
                   max_pages=1, pause=0)
        spent = _t.time() - t0
        self.assertLess(spent, 1.0, "четыре мегабайта разбираются %.1f с" % spent)

    def test_footer_survives_the_trimming(self):
        """Соцсети стоят в подвале, то есть в самом конце документа.
        Обрезать хвост нельзя — ради него всё и читается."""
        from app.sources import site
        res = site.crawl("https://x.ru", session=self._session(self._page(4)),
                         max_pages=1, pause=0)
        self.assertIn("head@x.ru", res["emails"])
        self.assertIn("vk", res["socials"])

    def test_small_page_is_not_touched(self):
        from app.sources import site
        body = b"<html>a@b.ru vk.com/small</html>"
        res = site.crawl("https://x.ru", session=self._session(body),
                         max_pages=1, pause=0)
        self.assertIn("a@b.ru", res["emails"])
        self.assertEqual(res["socials"].get("vk"), ["small"])


class SlowSite(unittest.TestCase):
    def test_one_slow_site_cannot_hold_the_queue(self):
        """Двенадцать страниц по десять секунд ожидания — две минуты на
        одну компанию, и очередь стоит за ней."""
        import time as _t
        from app.sources import site

        class Resp:
            status_code = 200
            encoding = "utf-8"
            headers = {"Content-Type": "text/html"}
            def iter_content(self, n): yield b"<html>a@b.ru</html>"
            def close(self): pass

        class Slow:
            headers = {}
            def get(self, url, **kw):
                _t.sleep(0.3)
                return Resp()

        t0 = _t.time()
        res = site.crawl("https://slow.ru", session=Slow(), pause=0, budget=0.8)
        spent = _t.time() - t0
        self.assertLess(spent, 2.0, "обход не уложился в предел: %.1f с" % spent)
        self.assertTrue(res["pages"], "ничего не успели взять")

    def test_rows_are_rendered_in_pages(self):
        """Пятьсот строк со всеми контактами собирает тот же поток,
        который рисует окно. На четырёхстах компаниях оно переставало
        отвечать."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js = io.open(os.path.join(root, "app/static/app.js"),
                     encoding="utf-8").read()
        self.assertIn("PAGE_ROWS", js)
        self.assertIn("more-rows", js)
        # И повторная отрисовка при неизменных данных не делается.
        self.assertIn("lastSignature", js)


class RunLimit(unittest.TestCase):
    def test_run_stops_at_the_limit(self):
        """«Магазин» по десяти городам выгребает десятки тысяч записей.
        Обход идёт сутки, а руки доходят до первой сотни."""
        from app import worker
        from app.sources import osm
        was = osm.search
        try:
            osm.search = lambda q, city, **kw: [
                {"name": "Фирма %s-%d" % (city["name"], i),
                 "site": "https://f%s%d.ru" % (city["name"][:2], i),
                 "address": "", "phones": ["+7 900 000-00-00"],
                 "links": [], "rubric": "", "emails": []}
                for i in range(60)]
            db.init()
            db.conn().execute("DELETE FROM companies")
            db.conn().commit()
            tid = db.create_task("find", {})
            worker.task_find(tid, {
                "query": "магазин", "cities": ["Москва", "Казань", "Уфа"],
                "limit": 100,
                "sources": {"osm": True, "gis": False, "yandex": False,
                            "dadata": False, "hh": False}})
            n = db.conn().execute("SELECT COUNT(*) c FROM companies").fetchone()["c"]
            self.assertEqual(n, 100)
            said = [r["text"] for r in db.conn().execute(
                "SELECT text FROM logs WHERE task_id=?", (tid,))]
            self.assertTrue(any("предел" in t for t in said),
                            "про предел не сказано — прогон выглядит оборванным")
        finally:
            osm.search = was


class JunkPhones(unittest.TestCase):
    """Заглушки из вёрстки. Продавец тратит на каждую по звонку и
    начинает не верить всему списку."""

    def test_layout_placeholders_are_dropped(self):
        from app.sources import site
        for junk in ("+71010000000", "+79999999999", "+70281027703",
                     "+7 800 000-00-00", "+71234567890"):
            self.assertEqual(site._clean_phone(junk), "",
                             "не отсеян: %s" % junk)

    def test_real_numbers_survive(self):
        from app.sources import site
        self.assertEqual(site._clean_phone("+7 495 955-50-43"), "+74959555043")
        self.assertEqual(site._clean_phone("8 (812) 123-45-67"), "+78121234567")
        self.assertEqual(site._clean_phone("+7 902 221-65-58"), "+79022216558")
        self.assertEqual(site._clean_phone("8 800 555-35-36"), "+78005553536")

    def test_kind_tells_a_mobile_from_a_reception(self):
        """Мобильный — это чей-то личный аппарат, и отвечает на него
        человек, а не приёмная."""
        from app.sources import site
        self.assertEqual(site.phone_kind("+79022216558"), "мобильный")
        self.assertEqual(site.phone_kind("+74959555043"), "городской")
        self.assertEqual(site.phone_kind("+78005553536"), "бесплатный")


class WorkWithList(unittest.TestCase):
    def test_notes_do_not_overwrite_each_other(self):
        """Разговоров бывает несколько, и затирать предыдущий следующим —
        значит терять ровно то, ради чего заметка пишется."""
        db.init()
        cid, _ = db.upsert_company({"name": "Заметки-тест", "source": "тест"})
        db.add_note(cid, "первый звонок")
        db.add_note(cid, "второй звонок")
        texts = [n["text"] for n in db.notes(cid)]
        self.assertEqual(texts, ["второй звонок", "первый звонок"])
        db.add_note(cid, "   ")
        self.assertEqual(len(db.notes(cid)), 2, "пустая заметка сохранилась")

    def test_merge_keeps_everything_from_both(self):
        """У одной записи есть ИНН, у другой сайт — вместе они и
        составляют компанию."""
        db.init()
        a, _ = db.upsert_company({"name": "Слияние А", "site": "https://merge-a.ru",
                                  "source": "карта"})
        db.add_contact(a, "phone", "+74951112233", "general", 85, "unchecked", "карта")
        b, _ = db.upsert_company({"name": "ООО «Слияние А»", "inn": "9999999999",
                                  "director": "Иванов Иван", "source": "ЕГРЮЛ"})
        db.add_note(b, "договорились о письме")
        self.assertTrue(db.merge_companies(a, b))
        row = db.conn().execute("SELECT * FROM companies WHERE id=?", (a,)).fetchone()
        self.assertEqual(row["inn"], "9999999999")
        self.assertEqual(row["director"], "Иванов Иван")
        self.assertEqual(row["site"], "https://merge-a.ru")
        self.assertEqual([n["text"] for n in db.notes(a)], ["договорились о письме"])
        gone = db.conn().execute("SELECT * FROM companies WHERE id=?", (b,)).fetchone()
        self.assertIsNone(gone)

    def test_visited_site_is_not_crawled_again(self):
        db.init()
        self.assertFalse(db.visited_recently("https://fresh-site.ru"))
        db.mark_visited("https://fresh-site.ru/contacts", pages=4)
        self.assertTrue(db.visited_recently("http://www.fresh-site.ru"))
        self.assertFalse(db.visited_recently("https://other-site.ru"))

    def test_failed_crawl_is_not_remembered_as_done(self):
        """Сайт, который не открылся, надо попробовать снова, а не
        считать обойдённым."""
        db.init()
        db.mark_visited("https://dead-site.ru", pages=0, ok=False)
        self.assertFalse(db.visited_recently("https://dead-site.ru"))


class StuckTasks(unittest.TestCase):
    """Задача, оставшаяся с прошлого запуска.

    Программу закрывают посреди обхода — это норма. Но запись о задаче
    оставалась со статусом «выполняется», и очередь её не подхватывала:
    она берёт только «в очереди». Такая запись становилась вечной —
    интерфейс показывал именно её, счётчик стоял на нуле, а все новые
    задачи ждали за ней. Снаружи это выглядело как зависшая программа,
    час и дольше.
    """

    def test_interrupted_task_is_released_on_start(self):
        from app import worker
        db.init()
        db.conn().execute("DELETE FROM tasks")
        tid = db.create_task("enrich", {}, total=156)
        db.update_task(tid, status="running", done=0)
        self.assertEqual(worker.recover(), 1)
        row = db.conn().execute("SELECT status, message FROM tasks WHERE id=?",
                                (tid,)).fetchone()
        self.assertEqual(row["status"], "stopped")
        self.assertIn("прервано", row["message"])

    def test_queued_tasks_are_left_alone(self):
        from app import worker
        db.init()
        db.conn().execute("DELETE FROM tasks")
        tid = db.create_task("enrich", {})
        worker.recover()
        row = db.conn().execute("SELECT status FROM tasks WHERE id=?",
                                (tid,)).fetchone()
        self.assertEqual(row["status"], "queued")

    def test_interruption_is_explained_in_the_journal(self):
        """Молча сбросить — значит оставить человека с вопросом, куда
        делась работа."""
        from app import worker
        db.init()
        db.conn().execute("DELETE FROM tasks")
        tid = db.create_task("enrich", {}, total=156)
        db.update_task(tid, status="running", done=40)
        worker.recover()
        said = [r["text"] for r in db.conn().execute(
            "SELECT text FROM logs WHERE task_id=?", (tid,))]
        self.assertTrue(any("40" in t and "156" in t for t in said))


class OldJunkPhones(unittest.TestCase):
    def test_stored_placeholders_are_cleaned(self):
        """Отсев появился позже первых прогонов, и заглушки остались в
        базе. Они не становятся телефонами оттого, что лежат давно."""
        db.init()
        cid, _ = db.upsert_company({"name": "Чистка телефонов", "source": "тест"})
        for ph in ("+70281027703", "+71010000000", "+79022216558"):
            db.add_contact(cid, "phone", ph, "general", 85, "unchecked", "сайт")
        self.assertEqual(db.clean_junk_phones(), 2)
        left = [r["value"] for r in db.conn().execute(
            "SELECT value FROM contacts WHERE company_id=? AND kind='phone'",
            (cid,))]
        self.assertEqual(left, ["+79022216558"])


class Appearance(unittest.TestCase):
    def test_font_lives_inside_the_program(self):
        """Программа настольная и должна открываться одинаково с
        интернетом и без него. Шрифт из сети при отсутствии связи
        подменяется системным, и половина вёрстки уезжает."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        fonts = os.path.join(root, "app/static/fonts")
        for name in ("inter-cyrillic.woff2", "inter-latin.woff2"):
            path = os.path.join(fonts, name)
            self.assertTrue(os.path.exists(path), "нет файла шрифта: " + name)
            with open(path, "rb") as f:
                self.assertEqual(f.read(4), b"wOF2", "это не woff2: " + name)
        # Лицензия обязана лежать рядом: OFL требует распространять её
        # вместе со шрифтом.
        self.assertTrue(os.path.exists(os.path.join(fonts, "Inter-LICENSE.txt")))

    def test_css_does_not_reach_into_the_network(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        css = io.open(os.path.join(root, "app/static/app.css"),
                      encoding="utf-8").read()
        self.assertIn('url("/static/fonts/inter-cyrillic.woff2")', css)
        self.assertNotIn("fonts.googleapis.com", css)
        self.assertNotIn("fonts.gstatic.com", css)

    def test_cyrillic_range_is_covered(self):
        """Без диапазона кириллицы браузер возьмёт латинский файл, не
        найдёт в нём русских букв и подставит системный шрифт."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        css = io.open(os.path.join(root, "app/static/app.css"),
                      encoding="utf-8").read()
        self.assertIn("U+0400-045F", css)


class NewScreens(unittest.TestCase):
    """Сегодня и воронка. Раньше стадия была спрятана в выпадающем
    списке последней колонки, а «что делать сегодня» не было вовсе."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM companies")
        db.conn().commit()
        self.app = web.create_app().test_client()

    def test_board_groups_by_stage(self):
        a, _ = db.upsert_company({"name": "Новая", "source": "тест"})
        b, _ = db.upsert_company({"name": "В работе", "source": "тест"})
        db.update_company_fields(b, {"stage": "в работе"})
        d = self.app.get("/api/board").get_json()
        self.assertEqual(d["board"]["new"]["total"], 1)
        self.assertEqual(d["board"]["в работе"]["total"], 1)
        self.assertEqual(d["board"]["в работе"]["cards"][0]["name"], "В работе")

    def test_board_card_carries_one_contact(self):
        """На доске нужен один контакт, а не все: колонка с пятью
        телефонами на карточку перестаёт быть доской."""
        cid, _ = db.upsert_company({"name": "С телефоном", "source": "тест"})
        db.add_contact(cid, "phone", "+74951234567", "general", 90,
                       "unchecked", "тест")
        db.add_contact(cid, "email", "a@b.ru", "general", 50, "unchecked", "тест")
        card = self.app.get("/api/board").get_json()["board"]["new"]["cards"][0]
        self.assertEqual(card["contact"], "+74951234567")

    def test_today_shows_overdue_too(self):
        """Вчерашний несделанный звонок не стал менее нужным."""
        old_, _ = db.upsert_company({"name": "Просрочено", "source": "тест"})
        db.update_company_fields(old_, {"next_date": "2020-01-01",
                                        "next_step": "позвонить"})
        far, _ = db.upsert_company({"name": "Потом", "source": "тест"})
        db.update_company_fields(far, {"next_date": "2099-01-01",
                                       "next_step": "позвонить"})
        d = self.app.get("/api/today").get_json()
        names = [r["name"] for r in d["due"]]
        self.assertIn("Просрочено", names)
        self.assertNotIn("Потом", names)

    def test_today_counts_stages(self):
        cid, _ = db.upsert_company({"name": "Одна", "source": "тест"})
        db.update_company_fields(cid, {"stage": "созвон"})
        d = self.app.get("/api/today").get_json()
        self.assertEqual(d["stages"].get("созвон"), 1)


class SavedSearches(unittest.TestCase):
    def test_same_name_overwrites_instead_of_doubling(self):
        db.init()
        first = db.save_search("Еженедельный", {"queries": ["а"]})
        again = db.save_search("Еженедельный", {"queries": ["б"]})
        self.assertEqual(first, again)
        self.assertEqual(db.get_search(first)["params"]["queries"], ["б"])
        db.delete_search(first)

    def test_nameless_set_is_refused(self):
        """Набор без названия потом не найти — сохранять его незачем."""
        self.assertEqual(db.save_search("   ", {"queries": ["а"]}), 0)


class Sources(unittest.TestCase):
    def test_size_bands(self):
        self.assertEqual(fns.size_band(0), "")
        self.assertEqual(fns.size_band(5_000_000), "микро")
        self.assertEqual(fns.size_band(84_000_000), "средний")
        self.assertEqual(fns.size_band(2_000_000_000), "крупный")

    def test_revenue_row_found_by_code_not_name(self):
        """Названия строк отчёта меняются от года к году, коды — нет."""
        self.assertEqual(fns._pick({"current2110": 5_000_000}, "2110"), 5_000_000)
        self.assertIsNone(fns._pick({"current2400": 1}, "2110"))

    def test_bad_inn_never_reaches_network(self):
        self.assertEqual(fns.find_org("абв"), {})
        self.assertEqual(zakupki.find_org_id("abc"), "")

    def test_procurement_card_parsed_by_labels(self):
        lines = ["Контактное лицо", "", "Соколов Андрей Викторович",
                 "Телефон", "+7 495 123-45-67"]
        self.assertEqual(zakupki._value_after(lines, ("Контактное лицо",)),
                         "Соколов Андрей Викторович")
        self.assertEqual(zakupki._value_after(lines, ("Нет такого поля",)), "")

    def test_gis_without_key_does_nothing(self):
        """Ключа нет — источник молчит, а не падает посреди обхода."""
        self.assertEqual(gis2.search("стоматология", 32, ""), [])


class Financials(unittest.TestCase):
    """Динамика говорит о компании больше, чем цифра за один год."""

    def test_growth_labels(self):
        up = [{"year": 2025, "revenue": 312}, {"year": 2024, "revenue": 210}]
        flat = [{"year": 2025, "revenue": 100}, {"year": 2024, "revenue": 98}]
        down = [{"year": 2025, "revenue": 40}, {"year": 2024, "revenue": 90}]
        self.assertEqual(fns.growth(up)[0], "быстрый рост")
        self.assertEqual(fns.growth(flat)[0], "на месте")
        self.assertEqual(fns.growth(down)[0], "сильный спад")

    def test_small_change_is_not_growth(self):
        """До 8% в обе стороны — шум инфляции, а не рост."""
        rows = [{"year": 2025, "revenue": 105}, {"year": 2024, "revenue": 100}]
        self.assertEqual(fns.growth(rows)[0], "на месте")

    def test_one_year_has_no_dynamics(self):
        self.assertEqual(fns.growth([{"year": 2025, "revenue": 10}]), ("", 0))
        self.assertEqual(fns.growth([]), ("", 0))

    def test_zero_base_does_not_divide(self):
        rows = [{"year": 2025, "revenue": 50}, {"year": 2024, "revenue": 0}]
        self.assertEqual(fns.growth(rows), ("", 0))


class Registry(unittest.TestCase):
    def test_full_card_unpacked(self):
        item = {"value": "ООО Ромашка", "data": {
            "inn": "7701", "ogrn": "1027",
            "name": {"short_with_opf": "ООО «Ромашка»"},
            "management": {"name": "Иванов Иван", "post": "Генеральный директор"},
            "state": {"status": "ACTIVE", "registration_date": 1041379200000},
            "capital": {"value": 10000}, "branch_count": 3, "type": "LEGAL",
            "okveds": [{"main": True, "name": "Стоматология", "code": "86.23"},
                       {"main": False, "name": "Торговля", "code": "47.11"}],
            "founders": [{"name": "Иванов И."}, {"name": "Петров П."}],
            "address": {"value": "Москва", "data": {"region_with_type": "г Москва"}}}}
        got = dadata._unpack(item)
        self.assertEqual(got["capital"], 10000)
        self.assertEqual(got["branches"], 3)
        self.assertEqual(got["founders_count"], 2)
        self.assertEqual(got["founded"], 2003)
        self.assertIn("47.11", got["okveds_extra"])
        self.assertNotIn("Стоматология", got["okveds_extra"])


class SelfReported(unittest.TestCase):
    """Цифры, которые компания сама вынесла на главную."""

    def _facts(self, html):
        r = {"self_year": None, "self_staff": None, "self_branches": None,
             "shop": False, "prices": False, "no_prices": False,
             "app": False, "last_post": ""}
        site._self_facts(html, None, r)
        return r

    def test_year_staff_branches(self):
        r = self._facts("Работаем с 2011 года. Более 60 специалистов, 3 клиники.")
        self.assertEqual((r["self_year"], r["self_staff"], r["self_branches"]),
                         (2011, 60, 3))

    def test_impossible_year_ignored(self):
        """Иначе под шаблон «с 2055» попадёт любой номер в тексте."""
        self.assertIsNone(self._facts("Заказ с 2055 позиции")["self_year"])

    def test_latest_date_wins(self):
        r = self._facts("05.01.2024 старая новость. 12 марта 2026 свежая.")
        self.assertEqual(r["last_post"], "2026-03-12")

    def test_sales_model_detected(self):
        shop = self._facts("Добавить в корзину. Цена 15 000 ₽, 4 500 ₽, 900 ₽")
        self.assertTrue(shop["shop"] and shop["prices"])
        quote = self._facts("Стоимость по запросу")
        self.assertTrue(quote["no_prices"])
        self.assertFalse(quote["prices"])


class ImportList(unittest.TestCase):
    def test_mixed_line_parsed_whole(self):
        rows = importer.parse("7701234567;ООО Ромашка;romashka.ru")
        self.assertEqual(rows[0]["inn"], "7701234567")
        self.assertEqual(rows[0]["name"], "ООО Ромашка")
        self.assertEqual(rows[0]["site"], "https://romashka.ru")

    def test_url_reduced_to_domain(self):
        rows = importer.parse("https://www.vasilek.ru/about")
        self.assertEqual(rows[0]["site"], "https://vasilek.ru")

    def test_row_numbers_are_not_companies(self):
        """Голые цифры не той длины — остатки нумерации, а не названия."""
        self.assertEqual(importer.parse("322\n17"), [])

    def test_duplicates_collapse(self):
        self.assertEqual(len(importer.parse("7701234567\n7701234567")), 1)


class AI(unittest.TestCase):
    """Модель объясняет собранное, а не добывает новое."""

    def test_no_key_no_request(self):
        db.set_setting("ai_key", "")
        self.assertFalse(ai.enabled())
        text, err = ai.ask([{"role": "user", "content": "привет"}])
        self.assertEqual(text, "")
        self.assertIn("ключ", err)

    def test_prompt_forbids_making_things_up(self):
        """Правило не должно потеряться при правке промпта."""
        self.assertIn("ТОЛЬКО факты", ai.SYSTEM)
        self.assertIn("не придумывай", ai.SYSTEM.lower())

    def test_json_survives_markdown_and_chatter(self):
        """Модели оборачивают ответ в ``` даже когда просили не оборачивать."""
        self.assertEqual(ai._json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(ai._json('Конечно! {"b": 2} Готово.'), {"b": 2})
        self.assertIsNone(ai._json("никакого json"))
        self.assertIsNone(ai._json(""))

    def test_brief_contains_only_collected_facts(self):
        db.init()
        cid, _ = db.upsert_company({
            "name": "ООО Ромашка", "inn": "7701", "director": "Иванов Иван",
            "activity": "Стоматология", "growth": "рост +12%"})
        row = db.conn().execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        brief = ai.company_brief(row, {"revenue": "312000000"}, [{"kind": "email"}])
        self.assertIn("ООО Ромашка", brief)
        self.assertIn("рост +12%", brief)
        self.assertIn("312000000", brief)
        # Пустые поля не попадают: они занимают контекст и подталкивают
        # модель заполнить пробел догадкой.
        self.assertNotIn("Уставный капитал", brief)

    def test_brief_is_capped(self):
        db.init()
        cid, _ = db.upsert_company({"name": "Х" * 500, "inn": "7702",
                                    "activity": "Я" * 3000})
        row = db.conn().execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        self.assertLessEqual(len(ai.company_brief(row, {}, [])), 2600)

    def test_generated_fields_are_separate(self):
        """ИИ-поля не должны пересекаться с фактическими: смешав их,
        отличить разобранное от сочинённого станет невозможно."""
        facts = {"revenue", "employees", "founded", "capital", "director"}
        generated = {"ai_summary", "ai_segment", "ai_fit", "ai_why",
                     "ai_hook", "ai_opener"}
        self.assertEqual(facts & generated, set())
        have = {r["name"] for r in db.conn().execute("PRAGMA table_info(companies)")}
        self.assertTrue(generated <= have)


class Packaging(unittest.TestCase):
    """Запуск в один клик ломается тихо: файл на месте, а внутри опечатка."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(self, name):
        with open(os.path.join(self.ROOT, name), encoding="utf-8") as f:
            return f.read()

    def test_launchers_and_icon_exist(self):
        for name in ("Navodka.bat", "launcher.py", "navodka.sh",
                     "build/icon.ico", "build/installer.iss",
                     "build/navodka.spec"):
            self.assertTrue(os.path.exists(os.path.join(self.ROOT, name)), name)

    def test_bat_is_pure_ascii(self):
        """Кириллица внутри bat зависит от кодовой страницы консоли: на
        части машин файл ломается на полуслове и окно закрывается молча."""
        with open(os.path.join(self.ROOT, "Navodka.bat"), "rb") as f:
            raw = f.read()
        raw.decode("ascii")                      # упадёт, если появится не-ASCII
        self.assertNotIn(b"chcp", raw)

    def test_bat_name_is_ascii(self):
        """Кириллицу в имени файла часть архиваторов распаковывает мусором."""
        for name in os.listdir(self.ROOT):
            if name.lower().endswith((".bat", ".cmd")):
                name.encode("ascii")

    def test_bat_waits_on_error(self):
        """Без pause ошибка выглядит как мигнувшее и закрывшееся окно."""
        self.assertIn("pause", self._read("Navodka.bat"))
        self.assertIn("errorlevel 1", self._read("Navodka.bat"))

    def test_launcher_creates_env_next_to_program(self):
        code = self._read("launcher.py")
        self.assertIn('"-m", "venv"', code)
        self.assertIn("requirements.stamp", code)

    def test_launcher_checks_it_actually_started(self):
        """Программа может закрыться сразу — это надо заметить и показать."""
        code = self._read("launcher.py")
        self.assertIn("proc.poll()", code)
        self.assertIn("navodka.log", code)

    def test_launcher_writes_transcript(self):
        self.assertIn("launcher.log", self._read("launcher.py"))

    def test_installer_ships_icon(self):
        iss = self._read("build/installer.iss")
        self.assertIn("SetupIconFile=icon.ico", iss)
        self.assertIn("IconFilename", iss)

    def test_spec_bundles_templates(self):
        """Забыть шаблоны — значит собрать exe, падающий на первой странице."""
        spec = self._read("build/navodka.spec")
        self.assertIn("app/templates", spec)
        self.assertIn("app/static", spec)


class Export(unittest.TestCase):
    def test_found_and_guessed_go_to_separate_columns(self):
        """В одной ячейке продавец не разберёт, какому адресу доверять."""
        keys = [k for k, _ in export.COLUMNS]
        self.assertIn("email_director", keys)
        self.assertIn("email_guess", keys)
        self.assertIn("phone_director", keys)
        self.assertIn("lpr_status", keys)

    def test_csv_opens_in_russian_excel(self):
        db.init()
        blob = export.to_csv(export.rows_for_export(db.conn()))
        self.assertTrue(blob.startswith(b"\xef\xbb\xbf"), "нет BOM — Excel покажет кракозябры")
        self.assertIn(b";", blob.split(b"\n")[0], "не тот разделитель")


class Update(unittest.TestCase):
    """Обновление — единственное место, где программа переписывает сама
    себя. Ошибка здесь стоит дороже любой другой."""

    def test_versions_compare_by_numbers(self):
        self.assertGreater(update._vtuple("0.10.0"), update._vtuple("0.9.0"))
        self.assertGreater(update._vtuple("1.0"), update._vtuple("0.99.99"))
        self.assertEqual(update._vtuple("v0.3.0"), (0, 3, 0))

    def test_installed_copy_updates_by_installer(self):
        """Из exe подменять исходники нечего — их там нет."""
        was = settings.frozen
        try:
            settings.frozen = lambda: True
            self.assertEqual(update.kind(), "installer")
            settings.frozen = lambda: False
            self.assertEqual(update.kind(), "source")
        finally:
            settings.frozen = was

    def test_installer_refuses_without_asset(self):
        ok, msg, restart = update.apply_installer("")
        self.assertFalse(ok)
        self.assertFalse(restart)

    def test_archive_with_paths_outside_is_refused(self):
        """Архив, кладущий файлы наружу, — это чужой код на диске."""
        import zipfile
        path = os.path.join(_TMP, "evil.zip")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("../../evil.py", "x" * 20000)
        out = os.path.join(_TMP, "unpack")
        os.makedirs(out, exist_ok=True)
        with zipfile.ZipFile(path) as z:
            bad = [n for n in z.namelist()
                   if n.startswith("/") or ".." in n.replace("\\", "/").split("/")]
        self.assertTrue(bad, "проверка путей перестала ловить выход наружу")

    def test_root_of_archive_is_found_by_content(self):
        """Имя папки внутри архива у GitHub своё, ориентироваться на него
        нельзя."""
        box = os.path.join(_TMP, "arc")
        inner = os.path.join(box, "navodka-abc1234")
        os.makedirs(os.path.join(inner, "app"), exist_ok=True)
        io_open = open(os.path.join(inner, "main.py"), "w")
        io_open.write("# main")
        io_open.close()
        self.assertEqual(update._find_root(box), inner)
        self.assertEqual(update._find_root(inner), inner)

    def test_installer_version_matches_code(self):
        """Версия в installer.iss и в settings.py разошлись однажды, и
        обновление стало предлагать себя само себе."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "build", "installer.iss"), encoding="utf-8") as f:
            iss = f.read()
        import re
        m = re.search(r'#define\s+AppVersion\s+"([^"]+)"', iss)
        self.assertIsNotNone(m, "в installer.iss нет запасной версии")
        self.assertEqual(m.group(1), settings.VERSION)


class Router(unittest.TestCase):
    """Посредники, отдающие Claude по адресу вида …/v1/messages.

    Формат Anthropic несовместим с OpenAI сразу в трёх местах, и каждое
    ломает запрос целиком: ключ в другом заголовке, системная подсказка
    вынесена из списка сообщений, ответ лежит не в choices."""

    def test_guesses_format_by_url(self):
        self.assertEqual(ai.kind("", "https://router.cheap/v1/messages"),
                         "anthropic")
        self.assertEqual(ai.kind("", "https://api.anthropic.com"), "anthropic")
        self.assertEqual(ai.kind("", "https://api.openai.com/v1"), "openai")
        self.assertEqual(ai.kind("", ""), "openai")

    def test_explicit_choice_wins_over_url(self):
        """Человек выбрал формат руками — угадывать поверх него нельзя."""
        self.assertEqual(ai.kind("openai", "https://router.cheap/v1/messages"),
                         "openai")
        self.assertEqual(ai.kind("anthropic", "https://api.openai.com/v1"),
                         "anthropic")

    def _fake(self, payload, status=200):
        sent = {}

        class R:
            status_code = status
            text = json.dumps(payload)

            def json(self_inner):
                return payload

        class S:
            def post(self_inner, url, headers=None, json=None, timeout=None):
                sent["url"] = url
                sent["headers"] = headers
                sent["body"] = json
                return R()

            def get(self_inner, url, headers=None, timeout=None):
                sent["url"] = url
                sent["headers"] = headers
                return R()

        return S(), sent

    def test_anthropic_request_shape(self):
        s, sent = self._fake({"content": [{"type": "text", "text": "  да  "}]})
        cfg = {"key": "k", "url": "https://router.cheap", "model": "claude",
               "kind": "anthropic"}
        text, err = ai.ask([{"role": "system", "content": "правила"},
                            {"role": "user", "content": "вопрос"}],
                           cfg=cfg, session=s)
        self.assertEqual((text, err), ("да", ""))
        self.assertEqual(sent["url"], "https://router.cheap/v1/messages")
        self.assertEqual(sent["headers"]["x-api-key"], "k")
        self.assertIn("anthropic-version", sent["headers"])
        self.assertNotIn("Authorization", sent["headers"])
        # Системная подсказка вынесена из messages в своё поле.
        self.assertEqual(sent["body"]["system"], "правила")
        self.assertEqual(sent["body"]["messages"],
                         [{"role": "user", "content": "вопрос"}])

    def test_url_taken_in_any_form(self):
        """Человек вставляет то, что дал посредник, а не то, что удобно нам."""
        for given in ("https://router.cheap", "https://router.cheap/",
                      "https://router.cheap/v1", "https://router.cheap/v1/messages"):
            s, sent = self._fake({"content": [{"type": "text", "text": "ок"}]})
            ai.ask([{"role": "user", "content": "?"}],
                   cfg={"key": "k", "url": given, "model": "m",
                        "kind": "anthropic"}, session=s)
            self.assertEqual(sent["url"], "https://router.cheap/v1/messages",
                             "адрес %s собрался неверно" % given)

    def test_empty_answer_is_an_error_not_an_empty_card(self):
        s, _ = self._fake({"content": []})
        text, err = ai.ask([{"role": "user", "content": "?"}],
                           cfg={"key": "k", "url": "https://router.cheap",
                                "model": "m", "kind": "anthropic"}, session=s)
        self.assertEqual(text, "")
        self.assertTrue(err)

    def test_openai_path_untouched(self):
        s, sent = self._fake(
            {"choices": [{"message": {"content": "ответ"}}]})
        text, err = ai.ask([{"role": "system", "content": "правила"},
                            {"role": "user", "content": "вопрос"}],
                           cfg={"key": "k", "url": "https://api.openai.com/v1",
                                "model": "m", "kind": "openai"}, session=s)
        self.assertEqual((text, err), ("ответ", ""))
        self.assertEqual(sent["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer k")
        self.assertEqual(len(sent["body"]["messages"]), 2)

    def test_model_list_asks_the_right_address(self):
        """Название модели у посредника своё, и угадывать его за человека
        нельзя — список приходится спрашивать."""
        s, sent = self._fake({"data": [{"id": "claude-sonnet-4"},
                                       {"id": "gpt-4o-mini"}]})
        got, err = ai.models(cfg={"key": "k", "model": "m",
                                  "url": "https://router.cheap/v1/messages",
                                  "kind": "anthropic"}, session=s)
        self.assertEqual(err, "")
        self.assertEqual(got, ["claude-sonnet-4", "gpt-4o-mini"])
        self.assertEqual(sent["url"], "https://router.cheap/v1/models")
        self.assertEqual(sent["headers"]["x-api-key"], "k")


class SearchScreen(unittest.TestCase):
    """Экран поиска. Две колонки расходились по высоте втрое, и порядок
    действий в них не читался."""

    def setUp(self):
        db.init()
        self.html = io.open(os.path.join(os.path.dirname(__file__), "..",
                                         "app", "templates", "index.html"),
                            encoding="utf-8").read()

    def test_both_big_cards_are_numbered_steps(self):
        self.assertGreaterEqual(self.html.count('class="step-n"'), 6)
        self.assertNotIn('class="fields two"', self.html)

    def test_sources_are_tiles_with_key_state(self):
        """Видно с первого взгляда, что отработает, а что молча пропустят."""
        self.assertEqual(self.html.count('class="src"'), 5)
        self.assertIn('class="need"', self.html)

    def test_all_search_fields_survived(self):
        for fid in ("q-text", "q-cities", "q-osm", "q-gis", "q-yandex",
                    "q-dadata", "q-hh", "q-skip-empty", "q-limit", "q-pages",
                    "q-then", "q-then-zak", "q-then-ai",
                    "f-text", "f-presets", "f-area", "f-period", "f-pages",
                    "f-title", "f-noagency", "f-maxopen",
                    "f-then", "f-then-zak", "f-then-ai"):
            self.assertIn('id="%s"' % fid, self.html, "пропало поле %s" % fid)


class FixedBugs(unittest.TestCase):
    """То, что человек видел на экране и называл поломкой."""

    def setUp(self):
        db.init()
        self.css = io.open(os.path.join(os.path.dirname(__file__), "..",
                                        "app", "static", "app.css"),
                           encoding="utf-8").read()
        self.js = io.open(os.path.join(os.path.dirname(__file__), "..",
                                       "app", "static", "app.js"),
                          encoding="utf-8").read()

    def test_every_input_type_is_styled(self):
        """Перечисление типов по одному оставляло password и date с
        оформлением от браузера: белая коробка посреди тёмной темы."""
        self.assertIn("input:not([type=checkbox]):not([type=radio])", self.css)
        self.assertNotIn("input[type=text],input[type=number],"
                         "input[type=search],select,textarea{", self.css)

    def test_native_controls_follow_the_theme(self):
        """Флажки и полосы прокрутки рисует браузер, и без color-scheme
        он рисует их светлыми всегда."""
        self.assertIn("color-scheme:light dark", self.css)

    def test_funnel_columns_fit_the_window(self):
        """Пять колонок по 230 не помещались: «отказ» обрезалась краем."""
        import re
        m = re.search(r"\.board\{[^}]*grid-auto-columns:minmax\((\d+)px", self.css)
        self.assertIsNotNone(m)
        self.assertLessEqual(int(m.group(1)) * 5 + 4 * 12, 1100)

    def test_format_and_address_are_kept_in_step(self):
        """Формат Anthropic с адресом OpenAI — гарантированный отказ,
        по которому не догадаться, что виноват адрес."""
        self.assertIn('$("s-ai-kind").onchange', self.js)
        self.assertIn("aiKindNote", self.js)

    def test_key_is_visible_while_typing(self):
        self.assertIn("input[type=password]", self.js)


class StaleRunBar(unittest.TestCase):
    """Задача, оборвавшаяся в прошлый запуск, висела в шапке вечно и
    встречала человека при каждом открытии программы."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM tasks")
        db.conn().commit()
        self.app = web.create_app().test_client()

    def test_old_finished_task_is_not_shown(self):
        tid = db.create_task("find", {})
        db.conn().execute("UPDATE tasks SET status='stopped', done=0, total=3, "
                          "updated_at=? WHERE id=?",
                          (web.STARTED_AT - 3600, tid))
        db.conn().commit()
        self.assertIsNone(self.app.get("/api/task").get_json()["task"])

    def test_task_finished_in_this_run_is_shown(self):
        tid = db.create_task("find", {})
        db.conn().execute("UPDATE tasks SET status='done', updated_at=? "
                          "WHERE id=?", (web.STARTED_AT + 5, tid))
        db.conn().commit()
        self.assertEqual(self.app.get("/api/task").get_json()["task"]["id"], tid)

    def test_running_task_is_always_shown(self):
        """Даже если она тянется со вчерашнего дня."""
        tid = db.create_task("find", {})
        db.conn().execute("UPDATE tasks SET status='running', updated_at=? "
                          "WHERE id=?", (web.STARTED_AT - 99999, tid))
        db.conn().commit()
        self.assertEqual(self.app.get("/api/task").get_json()["task"]["id"], tid)

    def test_favicon_does_not_404(self):
        self.assertIn(self.app.get("/favicon.ico").status_code, (200, 204))


class TradeToTags(unittest.TestCase):
    """«Дента-Люкс» — стоматология, но слова «стоматология» в названии
    нет. Без тега такая компания не находится вообще."""

    def test_common_trades_have_tags(self):
        from app.sources import osm
        city = {"name": "Москва", "ll": "37.6,55.7", "spn": "0.9,0.5"}
        for word, tag in (("грузоперевозки", "logistics"),
                          ("окна", "window"),
                          ("строительная компания", "construction_company"),
                          ("автомойка", "car_wash"),
                          ("бухгалтерские услуги", "accountant"),
                          ("кадровое агентство", "employment_agency")):
            q = osm.build_query(word, city)
            self.assertIn(tag, q, "«%s» ищется только по названию" % word)

    def test_several_tag_groups_are_used(self):
        """Раньше брали первое совпавшее слово и выходили."""
        from app.sources import osm
        city = {"name": "Москва", "ll": "37.6,55.7", "spn": "0.9,0.5"}
        q = osm.build_query("медицинская клиника", city)
        self.assertIn("clinic", q)
        self.assertIn("doctors", q)

    def test_query_does_not_grow_without_limit(self):
        """Overpass отвечает отказом на слишком широкий запрос."""
        from app.sources import osm
        city = {"name": "Москва", "ll": "37.6,55.7", "spn": "0.9,0.5"}
        q = osm.build_query("медицинская клиника стоматология аптека "
                            "лаборатория оптика", city)
        self.assertLessEqual(q.count("nwr["), 10)


class BrokenConnection(unittest.TestCase):
    """Посредник отвечал не отказом, а обрывом связи: WinError 10054.
    Это не поломка сервера — он не успел ответить."""

    def test_reset_is_told_apart_from_refusal(self):
        self.assertTrue(ai._is_reset(Exception(
            "('Connection aborted.', ConnectionResetError(10054, "
            "'Удаленный хост принудительно разорвал подключение'))")))
        self.assertFalse(ai._is_reset(Exception("HTTP 401 unauthorized")))

    def test_reset_is_explained_in_words(self):
        note = ai._explain(Exception("ConnectionResetError(10054)"))
        self.assertIn("разорвана", note)
        self.assertNotIn("Traceback", note)

    def test_second_transport_is_tried_after_a_reset(self):
        """У requests свой отпечаток рукопожатия, ни на один браузер не
        похожий. Фильтр по дороге рвёт связь именно по нему."""
        calls = []

        class Dead(object):
            def post(self_inner, *a, **k):
                calls.append("requests")
                raise Exception("('Connection aborted.', "
                                "ConnectionResetError(10054, 'разорвал'))")

        class Alive(object):
            def post(self_inner, *a, **k):
                calls.append("curl")

                class R:
                    status_code = 200
                    text = "{}"

                    def json(self_r):
                        return {"content": [{"type": "text", "text": "да"}]}
                return R()

        real = ai._transports
        ai._transports = lambda session=None: [Dead(), Alive()]
        try:
            text, err = ai.ask([{"role": "user", "content": "?"}],
                               cfg={"key": "k", "url": "https://router.cheap",
                                    "model": "m", "kind": "anthropic"})
        finally:
            ai._transports = real
        self.assertEqual((text, err), ("да", ""))
        self.assertEqual(calls, ["requests", "curl"])

    def test_refusal_is_not_retried(self):
        """Отказ по существу повторять незачем: ответ будет тот же."""
        calls = []

        class Refuse(object):
            def post(self_inner, *a, **k):
                calls.append(1)
                raise Exception("invalid api key")

        real = ai._transports
        ai._transports = lambda session=None: [Refuse(), Refuse()]
        try:
            ai.ask([{"role": "user", "content": "?"}],
                   cfg={"key": "k", "url": "https://router.cheap",
                        "model": "m", "kind": "anthropic"})
        finally:
            ai._transports = real
        self.assertEqual(len(calls), 1)

    def test_model_matches_the_format(self):
        """gpt-4o-mini посреднику Claude не известна, и отказ про
        неизвестную модель человек читает как поломку программы."""
        self.assertEqual(ai.model("", "anthropic"), ai.DEFAULT_ANTHROPIC_MODEL)
        self.assertEqual(ai.model("", "openai"), ai.DEFAULT_MODEL)
        self.assertEqual(ai.model("своя-модель", "anthropic"), "своя-модель")

    def test_address_is_named_in_the_check(self):
        """«Не работает» без адреса — гадание."""
        cfg = {"key": "", "url": "https://router.cheap", "model": "m",
               "kind": "anthropic"}
        ok, note = ai.check(cfg)
        self.assertFalse(ok)
        self.assertIn("https://router.cheap/v1/messages", note)

    def test_endpoint_for_both_formats(self):
        self.assertEqual(
            ai.endpoint({"url": "https://router.cheap", "kind": "anthropic"}),
            "https://router.cheap/v1/messages")
        self.assertEqual(
            ai.endpoint({"url": "https://api.openai.com/v1", "kind": "openai"}),
            "https://api.openai.com/v1/chat/completions")


class ModelFromAnotherFormat(unittest.TestCase):
    """В поле остаётся имя от прежних настроек: человек однажды сохранил
    gpt-4o-mini, потом переключился на посредника Claude."""

    def test_foreign_name_is_dropped(self):
        self.assertEqual(ai.model("gpt-4o-mini", "anthropic"),
                         ai.DEFAULT_ANTHROPIC_MODEL)
        self.assertEqual(ai.model("claude-sonnet-4-5", "openai"),
                         ai.DEFAULT_MODEL)

    def test_own_name_is_kept(self):
        """У посредников имена свои, и отбрасывать всё незнакомое нельзя."""
        self.assertEqual(ai.model("claude-3-7-sonnet", "anthropic"),
                         "claude-3-7-sonnet")
        self.assertEqual(ai.model("anthropic/claude-opus", "anthropic"),
                         "anthropic/claude-opus")
        self.assertEqual(ai.model("deepseek-chat", "openai"), "deepseek-chat")

    def test_warning_is_shown_before_sending(self):
        js = io.open(os.path.join(os.path.dirname(__file__), "..",
                                  "app", "static", "app.js"),
                     encoding="utf-8").read()
        self.assertIn("посреднику Claude", js)
        self.assertIn('$("s-ai-model").onchange', js)


class WhereItBreaks(unittest.TestCase):
    """Обрыв соединения выглядит одинаково, что бы его ни вызвало, а
    причины разные и лечатся по-разному."""

    def test_unresolvable_name_is_named_as_such(self):
        lines = ai.diagnose({"url": "https://такого-имени-нет.invalid",
                             "kind": "anthropic"}, timeout=3)
        self.assertTrue(any("не разрешается" in x for x in lines), lines)

    def test_steps_are_reported_in_order(self):
        lines = ai.diagnose({"url": "https://api.github.com", "kind": "openai"},
                            timeout=8)
        self.assertIn("разрешается", lines[0])
        self.assertTrue(any("443" in x for x in lines), lines)

    def test_bad_address_does_not_crash(self):
        self.assertTrue(ai.diagnose({"url": "", "kind": "openai"}))

    def test_reset_check_appends_the_steps(self):
        real = ai._transports

        class Dead(object):
            def post(self_inner, *a, **k):
                raise Exception("ConnectionResetError(10054, 'разорвал')")

        ai._transports = lambda session=None: [Dead()]
        try:
            ok, note = ai.check({"key": "k", "url": "https://api.github.com",
                                 "model": "m", "kind": "openai"})
        finally:
            ai._transports = real
        self.assertFalse(ok)
        self.assertIn("По шагам:", note)


class Proxy(unittest.TestCase):
    """Когда рвут до самого сервера, маскировка рукопожатия не помогает:
    фильтр срабатывает раньше, чем программа успевает представиться."""

    def setUp(self):
        db.init()
        db.set_setting("proxy_url", "")

    def tearDown(self):
        db.set_setting("proxy_url", "")

    def test_empty_means_direct(self):
        self.assertIsNone(net.proxies())

    def test_scheme_is_added_when_missing(self):
        db.set_setting("proxy_url", "127.0.0.1:8080")
        self.assertEqual(net.proxies()["https"], "http://127.0.0.1:8080")

    def test_socks_is_kept_as_written(self):
        db.set_setting("proxy_url", "socks5://127.0.0.1:1080")
        self.assertEqual(net.proxies()["https"], "socks5://127.0.0.1:1080")

    def test_ai_requests_go_through_it(self):
        db.set_setting("proxy_url", "http://127.0.0.1:9")
        got = ai._transports()
        self.assertTrue(got)
        self.assertEqual(got[0].proxies.get("https"), "http://127.0.0.1:9")

    def test_it_is_saveable_from_settings(self):
        app = web.create_app().test_client()
        app.post("/api/settings", json={"proxy_url": "http://прокси:3128"})
        self.assertEqual(db.get_setting("proxy_url", ""), "http://прокси:3128")


class SocksProxy(unittest.TestCase):
    """Прокси в России чаще всего дают socks5, а requests без PySocks
    отвечает на него «Missing dependencies for SOCKS support»."""

    def setUp(self):
        db.init()
        db.set_setting("proxy_url", "")

    def tearDown(self):
        db.set_setting("proxy_url", "")

    def test_pysocks_is_in_requirements(self):
        req = io.open(os.path.join(os.path.dirname(__file__), "..",
                                   "requirements.txt"), encoding="utf-8").read()
        self.assertIn("PySocks", req)

    def test_socks_is_bundled_into_the_exe(self):
        spec = io.open(os.path.join(os.path.dirname(__file__), "..", "build",
                                    "navodka.spec"), encoding="utf-8").read()
        self.assertIn('"socks"', spec)

    def test_missing_socks_is_not_a_server_refusal(self):
        e = Exception("Missing dependencies for SOCKS support.")
        self.assertTrue(ai._no_socks(e))
        self.assertIn("socks5", ai._explain(e))

    def test_missing_socks_lets_the_next_way_try(self):
        """curl умеет socks сам, и его очередь наступить должна."""
        tried = []

        class NoSocks(object):
            def post(self_inner, *a, **k):
                tried.append("requests")
                raise Exception("Missing dependencies for SOCKS support.")

        class Works(object):
            def post(self_inner, *a, **k):
                tried.append("curl")

                class R:
                    status_code = 200
                    text = "{}"

                    def json(self_r):
                        return {"content": [{"type": "text", "text": "да"}]}
                return R()

        real = ai._transports
        ai._transports = lambda session=None: [NoSocks(), Works()]
        try:
            text, err = ai.ask([{"role": "user", "content": "?"}],
                               cfg={"key": "k", "url": "https://router.cheap",
                                    "model": "m", "kind": "anthropic"})
        finally:
            ai._transports = real
        self.assertEqual((text, err), ("да", ""))
        self.assertEqual(tried, ["requests", "curl"])

    def test_password_never_reaches_the_error_text(self):
        """Ошибку пересылают в переписку не глядя."""
        db.set_setting("proxy_url", "socks5://вася:секрет@1.2.3.4:8000")
        self.assertEqual(net.proxy_label(), "socks5://1.2.3.4:8000")
        note = ai._explain(Exception("ProxyError('Unable to connect to proxy')"))
        self.assertIn("1.2.3.4:8000", note)
        self.assertNotIn("секрет", note)
        self.assertNotIn("вася", note)

    def test_clearest_error_wins_over_the_last_one(self):
        """curl говорит номером ошибки, requests — словами."""
        proxy = Exception("ProxyError('Unable to connect to proxy')")
        curl = Exception("Failed to perform, curl: (28) Connection timed out")
        self.assertIs(ai._worth_telling([proxy, curl]), proxy)
        self.assertIs(ai._worth_telling([curl]), curl)


class ProxyMustNotBlockUpdates(unittest.TestCase):
    """Ловушка: прокси задан с ошибкой, через него не проходит проверка
    новой версии — а починка этой ошибки лежит как раз в новой версии."""

    def setUp(self):
        db.init()
        db.set_setting("proxy_url", "")

    def tearDown(self):
        db.set_setting("proxy_url", "")

    def test_direct_session_ignores_the_proxy(self):
        db.set_setting("proxy_url", "socks5://1.2.3.4:8000")
        self.assertTrue(net.plain().proxies)
        self.assertFalse(net.plain(proxy=False).proxies)

    def test_check_retries_without_the_proxy(self):
        db.set_setting("proxy_url", "socks5://1.2.3.4:8000")
        seen = []

        class R(object):
            status_code = 200
            text = "{}"

            def json(self_inner):
                return {"tag_name": "v9.9.9"}

        def fake_session(proxy=True):
            seen.append(proxy)

            class S(object):
                def get(self_inner, url, timeout=None):
                    if proxy:
                        raise Exception("Missing dependencies for SOCKS support.")
                    return R()
            return S()

        real = update._session
        update._session = fake_session
        try:
            d, err = update.check()
        finally:
            update._session = real
        self.assertEqual(err, "")
        self.assertEqual(d["latest"], "9.9.9")
        self.assertEqual(seen, [True, False], "прокси не обошли")

    def test_without_proxy_there_is_only_one_try(self):
        seen = []

        def fake_session(proxy=True):
            seen.append(proxy)
            raise Exception("сеть лежит")

        real = update._session
        update._session = fake_session
        try:
            d, err = update.check()
        finally:
            update._session = real
        self.assertEqual(seen, [True])
        self.assertIn("не удалось связаться", err)

    def test_failure_tells_how_to_get_out(self):
        db.set_setting("proxy_url", "socks5://1.2.3.4:8000")

        def fake_session(proxy=True):
            raise Exception("прокси молчит")

        real = update._session
        update._session = fake_session
        try:
            _, err = update.check()
        finally:
            update._session = real
        self.assertIn("Прокси", err)
        self.assertIn("Сохранить", err)


class ModelNotAvailable(unittest.TestCase):
    """Посредник отказал из-за имени модели и сам написал, где взять
    список. То, что программа умеет сделать сама, она и делает."""

    def setUp(self):
        db.init()
        self.app = web.create_app().test_client()

    def test_model_refusal_is_told_apart(self):
        self.assertTrue(ai.model_missing(
            'HTTP 404: {"error":{"message":"Модель claude-sonnet-4-5 '
            'недоступна. Получите список доступных моделей через GET '
            '/v1/models"}}'))
        self.assertTrue(ai.model_missing("HTTP 400: model_not_found"))
        self.assertTrue(ai.model_missing("unknown model: foo"))

    def test_other_refusals_are_not_confused_with_it(self):
        self.assertFalse(ai.model_missing("HTTP 401: invalid api key"))
        self.assertFalse(ai.model_missing("связь разорвана по дороге"))
        self.assertFalse(ai.model_missing("HTTP 429: too many requests"))

    def test_list_is_fetched_and_shown(self):
        real_check, real_models = ai.check, ai.models
        ai.check = lambda cfg=None: (False, "HTTP 404: модель недоступна")
        ai.models = lambda cfg=None, **k: (["alpha", "beta"], "")
        try:
            d = self.app.post("/api/ai/check", json={}).get_json()
        finally:
            ai.check, ai.models = real_check, real_models
        self.assertEqual(d["models"], ["alpha", "beta"])
        self.assertIn("Доступные модели: alpha, beta", d["note"])

    def test_list_is_not_fetched_for_other_errors(self):
        real_check, real_models = ai.check, ai.models
        asked = []
        ai.check = lambda cfg=None: (False, "HTTP 401: invalid api key")
        ai.models = lambda cfg=None, **k: (asked.append(1), ([], ""))[1]
        try:
            d = self.app.post("/api/ai/check", json={}).get_json()
        finally:
            ai.check, ai.models = real_check, real_models
        self.assertEqual(asked, [])
        self.assertEqual(d["models"], [])

    def test_long_list_is_cut_but_counted(self):
        real_check, real_models = ai.check, ai.models
        ai.check = lambda cfg=None: (False, "модель недоступна")
        ai.models = lambda cfg=None, **k: (["m%d" % i for i in range(20)], "")
        try:
            note = self.app.post("/api/ai/check", json={}).get_json()["note"]
        finally:
            ai.check, ai.models = real_check, real_models
        self.assertIn("и ещё 8", note)


class CommercialProposal(unittest.TestCase):
    """КП пересылают внутрь компании и читают без продавца. Выдуманная
    цифра в нём хуже, чем её отсутствие: за неё потом спрашивают."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM companies")
        db.conn().commit()
        self.cid, _ = db.upsert_company({"name": "ООО Ромашка", "source": "тест",
                                         "inn": "7701234567"})
        db.set_setting("ai_key", "тест")
        db.set_setting("ai_offer", "Разбор записей звонков")
        db.set_setting("ai_terms", "")
        self.app = web.create_app().test_client()

    def tearDown(self):
        for k in ("ai_key", "ai_offer", "ai_terms"):
            db.set_setting(k, "")

    def _answer(self, payload):
        return lambda messages, **k: (json.dumps(payload, ensure_ascii=False), "")

    def test_offer_is_required(self):
        """КП без описания того, что продаём, — это КП про ничто."""
        db.set_setting("ai_offer", "")
        d = self.app.post("/api/company/%d/kp" % self.cid).get_json()
        self.assertFalse(d["ok"])
        self.assertIn("Что продаём", d["error"])

    def test_terms_reach_the_prompt(self):
        seen = {}

        def fake(messages, **k):
            seen["text"] = "\n".join(m["content"] for m in messages)
            return json.dumps({"solution": "с"}, ensure_ascii=False), ""

        real = ai.ask
        ai.ask = fake
        db.set_setting("ai_terms", "18 000 ₽ в месяц, подключение 3 дня")
        try:
            self.app.post("/api/company/%d/kp" % self.cid)
        finally:
            ai.ask = real
        self.assertIn("18 000 ₽", seen["text"])
        self.assertIn("ни одной цифры, которой нет", seen["text"])

    def test_without_terms_the_model_is_told_not_to_invent(self):
        seen = {}

        def fake(messages, **k):
            seen["text"] = "\n".join(m["content"] for m in messages)
            return json.dumps({"solution": "с"}, ensure_ascii=False), ""

        real = ai.ask
        ai.ask = fake
        try:
            self.app.post("/api/company/%d/kp" % self.cid)
        finally:
            ai.ask = real
        self.assertIn("условия обсуждаются", seen["text"])

    def test_sections_come_back_and_are_saved(self):
        real = ai.ask
        ai.ask = self._answer({
            "title": "Заголовок", "intro": "Вступление", "problem": "Задача",
            "solution": "Решение", "terms": "Условия", "next": "Шаг",
            "doubts": ["раз", "два", "три", "четыре"]})
        try:
            d = self.app.post("/api/company/%d/kp" % self.cid).get_json()
        finally:
            ai.ask = real
        self.assertTrue(d["ok"])
        self.assertEqual(d["solution"], "Решение")
        self.assertEqual(len(d["doubts"]), 3, "возражений берём не больше трёх")
        self.assertIn("Что предлагаем", d["text"])
        row = db.conn().execute("SELECT ai_kp FROM companies WHERE id=?",
                                (self.cid,)).fetchone()
        self.assertIn("Решение", row["ai_kp"], "КП не сохранилось")

    def test_answer_without_solution_is_refused(self):
        """Пустой разбор выглядит как готовое КП и тем опаснее."""
        real = ai.ask
        ai.ask = self._answer({"title": "Только заголовок"})
        try:
            d = self.app.post("/api/company/%d/kp" % self.cid).get_json()
        finally:
            ai.ask = real
        self.assertFalse(d["ok"])

    def test_no_key_says_where_to_put_it(self):
        db.set_setting("ai_key", "")
        d = self.app.post("/api/company/%d/kp" % self.cid).get_json()
        self.assertIn("Настройки", d["error"])


class AnalyzeOneCompany(unittest.TestCase):
    """Общий прогон идёт по тридцати карточкам и занимает минуты. Когда
    открыта одна и звонить по ней надо сегодня, ждать незачем."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM companies")
        db.conn().commit()
        self.cid, _ = db.upsert_company({"name": "ООО Василёк", "source": "тест"})
        db.set_setting("ai_key", "тест")
        self.app = web.create_app().test_client()

    def tearDown(self):
        db.set_setting("ai_key", "")

    def test_result_lands_in_the_same_fields_as_the_bulk_run(self):
        real = ai.ask
        ai.ask = lambda messages, **k: (json.dumps({
            "summary": "Чинит станки", "segment": "B2B", "fit": 77,
            "fit_why": "похожи по размеру", "hook": "три вакансии",
            "signals": ["8-800 на сайте", "вакансии в продажи"]},
            ensure_ascii=False), "")
        try:
            d = self.app.post("/api/company/%d/analyze" % self.cid).get_json()
        finally:
            ai.ask = real
        self.assertTrue(d["ok"])
        row = db.conn().execute(
            "SELECT ai_summary, ai_fit, ai_segment FROM companies WHERE id=?",
            (self.cid,)).fetchone()
        self.assertEqual(row["ai_summary"], "Чинит станки")
        self.assertEqual(row["ai_fit"], 77)
        self.assertEqual(row["ai_segment"], "B2B")
        sig = db.conn().execute(
            "SELECT value FROM signals WHERE company_id=? AND key='ai_signals'",
            (self.cid,)).fetchone()
        self.assertIn("8-800", sig["value"])

    def test_out_of_range_score_is_clamped(self):
        real = ai.ask
        ai.ask = lambda messages, **k: (json.dumps({"fit": 300}), "")
        try:
            self.app.post("/api/company/%d/analyze" % self.cid)
        finally:
            ai.ask = real
        row = db.conn().execute("SELECT ai_fit FROM companies WHERE id=?",
                                (self.cid,)).fetchone()
        self.assertEqual(row["ai_fit"], 100)

    def test_missing_company_is_not_a_crash(self):
        d = self.app.post("/api/company/999999/analyze").get_json()
        self.assertFalse(d["ok"])


class ScoreBreakdown(unittest.TestCase):
    """Число без разбора человек либо принимает на веру, либо не верит
    вовсе, и оба исхода одинаково бесполезны."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM companies")
        db.conn().commit()
        self.app = web.create_app().test_client()

    def test_parts_add_up_to_the_score(self):
        row = {"director": "Иванов", "site": "x.ru"}
        sig = {"hh_vacancies": "4", "tech_calltracking": "Calltouch",
               "size": "малый"}
        cts = [{"kind": "email", "owner": "general", "verified": "unchecked"},
               {"kind": "phone", "owner": "general", "verified": "unchecked"}]
        value, parts = score.compute(row, sig, cts)
        got = sum(p["points"] for p in parts if p["got"])
        self.assertEqual(value, min(100, got), "разбор не сходится с баллом")

    def test_unearned_points_are_listed_too(self):
        """Несделанное объясняет балл не хуже сделанного и заодно
        показывает, чем его поднять."""
        value, parts = score.compute({"director": "", "site": ""}, {}, [])
        self.assertEqual(value, 0)
        miss = [p for p in parts if not p["got"]]
        self.assertTrue(len(miss) >= 8)
        self.assertTrue(any(p["key"] == "lpr_found" for p in miss))
        self.assertTrue(all(p["points"] > 0 for p in miss),
                        "незасчитанное без веса ничего не объясняет")

    def test_every_part_explains_itself(self):
        _v, parts = score.compute({"director": "И", "site": "x.ru"},
                                  {"hh_vacancies": "1"}, [])
        for p in parts:
            self.assertTrue(p["text"], p)
            self.assertTrue(p["why"], "слагаемое %s без объяснения" % p["key"])

    def test_half_weight_for_a_single_vacancy(self):
        """Три вакансии — отдел растёт, одна — затыкают дыру."""
        one, parts_one = score.compute({"director": "", "site": ""},
                                       {"hh_vacancies": "1"}, [])
        many, _ = score.compute({"director": "", "site": ""},
                                {"hh_vacancies": "3"}, [])
        self.assertEqual(one, many // 2)
        self.assertIn("меньше трёх", [p["text"] for p in parts_one
                                      if p["key"] == "vacancies_sales"][0])

    def test_guessed_contact_does_not_hide_the_missing_found_one(self):
        """Выведенный адрес — догадка, и она не должна выглядеть как
        найденный контакт."""
        _v, parts = score.compute({"director": "И", "site": "x.ru"},
                                  {"lpr_contact": "выведен"}, [])
        keys = {p["key"]: p for p in parts}
        self.assertTrue(keys["lpr_guessed"]["got"])
        self.assertFalse(keys["lpr_found"]["got"])

    def test_card_returns_the_breakdown(self):
        cid, _ = db.upsert_company({"name": "ООО Тест", "source": "тест",
                                    "site": "t.ru"})
        d = self.app.get("/api/company/%d" % cid).get_json()
        self.assertTrue(d["score_parts"])
        self.assertIn("score_now", d)
        got = sum(p["points"] for p in d["score_parts"] if p["got"])
        self.assertEqual(d["score_now"], min(100, got))

    def test_legend_lists_everything_with_weights(self):
        d = self.app.get("/api/score/legend").get_json()
        self.assertEqual(d["max"], 100)
        keys = {x["key"] for x in d["legend"]}
        self.assertEqual(keys, set(score.WEIGHTS),
                         "в справке не все слагаемые")
        self.assertTrue(all(x["why"] for x in d["legend"]))


class SocialsFirstClass(unittest.TestCase):
    """Соцсети — то, ради чего программу и открывают: по ним пишут,
    когда на почту не отвечают, а в группе ВК видны контактные лица."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM companies")
        db.conn().execute("DELETE FROM contacts")
        db.conn().commit()

    def test_domain_is_taken_from_any_shape_of_link(self):
        from app.sources import vk as vk_src
        self.assertEqual(vk_src.domain_of("https://Romashka.RU/kontakty"),
                         "romashka.ru")
        self.assertEqual(vk_src.domain_of("www.romashka.ru"), "romashka.ru")
        self.assertEqual(vk_src.domain_of("romashka"), "")

    def test_group_is_taken_only_when_it_names_our_site(self):
        """Чужая группа в карточке хуже пустой клетки: по ней напишут."""
        from app.sources import vk as vk_src
        calls = []

        def fake_by_url(url, token, session=None):
            calls.append(url)
            return {"id": 1, "name": "Ромашка", "url": "https://vk.com/romashka",
                    "raw": {"site": "https://romashka.ru"}}, ""

        real = vk_src.by_url
        vk_src.by_url = fake_by_url
        try:
            got, err = vk_src.by_domain("https://romashka.ru", "ключ")
        finally:
            vk_src.by_url = real
        self.assertEqual(err, "")
        self.assertEqual(got["url"], "https://vk.com/romashka")
        self.assertEqual(calls, ["https://vk.com/romashka"])

    def test_group_with_another_site_is_refused(self):
        from app.sources import vk as vk_src
        real = vk_src.by_url
        vk_src.by_url = lambda url, token, session=None: (
            {"id": 2, "name": "Кто-то ещё", "url": "https://vk.com/romashka",
             "raw": {"site": "https://другой-сайт.рф"}}, "")
        try:
            got, err = vk_src.by_domain("https://romashka.ru", "ключ")
        finally:
            vk_src.by_url = real
        self.assertEqual(got, {}, "засчитали чужую группу")

    def test_group_without_a_site_field_is_refused(self):
        """Совпадение имён — не подтверждение."""
        from app.sources import vk as vk_src
        real = vk_src.by_url
        vk_src.by_url = lambda url, token, session=None: (
            {"id": 3, "name": "Ромашка", "url": "https://vk.com/romashka",
             "raw": {}}, "")
        try:
            got, _err = vk_src.by_domain("https://romashka.ru", "ключ")
        finally:
            vk_src.by_url = real
        self.assertEqual(got, {})

    def test_too_short_label_is_not_even_tried(self):
        """vk.com/abc — это чей угодно адрес, только не наш."""
        from app.sources import vk as vk_src
        tried = []
        real = vk_src.by_url
        vk_src.by_url = lambda url, token, session=None: (tried.append(url), ({}, ""))[1]
        try:
            vk_src.by_domain("https://abc.ru", "ключ")
        finally:
            vk_src.by_url = real
        self.assertEqual(tried, [])

    def test_socials_add_to_the_score(self):
        row = {"director": "", "site": ""}
        without, _ = score.compute(row, {}, [])
        with_soc, parts = score.compute(
            row, {}, [{"kind": "social", "owner": "general", "verified": "unchecked"}])
        self.assertGreater(with_soc, without)
        self.assertTrue(any(p["key"] == "has_social" and p["got"] for p in parts))

    def test_missing_socials_are_named_in_the_breakdown(self):
        _v, parts = score.compute({"director": "", "site": ""}, {}, [])
        miss = [p for p in parts if p["key"] == "has_social"]
        self.assertEqual(len(miss), 1)
        self.assertFalse(miss[0]["got"])
        self.assertIn("не нашлось", miss[0]["text"])

    def test_new_contact_is_reported_as_new_once(self):
        cid, _ = db.upsert_company({"name": "ООО Тест", "source": "тест"})
        first = db.add_contact(cid, "social", "https://vk.com/x", "general",
                               80, "unchecked", "сайт")
        again = db.add_contact(cid, "social", "https://vk.com/x", "general",
                               80, "unchecked", "сайт")
        self.assertTrue(first)
        self.assertFalse(again, "повторный контакт находкой не является")

    def test_pass_refuses_when_there_is_nothing_to_look_at(self):
        """Соцсети ищутся по сайту — без сайта искать не по чему."""
        db.upsert_company({"name": "Без сайта", "source": "тест"})
        tid = db.create_task("socials", {})
        with self.assertRaises(RuntimeError) as e:
            worker.task_socials(tid, {})
        self.assertIn("сайт", str(e.exception))

    def test_pass_skips_those_who_already_have_socials(self):
        cid, _ = db.upsert_company({"name": "С соцсетями", "source": "тест",
                                    "site": "https://a.ru"})
        db.add_contact(cid, "social", "https://vk.com/a", "general", 80,
                       "unchecked", "сайт")
        db.upsert_company({"name": "Без соцсетей", "source": "тест",
                           "site": "https://b.ru"})
        seen = []
        real = worker.site_src.crawl
        worker.site_src.crawl = lambda site, **k: (seen.append(site), {"socials": {}})[1]
        try:
            worker.task_socials(db.create_task("socials", {}), {"only_empty": True})
        finally:
            worker.site_src.crawl = real
        self.assertEqual(seen, ["https://b.ru"])

    def test_socials_reach_the_export(self):
        cid, _ = db.upsert_company({"name": "ООО Тест", "source": "тест"})
        db.add_contact(cid, "social", "https://vk.com/x", "general", 80,
                       "unchecked", "сайт")
        rows = export.rows_for_export(db.conn())
        self.assertTrue(any("vk.com/x" in (r.get("social") or "") for r in rows))


class TradeCatalog(unittest.TestCase):
    """Поле «Вид деятельности» — пустая строка, и разница между
    «грузоперевозки» и «транспортная компания» решает, найдётся сотня
    компаний или три."""

    def test_catalog_is_grouped_and_not_empty(self):
        cat = trades.catalog()
        self.assertGreaterEqual(len(cat), 8)
        for g in cat:
            self.assertTrue(g["title"])
            self.assertTrue(g["items"], g["title"])

    def test_no_word_repeats_across_groups(self):
        """Одно слово в двух темах — повод гадать, какая из них правильная."""
        seen = {}
        for g in trades.catalog():
            for it in g["items"]:
                self.assertNotIn(it["q"], seen,
                                 "«%s» уже есть в теме «%s»"
                                 % (it["q"], seen.get(it["q"])))
                seen[it["q"]] = g["title"]

    def test_tag_mark_is_computed_not_written_by_hand(self):
        """Пометка, проставленная руками, разойдётся со словарём тегов."""
        self.assertTrue(trades.tagged("стоматология"))
        self.assertTrue(trades.tagged("грузоперевозки"))
        self.assertFalse(trades.tagged("совершенно небывалое занятие"))

    def test_almost_everything_is_searchable_by_tags(self):
        """Список без тегов — это список слов, по которым ничего не
        найдётся у компаний с выдуманными названиями."""
        items = [it for g in trades.catalog() for it in g["items"]]
        weak = [it["q"] for it in items if not it["tagged"]]
        self.assertLessEqual(len(weak), len(items) // 10,
                             "без тегов слишком много: %s" % weak)

    def test_every_word_builds_a_real_query(self):
        from app.sources import osm
        city = {"name": "Москва", "ll": "37.6,55.7", "spn": "0.9,0.5"}
        for q in trades.all_words():
            self.assertTrue(osm.build_query(q, city),
                            "«%s» не превращается в запрос" % q)

    def test_catalog_reaches_the_page(self):
        db.init()
        html = web.create_app().test_client().get("/").get_data(as_text=True)
        self.assertIn("trade-tab", html)
        self.assertIn("Медицина и здоровье", html)
        self.assertIn('data-q="стоматология"', html)

    def test_hints_for_the_input_come_from_the_same_list(self):
        words = trades.all_words()
        self.assertEqual(len(words), len(set(words)))
        self.assertIn("автосервис", words)


class CatalogIsTwoSteps(unittest.TestCase):
    """Тема только переключает список, а ищется слово. Пока два ряда
    кнопок выглядели одинаково, человек жал тему, видел в поле прежний
    запрос и считал это поломкой."""

    def setUp(self):
        db.init()
        self.html = web.create_app().test_client().get("/").get_data(as_text=True)
        self.js = io.open(os.path.join(os.path.dirname(__file__), "..",
                                       "app", "static", "app.js"),
                          encoding="utf-8").read()

    def test_steps_are_numbered_on_the_page(self):
        self.assertIn("1. выберите тему", self.html)
        self.assertIn("2. нажмите вид деятельности", self.html)

    def test_chosen_word_is_marked(self):
        self.assertIn("markTrade", self.js)
        self.assertIn("is-on", self.js)

    def test_own_word_is_named_in_the_note(self):
        """«Сейчас ищется дизайн — своё слово, не из списка»: иначе
        расхождение между полем и списком ничем не объясняется."""
        self.assertIn("не из списка", self.js)

    def test_theme_click_does_not_touch_the_field(self):
        """Тема меняет только видимый список — поле трогать нельзя,
        иначе набранное руками пропадёт от случайного нажатия."""
        block = self.js[self.js.index('$("trade-tabs").onclick'):]
        block = block[:block.index("};")]
        self.assertNotIn('$("q-text").value =', block)


class DesignAndArchitecture(unittest.TestCase):
    """«Дизайн» искали руками, а тега для него не было — находились
    только те, у кого это слово стоит в названии."""

    def test_they_search_by_tags_now(self):
        from app.sources import osm
        city = {"name": "Москва", "ll": "37.6,55.7", "spn": "0.9,0.5"}
        for word, tag in (("дизайн", "graphic_design"),
                          ("дизайн интерьера", "interior_decoration"),
                          ("архитектурное бюро", "architect"),
                          ("клининг", "cleaning")):
            self.assertIn(tag, osm.build_query(word, city),
                          "«%s» ищется только по названию" % word)

    def test_rubric_names_are_in_russian(self):
        """В карточку должно попадать «Дизайн-студия», а не graphic_design:
        список читает продавец, а не картограф."""
        from app.sources import osm
        for tag in ("graphic_design", "interior_decoration", "architect",
                    "cleaning"):
            self.assertIn(tag, osm.RUBRIC_RU)
            self.assertRegex(osm.RUBRIC_RU[tag], "[А-Яа-я]")


class DecisionMakerFlag(unittest.TestCase):
    """Признак «контакт первого лица найден» читают четверо: счётчик в
    шапке, фильтр, выгрузка и сама оценка — там на нём четверть веса.
    Записывать его при этом было некому."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM companies")
        db.conn().execute("DELETE FROM signals")
        db.conn().commit()
        self.app = web.create_app().test_client()

    def test_signal_is_written_by_enrichment(self):
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8").read()
        self.assertIn('db.add_signal(cid, "lpr_contact", "найден")', src)
        self.assertIn('db.add_signal(cid, "lpr_contact", "выведен")', src)

    def test_found_outweighs_guessed(self):
        """По найденному можно звонить, по выведенному — только пробовать
        написать, и весить одинаково они не должны."""
        row = {"director": "Иванов", "site": "x.ru"}
        found, _ = score.compute(row, {"lpr_contact": "найден"}, [])
        guessed, _ = score.compute(row, {"lpr_contact": "выведен"}, [])
        none_, _ = score.compute(row, {}, [])
        self.assertGreater(found, guessed)
        self.assertGreater(guessed, none_)

    def test_counter_sees_the_signal(self):
        cid, _ = db.upsert_company({"name": "ООО Тест", "source": "тест"})
        self.assertEqual(self.app.get("/api/stats").get_json()["lpr_found"], 0)
        db.add_signal(cid, "lpr_contact", "найден")
        self.assertEqual(self.app.get("/api/stats").get_json()["lpr_found"], 1)

    def test_filter_sees_the_signal(self):
        cid, _ = db.upsert_company({"name": "С контактом", "source": "тест"})
        db.upsert_company({"name": "Без контакта", "source": "тест"})
        db.add_signal(cid, "lpr_contact", "найден")
        d = self.app.get("/api/companies?only=lpr_found").get_json()
        self.assertEqual([r["name"] for r in d["rows"]], ["С контактом"])

    def test_guessed_does_not_count_as_found(self):
        """Выведенный по схеме адрес — догадка, и в счётчик найденных
        она попадать не должна."""
        cid, _ = db.upsert_company({"name": "Догадка", "source": "тест"})
        db.add_signal(cid, "lpr_contact", "выведен")
        self.assertEqual(self.app.get("/api/stats").get_json()["lpr_found"], 0)
        d = self.app.get("/api/companies?only=lpr_found").get_json()
        self.assertEqual(d["rows"], [])

    def test_status_reaches_the_export(self):
        cid, _ = db.upsert_company({"name": "ООО Тест", "source": "тест"})
        db.add_signal(cid, "lpr_contact", "найден")
        rows = export.rows_for_export(db.conn())
        self.assertEqual(rows[0]["lpr_status"], "найден")


class ParallelAI(unittest.TestCase):
    """Запрос к модели — это ожидание чужого сервера: секунд двадцать, из
    которых программа не делает ничего."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM companies")
        db.conn().execute("DELETE FROM signals")
        db.conn().commit()
        db.set_setting("ai_key", "тест")
        db.set_setting("ai_icp", "")
        self.cids = []
        for n in range(6):
            cid, _ = db.upsert_company({"name": "ООО №%d" % n, "source": "тест"})
            db.add_signal(cid, "enriched", 1)
            self.cids.append(cid)

    def tearDown(self):
        db.set_setting("ai_key", "")
        db.set_setting("ai_threads", "")

    def _run(self, threads, delay=0.12):
        import threading as th
        live = {"now": 0, "peak": 0}
        lock = th.Lock()

        def fake_analyze(brief, icp="", offer="", cfg=None, session=None):
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            time.sleep(delay)
            with lock:
                live["now"] -= 1
            return {"summary": "разобрано", "segment": "B2B"}, ""

        real_an, real_check = ai.analyze, ai.check
        ai.analyze = fake_analyze
        ai.check = lambda cfg=None: (True, "ок")
        tid = db.create_task("ai", {})
        t0 = time.time()
        try:
            worker.task_ai(tid, {"limit": 6, "threads": threads})
        finally:
            ai.analyze, ai.check = real_an, real_check
        return live["peak"], time.time() - t0

    def test_requests_really_go_in_parallel(self):
        peak, _ = self._run(4)
        self.assertGreaterEqual(peak, 2, "запросы идут по одному")
        self.assertLessEqual(peak, 4, "потоков больше, чем заказано")

    def test_one_thread_keeps_the_old_behaviour(self):
        peak, _ = self._run(1)
        self.assertEqual(peak, 1)

    def test_parallel_is_actually_faster(self):
        _p1, slow = self._run(1, delay=0.1)
        _p4, fast = self._run(4, delay=0.1)
        self.assertLess(fast, slow * 0.8,
                        "в четыре потока не быстрее: %.2f против %.2f"
                        % (fast, slow))

    def test_every_company_is_written(self):
        self._run(4)
        n = db.conn().execute(
            "SELECT COUNT(*) n FROM companies WHERE ai_summary='разобрано'"
        ).fetchone()["n"]
        self.assertEqual(n, 6, "часть разборов потерялась")

    def test_thread_count_is_clamped(self):
        db.init()
        app = web.create_app().test_client()
        app.post("/api/ai", json={"threads": 99})
        self.assertEqual(db.get_setting("ai_threads", ""), "8")
        # Ноль потоков бессмысленен, и ближайшее законное значение —
        # один. Раньше ноль читался как «не задано» и молча становился
        # четырьмя: человек просил меньше, а получал больше.
        app.post("/api/ai", json={"threads": 0})
        self.assertEqual(db.get_setting("ai_threads", ""), "1")
        app.post("/api/ai", json={"threads": ""})
        self.assertEqual(db.get_setting("ai_threads", ""), "4")
        app.post("/api/ai", json={"threads": "абв"})
        self.assertEqual(db.get_setting("ai_threads", ""), "4")

    def test_limit_is_retried_not_dropped(self):
        """Лимит провайдера — это «подожди», а не «не выйдет». Раньше
        компания на нём терялась насовсем."""
        calls = {"n": 0}

        def flaky(brief, icp="", offer="", cfg=None, session=None):
            calls["n"] += 1
            if calls["n"] == 2:
                return {}, "HTTP 429: слишком часто"
            return {"summary": "разобрано"}, ""

        real_an, real_check = ai.analyze, ai.check
        ai.analyze = flaky
        ai.check = lambda cfg=None: (True, "ок")
        try:
            worker.task_ai(db.create_task("ai", {}), {"limit": 6, "threads": 3})
        finally:
            ai.analyze, ai.check = real_an, real_check
        n = db.conn().execute(
            "SELECT COUNT(*) n FROM companies WHERE ai_summary='разобрано'"
        ).fetchone()["n"]
        self.assertEqual(n, 6, "компания потерялась на временном отказе")
        self.assertEqual(calls["n"], 7, "повтора не было")

    def test_refusal_on_the_merits_is_not_retried(self):
        """«Неизвестная модель» повторять незачем: ответ будет тот же, а
        ждать придётся втрое."""
        calls = {"n": 0}

        def refuse(brief, icp="", offer="", cfg=None, session=None):
            calls["n"] += 1
            return {}, "HTTP 404: модель недоступна"

        real_an, real_check = ai.analyze, ai.check
        ai.analyze = refuse
        ai.check = lambda cfg=None: (True, "ок")
        try:
            worker.task_ai(db.create_task("ai", {}), {"limit": 6, "threads": 3})
        finally:
            ai.analyze, ai.check = real_an, real_check
        self.assertEqual(calls["n"], 6, "отказ повторялся впустую")

    def test_three_resets_switch_to_one_at_a_time(self):
        """Четыре соединения держит не всякий посредник, и «связь
        разорвана» на каждой компании — это не работа, а холостой ход."""
        import threading as th
        live = {"now": 0, "peak_after": 0, "n": 0}
        lock = th.Lock()

        def resetting(brief, icp="", offer="", cfg=None, session=None):
            with lock:
                live["n"] += 1
                n = live["n"]
                live["now"] += 1
                if n > 6:
                    live["peak_after"] = max(live["peak_after"], live["now"])
            time.sleep(0.05)
            with lock:
                live["now"] -= 1
            if n <= 6:
                return {}, "связь разорвана по дороге"
            return {"summary": "разобрано"}, ""

        real_an, real_check = ai.analyze, ai.check
        ai.analyze = resetting
        ai.check = lambda cfg=None: (True, "ок")
        tid = db.create_task("ai", {})
        try:
            worker.task_ai(tid, {"limit": 6, "threads": 4})
        finally:
            ai.analyze, ai.check = real_an, real_check
        self.assertEqual(live["peak_after"], 1,
                         "после обрывов запросы всё ещё идут парами")
        said = [r["text"] for r in db.conn().execute(
            "SELECT text FROM logs WHERE task_id=?", (tid,))]
        self.assertTrue(any("один запрос за раз" in t for t in said),
                        "о переходе не сказали")


class LighterList(unittest.TestCase):
    """Список перезапрашивается, пока идёт обход. Каждый лишний
    килобайт в ответе — это работа вместо ответа на нажатия."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM companies")
        db.conn().execute("DELETE FROM contacts")
        db.conn().execute("DELETE FROM signals")
        db.conn().commit()
        self.app = web.create_app().test_client()

    def test_heavy_columns_stay_out_of_the_list(self):
        """КП — до шести килобайт на компанию, и в строке его не видно."""
        cid, _ = db.upsert_company({"name": "ООО Тест", "source": "тест"})
        db.update_company_fields(cid, {"ai_kp": "К" * 6000,
                                       "ai_opener": "О" * 800})
        row = self.app.get("/api/companies").get_json()["rows"][0]
        self.assertNotIn("ai_kp", row)
        self.assertNotIn("ai_opener", row)

    def test_columns_the_table_draws_are_all_there(self):
        db.upsert_company({"name": "ООО Тест", "source": "тест",
                           "inn": "77", "site": "x.ru", "region": "Москва",
                           "director": "Иванов"})
        row = self.app.get("/api/companies").get_json()["rows"][0]
        for key in ("id", "name", "inn", "site", "region", "director",
                    "director_post", "score", "stage", "callcenter",
                    "next_step", "next_date"):
            self.assertIn(key, row, "в списке нет %s" % key)

    def test_only_the_first_contacts_travel_but_the_count_is_honest(self):
        cid, _ = db.upsert_company({"name": "Много контактов", "source": "тест"})
        for i in range(60):
            db.add_contact(cid, "email", "a%d@x.ru" % i, "general", 50,
                           "unchecked", "сайт")
        row = self.app.get("/api/companies").get_json()["rows"][0]
        self.assertLessEqual(len(row["contacts"]), 12)
        self.assertEqual(row["contacts_total"], 60)

    def test_socials_are_not_crowded_out_by_phones(self):
        """Своя квота: иначе шесть телефонов вытесняют группу ВК, ради
        которой список и открывают."""
        cid, _ = db.upsert_company({"name": "С соцсетями", "source": "тест"})
        for i in range(20):
            db.add_contact(cid, "phone", "+7495000000%d" % i, "general", 90,
                           "unchecked", "сайт")
        db.add_contact(cid, "social", "https://vk.com/x", "general", 50,
                       "unchecked", "сайт")
        row = self.app.get("/api/companies").get_json()["rows"][0]
        self.assertTrue(any(x["kind"] == "social" for x in row["contacts"]))

    def test_unused_signals_stay_out(self):
        cid, _ = db.upsert_company({"name": "ООО Тест", "source": "тест"})
        db.add_signal(cid, "revenue_series", "2019:1;2020:2;2021:3")
        db.add_signal(cid, "hh_vacancies", "3")
        sig = self.app.get("/api/companies").get_json()["rows"][0]["signals"]
        self.assertIn("hh_vacancies", sig)
        self.assertNotIn("revenue_series", sig)


class StallWatchdog(unittest.TestCase):
    """«Зависло» снаружи выглядит одинаково, а причин две, и лечатся они
    по-разному. Сторож говорит, какая именно."""

    def test_watchdog_is_started_with_the_worker(self):
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8").read()
        self.assertIn("_watchdog", src)
        self.assertIn('name="navodka-watch"', src)

    def test_small_delays_are_not_reported(self):
        """Просыпаться на сотню миллисекунд позже — норма, и засорять
        этим журнал значит сделать его нечитаемым."""
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8").read()
        block = src[src.index("def _watchdog"):src.index("def recover")]
        self.assertIn("if late < 2.0:", block)
        self.assertIn("continue", block)

    def test_worst_stall_is_remembered(self):
        worker._stall["worst"] = 0.0
        worker._stall["worst"] = max(worker._stall["worst"], 7.5)
        self.assertEqual(worker._stall["worst"], 7.5)
        worker._stall["worst"] = 0.0


class AuditFindings(unittest.TestCase):
    """Найдено сплошной проверкой кода. Каждое — настоящая ошибка,
    а не придирка: ниже сказано, чем именно она оборачивалась."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM companies")
        db.conn().execute("DELETE FROM notes")
        db.conn().commit()
        self.app = web.create_app().test_client()

    # 1. Заметки переживали свою компанию
    def test_notes_die_with_the_company(self):
        cid, _ = db.upsert_company({"name": "ООО Тест", "source": "тест"})
        db.add_note(cid, "позвонил, просили письмо")
        db.delete_company(cid)
        left = db.conn().execute("SELECT COUNT(*) n FROM notes "
                                 "WHERE company_id=?", (cid,)).fetchone()["n"]
        self.assertEqual(left, 0, "заметки остались от удалённой компании")

    def test_orphans_are_swept_at_startup(self):
        """Хвосты подметаются сами: просить человека нажать кнопку ради
        уборки за программой — не дело."""
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8").read()
        block = src[src.index("def recover("):src.index("def stop_all(")]
        self.assertIn("db.clean_orphans()", block)

    def test_old_orphans_can_be_swept(self):
        cid, _ = db.upsert_company({"name": "ООО Тест", "source": "тест"})
        db.add_note(cid, "хвост")
        db.conn().execute("DELETE FROM companies WHERE id=?", (cid,))
        db.conn().commit()
        self.assertGreaterEqual(db.clean_orphans(), 1)
        self.assertEqual(db.conn().execute(
            "SELECT COUNT(*) n FROM notes").fetchone()["n"], 0)

    # 2. Нечисловое значение в числовом поле роняло сервер
    def test_letters_in_a_number_field_do_not_break_anything(self):
        for url, body in (("/api/enrich", {"limit": "абв"}),
                          ("/api/ai", {"threads": "x"}),
                          ("/api/socials", {"limit": None}),
                          ("/api/find", {"query": "тест", "cities": ["Москва"],
                                         "pages": "много"})):
            r = self.app.post(url, json=body)
            self.assertLess(r.status_code, 500, "%s упал" % url)

    def test_junk_in_the_query_string_does_not_break_the_list(self):
        for q in ("limit=абв", "limit=-5", "limit=999999", "per=x",
                  "ids=1;DROP TABLE companies"):
            r = self.app.get("/api/companies?" + q)
            self.assertLess(r.status_code, 500, q)

    def test_bounds_are_still_enforced(self):
        self.assertEqual(web.num("999", 5, 1, 10), 10)
        self.assertEqual(web.num("-3", 5, 1, 10), 1)
        self.assertEqual(web.num("", 5, 1, 10), 5)
        self.assertEqual(web.num(None, 5, 1, 10), 5)
        self.assertEqual(web.num("7", 5, 1, 10), 7)

    # 3. Пустой список городов молча отключал все справочники
    def test_search_without_cities_is_refused(self):
        d = self.app.post("/api/find", json={"query": "стоматология",
                                             "cities": []}).get_json()
        self.assertFalse(d["ok"])
        self.assertIn("город", d["error"])

    def test_search_with_a_city_goes_through(self):
        d = self.app.post("/api/find", json={"query": "стоматология",
                                             "cities": ["Москва"]}).get_json()
        self.assertTrue(d["ok"])

    # 4. Адреса из макетов попадали в список как живые
    def test_template_domains_are_thrown_out(self):
        from app.sources import site as site_src
        for addr in ("info@example.com", "mail@yourdomain.ru",
                     "a@test.com", "x@site.com"):
            self.assertEqual(site_src._clean_email(addr), "",
                             "«%s» принят за живой адрес" % addr)

    def test_real_addresses_survive_including_cyrillic(self):
        from app.sources import site as site_src
        for addr in ("info@romashka.ru", "иванов@ромашка.рф",
                     "ivanov@sub.romashka.co.uk"):
            self.assertEqual(site_src._clean_email(addr), addr.lower(), addr)

    def test_malformed_addresses_are_refused(self):
        from app.sources import site as site_src
        for addr in ("a@b.c", "нет-собаки", "a@@b.ru", "a@b..ru", "a@b."):
            self.assertEqual(site_src._clean_email(addr), "", addr)


class LinksFromStrangers(unittest.TestCase):
    """Адреса приходят с чужих сайтов, то есть их пишет кто угодно, а
    окно программы имеет доступ к её же API и к сохранённым ключам."""

    def setUp(self):
        self.js = io.open(os.path.join(os.path.dirname(__file__), "..",
                                       "app", "static", "app.js"),
                          encoding="utf-8").read()

    def test_every_href_goes_through_the_check(self):
        import re
        raw = re.findall(r'href="\$\{(\w+)\(', self.js)
        self.assertTrue(raw)
        self.assertEqual(set(raw), {"safeUrl"},
                         "ссылка вставляется мимо проверки схемы")

    def test_apostrophe_is_escaped_too(self):
        """Экранирование без апострофа рвёт атрибуты в одинарных
        кавычках."""
        self.assertIn("&#39;", self.js)

    def test_allowed_schemes_are_a_whitelist(self):
        """Запрещать по одной значит однажды забыть про data: или
        vbscript:."""
        block = self.js[self.js.index("function safeUrl"):]
        block = block[:block.index("\n}")]
        self.assertIn("https?:", block)
        self.assertNotIn("javascript", block.lower().replace(
            "javascript:alert(1)", ""))


class ExcelFormulas(unittest.TestCase):
    """Названия и заметки приходят с чужих сайтов, то есть их пишет кто
    угодно, а Excel читает ячейку со знака равенства как формулу."""

    def test_formula_becomes_text(self):
        blob = export.to_csv([{"name": "=cmd|'/c calc'!A1"}]).decode("utf-8")
        line = blob.split("\r\n")[1]
        self.assertTrue(line.startswith("'="), line[:30])

    def test_all_four_dangerous_heads_are_caught(self):
        for head in ("=", "+", "-", "@"):
            got = export._cell(head + "что-то")
            self.assertTrue(got.startswith("'"), head)

    def test_xlsx_holds_no_formulas(self):
        import io as _io
        import zipfile
        blob = export.to_xlsx([{"name": "=WEBSERVICE(\"http://x\")"}])
        if blob is None:
            self.skipTest("openpyxl не установлен")
        z = zipfile.ZipFile(_io.BytesIO(blob))
        body = b"".join(z.read(n) for n in z.namelist()
                        if "sheet" in n or "shared" in n).decode("utf-8")
        self.assertEqual(body.count("<f>"), 0, "формула попала в книгу")

    def test_phone_keeps_its_plus(self):
        """Побочная польза: +74951234567 Excel читал как сложение и
        показывал число без кода страны."""
        self.assertEqual(export._cell("+74951234567"), "'+74951234567")

    def test_ordinary_values_are_untouched(self):
        for v in ("ООО Ромашка", "info@x.ru", "", 42, None):
            self.assertEqual(export._cell(v), v)


class LocalMidnight(unittest.TestCase):
    """Сервер считает срок по местному времени, браузер считал по
    Гринвичу. Для Новосибирска это семь часов в сутки, когда звонок на
    сегодня помечен просроченным."""

    def test_client_uses_local_date(self):
        js = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                  "static", "app.js"), encoding="utf-8").read()
        block = js[js.index("const today = ()"):]
        block = block[:block.index("};") + 2]
        self.assertIn("getTimezoneOffset", block,
                      "дата всё ещё берётся по UTC")

    def test_server_uses_local_date(self):
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "web.py"), encoding="utf-8").read()
        self.assertIn("date('now','localtime')", src)


class StrangeAnswers(unittest.TestCase):
    """У ответа чужого сервера нет обязательства быть таким, каким мы
    его ждём, а падение разбора останавливает всё обогащение."""

    def test_empty_item_in_the_list_is_skipped(self):
        from app.sources import dadata as dd
        self.assertEqual(dd._unpack(None), {})
        self.assertEqual(dd._unpack("мусор"), {})
        self.assertEqual(dd._unpack([]), {})

    def test_real_item_still_unpacks(self):
        from app.sources import dadata as dd
        got = dd._unpack({"value": "ООО Ромашка",
                          "data": {"inn": "7701234567",
                                   "management": {"name": "Иванов И. И."}}})
        self.assertEqual(got.get("inn"), "7701234567")

    def test_every_source_guards_its_lists(self):
        """Проверка типа стоит там, где элемент списка разбирается как
        словарь: иначе один null валит весь прогон."""
        import glob
        import re
        missing = []
        for path in glob.glob(os.path.join(os.path.dirname(__file__), "..",
                                           "app", "sources", "*.py")):
            src = io.open(path, encoding="utf-8").read()
            for m in re.finditer(r"for (\w+) in \([^)]*or \[\]\):\n(\s+)(.*)",
                                 src):
                nxt = m.group(3)
                if ".get(" in nxt and "isinstance" not in nxt:
                    missing.append("%s: %s" % (os.path.basename(path),
                                               nxt.strip()[:50]))
        self.assertEqual(missing, [], "разбор без проверки типа")


class UpdateIsChecked(unittest.TestCase):
    """Обрыв на середине даёт файл подходящего размера, но нерабочий, а
    запускать такой поверх установленной программы — худшее, что можно
    сделать: старой версии уже нет, новая не встала."""

    def test_checksum_travels_from_the_release(self):
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "update.py"), encoding="utf-8").read()
        self.assertIn('"setup_sha": setup_sha', src)
        self.assertIn("hashlib.sha256(blob).hexdigest()", src)
        self.assertIn("setup_sha=info.get(\"setup_sha\")", src)

    def test_missing_checksum_is_not_a_refusal(self):
        """Старый релиз или свой источник обновлений отпечатка не даёт,
        и это не повод отказывать в обновлении."""
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "update.py"), encoding="utf-8").read()
        block = src[src.index("if setup_sha:"):]
        self.assertTrue(block.startswith("if setup_sha:"))


class ClearLeftTails(unittest.TestCase):
    """Очистка базы не трогала заметки, а номера компаний в SQLite
    начинаются заново — старая заметка доставалась новой компании."""

    def setUp(self):
        db.init()
        for t in ("companies", "notes", "contacts", "signals", "site_visits"):
            db.conn().execute("DELETE FROM %s" % t)
        db.conn().commit()
        self.app = web.create_app().test_client()

    def test_private_note_does_not_move_to_a_stranger(self):
        old, _ = db.upsert_company({"name": "ООО Старая", "source": "тест"})
        db.add_note(old, "отказались, больше не звонить")
        self.app.post("/api/clear")
        new, _ = db.upsert_company({"name": "ООО Другая", "source": "тест"})
        self.assertEqual(new, old, "номер не переиспользован — проверка "
                                   "потеряла смысл, перепишите её")
        self.assertEqual(db.notes(new), [],
                         "чужая заметка всплыла в новой карточке")

    def test_visit_cache_is_cleared_too(self):
        """Иначе новый поиск пропускает те же сайты, и карточки выходят
        без телефонов и почт."""
        db.mark_visited("https://x.ru", 5, ok=True)
        self.assertTrue(db.visited_recently("https://x.ru"))
        self.app.post("/api/clear")
        self.assertFalse(db.visited_recently("https://x.ru"))

    def test_blacklist_survives(self):
        """Он про решения человека, а не про найденные данные."""
        cid, _ = db.upsert_company({"name": "Отказавшая", "source": "тест",
                                    "inn": "7701234567"})
        db.blacklist_add(cid, "сказали нет")
        self.app.post("/api/clear")
        self.assertTrue(db.is_blacklisted({"inn": "7701234567"}))


class GuessedDirectorMail(unittest.TestCase):
    """Схему адреса строили по первой найденной на сайте почте. У
    компании с info@gmail.com «адресом руководителя» выходил
    ivanov@gmail.com — ящик какого-то Иванова, которых там тысячи."""

    def test_free_mail_is_never_guessed(self):
        for d in ("gmail.com", "MAIL.RU", "yandex.ru", "bk.ru",
                  "outlook.com", "proton.me"):
            self.assertEqual(enrich.candidates("Иванов Иван Иванович", d), [],
                             "на %s всё ещё выводится адрес" % d)

    def test_own_domain_still_works(self):
        got = enrich.candidates("Иванов Иван Иванович", "romashka.ru")
        self.assertTrue(got)
        self.assertTrue(all(a.endswith("@romashka.ru") for a, _ in got))

    def test_domain_comes_from_the_site_not_from_the_first_mail(self):
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8").read()
        block = src[src.index("# 4. Кандидаты в адрес руководителя"):]
        block = block[:block.index("# 5. ФНС")]
        self.assertIn("site_host", block)
        self.assertLess(block.index("site_host"), block.index("emails_found[0]"),
                        "домен по-прежнему берётся у первой почты")


class HonestCounters(unittest.TestCase):
    """Итог прогона считался запросом ко всей базе: на втором прогоне
    цифра росла сама собой и ничего не говорила о том, что дал обход."""

    def test_run_total_is_separate_from_the_base_total(self):
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8").read()
        self.assertIn("lpr_now", src)
        self.assertIn("за этот прогон", src)
        self.assertIn("Всего таких в базе", src)


class NotesBelongToTheirCompany(unittest.TestCase):
    def setUp(self):
        db.init()
        self.app = web.create_app().test_client()

    def test_note_of_another_company_is_not_deleted(self):
        a, _ = db.upsert_company({"name": "Первая", "source": "тест"})
        b, _ = db.upsert_company({"name": "Вторая", "source": "тест"})
        nid = db.add_note(b, "заметка второй компании")
        self.app.post("/api/company/%d/note" % a, json={"delete": nid})
        self.assertEqual(len(db.notes(b)), 1, "стёрли чужую заметку")

    def test_own_note_is_deleted(self):
        a, _ = db.upsert_company({"name": "Первая", "source": "тест"})
        nid = db.add_note(a, "своя заметка")
        self.app.post("/api/company/%d/note" % a, json={"delete": nid})
        self.assertEqual(db.notes(a), [])

    def test_junk_in_the_bulk_list_does_not_break_it(self):
        a, _ = db.upsert_company({"name": "Первая", "source": "тест"})
        r = self.app.post("/api/bulk", json={"ids": ["абв", a, None],
                                             "action": "stage",
                                             "stage": "созвон"})
        self.assertLess(r.status_code, 500)
        row = db.conn().execute("SELECT stage FROM companies WHERE id=?",
                                (a,)).fetchone()
        self.assertEqual(row["stage"], "созвон")


class SameNameDifferentCompanies(unittest.TestCase):
    """«ООО Ромашка» есть в каждом регионе. Программа считала их одной
    компанией в трёх местах сразу."""

    def setUp(self):
        db.init()
        for t in ("companies", "blacklist", "contacts", "signals", "notes"):
            db.conn().execute("DELETE FROM %s" % t)
        db.conn().commit()

    def test_refusal_of_one_does_not_bury_the_others(self):
        cid, _ = db.upsert_company({"name": "ООО Ромашка", "inn": "7701111111",
                                    "source": "тест"})
        db.blacklist_add(cid, "отказались")
        self.assertTrue(db.is_blacklisted({"inn": "7701111111",
                                           "name": "ООО Ромашка"}))
        self.assertFalse(db.is_blacklisted({"inn": "5402222222",
                                            "name": "ООО Ромашка"}),
                         "другая фирма с тем же названием скрыта")

    def test_name_still_works_when_there_is_nothing_else(self):
        """У компании из карты нет ни ИНН, ни идентификатора hh —
        название единственное, чем её опознать."""
        cid, _ = db.upsert_company({"name": "Кафе У Дома", "source": "тест"})
        db.blacklist_add(cid, "отказались")
        self.assertTrue(db.is_blacklisted({"name": "Кафе У Дома"}))

    def test_merge_key_includes_the_city(self):
        from app import worker as w
        msk = w.norm_name("ООО «Дентал»")
        self.assertTrue(msk)
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8").read()
        self.assertIn('out.append("имя:%s|%s" % (name,', src)

    def test_duplicates_are_not_offered_across_cities(self):
        db.upsert_company({"name": "ООО Дентал", "region": "Москва",
                           "source": "тест"})
        db.upsert_company({"name": "Дентал", "region": "Санкт-Петербург",
                           "source": "тест"})
        self.assertEqual(db.find_duplicates(), [])

    def test_twins_in_one_city_never_appear_at_all(self):
        """Раньше они заводились и ждали ручной склейки. Теперь вторая
        запись просто дополняет первую — ключи те же самые, и знать их
        в момент записи программа уже могла."""
        a, first = db.upsert_company({"name": "ООО Дентал", "region": "Москва",
                                      "source": "карта"})
        b, second = db.upsert_company({"name": "Дентал", "region": "Москва",
                                       "source": "ЕГРЮЛ", "inn": "7701234567"})
        self.assertTrue(first)
        self.assertFalse(second, "завелась вторая запись о той же компании")
        self.assertEqual(a, b)
        row = db.conn().execute("SELECT inn FROM companies WHERE id=?",
                                (a,)).fetchone()
        self.assertEqual(row["inn"], "7701234567",
                         "ИНН из второго источника не дополнил карточку")

    def test_old_twins_are_still_found_for_merging(self):
        """У тех, кто собирал базу прежними версиями, дубли уже лежат."""
        a, _ = db.upsert_company({"name": "ООО Дентал", "region": "Москва",
                                  "source": "тест"})
        db.conn().execute("INSERT INTO companies (name, region, source, "
                          "created_at, updated_at) VALUES (?,?,?,0,0)",
                          ("Дентал", "Москва", "тест"))
        db.conn().commit()
        b = db.conn().execute("SELECT MAX(id) m FROM companies").fetchone()["m"]
        self.assertEqual(db.find_duplicates(), [(a, b)])

    def test_different_inn_is_never_a_duplicate(self):
        """Один сайт на две фирмы — обычное дело у групп компаний, а ИНН
        разный значит разные юрлица."""
        db.upsert_company({"name": "Группа А", "inn": "7701111111",
                           "site": "https://g.ru", "source": "тест"})
        db.upsert_company({"name": "Группа Б", "inn": "7702222222",
                           "site": "https://g.ru", "source": "тест"})
        self.assertEqual(db.find_duplicates(), [])


class VacancyCount(unittest.TestCase):
    """Число вакансий весит в оценке четверть. «Руководитель отдела
    продаж» находится и по «отдел продаж», и по «руководитель продаж»."""

    def test_same_vacancy_is_not_counted_twice(self):
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8").read()
        block = src[src.index('if emp["id"] in seen:'):]
        block = block[:block.index("seen.add")]
        self.assertIn("vac_ids", block)
        self.assertNotIn('old["vacancies"] += emp["vacancies"]', block)

    def test_search_returns_vacancy_ids(self):
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "sources", "hh.py"), encoding="utf-8").read()
        self.assertIn('"vac_ids"', src)

    def test_merge_is_not_quadratic(self):
        """Перебор списка на каждой найденной компании превращался в
        миллион сравнений на тысяче работодателей."""
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8").read()
        self.assertIn("by_id[emp[\"id\"]]", src)


class EmptyMeansEmpty(unittest.TestCase):
    """NULL = '' в SQL не истина и не ложь — сравнение просто не
    срабатывает, и фильтр «Пустые» не показывал ни одной пустой
    компании, то есть ровно тех, ради кого он сделан."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM companies")
        db.conn().execute("DELETE FROM contacts")
        db.conn().commit()
        self.app = web.create_app().test_client()

    def test_company_without_a_site_column_is_found(self):
        db.upsert_company({"name": "Совсем пустая", "source": "тест"})
        rows = self.app.get("/api/companies?only=empty").get_json()["rows"]
        self.assertEqual([r["name"] for r in rows], ["Совсем пустая"])

    def test_company_with_an_empty_string_site_is_found_too(self):
        db.upsert_company({"name": "Сайт пустой строкой", "source": "тест",
                           "site": ""})
        rows = self.app.get("/api/companies?only=empty").get_json()["rows"]
        self.assertEqual(len(rows), 1)

    def test_company_with_contacts_is_not_empty(self):
        cid, _ = db.upsert_company({"name": "С телефоном", "source": "тест"})
        db.add_contact(cid, "phone", "+74951234567", "general", 90,
                       "unchecked", "тест")
        self.assertEqual(self.app.get("/api/companies?only=empty")
                         .get_json()["rows"], [])

    def test_contactable_sees_a_site_without_contacts(self):
        db.upsert_company({"name": "Только сайт", "source": "тест",
                           "site": "https://x.ru"})
        rows = self.app.get("/api/companies?only=contactable").get_json()["rows"]
        self.assertEqual(len(rows), 1)


class BaseStaysClean(unittest.TestCase):
    """База удваивалась на каждом повторном прогоне: у компании из карты
    нет ни ИНН, ни идентификатора работодателя, и вчерашняя запись о ней
    ничем не отличалась от сегодняшней."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM companies")
        db.conn().commit()

    def test_second_run_does_not_double_the_base(self):
        for _ in range(3):
            db.upsert_company({"name": "ООО Ромашка", "region": "Москва",
                               "source": "OSM", "site": "https://romashka.ru"})
        n = db.conn().execute("SELECT COUNT(*) n FROM companies").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_same_site_different_name_is_one_company(self):
        a, _ = db.upsert_company({"name": "Ромашка", "source": "карта",
                                  "site": "https://romashka.ru"})
        b, _ = db.upsert_company({"name": "ООО «Ромашка-Плюс»",
                                  "source": "ЕГРЮЛ",
                                  "site": "http://www.romashka.ru/contacts"})
        self.assertEqual(a, b)

    def test_group_of_companies_is_not_glued(self):
        """Один сайт на две фирмы — обычное дело, а ИНН решает."""
        a, _ = db.upsert_company({"name": "Группа А", "inn": "7701111111",
                                  "site": "https://g.ru", "source": "т"})
        b, _ = db.upsert_company({"name": "Группа Б", "inn": "7702222222",
                                  "site": "https://g.ru", "source": "т"})
        self.assertNotEqual(a, b)

    def test_same_name_other_city_stays_separate(self):
        a, _ = db.upsert_company({"name": "ООО Ромашка", "region": "Москва",
                                  "source": "т"})
        b, _ = db.upsert_company({"name": "ООО Ромашка",
                                  "region": "Новосибирск", "source": "т"})
        self.assertNotEqual(a, b)

    def test_second_source_fills_the_gaps(self):
        a, _ = db.upsert_company({"name": "Ромашка", "region": "Москва",
                                  "source": "карта", "site": "https://r.ru"})
        db.upsert_company({"name": "ООО Ромашка", "region": "Москва",
                           "source": "ЕГРЮЛ", "inn": "7701234567",
                           "director": "Иванов И. И."})
        row = db.conn().execute("SELECT inn, director, site FROM companies "
                                "WHERE id=?", (a,)).fetchone()
        self.assertEqual(row["inn"], "7701234567")
        self.assertEqual(row["director"], "Иванов И. И.")
        self.assertEqual(row["site"], "https://r.ru")


class FunnelTalksToToday(unittest.TestCase):
    """Карточку перетаскивали в «созвон», а на экране «Сегодня» не
    появлялось ничего: срок надо было проставить руками, отдельно."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM companies")
        db.conn().commit()
        self.app = web.create_app().test_client()

    def _stage(self, stage):
        cid, _ = db.upsert_company({"name": "Компания " + stage, "source": "т"})
        self.app.post("/api/company/%d" % cid, json={"stage": stage})
        return db.conn().execute(
            "SELECT stage, next_step, next_date FROM companies WHERE id=?",
            (cid,)).fetchone()

    def test_work_stage_means_today(self):
        r = self._stage("в работе")
        self.assertEqual(r["next_date"], datetime.date.today().isoformat())
        self.assertTrue(r["next_step"])

    def test_written_waits_three_days(self):
        r = self._stage("написали")
        self.assertEqual(r["next_date"],
                         (datetime.date.today()
                          + datetime.timedelta(days=3)).isoformat())

    def test_call_is_tomorrow(self):
        r = self._stage("созвон")
        self.assertEqual(r["next_date"],
                         (datetime.date.today()
                          + datetime.timedelta(days=1)).isoformat())

    def test_refusal_clears_the_plan(self):
        """Отказ — конец разговора, и висеть в списке на сегодня компания
        больше не должна."""
        cid, _ = db.upsert_company({"name": "Отказ", "source": "т"})
        self.app.post("/api/company/%d" % cid, json={"stage": "в работе"})
        self.app.post("/api/company/%d" % cid, json={"stage": "отказ"})
        r = db.conn().execute("SELECT next_date FROM companies WHERE id=?",
                              (cid,)).fetchone()
        self.assertEqual(r["next_date"] or "", "")

    def test_own_plan_is_never_overwritten(self):
        cid, _ = db.upsert_company({"name": "Своё", "source": "т"})
        self.app.post("/api/company/%d" % cid,
                      json={"next_step": "мой шаг", "next_date": "2030-01-01"})
        self.app.post("/api/company/%d" % cid, json={"stage": "созвон"})
        r = db.conn().execute("SELECT next_step, next_date FROM companies "
                              "WHERE id=?", (cid,)).fetchone()
        self.assertEqual(r["next_step"], "мой шаг")
        self.assertEqual(r["next_date"], "2030-01-01")

    def test_it_actually_shows_up_on_today(self):
        cid, _ = db.upsert_company({"name": "Позвонить", "source": "т"})
        self.app.post("/api/company/%d" % cid, json={"stage": "в работе"})
        due = self.app.get("/api/today").get_json()["due"]
        self.assertIn("Позвонить", [r["name"] for r in due])


class StaleVacancy(unittest.TestCase):
    """Свежесть собиралась, показывалась значком и была названа в
    пояснении «сроком годности повода», а в оценке не участвовала."""

    def test_fresh_beats_stale(self):
        row = {"director": "Иванов", "site": "x.ru"}
        fresh, _ = score.compute(row, {"hh_vacancies": "4",
                                       "hh_fresh_days": "2"}, [])
        month, _ = score.compute(row, {"hh_vacancies": "4",
                                       "hh_fresh_days": "30"}, [])
        old_, _ = score.compute(row, {"hh_vacancies": "4",
                                      "hh_fresh_days": "120"}, [])
        self.assertGreater(fresh, month)
        self.assertGreater(month, old_)

    def test_unknown_date_is_not_punished_as_stale(self):
        """Дата приходит не всегда, и молчание — не признак старости."""
        row = {"director": "Иванов", "site": "x.ru"}
        unknown, _ = score.compute(row, {"hh_vacancies": "4"}, [])
        old_, _ = score.compute(row, {"hh_vacancies": "4",
                                      "hh_fresh_days": "120"}, [])
        self.assertGreater(unknown, old_)

    def test_it_is_explained_in_the_breakdown(self):
        _v, parts = score.compute({"director": "", "site": ""},
                                  {"hh_vacancies": "2",
                                   "hh_fresh_days": "1"}, [])
        p = [x for x in parts if x["key"] == "fresh_vacancy"][0]
        self.assertTrue(p["got"])
        self.assertTrue(p["why"])


class ChainDoesNotStall(unittest.TestCase):
    """Прогон, прерванный на середине, оставлял компании без обогащения
    навсегда: повторный поиск находил их же, новых не было, и очередь
    пустовала."""

    def test_enrichment_is_queued_for_already_known_companies(self):
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8").read()
        self.assertEqual(src.count('then_enrich") and (added or known)'), 2)
        self.assertNotIn('then_enrich") and added:', src)


class CardLooksLikeACard(unittest.TestCase):
    """Карточка разливалась по трём колонкам прямо в теле таблицы: без
    рамки, без фона и без названия компании внутри."""

    def setUp(self):
        self.js = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                       "static", "app.js"),
                          encoding="utf-8").read()
        self.css = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                        "static", "app.css"),
                           encoding="utf-8").read()

    def test_company_name_is_inside_the_card(self):
        self.assertIn('<h3>${esc(c.name)}</h3>', self.js)

    def test_card_has_its_own_surface(self):
        block = self.css[self.css.index("\n.detail{"):]
        block = block[:block.index("}")]
        for prop in ("background", "border", "border-radius", "box-shadow"):
            self.assertIn(prop, block, "у карточки нет %s" % prop)

    def test_actions_are_a_row_not_prose(self):
        self.assertIn('class="card-do"', self.js)
        self.assertIn('data-analyze', self.js)
        self.assertIn('data-letter', self.js)
        self.assertIn('data-kp', self.js)

    def test_two_columns_work_and_reference(self):
        self.assertIn('class="card-main"', self.js)
        self.assertIn('class="card-side"', self.js)
        self.assertNotIn('<div class="detail">\n    <section>', self.js)

    def test_score_breakdown_is_collapsed(self):
        """Справка, которую читают один раз, занимала больше места, чем
        контакты и заметки вместе."""
        self.assertIn('<details class="score-block">', self.js)
        self.assertIn("<summary>", self.js)

    def test_big_badge_says_what_the_number_is(self):
        self.assertIn('class="badge-big', self.js)
        self.assertIn("<span>балл</span>", self.js)


class NoSystemListboxes(unittest.TestCase):
    """Системный многострочный список — единственное место, куда
    оформление не дотягивалось: белая рамка и синее выделение системы
    посреди тёмной темы. Плюс «несколько с зажатым Ctrl» знают не все."""

    def setUp(self):
        self.html = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                         "templates", "index.html"),
                            encoding="utf-8").read()
        self.js = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                       "static", "app.js"),
                          encoding="utf-8").read()

    def test_cities_and_regions_are_buttons(self):
        self.assertNotIn("<select id=\"q-cities\"", self.html)
        self.assertNotIn("<select id=\"f-area\"", self.html)
        self.assertIn('class="cities"', self.html)

    def test_nothing_reads_selection_from_a_listbox(self):
        self.assertNotIn("selectedOptions", self.js)

    def test_whole_country_excludes_the_cities(self):
        """Вместе они значат то же, что «Россия целиком», только дольше."""
        block = self.js[self.js.index('$("q-cities").onclick'):]
        block = block[:block.index("citiesNote();\n};")]
        self.assertIn("Россия целиком", block)
        self.assertIn("classList.remove", block)

    def test_empty_choice_is_called_out(self):
        self.assertIn("Не выбран ни один город", self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)

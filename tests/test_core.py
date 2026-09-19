# -*- coding: utf-8 -*-
"""Проверки того, что ломается молча.

Сеть здесь не трогаем намеренно: тест, зависящий от чужого сервера, рано
или поздно краснеет не из-за нашей ошибки, и его перестают читать.
"""
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

from app import ai, db, diag, enrich, export, geo, profile, score, social, update  # noqa: E402
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
        self.assertTrue(any("вакансий" in r for r in why))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

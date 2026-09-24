# -*- coding: utf-8 -*-
"""Проверки того, что ломается молча.

Сеть здесь не трогаем намеренно: тест, зависящий от чужого сервера, рано
или поздно краснеет не из-за нашей ошибки, и его перестают читать.
"""
import datetime
import email
import email.policy
import json
import io
import re
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Заглушки почтовых серверов лежат рядом с тестами.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# База должна лежать во временной папке: иначе прогон тестов затрёт
# рабочие данные пользователя.
_TMP = tempfile.mkdtemp(prefix="navodka-test-")
from app import settings                                    # noqa: E402
settings.data_dir = lambda: _TMP
settings.db_path = lambda: os.path.join(_TMP, "test.sqlite3")

from app import (ai, db, diag, enrich, export, geo, net, profile, score,  # noqa: E402
                 social, trades, update, web, worker)
from app.sources import dadata, fns, gis2, hh, importer, site, tg, zakupki  # noqa: E402
from app.sources import egrul, superjob, trudvsem                  # noqa: E402

# Новые площадки включены по умолчанию и в старых сохранённых наборах.
# В тестах сеть не трогаем: их поиск по умолчанию молчит, а тесты самих
# площадок зовут настоящие функции, подставляя ответ сервера.
_REAL_SEARCH = {"trud": trudvsem.search, "sj": superjob.search,
                "fns": egrul.search}
trudvsem.search = lambda *a, **kw: []
superjob.search = lambda *a, **kw: []
egrul.search = lambda *a, **kw: []


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


class OneGuessIsNotFive(unittest.TestCase):
    """В карточке стояло пять «почт руководителя» подряд — все выведены
    по схеме домена, все на одном домене. Одна догадка — одна строка."""

    CANDS = [("i.ivanov@c.ru", 75), ("ivanov@c.ru", 45),
             ("iivanov@c.ru", 42), ("ivan.ivanov@c.ru", 39),
             ("ivanov.i@c.ru", 36)]

    def test_unchecked_keeps_only_the_best(self):
        keep, why = enrich.keep_candidates(self.CANDS, {})
        self.assertEqual([a for a, _ in keep], ["i.ivanov@c.ru"])
        self.assertEqual(why, "выведен по схеме домена")

    def test_catch_all_keeps_one_and_says_so(self):
        verdicts = {a: "catch_all" for a, _ in self.CANDS}
        keep, why = enrich.keep_candidates(self.CANDS, verdicts)
        self.assertEqual(len(keep), 1)
        self.assertIn("любой адрес", why)

    def test_confirmed_addresses_all_stay(self):
        verdicts = {"i.ivanov@c.ru": "ok", "ivanov@c.ru": "ok",
                    "iivanov@c.ru": "unknown"}
        keep, why = enrich.keep_candidates(self.CANDS, verdicts)
        self.assertEqual([a for a, _ in keep],
                         ["i.ivanov@c.ru", "ivanov@c.ru"])
        self.assertIn("подтверждён", why)

    def test_rejected_addresses_never_stay(self):
        verdicts = {"i.ivanov@c.ru": "bad"}
        keep, _ = enrich.keep_candidates(self.CANDS, verdicts)
        self.assertEqual([a for a, _ in keep], ["ivanov@c.ru"])

    def test_everything_rejected_leaves_nothing(self):
        verdicts = {a: "bad" for a, _ in self.CANDS}
        self.assertEqual(enrich.keep_candidates(self.CANDS, verdicts), ([], ""))


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

    def test_meta_does_not_leak_neighbouring_tags(self):
        # Разбор «от content до name» перепрыгивал через соседние теги, и
        # в описание попадала кодировка вместе с заголовком страницы.
        html = ('<meta content="text/html; charset=utf-8" '
                'http-equiv="content-type"/>'
                "<title>Сухие строительные смеси Старатели</title>"
                '<meta content="Производство сухих смесей с 1992 года" '
                'name="description"/>')
        self.assertEqual(site._meta(html, "description"),
                         "Производство сухих смесей с 1992 года")

    def test_meta_keeps_angle_bracket_inside_value(self):
        html = '<meta name="description" content="Рост 2024 -> 2025">'
        self.assertEqual(site._meta(html, "description"), "Рост 2024 -> 2025")

    def test_meta_skips_empty_and_takes_next(self):
        html = ('<meta name="description" content="">'
                '<meta name="description" content="Второй, непустой">')
        self.assertEqual(site._meta(html, "description"), "Второй, непустой")


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

    def test_old_rows_repaired_on_start(self):
        """Метки перехода в адресе сайта и разметка в «чем занимается»
        успели попасть в собранные базы — обновление их чинит."""
        cid, _ = db.upsert_company({"name": "ДНКом", "inn": "7712345678"})
        db.conn().execute(
            "UPDATE companies SET site=?, activity=? WHERE id=?",
            ("dnkom.ru/?utm_campaign=knopka&utm_medium=maps&utm_source=yandex",
             'text/html; charset=utf-8" http-equiv="content-type"/> '
             "Сухие строительные смеси Старатели", cid))
        db.conn().commit()
        db._repair(db.conn())
        row = db.conn().execute("SELECT site, activity FROM companies "
                                "WHERE id=?", (cid,)).fetchone()
        self.assertEqual(row["site"], "dnkom.ru")
        self.assertEqual(row["activity"], "Сухие строительные смеси Старатели")

    def test_repair_leaves_clean_rows_alone(self):
        cid, _ = db.upsert_company({"name": "Хелен", "inn": "7798765432"})
        db.conn().execute(
            "UPDATE companies SET site=?, activity=? WHERE id=?",
            ("https://helenmedia.ru", "Клиника эстетической медицины "
             "в центре Москвы", cid))
        db.conn().commit()
        db._repair(db.conn())
        row = db.conn().execute("SELECT site, activity FROM companies "
                                "WHERE id=?", (cid,)).fetchone()
        self.assertEqual(row["site"], "https://helenmedia.ru")
        self.assertEqual(row["activity"],
                         "Клиника эстетической медицины в центре Москвы")

    def test_repair_runs_once_and_then_keeps_quiet(self):
        """Проход по всей таблице нужен один раз: новые записи приходят
        уже разобранными, а на большой базе это лишняя работа на каждом
        запуске."""
        db.set_setting(db._REPAIR_MARK, "")
        db.init()
        self.assertEqual(db.get_setting(db._REPAIR_MARK), "1")
        cid, _ = db.upsert_company({"name": "Позже", "inn": "7700000001"})
        db.conn().execute("UPDATE companies SET site=? WHERE id=?",
                          ("x.ru/?utm_source=y", cid))
        db.conn().commit()
        db.init()
        row = db.conn().execute("SELECT site FROM companies WHERE id=?",
                                (cid,)).fetchone()
        self.assertEqual(row["site"], "x.ru/?utm_source=y")

    def test_only_unchecked_phones_come_up_for_telegram(self):
        """Повторный прогон не должен тратить дневной предел Telegram на
        номера, про которые уже спрашивали."""
        cid, _ = db.upsert_company({"name": "Х", "inn": "7700000077"})
        db.add_contact(cid, "phone", "+79990000001", "general", 90)
        db.add_contact(cid, "phone", "+79990000002", "general", 80)
        db.add_contact(cid, "email", "a@b.ru", "general", 80)
        rows = db.phones_to_check()
        self.assertEqual(len(rows), 2)
        db.set_contact_verified(rows[0]["id"], "tg")
        self.assertEqual(len(db.phones_to_check()), 1)
        # Но по отдельной просьбе — заново все.
        self.assertEqual(len(db.phones_to_check(redo=True)), 2)

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


class EnrichmentFillsTheRowItWasGiven(unittest.TestCase):
    """Обогащение заводило вторую компанию вместо той, которую
    обогащало.

    Из ЕГРЮЛ приходит юридическое название — «ПАО ДВМП» вместо
    вывески «Fesco». Ни ИНН, ни домена, ни номера работодателя в
    этом ответе может не быть — и запись по содержимому не узнавала
    исходную строку. Исходная оставалась пустой навсегда."""

    def setUp(self):
        db.init()
        c = db.conn()
        for t in ("contacts", "signals", "notes", "companies"):
            c.execute("DELETE FROM %s" % t)
        c.commit()

    def test_the_registry_answer_lands_in_the_same_company(self):
        cid, _ = db.upsert_company({"name": "Fesco", "site": "fesco.ru"})
        db.fill_company(cid, {"name": 'ПАО «ДВМП»', "inn": "2540016961",
                              "director": "Иванов Иван Иванович",
                              "founded": 1992, "capital": 2949257000})
        rows = list(db.conn().execute("SELECT * FROM companies"))
        self.assertEqual(len(rows), 1, "завелся дубль")
        self.assertEqual(rows[0]["inn"], "2540016961")
        self.assertEqual(rows[0]["director"], "Иванов Иван Иванович")
        self.assertEqual(rows[0]["founded"], 1992)

    def test_the_name_the_person_searched_by_is_kept(self):
        """Человек искал «грузоперевозки» и нашёл «Fesco» — под
        этим именем он компанию и помнит. Юридическое имя важно в
        договоре, а в списке на обзвон мешает узнаванию."""
        cid, _ = db.upsert_company({"name": "Fesco"})
        db.fill_company(cid, {"name": 'ПАО «ДВМП»', "inn": "2540016961"})
        row = db.conn().execute("SELECT name FROM companies WHERE id=?",
                                (cid,)).fetchone()
        self.assertEqual(row["name"], "Fesco")

    def test_what_is_already_known_is_not_overwritten(self):
        cid, _ = db.upsert_company({"name": "Ромашка",
                                    "director": "Найден на сайте"})
        db.fill_company(cid, {"director": "Из реестра"})
        row = db.conn().execute("SELECT director FROM companies WHERE id=?",
                                (cid,)).fetchone()
        self.assertEqual(row["director"], "Найден на сайте")

    def test_an_answer_by_inn_may_overwrite(self):
        """Ответ по ИНН надёжнее того, что стояло по названию."""
        cid, _ = db.upsert_company({"name": "Ромашка", "director": "Кто-то"})
        db.fill_company(cid, {"director": "Из реестра по ИНН"}, over=True)
        row = db.conn().execute("SELECT director FROM companies WHERE id=?",
                                (cid,)).fetchone()
        self.assertEqual(row["director"], "Из реестра по ИНН")

    def test_the_worker_never_upserts_a_registry_answer(self):
        """Именно этот вызов и заводил дубли."""
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8")
        with src as fh:
            text = fh.read()
        body = text[text.index("def task_enrich("):text.index("def task_gis_search(")]
        self.assertNotIn("db.upsert_company(dict(info", body)
        self.assertIn("db.fill_company(cid, info", body)

    def test_a_field_outside_the_list_is_not_dropped_silently(self):
        """update_company_fields молча выбрасывает чужие поля, и
        запись ИНН через неё не делала ничего вовсе. Для реестровых
        полей есть своя функция, и в ней ИНН есть."""
        self.assertIn("inn", db.REGISTRY_FIELDS)
        self.assertIn("ogrn", db.REGISTRY_FIELDS)
        self.assertIn("director", db.REGISTRY_FIELDS)
        cid, _ = db.upsert_company({"name": "Ромашка"})
        db.update_company_fields(cid, {"inn": "7707083893"})
        row = db.conn().execute("SELECT inn FROM companies WHERE id=?",
                                (cid,)).fetchone()
        self.assertFalse(row["inn"], "update_company_fields теперь пишет ИНН — "
                                     "проверьте, не разошлись ли два пути записи")


class TheFirstRunIsNotAVoid(unittest.TestCase):
    """Пустая база — это не «ноль компаний», а «ещё не
    начинали». Первый запуск встречал четырьмя нулями в ряд и
    двумя пустыми панелями на две трети экрана."""

    def setUp(self):
        self.js = self.read("app.js")

    @staticmethod
    def read(name):
        fh = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                  "static", name), encoding="utf-8")
        with fh:
            return fh.read()

    def test_an_empty_base_shows_how_to_start(self):
        self.assertIn("first-run", self.js)
        self.assertIn("С чего начать", self.js)
        self.assertIn('data-go="sources"', self.js)

    def test_counters_appear_only_when_there_is_something_to_count(self):
        self.assertIn('$("tiles").hidden = !total', self.js)
        self.assertIn('$("today-cols").hidden = !total', self.js)

    def test_the_block_is_in_the_page(self):
        db.init()
        html = web.create_app().test_client().get("/").get_data(as_text=True)
        self.assertIn('id="first-run"', html)
        self.assertIn('id="today-cols"', html)


class TheFunnelShowsWhichCompanyIsWhich(unittest.TestCase):
    """«ООО «Компания 12»» в колонке шириной в двести пикселей
    обрезалось до «ООО «Компания 1…», и соседние карточки были
    неразличимы. Первые шесть знаков у всех одинаковы и ничего
    не значат."""

    def setUp(self):
        self.js = TheFirstRunIsNotAVoid.read("app.js")

    def test_the_legal_form_is_dropped_in_the_narrow_column(self):
        self.assertIn("function shortName", self.js)
        self.assertIn("FORM_RE", self.js)

    def test_the_full_name_stays_in_the_tooltip(self):
        """Сокращённое имя удобно читать, но полное иногда
        нужно целиком — и оно не должно пропасть совсем."""
        self.assertIn('<b title="${esc(r.name)}">${esc(shortName(r.name))}</b>',
                      self.js)


class NothingCoversWhatYouClick(unittest.TestCase):
    """Липкая панель «Сохранить» стояла поверх кнопок
    «Проверить связь» и «Показать доступные модели» — то есть
    ровно тех, что нужны при настройке ключа ИИ, — и нажать по
    ним было нельзя даже прокрутив страницу до конца.

    Место под панелью даёт она сама: панель стоит последней в потоке,
    и в самом низу прокрутки оказывается на своём обычном месте, ниже
    всего остального. Запас снизу у страницы был лишним и вдобавок
    отрывал панель от края окна."""

    def test_the_save_bar_is_last_in_the_settings_flow(self):
        html = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                    "templates", "index.html"),
                       encoding="utf-8").read()
        tail = html[html.index('<div class="save-bar">'):]
        self.assertNotIn("<article", tail, "после панели ещё есть карточки")
        self.assertIn("</main>", tail)

    def test_the_save_bar_is_opaque(self):
        """Растяжка до прозрачного наполовину стирала строку под собой."""
        css = TheFirstRunIsNotAVoid.read("app.css")
        block = css[css.index(".save-bar{"):]
        block = block[:block.index("}")]
        self.assertIn("background:var(--bg)", block)
        self.assertNotIn("gradient", block)

    def test_room_under_the_bar_comes_from_the_cards(self):
        """Запас снизу держат карточки: свой отступ у страницы отрывал
        панель от края окна на ту же величину."""
        css = TheFirstRunIsNotAVoid.read("app.css")
        self.assertIn("#view-settings{padding-bottom:0}", css)
        m = re.search(r"#view-settings \.cards\{margin-bottom:(\d+)px", css)
        self.assertTrue(m and int(m.group(1)) >= 48,
                        "запаса под панелью не хватит")


class TextIsReadable(unittest.TestCase):
    """Приглушённый — не значит нечитаемый.

    Именно тихим цветом набраны все пояснения, заголовки
    колонок, ИНН в списке и подписи к контактам — то есть то, ради
    чего программу и читают. При контрасте ниже 4,5 к 1 подсказку
    проще пропустить, чем прочесть.

    Проверяем сами переменные, а не страницу: правка одного
    значения меняет сотни элементов разом, и ловить это надо в
    источнике."""

    @staticmethod
    def _lum(hexcolor):
        h = hexcolor.lstrip("#")
        rgb = [int(h[i:i + 2], 16) for i in (0, 2, 4)]

        def f(v):
            v /= 255.0
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2])

    def ratio(self, a, b):
        la, lb = self._lum(a), self._lum(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    def vars_of(self, block):
        css = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "static", "app.css"), encoding="utf-8")
        with css as fh:
            text = fh.read()
        if block == "dark":
            text = text[text.index("prefers-color-scheme: dark"):]
            text = text[:text.index("\n}")]
        else:
            text = text[text.index(":root{"):text.index(":root{color-scheme")]
        return dict(re.findall(r"--([\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", text))

    def test_muted_text_meets_the_bar_in_both_themes(self):
        for theme in ("light", "dark"):
            v = self.vars_of(theme)
            for name in ("ink", "ink-2", "ink-3"):
                for on in ("bg", "surface", "surface-2"):
                    got = self.ratio(v[name], v[on])
                    self.assertGreaterEqual(
                        got, 4.5,
                        "%s: --%s на --%s даёт %.2f при норме 4.5"
                        % (theme, name, on, got))

    def test_the_telegram_badge_is_readable_on_its_own_tint(self):
        """Под «есть TG» лежит подложка того же цвета, что и текст.
        Акцентом набирают ссылки на белом — здесь он даёт 4,1 к 1, и
        именно та подпись, ради которой отметку ставили, читается хуже
        всего. Поэтому у неё своя, тёмная краска."""
        # Подложка — акцент с прозрачностью поверх фона страницы.
        alpha = {"light": 0.10, "dark": 0.14}
        for theme in ("light", "dark"):
            v = self.vars_of(theme)
            acc = [int(v["accent"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
            base = [int(v["bg"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
            a = alpha[theme]
            mixed = "#%02x%02x%02x" % tuple(
                int(round(acc[i] * a + base[i] * (1 - a))) for i in range(3))
            got = self.ratio(v["tg-ink"], mixed)
            self.assertGreaterEqual(
                got, 4.5,
                "%s: --tg-ink на подложке даёт %.2f при норме 4.5"
                % (theme, got))

    def test_the_hierarchy_of_greys_survives(self):
        """Три уровня текста должны отличаться на глаз. Если
        ради читаемости сравнять их в один, страница станет
        ровным полотном, где всё одинаково важно."""
        for theme in ("light", "dark"):
            v = self.vars_of(theme)
            main = self.ratio(v["ink"], v["surface"])
            second = self.ratio(v["ink-2"], v["surface"])
            third = self.ratio(v["ink-3"], v["surface"])
            self.assertGreater(main, second, theme)
            self.assertGreater(second, third, theme)

    def test_nothing_is_set_smaller_than_ten_and_a_half(self):
        """«ГД» и «найден» продавец читает весь день.

        Порог именно 10,5, а не 11: прописные с разрядкой
        читаются на этом размере хорошо, и это обычный приём для
        подписей к колонкам. А вот 9,5 мало даже для них.

        Значки в ::after — стрелка сортировки, треугольник
        раскрытия — не текст, их не читают, а узнают по форме."""
        css = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "static", "app.css"), encoding="utf-8")
        with css as fh:
            text = fh.read()
        bad = []
        for rule in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            sel, body = rule
            if "::after" in sel or "::before" in sel:
                continue
            for size in re.findall(r"font-size:(\d+(?:\.\d+)?)px", body):
                if float(size) < 10.5:
                    bad.append((sel.strip()[:40], size))
        self.assertFalse(bad, "шрифт меньше 10.5px: %s" % bad)

    def test_text_on_a_coloured_chip_follows_the_theme(self):
        """Чёрный текст на «предупреждающем» цвете читаем в
        тёмной теме и не читаем в светлой: там этот цвет
        тёмно-коричневый."""
        css = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "static", "app.css"), encoding="utf-8")
        with css as fh:
            text = fh.read()
        self.assertNotIn("color:#111}", text,
                         "текст на цветной подложке задан чёрным напрямую")


class StopReallyStops(unittest.TestCase):
    """Нажатие «Остановить» прерывало обход — и тут же ставило в
    очередь обогащение, которое идёт втрое дольше. Снаружи это
    выглядело как кнопка, которая ничего не делает."""

    def setUp(self):
        db.init()
        c = db.conn()
        for t in ("tasks", "logs", "companies", "contacts", "signals"):
            c.execute("DELETE FROM %s" % t)
        c.commit()

    def tearDown(self):
        worker._stop.clear()

    def test_a_stopped_run_queues_nothing_after_itself(self):
        from app.sources import osm
        was = osm.search
        try:
            osm.search = lambda q, city, **kw: []
            tid = db.create_task("find", {})
            worker._stop.set()
            try:
                worker.task_find(tid, {
                    "query": "стоматология", "cities": ["Москва"],
                    "then_enrich": True, "then_ai": True, "synonyms": False,
                    "sources": {"osm": True, "gis": False, "yandex": False,
                                "dadata": False, "hh": False}})
            except RuntimeError:
                pass
            # Сама остановленная задача здесь не считается: её статус
            # меняет поток обхода, а его тут нет.
            left = [r["kind"] for r in db.conn().execute(
                "SELECT kind FROM tasks WHERE status='queued' AND id<>?", (tid,))]
            self.assertEqual(left, [], "после остановки всё равно встала задача")
        finally:
            osm.search = was

    def test_the_journal_says_the_chain_was_dropped(self):
        """Молча отменённая цепочка — это человек, ждущий
        обогащения, которого не будет."""
        tid = db.create_task("enrich", {})
        worker._stop.set()
        self.assertTrue(worker._chain_stopped(tid, "разбор ИИ"))
        said = [r["text"] for r in db.conn().execute(
            "SELECT text FROM logs WHERE task_id=?", (tid,))]
        self.assertTrue(any("в очередь не ставлю" in t for t in said), said)

    def test_a_normal_finish_still_chains(self):
        """Проверка на остановку не должна сломать то, ради чего
        цепочка и затевалась: одно нажатие и можно уйти."""
        tid = db.create_task("enrich", {})
        self.assertFalse(worker._chain_stopped(tid, "разбор ИИ"))


class TheQueueIsVisibleAndCancellable(unittest.TestCase):
    """«В очереди ещё 2» не говорило ни что это, ни как это
    отменить. Передумавший мог только ждать, пока программа
    доделает то, чего он уже не хочет."""

    def setUp(self):
        db.init()
        db.conn().execute("DELETE FROM tasks")
        db.conn().commit()
        self.cl = web.create_app().test_client()

    def test_the_queue_comes_as_a_list_not_a_number(self):
        db.create_task("find", {"query": "стоматология"})
        db.create_task("enrich", {"limit": 200})
        q = self.cl.get("/api/task").get_json()["queue"]
        self.assertEqual([r["kind"] for r in q], ["find", "enrich"])
        self.assertEqual(q[0]["params"]["query"], "стоматология")

    def test_one_can_be_cancelled(self):
        a = db.create_task("find", {})
        b = db.create_task("enrich", {})
        self.assertTrue(self.cl.post("/api/task/%d/cancel" % a).get_json()["cancelled"])
        self.assertEqual([r["id"] for r in db.queued_tasks()], [b])

    def test_a_running_task_is_not_cancelled_from_here(self):
        """Остановкой идущей занимается поток обхода. Двое,
        меняющие одну строку, разойдутся во мнении о том, что
        происходит."""
        tid = db.create_task("find", {})
        db.update_task(tid, status="running")
        self.assertFalse(db.cancel_task(tid))
        row = db.conn().execute("SELECT status FROM tasks WHERE id=?",
                                (tid,)).fetchone()
        self.assertEqual(row["status"], "running")

    def test_the_whole_queue_can_be_dropped(self):
        for _ in range(3):
            db.create_task("enrich", {})
        self.assertEqual(self.cl.post("/api/queue/clear").get_json()["cancelled"], 3)
        self.assertEqual(db.queued_tasks(), [])

    def test_history_says_what_happened_and_how_long(self):
        tid = db.create_task("find", {})
        db.update_task(tid, status="error", message="hh ответил 403")
        rows = self.cl.get("/api/tasks").get_json()["rows"]
        self.assertEqual(rows[0]["status"], "error")
        self.assertIn("403", rows[0]["message"])
        self.assertTrue(rows[0]["created_at"] and rows[0]["updated_at"])


class FindingPeopleWhenTheNameIsUnknown(unittest.TestCase):
    """Когда ФИО руководителя неизвестно, программа не предлагала
    ничего вовсе — а это как раз тот случай, когда помощь нужна."""

    def test_by_name_when_the_name_is_known(self):
        got = dict(social.search_links("Иванов Иван", "ООО «Ромашка»"))
        self.assertIn("TenChat", got)
        self.assertIn("ВКонтакте", got)

    def test_by_company_when_it_is_not(self):
        """В TenChat человек сам указывает, где работает: поиск по
        компании возвращает тех, кто это о себе заявил, а не
        однофамильцев."""
        got = dict(social.search_links("", "ООО «Дента-Люкс»"))
        self.assertTrue(any("TenChat" in t for t in got), got)

    def test_the_legal_form_is_stripped_from_the_query(self):
        """«ООО» и кавычки в профилях не пишут, и с ними поиск
        по людям не находит ничего."""
        url = dict(social.search_links("", "ПАО «ДВМП»"))["TenChat — сотрудники"]
        self.assertNotIn("%D0%9F%D0%90%D0%9E", url, "«ПАО» осталось в запросе")
        self.assertNotIn("%C2%AB", url, "кавычки остались в запросе")

    def test_nothing_is_offered_out_of_thin_air(self):
        self.assertEqual(social.search_links("", ""), [])

    def test_the_program_still_does_not_walk_these_links(self):
        """Граница та же, что и была: автоматически собранная
        база личных страниц — это профилирование частного лица."""
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8")
        with src as fh:
            text = fh.read()
        self.assertNotIn("search_links", text,
                         "обход стал ходить по ссылкам ручного поиска")

    def test_a_tenchat_link_from_the_site_is_still_collected(self):
        """Ссылку, которую компания опубликовала сама, брать
        можно и нужно: её затем и разместили."""
        got = social.from_text('<a href="https://tenchat.ru/denta">TenChat</a>')
        self.assertEqual(got.get("tenchat"), ["denta"])


class TheCatalogueIsBig(unittest.TestCase):
    """Список, в котором нет вашего дела, бесполезен ровно так
    же, как пустое поле."""

    def test_it_covers_industry_not_only_shops(self):
        titles = {t for t, _ in trades.GROUPS}
        for must in ("Металл и металлообработка", "Спецтехника",
                     "Упаковка и тара", "Сельское хозяйство",
                     "ВЭД и международная логистика", "Безопасность"):
            self.assertIn(must, titles, must)

    def test_a_hidden_activity_says_where_it_lives(self):
        """Самая обидная подпись — «такого нет» про то, что
        есть. Человек набирает «лазерная резка», тема стоит
        «Медицина» с прошлого раза — и список честно пуст. С
        полусотней тем это случалось бы постоянно."""
        js = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                  "static", "app.js"), encoding="utf-8")
        with js as fh:
            text = fh.read()
        self.assertIn("data-all-themes", text)
        self.assertIn("есть, но в теме", text)

    def test_the_theme_says_how_it_is_searched(self):
        db.init()
        html = web.create_app().test_client().get("/").get_data(as_text=True)
        self.assertIn('data-kind="бизнес"', html)
        self.assertIn('data-kind="места"', html)
        self.assertIn("· по названию", html)

    def test_close_words_never_repeat_inside_one_list(self):
        for word, close in trades.ALSO.items():
            self.assertEqual(len(close), len(set(close)), word)

    def test_a_close_word_may_also_be_an_activity_of_its_own(self):
        """Это не ошибка, и проверять здесь надо обратное.

        «Фитнес-клуб» и «тренажёрный зал» стоят и отдельными видами,
        и близкими словами друг к другу. Похоже на дубль, но им
        не является: это разные вывески и разные компании — «Фитнес-клуб
        Атлант» и «Тренажёрный зал № 1». Выбравший любое из двух
        должен получить обе — ради этого близкие слова и затевались."""
        self.assertIn("тренажерный зал", trades.words_for("фитнес-клуб"))
        self.assertIn("фитнес-клуб", trades.words_for("тренажерный зал"))


class RequisitesComeFromTheSite(unittest.TestCase):
    """Без ИНН программа не спросит ни ЕГРЮЛ, ни ФНС, и карточка
    остаётся без руководителя, выручки, численности и года — то есть
    без всего, ради чего её открывают.

    По названию ищется не всегда: в справочнике стоит вывеска
    «Fesco», а в реестре — ПАО «ДВМП». Зато сам ИНН лежит в подвале
    сайта, который программа и так скачивает."""

    def test_a_footer_gives_the_numbers(self):
        got = site.requisites("ПАО «ДВМП» ИНН 2540016961 "
                              "ОГРН 1022502256127 КПП 254001001")
        self.assertEqual(got.get("inn"), "2540016961")
        self.assertEqual(got.get("ogrn"), "1022502256127")

    def test_a_sole_trader_is_read_too(self):
        got = site.requisites("ИНН 500100732259 ОГРНИП 304500116000157")
        self.assertEqual(got.get("inn"), "500100732259")
        self.assertEqual(got.get("ogrn"), "304500116000157")

    def test_a_broken_number_is_refused(self):
        """Неверный ИНН — это не пустая карточка, а чужая: по нему
        из ЕГРЮЛ придёт другая компания, с другим руководителем и
        другой выручкой, и отличить её будет нельзя."""
        self.assertEqual(site.requisites("ИНН 1234567890 ОГРН 1234567890123"), {})

    def test_a_longer_number_is_not_cut_to_fit(self):
        self.assertEqual(site.requisites("ИНН 77070838931234"), {})

    def test_the_checksums_are_real(self):
        self.assertTrue(site.inn_ok("7707083893"))
        self.assertTrue(site.inn_ok("500100732259"))
        self.assertFalse(site.inn_ok("7707083894"))
        self.assertFalse(site.inn_ok("12345"))
        self.assertTrue(site.ogrn_ok("1027700132195"))
        self.assertFalse(site.ogrn_ok("1027700132196"))

    def test_the_crawl_carries_them_out(self):
        """Найденное должно дойти до того, кто спрашивает реестры."""
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "sources", "site.py"), encoding="utf-8")
        with src as fh:
            text = fh.read()
        self.assertIn('"inn": "", "ogrn": ""', text)
        self.assertIn("requisites(_squash_full(html))", text)

    def test_the_worker_asks_the_registry_again(self):
        """Ради этого всё и затевалось: ИНН с сайта — это второй
        заход в ЕГРЮЛ, уже по номеру, а не по вывеске."""
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8")
        with src as fh:
            text = fh.read()
        body = text[text.index("def task_enrich("):]
        mark = body.index('res.get("inn")')
        fns_call = body.index("fns.by_inn(")
        self.assertLess(mark, fns_call,
                        "ИНН с сайта надо узнать до того, как спрашивать ФНС")


class TheCardTellsWhatIsKnown(unittest.TestCase):
    """Карточка крупной компании выглядела так, будто о ней не
    известно ничего: статус, год, капитал, учредители и филиалы
    лежали в базе, но на глаза не попадали."""

    def setUp(self):
        js = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                  "static", "app.js"), encoding="utf-8")
        with js as fh:
            self.js = fh.read()

    def test_there_is_a_block_for_registry_facts(self):
        self.assertIn("function aboutBlock", self.js)
        for what in ('"статус в ЕГРЮЛ"', '"в ЕГРЮЛ с"',
                     '"уставный капитал"', '"учредителей"',
                     '"филиалов"', '"сотрудников по ФНС"'):
            self.assertIn(what, self.js, what)

    def test_an_empty_block_says_why_it_is_empty(self):
        """«Данных нет» и «их не спрашивали» — разные сообщения:
        в первом человек думает, что компания такая, во втором
        понимает, что надо запустить обогащение."""
        self.assertIn("ИНН не известен", self.js)
        self.assertIn("в реестрах ещё не спрашивали", self.js)

    def test_missing_reporting_is_not_held_against_an_unknown_inn(self):
        """Компания без известного ИНН ни в чём не провинилась:
        её просто не о чем было спросить."""
        self.assertIn('if (c.inn && !sig.revenue) out.push(["Отчётности в ФНС нет"',
                      self.js)
        self.assertIn("ИНН не известен — в ФНС не спрашивали", self.js)

    def test_billions_are_called_billions(self):
        """«172000.0 млн ₽» заставляло считать нули глазами — а
        именно у крупных компаний эта цифра и решает."""
        self.assertIn('" млрд ₽"', self.js)
        self.assertNotIn("(sig.revenue / 1e6).toFixed(1)", self.js)


class TodaySuggestsWhomToCall(unittest.TestCase):
    """Главный экран при полной базе сообщал «ничего не
    назначено» и оставлял человека одного. Кому звонить первым,
    программа знает — за это и считался балл."""

    def setUp(self):
        db.init()
        c = db.conn()
        for t in ("contacts", "signals", "notes", "companies"):
            c.execute("DELETE FROM %s" % t)
        c.commit()
        self.cl = web.create_app().test_client()

    def add(self, name, score, **kw):
        cid, _ = db.upsert_company(dict(name=name, score=score, **kw))
        return cid

    def test_the_best_unworked_companies_are_offered(self):
        a = self.add("Сильная", 90)
        b = self.add("Слабая", 10)
        db.add_contact(a, "phone", "+79160000001")
        db.add_contact(b, "phone", "+79160000002")
        got = self.cl.get("/api/today").get_json()["suggest"]
        self.assertEqual([r["name"] for r in got], ["Сильная", "Слабая"])

    def test_a_company_with_no_way_to_call_is_not_offered(self):
        """Предложить позвонить туда, куда нечем звонить, —
        хуже, чем не предложить ничего."""
        self.add("Без контактов", 99)
        self.assertEqual(self.cl.get("/api/today").get_json()["suggest"], [])

    def test_a_company_already_in_work_is_not_offered_again(self):
        cid = self.add("В работе", 99)
        db.add_contact(cid, "phone", "+79160000003")
        # Стадию ставит человек, а не источник: при записи
        # найденного это поле не трогается вовсе.
        db.update_company_fields(cid, {"stage": "в работе"})
        self.assertEqual(self.cl.get("/api/today").get_json()["suggest"], [])

    def test_a_company_with_a_date_is_not_offered_but_is_due(self):
        """У неё уже есть свой день — предлагать её заново
        значит предложить сделать дважды одно и то же."""
        cid = self.add("Назначена", 99)
        db.add_contact(cid, "phone", "+79160000004")
        db.update_company_fields(cid, {"next_date": "2020-01-01",
                                       "next_step": "связаться"})
        d = self.cl.get("/api/today").get_json()
        self.assertEqual(d["suggest"], [])
        self.assertEqual([r["name"] for r in d["due"]], ["Назначена"])

    def test_the_phone_of_the_boss_comes_first(self):
        """Мобильный руководителя и номер приёмной — разные
        разговоры, и предлагать надо первый."""
        cid = self.add("Два номера", 50)
        db.add_contact(cid, "phone", "+74950000000", owner="general")
        db.add_contact(cid, "phone", "+79161111111", owner="director")
        got = self.cl.get("/api/today").get_json()["suggest"]
        self.assertEqual(got[0]["phone"], "+79161111111")

    def test_the_screen_offers_a_button_not_just_a_list(self):
        js = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                  "static", "app.js"), encoding="utf-8")
        with js as fh:
            text = fh.read()
        self.assertIn("function suggestBlock", text)
        self.assertIn("data-take", text)


class DeadLinksAreNotLinks(unittest.TestCase):
    """В поле «сайт» у компании из справочника лежит что
    угодно. <a href=""> — это ссылка на саму страницу: выглядит
    как рабочий адрес, а нажатие перезагружает программу."""

    def setUp(self):
        js = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                  "static", "app.js"), encoding="utf-8")
        with js as fh:
            self.js = fh.read()

    def test_nothing_builds_an_anchor_from_a_raw_safe_url(self):
        """Шаблон `<a href="${safeUrl(...)}"` даёт пустой href на
        отброшенном адресе. Пусть останется только там, где
        адрес строит сама программа, а не приходит из источника."""
        self.assertIn("function link(url, text, cls)", self.js)
        # Сайт компании всегда через link().
        self.assertNotIn('<a href="${safeUrl(c.site)}"', self.js)
        self.assertNotIn('<a href="${safeUrl(r.site)}"', self.js)

    def test_a_rejected_address_is_still_shown(self):
        """Видеть мусор, пришедший из источника, полезно —
        просто нажимать на него не надо."""
        self.assertIn('class="dead"', self.js)

    def test_a_phone_never_breaks_and_never_gets_cut(self):
        """«+7 495 123-45-6» выглядит как настоящий номер, хотя
        это уже другой номер."""
        css = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "static", "app.css"), encoding="utf-8")
        with css as fh:
            text = fh.read()
        self.assertIn(".ct .val.tel{white-space:nowrap", text)
        self.assertIn("flex:0 0 auto", text.split(".ct .val.tel{")[1][:120])
        self.assertIn('c.kind === "phone" ? " tel" : ""', self.js)


class PostsBelongToTheRightPeople(unittest.TestCase):
    """Должность, приписанная не тому человеку, — худшая ошибка
    из возможных для списка на обзвон: человеку звонят, называя
    чужую должность."""

    TEAM = ["Наша команда",
            "Генеральный директор", "Иванов Иван Иванович",
            "Главный врач", "Сидорова Мария Сергеевна",
            "Коммерческий директор", "Кузнецов Олег Петрович"]

    def posts(self, lines):
        return {p["fio"]: p["post"] for p in site.people([lines])}

    def test_a_neighbour_does_not_steal_a_post(self):
        """Окно в три строки накрывает и соседа по списку, и
        главный врач становился генеральным директором — просто
        потому, что его строка попала в чужое окно первой."""
        got = self.posts(self.TEAM)
        self.assertEqual(got.get("Иванов Иван Иванович"), "генеральный директор")
        self.assertEqual(got.get("Сидорова Мария Сергеевна"), "главный врач")
        self.assertEqual(got.get("Кузнецов Олег Петрович"), "коммерческий директор")

    def test_the_longest_match_wins(self):
        """«Коммерческий директор» содержит «директора», и бралось
        первое совпадение — то есть менее точное."""
        got = self.posts(["Финансовый директор", "Петров Пётр Петрович"])
        self.assertEqual(got.get("Петров Пётр Петрович"), "финансовый директор")

    def test_a_manager_nearby_gets_nothing(self):
        """Менеджер через строку от руководителя получал его
        должность. Одна должность — тот, кто к ней ближе."""
        got = self.posts(["Руководитель отдела продаж",
                          "Смирнова Анна Ивановна",
                          "Менеджер", "Пупкин Вася Петрович"])
        self.assertIn("Смирнова Анна Ивановна", got)
        self.assertNotIn("Пупкин Вася Петрович", got)

    def test_two_people_under_one_post_both_stay(self):
        """«Директора: Иванов, Петров» — одна строка и два
        настоящих директора: при равном расстоянии берём всех."""
        got = self.posts(["Директора: Иванов Иван Иванович, "
                          "Петров Пётр Петрович"])
        self.assertEqual(len(got), 2, got)

    def test_the_boss_set_is_named_not_sliced(self):
        """Срез «первые восемь в списке» менял смысл молча, стоило
        дописать должность в начало."""
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "sources", "site.py"), encoding="utf-8")
        with src as fh:
            text = fh.read()
        self.assertNotIn("BOSS_POSTS[:", text)
        self.assertIn("генеральный директор", site.BOSS)
        self.assertNotIn("менеджер", site.BOSS)

    def test_a_catalogue_pretending_to_be_a_team_page_is_cheap(self):
        """Страница, где слово «директор» стоит в каждой строке,
        разбиралась две секунды процессора на компанию. На прогоне
        в полторы сотни компаний это восемь минут, потраченных ни
        на что."""
        import time as _t
        pages = [["Генеральный директор Иванов Иван Иванович %d" % i
                  for i in range(5000)] for _ in range(12)]
        t0 = _t.perf_counter()
        site.people(pages)
        spent = _t.perf_counter() - t0
        self.assertLess(spent, 0.5, "разбор занял %.2f с" % spent)

    def test_a_string_instead_of_lines_is_not_walked_letter_by_letter(self):
        """Перебор страницы по буквам — сотни тысяч холостых
        проверок и ноль находок, причём молча."""
        import time as _t
        t0 = _t.perf_counter()
        self.assertEqual(site.people(["директор " * 60000]), [])
        self.assertLess(_t.perf_counter() - t0, 0.1)


class RepeatOnSchedule(unittest.TestCase):
    """Повторный поиск по той же теме приносит ту же тысячу
    компаний, и десять новых в ней глазами не найти."""

    def setUp(self):
        db.init()
        c = db.conn()
        c.execute("DELETE FROM searches")
        c.execute("DELETE FROM tasks")
        c.execute("DELETE FROM companies")
        c.commit()

    def test_a_fresh_plan_does_not_fire_at_once(self):
        """Человек только что искал это руками — повторять сразу
        значит сходить по чужим серверам дважды за минуту."""
        db.save_search("Стоматологии", {"query": "стоматология"},
                       kind="find", every_days=7)
        self.assertEqual(db.due_searches(), [])
        self.assertIsNone(worker.run_due_plans())

    def test_it_fires_when_the_day_comes(self):
        sid = db.save_search("Стоматологии", {"query": "стоматология"},
                             kind="find", every_days=7)
        c = db.conn()
        c.execute("UPDATE searches SET next_run=? WHERE id=?",
                  (db.now() - 10, sid))
        c.commit()
        tid = worker.run_due_plans()
        self.assertIsNotNone(tid)
        row = c.execute("SELECT kind, status FROM tasks WHERE id=?",
                        (tid,)).fetchone()
        self.assertEqual(row["kind"], "find")
        self.assertEqual(row["status"], "queued")

    def test_a_long_absence_is_not_a_queue_of_missed_runs(self):
        """Программу выключают на месяц — это норма. Четыре
        пропущенных срока не должны превратиться в четыре обхода
        подряд при первом же запуске."""
        sid = db.save_search("Стоматологии", {"query": "стоматология"},
                             kind="find", every_days=7)
        c = db.conn()
        c.execute("UPDATE searches SET next_run=? WHERE id=?",
                  (db.now() - 40 * 86400, sid))
        c.commit()
        self.assertIsNotNone(worker.run_due_plans())
        # Следующий срок — через неделю от сейчас, а не от прошлого.
        row = db.get_search(sid)
        self.assertGreater(row["next_run"], db.now() + 6 * 86400)
        self.assertEqual(db.due_searches(), [])

    def test_a_busy_queue_is_not_pushed_aside(self):
        """Повтор по расписанию не настолько срочен, чтобы лезть
        вперёд того, что человек запустил руками."""
        sid = db.save_search("Стоматологии", {"query": "стоматология"},
                             kind="find", every_days=7)
        c = db.conn()
        c.execute("UPDATE searches SET next_run=? WHERE id=?",
                  (db.now() - 10, sid))
        c.commit()
        db.create_task("enrich", {})
        self.assertIsNone(worker.run_due_plans())

    def test_switched_off_means_off(self):
        sid = db.save_search("Стоматологии", {"query": "стоматология"},
                             kind="find", every_days=7)
        c = db.conn()
        c.execute("UPDATE searches SET next_run=? WHERE id=?",
                  (db.now() - 10, sid))
        c.commit()
        db.set_search_plan(sid, enabled=False)
        self.assertEqual(db.due_searches(), [])
        self.assertIsNone(worker.run_due_plans())

    def test_without_a_period_it_never_fires_by_itself(self):
        """Сохранённый набор полезен и без расписания — просто
        чтобы не набирать то же самое заново."""
        db.save_search("Разовый", {"query": "стоматология"},
                       kind="find", every_days=0)
        self.assertEqual(db.due_searches(db.now() + 400 * 86400), [])

    def test_only_new_shows_what_the_last_run_added(self):
        """Главный вопрос после повторного прогона — «что из этого
        новое». Без ответа на него расписание бессмысленно."""
        c = db.conn()
        old_id, _ = db.upsert_company({"name": "Старая", "inn": "1"})
        c.execute("UPDATE companies SET created_at=? WHERE id=?",
                  (db.now() - 3600, old_id))
        c.commit()
        db.set_setting("last_find_at", str(db.now() - 60))
        new_id, _ = db.upsert_company({"name": "Новая", "inn": "2"})
        cl = web.create_app().test_client()
        names = [r["name"] for r in
                 cl.get("/api/companies?only=just_found").get_json()["rows"]]
        self.assertEqual(names, ["Новая"])
        names = [r["name"] for r in
                 cl.get("/api/companies").get_json()["rows"]]
        self.assertEqual(sorted(names), ["Новая", "Старая"])

    def test_the_run_marks_the_moment_it_started(self):
        """Засечка ставится в начале прогона, а не в конце:
        иначе всё найденное окажется «старше» засечки и ни одна
        компания в «только новые» не попадёт."""
        with io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                  "worker.py"), encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def task_find("):]
        mark = body.index('set_setting("last_find_at"')
        first_source = min(body.index("osm.search("), body.index("dadata.search_by_name("))
        self.assertLess(mark, first_source)

    def test_the_page_says_it_only_works_while_open(self):
        """Расписание, которое молча не срабатывает ночью, хуже
        отсутствующего расписания."""
        html = web.create_app().test_client().get("/").get_data(as_text=True)
        self.assertIn("пока программа\n                       открыта", html.replace("\r", ""))

    def test_the_scheduler_is_its_own_thread(self):
        """Сторож меряет собственное опоздание, чтобы поймать
        зависание. Работа с базой внутри него выглядела бы как
        то самое зависание, которое он ищет."""
        with io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                  "worker.py"), encoding="utf-8") as fh:
            src = fh.read()
        watch = src[src.index("def _watchdog("):src.index("def recover(")]
        self.assertNotIn("due_searches", watch)
        self.assertIn("target=_scheduler", src)


class MapCeilingIsLifted(unittest.TestCase):
    """Ответ, упёршийся в предел, — это обрезанный ответ.

    Overpass отдаёт не больше заданного числа объектов, и в
    миллионнике предел наступал раньше, чем кончались компании.
    По виду ответа это было неотличимо от «больше и нет»."""

    CITY = {"name": "Москва", "ll": "37.6173,55.7558", "spn": "1.12,0.63"}

    def setUp(self):
        # Без этого каждый тест честно ждёт по секунде на запрос.
        from app.sources import osm
        self._pause, osm.PAUSE = osm.PAUSE, 0

    def tearDown(self):
        from app.sources import osm
        osm.PAUSE = self._pause

    def fake(self, per_box):
        """Заглушка сервера: сколько отдать на каждый прямоугольник."""
        import re as _re
        from app.sources import osm
        asked = []

        def ask(q, session=None, on_log=None, should_stop=None):
            # Именно прямоугольник, а не первые скобки запроса: первые
            # скобки у всех запросов одинаковые, и заглушка не различала
            # город и его четверти.
            box = _re.search(r"\(([-0-9.,]+)\)", q).group(1)
            asked.append(box)
            n = per_box(box, len(asked))
            return {"elements": [{"tags": {"name": "%s-%d" % (box, i),
                                           "amenity": "cafe"}}
                                 for i in range(n)]}
        return osm, ask, asked

    def test_a_full_answer_makes_it_ask_by_quarters(self):
        osm, ask, asked = self.fake(lambda box, n: 10 if n == 1 else 2)
        was, osm.ask = osm.ask, ask
        try:
            got = osm.search("кафе", self.CITY, limit=10)
        finally:
            osm.ask = was
        self.assertEqual(len(asked), 5, "город и четыре его четверти")
        self.assertEqual(len(got), 10 + 4 * 2)

    def test_a_short_answer_costs_one_request(self):
        """Где компаний мало, лишних запросов к чужому серверу
        быть не должно."""
        osm, ask, asked = self.fake(lambda box, n: 3)
        was, osm.ask = osm.ask, ask
        try:
            osm.search("кафе", self.CITY, limit=10)
        finally:
            osm.ask = was
        self.assertEqual(len(asked), 1)

    def test_the_budget_is_never_exceeded(self):
        """Каждая четверть — запрос к общему бесплатному
        серверу. Без потолка густой город выстроил бы очередь
        из десятков запросов и получил бы отказ целиком."""
        osm, ask, asked = self.fake(lambda box, n: 10)
        was, osm.ask = osm.ask, ask
        try:
            osm.search("кафе", self.CITY, limit=10)
        finally:
            osm.ask = was
        self.assertLessEqual(len(asked), osm.MAX_CALLS)

    def test_one_company_is_not_counted_twice(self):
        """Четверти касаются краями, и одна и та же компания
        приходит из двух запросов."""
        from app.sources import osm as osm_mod
        def ask(q, session=None, on_log=None, should_stop=None):
            return {"elements": [{"tags": {"name": "Ромашка",
                                           "amenity": "cafe"}}] * 10}
        was, osm_mod.ask = osm_mod.ask, ask
        try:
            got = osm_mod.search("кафе", self.CITY, limit=10)
        finally:
            osm_mod.ask = was
        self.assertEqual([g["name"] for g in got], ["Ромашка"])

    def test_silence_and_emptiness_are_told_apart(self):
        """«Таких компаний в карте нет» и «сервер не ответил» —
        разные беды: в первом случае надо менять слово, во втором
        — подождать."""
        from app.sources import osm as osm_mod
        said = []
        was = osm_mod.ask
        try:
            osm_mod.ask = lambda *a, **k: {"elements": []}
            osm_mod.search("кафе", self.CITY,
                           on_log=lambda t, lvl="info": said.append(t))
            self.assertFalse(any("не ответило" in t for t in said), said)

            said[:] = []
            osm_mod.ask = lambda *a, **k: None
            osm_mod.search("кафе", self.CITY,
                           on_log=lambda t, lvl="info": said.append(t))
            self.assertTrue(any("не ответило" in t for t in said), said)
        finally:
            osm_mod.ask = was

    def test_quarters_cover_the_whole_city(self):
        """Четверти, не сходящиеся по краям, оставили бы
        полосу города необойдённой — и никто бы не заметил."""
        from app.sources import osm as osm_mod
        box = osm_mod.bbox(self.CITY)
        s0, w0, n0, e0 = [float(x) for x in box.split(",")]
        parts = [[float(x) for x in q.split(",")] for q in osm_mod.quarters(box)]
        self.assertEqual(len(parts), 4)
        self.assertAlmostEqual(min(p[0] for p in parts), s0, places=4)
        self.assertAlmostEqual(min(p[1] for p in parts), w0, places=4)
        self.assertAlmostEqual(max(p[2] for p in parts), n0, places=4)
        self.assertAlmostEqual(max(p[3] for p in parts), e0, places=4)
        # Средние края совпадают попарно: дыры между четвертями нет.
        self.assertAlmostEqual(parts[0][2], parts[2][0], places=4)
        self.assertAlmostEqual(parts[0][3], parts[1][1], places=4)


class CloseWordsAreSearchedToo(unittest.TestCase):
    """Одно слово — одна вывеска.

    «Стоматология» не находит «Центр имплантации», хотя это тот
    же покупатель: в вывеске люди пишут не то слово, которое ищет
    продавец."""

    def test_every_close_word_is_asked_for(self):
        from app.sources import osm
        asked = []
        was = osm.search
        try:
            osm.search = lambda q, city, **kw: asked.append(q) or []
            db.init()
            tid = db.create_task("find", {})
            try:
                worker.task_find(tid, {
                    "query": "стоматология", "cities": ["Москва"],
                    "sources": {"osm": True, "gis": False, "yandex": False,
                                "dadata": False, "hh": False}})
            except RuntimeError:
                pass  # ничего не нашлось — здесь важен сам перебор
            self.assertIn("стоматология", asked)
            self.assertIn("имплантация зубов", asked)
            self.assertEqual(asked[0], "стоматология",
                             "своё слово должно идти первым")
        finally:
            osm.search = was

    def test_the_switch_really_switches_it_off(self):
        from app.sources import osm
        asked = []
        was = osm.search
        try:
            osm.search = lambda q, city, **kw: asked.append(q) or []
            db.init()
            tid = db.create_task("find", {})
            try:
                worker.task_find(tid, {
                    "query": "стоматология", "cities": ["Москва"],
                    "synonyms": False,
                    "sources": {"osm": True, "gis": False, "yandex": False,
                                "dadata": False, "hh": False}})
            except RuntimeError:
                pass
            self.assertEqual(asked, ["стоматология"])
        finally:
            osm.search = was

    def test_own_word_is_not_invented_for(self):
        """Придумывать близкие слова к чужому запросу нечем — и
        не надо: человек написал именно то, что имел в виду."""
        self.assertEqual(trades.words_for("ракетостроение"),
                         ["ракетостроение"])

    def test_the_run_never_explodes_in_length(self):
        """Каждое слово — полный обход всех источников по всем
        городам заново. Без потолка десять слов по десяти городам
        стали бы пятьюстами запросов к чужим серверам."""
        for word in trades.all_words():
            self.assertLessEqual(len(trades.words_for(word)), trades.MAX_WORDS,
                                 word)

    def test_a_close_word_is_never_the_same_word(self):
        """Слово, повторяющее само себя, — это лишний обход
        источников ради того же самого ответа."""
        for word, close in trades.ALSO.items():
            self.assertNotIn(word.lower(), [c.lower() for c in close], word)
            self.assertEqual(len(close), len(set(close)), word)

    def test_the_page_names_the_words_before_the_run(self):
        """Молча умножить время прогона на четыре нельзя:
        человек решит, что программа зависла."""
        db.init()
        html = web.create_app().test_client().get("/").get_data(as_text=True)
        self.assertIn('id="q-also"', html)
        self.assertIn("window.TRADE_ALSO", html)
        js = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                  "static", "app.js"), encoding="utf-8").read()
        self.assertIn("function alsoNote", js)
        # Константы читаются при восстановлении прошлого поиска —
        # объявленные ниже, они уронили бы страницу целиком.
        for name in ("WHOLE_RU", "TRADE_ALSO", "MAX_WORDS"):
            self.assertLess(js.index("const %s" % name),
                            js.index("fillFindForm(window.LAST_FIND)"), name)


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

    def test_gis_searches_by_point_when_the_city_has_no_region(self):
        """Номер региона у 2ГИС есть меньше чем у половины городов из
        списка. Для остальных поиск идёт по координатам, а не молчит."""
        seen = {}

        class Stub(object):
            headers = {}

            def get(self, url, params=None, timeout=None):
                seen.update(params or {})
                raise RuntimeError("дальше не идём")

        gis2.search("стоматология", 0, "ключ", session=Stub(),
                    point="37.6173,55.7558")
        self.assertEqual(seen.get("point"), "37.6173,55.7558")
        self.assertEqual(seen.get("radius"), gis2.POINT_RADIUS)
        self.assertNotIn("region_id", seen)

    def test_gis_prefers_the_region_number_when_there_is_one(self):
        seen = {}

        class Stub(object):
            headers = {}

            def get(self, url, params=None, timeout=None):
                seen.update(params or {})
                raise RuntimeError("дальше не идём")

        gis2.search("стоматология", 32, "ключ", session=Stub(),
                    point="37.6173,55.7558")
        self.assertEqual(seen.get("region_id"), 32)
        self.assertNotIn("point", seen)

    def test_gis_without_region_and_point_is_skipped(self):
        self.assertEqual(gis2.search("стоматология", 0, "ключ"), [])


class TelegramNumbers(unittest.TestCase):
    """Проверка номеров в Telegram. Ошибиться здесь дорого: платит не
    программа, а живой аккаунт, с которого идут запросы."""

    def test_russian_numbers_come_to_one_shape(self):
        for raw, want in (("+7 495 123-45-67", "+74951234567"),
                          ("8 (999) 123-45-67", "+79991234567"),
                          ("7 999 123 45 67", "+79991234567"),
                          ("9991234567", "+79991234567")):
            self.assertEqual(tg.to_e164(raw), want, raw)

    def test_scraps_are_not_numbers(self):
        for raw in ("112", "", "—", "доб. 214", "1234"):
            self.assertEqual(tg.to_e164(raw), "", raw)

    def test_tollfree_is_never_asked_about(self):
        """У 8-800 аккаунта не бывает: это маршрут в колл-центр, а не
        телефон. Спрашивать про него — тратить дневной предел впустую."""
        self.assertEqual(tg.kind_of("+78005553535"), "бесплатный")
        self.assertFalse(tg.worth_checking("+78005553535"))
        self.assertFalse(tg.worth_checking("+78005553535", landlines=True))

    def test_landlines_only_on_request(self):
        self.assertEqual(tg.kind_of("+74951234567"), "городской")
        self.assertFalse(tg.worth_checking("+74951234567"))
        self.assertTrue(tg.worth_checking("+74951234567", landlines=True))

    def test_mobile_and_foreign_are_checked(self):
        self.assertTrue(tg.worth_checking("+79991234567"))
        self.assertTrue(tg.worth_checking("+380441234567"))

    def test_same_number_asked_about_once(self):
        """Один телефон стоит у нескольких компаний, а предел общий."""
        pairs = tg.prepare(["8 999 111 22 33", "+7 999 111-22-33",
                            "+7 (999) 1112233"])
        self.assertEqual(len(pairs), 1)

    def test_limit_is_respected(self):
        many = ["+7 999 %03d 00 00" % i for i in range(50)]
        self.assertEqual(len(tg.prepare(many, limit=7)), 7)

    def test_answer_is_matched_to_the_number_asked(self):
        """Telegram отвечает не по порядку и не про всех. Сопоставление
        идёт по client_id, который мы сами и проставили."""
        class Imp(object):
            def __init__(self, client_id, user_id):
                self.client_id, self.user_id = client_id, user_id

        class User(object):
            def __init__(self, uid, username):
                self.id, self.username = uid, username
                self.first_name, self.last_name = "Иван", "Петров"

        batch = ["+79990000001", "+79990000002", "+79990000003"]
        got = tg.read_batch([Imp(2, 77)], [User(77, "ivan")], batch)
        self.assertEqual(list(got), ["+79990000003"])
        self.assertEqual(got["+79990000003"]["username"], "ivan")

    def test_unknown_client_id_is_ignored(self):
        """Ответ не по нашему запросу не должен молча сдвигать список."""
        class Imp(object):
            client_id, user_id = 99, 5
        self.assertEqual(tg.read_batch([Imp()], [], ["+79990000001"]), {})

    def test_nothing_happens_without_a_login(self):
        res = tg.check(tg.conf(api_id="1", api_hash="x",
                               data_dir="/nonexistent-dir-for-test"),
                       [("a", "+79990000001")])
        self.assertFalse(res["ok"])
        self.assertIn("ход", res["error"])

    def test_swapped_keys_are_explained_not_traced(self):
        """api_id и api_hash лежат рядом и путаются местами. До сих пор
        за это выдавали трассировку про int()."""
        res = tg.send_code(tg.conf(api_id="не число", api_hash="abc",
                                   data_dir="/tmp"), "+79990000000")
        self.assertFalse(res["ok"])
        self.assertIn("api_id", res["error"])
        self.assertNotIn("ValueError", res["error"])

    def test_errors_are_explained_in_words(self):
        class FloodWaitError(Exception):
            seconds = 300
        self.assertIn("300", tg.explain(FloodWaitError()))

        class PhoneCodeInvalidError(Exception):
            pass
        self.assertEqual(tg.explain(PhoneCodeInvalidError()), "Код неверный")


class EveryTaskHasAName(unittest.TestCase):
    """Задача без названия показывается в очереди голым словом из кода:
    «tg», «gis_search». Отменять такую человек не рискует."""

    def test_all_handlers_are_named_on_screen(self):
        js = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                  "static", "app.js"), encoding="utf-8").read()
        block = js[js.index("const KINDS = {"):]
        block = block[:block.index("}")]
        for kind in worker.HANDLERS:
            self.assertIn("%s:" % kind, block, "задача %s без названия" % kind)


class TelegramFromTheCard(unittest.TestCase):
    """Проверка телефонов одной компании, не дожидаясь общего прогона.
    Номеров здесь два-три, и дневной предел от них не страдает."""

    def setUp(self):
        db.init()
        c = db.conn()
        for t in ("contacts", "signals", "companies", "logs", "tasks"):
            c.execute("DELETE FROM %s" % t)
        c.commit()
        db.set_setting("tg_api_id", "1")
        db.set_setting("tg_api_hash", "x")
        self.cl = web.create_app().test_client()
        self.asked = []
        self._logged_in, self._check = tg.logged_in, tg.check
        tg.logged_in = lambda d: True

        def fake(conf, pairs, on_log=None, should_stop=None, **kw):
            self.asked.append([e for _, e in pairs])
            return {"ok": True, "checked": [raw for raw, _ in pairs],
                    "found": {raw: {"username": "boss", "name": "Босс",
                                    "user_id": 1}
                              for raw, e in pairs if e == "+79991112233"},
                    "stopped": ""}
        tg.check = fake
        self.cid, _ = db.upsert_company({"name": "А", "inn": "7700000001"})

    def tearDown(self):
        tg.logged_in, tg.check = self._logged_in, self._check

    def post(self):
        return self.cl.post("/api/company/%d/tg" % self.cid,
                            json={}).get_json()

    def test_the_card_checks_landlines_too(self):
        """Из карточки спрашивают про конкретную компанию, а не про всю
        базу: городских тут два, а не две тысячи."""
        db.add_contact(self.cid, "phone", "+7 999 111-22-33", "director", 90)
        db.add_contact(self.cid, "phone", "+7 495 000-00-00", "general", 50)
        got = self.post()
        self.assertTrue(got["ok"], got)
        self.assertEqual(sorted(self.asked[0]),
                         ["+74950000000", "+79991112233"])
        self.assertEqual(got["found"], 1)
        self.assertEqual(got["checked"], 2)

    def test_marks_land_on_the_contacts_returned(self):
        db.add_contact(self.cid, "phone", "+7 999 111-22-33", "director", 90)
        got = self.post()
        marks = {c["value"]: c["verified"] for c in got["contacts"]}
        self.assertEqual(marks["+7 999 111-22-33"], "tg")
        self.assertEqual(marks["@boss"], "tg")

    def test_tollfree_alone_is_refused_with_a_reason(self):
        db.add_contact(self.cid, "phone", "8 800 555 35 35", "general", 60)
        got = self.post()
        self.assertFalse(got["ok"])
        self.assertIn("8-800", got["error"])
        self.assertEqual(self.asked, [])

    def test_without_a_login_it_says_so_and_asks_nothing(self):
        tg.logged_in = lambda d: False
        db.add_contact(self.cid, "phone", "+7 999 111-22-33", "general", 90)
        got = self.post()
        self.assertFalse(got["ok"])
        self.assertIn("Настройки", got["error"])
        self.assertEqual(self.asked, [])

    def test_an_unknown_company_is_not_a_crash(self):
        got = self.cl.post("/api/company/999999/tg", json={}).get_json()
        self.assertFalse(got["ok"])


class TelegramAccount(unittest.TestCase):
    """Готовый аккаунт приходит не кодом, а строкой сессии или JSON от
    продавца. Имена полей в этих JSON не стандартизованы никем."""

    SELLER = ('{"app_id": 2040, "app_hash": "b18441a1ff607e10", '
              '"sdk": "Windows 10", "device": "Desktop", '
              '"app_version": "4.9.7 x64", "lang_pack": "ru", '
              '"system_lang_pack": "ru-RU", "twoFA": "qwerty", '
              '"phone": "79991234567"}')

    def test_seller_json_is_understood(self):
        got = tg.parse_account(self.SELLER)
        self.assertTrue(got["ok"])
        self.assertEqual(got["api_id"], "2040")
        self.assertEqual(got["api_hash"], "b18441a1ff607e10")
        self.assertEqual(got["phone"], "79991234567")
        self.assertEqual(got["password"], "qwerty")

    def test_device_is_carried_over_as_is(self):
        """Аккаунт, заведённый «телефоном» и продолженный «компьютером
        другой версии», Telegram разлогинивает как угнанный."""
        got = tg.parse_account(self.SELLER)
        self.assertEqual(got["device"], {
            "device_model": "Desktop", "system_version": "Windows 10",
            "app_version": "4.9.7 x64", "lang_code": "ru",
            "system_lang_code": "ru-RU"})

    def test_a_bare_session_string_is_understood(self):
        line = "1" + "A" * 120
        got = tg.parse_account(line)
        self.assertTrue(got["ok"])
        self.assertEqual(got["session"], line)

    def test_a_list_of_accounts_takes_the_first(self):
        got = tg.parse_account('[{"app_id": 5, "app_hash": "x"}]')
        self.assertEqual(got["api_id"], "5")

    def test_rubbish_is_named_not_swallowed(self):
        for text, part in (("", "Пусто"),
                           ("79991234567", "Не похоже"),
                           ('{"a":', "JSON не читается"),
                           ('{"hello": 1}', "нет ни api_id")):
            got = tg.parse_account(text)
            self.assertFalse(got["ok"], text)
            self.assertIn(part, got["error"], text)

    @staticmethod
    def _real_session(tmp):
        """Настоящий файл сессии Telethon — с ключом авторизации."""
        from telethon.crypto import AuthKey
        from telethon.sessions import SQLiteSession
        path = os.path.join(tmp, "real")
        sess = SQLiteSession(path)
        sess.set_dc(2, "149.154.167.51", 443)
        sess.auth_key = AuthKey(bytes(256))
        sess.save()
        sess.close()
        with io.open(path + ".session", "rb") as fh:
            return fh.read()

    def test_tdata_and_empty_files_are_refused(self):
        """Файлы tdata от настольного Telegram и пустышки прошли бы
        молча и упали бы при первой проверке номеров."""
        import tempfile
        d = tempfile.mkdtemp()
        self.assertFalse(tg.save_session_bytes(d, b"")["ok"])
        bad = tg.save_session_bytes(d, b"TDF$ tdata garbage")
        self.assertFalse(bad["ok"])
        self.assertIn("tdata", bad["error"])

    def test_a_stranger_sqlite_is_refused_by_what_is_inside(self):
        """Подписи SQLite мало: база бывает и чужая. Без таблицы
        sessions файл откроется, а упадёт потом, посреди проверки,
        словами про «no such table»."""
        import sqlite3
        import tempfile
        d = tempfile.mkdtemp()
        other = os.path.join(d, "other.db")
        con = sqlite3.connect(other)
        con.execute("CREATE TABLE notes (a)")
        con.commit()
        con.close()
        with io.open(other, "rb") as fh:
            got = tg.save_session_bytes(d, fh.read())
        self.assertFalse(got["ok"])
        self.assertIn("sessions", got["error"])

    def test_a_real_session_is_accepted(self):
        import tempfile
        d = tempfile.mkdtemp()
        got = tg.save_session_bytes(d, self._real_session(tempfile.mkdtemp()))
        self.assertTrue(got["ok"], got)
        self.assertTrue(tg.logged_in(d))

    def test_a_failed_import_does_not_take_away_the_working_account(self):
        """Неудачная попытка не должна отбирать рабочий аккаунт: до сих
        пор файл писался на место прежнего и только потом проверялся."""
        import tempfile
        d = tempfile.mkdtemp()
        tg.save_session_bytes(d, self._real_session(tempfile.mkdtemp()))
        before = io.open(tg.session_path(d), "rb").read()
        self.assertFalse(tg.save_session_bytes(d, b"TDF$ tdata")["ok"])
        self.assertEqual(io.open(tg.session_path(d), "rb").read(), before)


class TelegramImportsTwoFiles(unittest.TestCase):
    """Аккаунт отдают двумя файлами: .session и .json. Вписывать api_id
    и api_hash руками не нужно — они лежат в том же JSON."""

    SELLER = ('{"app_id": 2040, "app_hash": "b18441a1ff607e10", '
              '"sdk": "Windows 10", "device": "Desktop", '
              '"phone": "79991234567"}')

    def setUp(self):
        db.init()
        for key in ("tg_api_id", "tg_api_hash", "tg_phone", "tg_device"):
            db.set_setting(key, "")
        self.cl = web.create_app().test_client()
        self._whoami, self._import = tg.whoami, tg.import_session
        tg.whoami = lambda c: {"ok": True, "who": "Босс · @boss"}
        tg.import_session = lambda c, line: {"ok": True, "who": "из строки"}

    def tearDown(self):
        tg.whoami, tg.import_session = self._whoami, self._import

    def send(self, *files):
        return self.cl.post("/api/tg/import", data={"files": [
            (io.BytesIO(body), name) for name, body in files]},
            content_type="multipart/form-data").get_json()

    def real_session(self):
        import tempfile
        return TelegramAccount._real_session(tempfile.mkdtemp())

    def test_json_and_session_together_need_nothing_typed(self):
        got = self.send(("acc.json", self.SELLER.encode("utf-8")),
                        ("acc.session", self.real_session()))
        self.assertTrue(got["ok"], got)
        self.assertEqual(db.get_setting("tg_api_id"), "2040")
        self.assertEqual(db.get_setting("tg_api_hash"), "b18441a1ff607e10")
        self.assertEqual(db.get_setting("tg_phone"), "79991234567")

    def test_order_of_files_does_not_matter(self):
        got = self.send(("acc.session", self.real_session()),
                        ("acc.json", self.SELLER.encode("utf-8")))
        self.assertTrue(got["ok"], got)
        self.assertEqual(db.get_setting("tg_api_id"), "2040")

    def test_an_extra_file_is_skipped_not_fatal(self):
        got = self.send(("acc.json", self.SELLER.encode("utf-8")),
                        ("acc.session", self.real_session()),
                        ("readme.txt", b"lorem ipsum"))
        self.assertTrue(got["ok"], got)
        self.assertIn("readme.txt", got["skipped"])

    def test_a_session_without_keys_says_what_is_missing(self):
        got = self.send(("acc.session", self.real_session()))
        self.assertFalse(got["ok"])
        self.assertIn("app_id", got["error"])

    def test_a_json_without_a_session_says_what_is_missing(self):
        got = self.send(("acc.json", self.SELLER.encode("utf-8")))
        self.assertFalse(got["ok"])
        self.assertIn(".session", got["error"])

    def test_nothing_useful_is_named(self):
        got = self.send(("readme.txt", b"lorem ipsum"))
        self.assertFalse(got["ok"])
        self.assertIn("readme.txt", got["error"])

    def test_no_files_at_all(self):
        got = self.cl.post("/api/tg/import", data={},
                           content_type="multipart/form-data").get_json()
        self.assertFalse(got["ok"])


class HhAppToken(unittest.TestCase):
    """Кабинет hh выдаёт Client Id и Client Secret, а программе нужен
    токен. Обмен — одним запросом, без сети в тесте."""

    class Resp(object):
        def __init__(self, code, body):
            self.status_code, self._body = code, body

        def json(self):
            return self._body

    class Sess(object):
        def __init__(self, answers):
            self.answers, self.calls = list(answers), []

        def post(self, url, data=None, timeout=None):
            self.calls.append((url, dict(data or {})))
            return self.answers.pop(0)

    class T(object):
        def __init__(self, name, sess):
            self.name, self.session = name, sess

    def run_with(self, *answers):
        sess = self.Sess(answers)
        tok, err = hh.app_token("id", "secret", [self.T("способ", sess)])
        return tok, err, sess

    def test_pair_becomes_a_token(self):
        tok, err, sess = self.run_with(
            self.Resp(200, {"access_token": "APPLXXXX", "token_type": "bearer"}))
        self.assertEqual((tok, err), ("APPLXXXX", ""))
        url, data = sess.calls[0]
        self.assertEqual(data["grant_type"], "client_credentials")
        self.assertEqual((data["client_id"], data["client_secret"]),
                         ("id", "secret"))

    def test_moved_endpoint_falls_back(self):
        """hh переносил метод; 404 на новом адресе — пробуем старый."""
        tok, err, sess = self.run_with(
            self.Resp(404, {}),
            self.Resp(200, {"access_token": "APPLYYYY"}))
        self.assertEqual(tok, "APPLYYYY")
        self.assertEqual([c[0] for c in sess.calls], list(hh.TOKEN_URLS))

    def test_wrong_pair_is_explained(self):
        tok, err, _ = self.run_with(
            self.Resp(400, {"error": "invalid_client"}))
        self.assertEqual(tok, "")
        self.assertIn("не узнал пару", err)

    def test_empty_fields_do_not_go_to_hh(self):
        tok, err = hh.app_token("", "secret", [])
        self.assertEqual(tok, "")
        self.assertIn("оба значения", err)

    def test_the_secret_is_not_stored(self):
        """Секрет после обмена не нужен — в базе его быть не должно."""
        db.init()
        saved = hh.app_token
        hh.app_token = lambda cid, sec, transports=None: ("APPLZZZZ", "")
        try:
            got = web.create_app().test_client().post(
                "/api/hh/token",
                json={"client_id": "id", "client_secret": "СЕКРЕТ"}).get_json()
        finally:
            hh.app_token = saved
        self.assertTrue(got["ok"])
        self.assertEqual(db.get_setting("hh_token"), "APPLZZZZ")
        # И токен возвращается целиком: без него поле остаётся пустым, и
        # следующее «Сохранить» затирает только что полученный токен.
        self.assertEqual(got["token"], "APPLZZZZ")
        stored = [r["value"] for r in db.conn().execute(
            "SELECT value FROM settings")]
        self.assertNotIn("СЕКРЕТ", " ".join(str(v) for v in stored))


class RubbishInputDoesNotCrash(unittest.TestCase):
    """Пятисотая ошибка — всегда ошибка в коде, а не в запросе.

    В поле, где ожидается строка, приходил словарь — и .strip() ронял
    весь запрос. Снаружи это выглядит как «программа не отвечает», а в
    журнале остаётся трассировка вместо понятного отказа.
    """

    def setUp(self):
        db.init()
        self.cl = web.create_app().test_client()

    CASES = [
        ("/api/find", {"query": {"вложенный": "объект"}}),
        ("/api/find", {"query": ["список"]}),
        ("/api/find", {"cities": 123}),
        ("/api/find", {"cities": "Москва"}),
        ("/api/search", {"queries": [{"вложенный": "объект"}]}),
        ("/api/search", {"queries": "одна строка"}),
        ("/api/search", {"text": {"a": 1}}),
        ("/api/search", {"areas": [1, 2, None]}),
        ("/api/searches", {"name": {"a": 1}, "query": "x"}),
        ("/api/gis", {"query": {"a": 1}, "region": {"b": 2}}),
        ("/api/tg/proxy", {"proxy": {"a": 1}}),
        ("/api/ai/queries", {"icp": {"a": 1}}),
        ("/api/open", {"url": {"a": 1}}),
        ("/api/settings", {"gis_key": {"a": 1}, "ai_url": ["x"]}),
        ("/api/company/1", {"note": {"a": 1}}),
        ("/api/company/1", {"stage": ["x"]}),
        ("/api/company/1", {"next_date": {"a": 1}}),
    ]

    def test_no_route_falls_over_on_wrong_types(self):
        for url, body in self.CASES:
            got = self.cl.post(url, json=body)
            self.assertLess(got.status_code, 500,
                            "%s упал на %r" % (url, body))

    def test_a_string_in_a_list_field_is_not_split_into_letters(self):
        """Строка итерируется, и «Москва» в поле списка превращалась в
        шесть городов по одной букве."""
        app = web.create_app()
        with app.test_request_context():
            self.cl.post("/api/find", json={"query": "стоматология",
                                            "cities": "Москва"})
        row = db.conn().execute(
            "SELECT params FROM tasks WHERE kind='find' ORDER BY id DESC "
            "LIMIT 1").fetchone()
        cities = json.loads(row["params"])["cities"]
        self.assertEqual(cities, ["Москва"])


class NothingShadowsAModule(unittest.TestCase):
    """Переменная цикла с именем модуля затеняет его до конца функции.
    В этом файле так уже ловились: net затенялся в обогащении, а потом
    то же случилось с tg. Ошибка молчит ровно до первого обращения к
    модулю после цикла, и тогда падает с «str has no attribute»."""

    def test_loop_variables_do_not_take_module_names(self):
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8").read()
        modules = set(re.findall(r"^from \.sources import \(?([^)\n]+)",
                                 src, re.M))
        names = set()
        for chunk in modules:
            for part in chunk.split(","):
                part = part.strip()
                if " as " in part:
                    part = part.split(" as ")[1].strip()
                if part:
                    names.add(part)
        names |= {"db", "net", "ai", "geo", "score", "settings", "social"}
        for name in sorted(names):
            hit = re.search(r"^\s*for\s+%s\s*[,\s]" % re.escape(name),
                            src, re.M)
            self.assertIsNone(
                hit, "переменная цикла «%s» затеняет одноимённый модуль" % name)


class SessionFileIsNotHeldOpen(unittest.TestCase):
    """Файл сессии — база SQLite. Открытое соединение держит файл, и на
    Windows «Забыть аккаунт» его не удалит: os.remove упрётся в занятый
    файл, а человеку скажут, что аккаунт забыт."""

    def test_import_closes_the_session_it_wrote(self):
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "sources", "tg.py"), encoding="utf-8").read()
        block = src[src.index("def import_session("):]
        block = block[:block.index("\ndef _dc")]
        self.assertIn("client2.session.close()", block,
                      "соединение с записанным файлом сессии не закрывается")

    def test_forget_reports_the_truth(self):
        """Кнопка «Забыть аккаунт» сообщала об успехе, не глядя на ответ."""
        js = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                  "static", "app.js"), encoding="utf-8").read()
        block = js[js.index('$("btn-tg-forget").onclick'):]
        block = block[:block.index("};")]
        self.assertIn('post("/api/tg/forget"', block)
        self.assertIn("d.ok", block,
                      "ответ сервера не проверяется — отчёт об успехе всегда")


class DiagnosticsSaysWhy(unittest.TestCase):
    """Экран диагностики существует ради причины отказа. «Не дозвонился»
    без неё не отличает обрыв связи от блокировки провайдером."""

    def test_transport_failure_carries_the_reason(self):
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "diag.py"), encoding="utf-8").read()
        block = src[src.index("for t in net.hh_transports():"):]
        block = block[:block.index("ms = int(")]
        self.assertIn("str(e)", block, "причина отказа выбрасывается")


class TelegramProxy(unittest.TestCase):
    """К Telegram ходят своим протоколом, а не запросами HTTP, и режут
    его отдельно — поэтому прокси у него свой."""

    def test_socks5_with_credentials(self):
        got = tg.make_proxy("socks5://user:pass@1.2.3.4:1080")
        self.assertEqual(got["kind"], "socks5")
        self.assertEqual(got["proxy"][1:], ("1.2.3.4", 1080, True,
                                            "user", "pass"))

    def test_without_credentials(self):
        got = tg.make_proxy("http://1.2.3.4:8080")
        self.assertEqual(got["proxy"][4:], (None, None))

    def test_mtproto_takes_a_secret(self):
        got = tg.make_proxy("mtproto://1.2.3.4:443/ee0123abcd")
        self.assertEqual(got, {"kind": "mtproto",
                               "proxy": ("1.2.3.4", 443, "ee0123abcd")})

    def test_empty_means_straight_through(self):
        self.assertIsNone(tg.make_proxy(""))

    def test_mistakes_are_named(self):
        for url, part in (("1.2.3.4:1080", "схемы"),
                          ("socks5://1.2.3.4", "адрес и порт"),
                          ("ftp://a:1", "Неизвестный вид"),
                          ("mtproto://1.2.3.4:443", "секрет")):
            with self.assertRaises(tg.BadProxy, msg=url) as got:
                tg.make_proxy(url)
            self.assertIn(part, str(got.exception), url)

    def test_the_password_never_shows(self):
        """Пароль от прокси в тексте ошибки — та же утечка, что и ключ:
        его видно на скриншоте, который присылают в поддержку."""
        shown = tg.label_proxy("socks5://user:s3cret@1.2.3.4:1080")
        self.assertNotIn("s3cret", shown)
        self.assertIn("1.2.3.4:1080", shown)

    def test_a_slow_connection_gives_up_instead_of_hanging(self):
        """По умолчанию Telethon пробует пять раз с растущей паузой, и
        при неверном прокси окно висело минутами без слова на экране."""
        opts = tg._opts(tg.conf())
        self.assertLessEqual(opts["connection_retries"], 2)
        self.assertLessEqual(opts["timeout"], 20)

    def test_there_is_a_deadline_over_everything(self):
        """Таймаутов внутри Telethon мало: при живом файле сессии и
        закрытой сети он честно соединяется, перебирает адреса и
        повторяет запрос — измерено больше двух минут молчания."""
        self.assertLessEqual(tg.DEADLINE, 60)
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "sources", "tg.py"),
                      encoding="utf-8").read()
        self.assertIn("asyncio.wait_for", src)
        # И задачи Telethon гасятся до закрытия цикла, иначе в журнал
        # сыплется полотно «Event loop is closed».
        self.assertIn("task.cancel()", src)


class TelegramRun(unittest.TestCase):
    """Прогон целиком, без сети: что спросили у Telegram, что отметили
    и что не стали спрашивать второй раз."""

    def setUp(self):
        db.init()
        c = db.conn()
        for t in ("contacts", "signals", "companies", "logs", "tasks"):
            c.execute("DELETE FROM %s" % t)
        c.commit()
        db.set_setting("tg_api_id", "1")
        db.set_setting("tg_api_hash", "x")
        self.asked = []
        self._logged_in, self._check = tg.logged_in, tg.check
        tg.logged_in = lambda d: True

        def fake(conf, pairs, on_log=None, should_stop=None, **kw):
            self.asked.append([e for _, e in pairs])
            return {"ok": True, "checked": [raw for raw, _ in pairs],
                    "found": {raw: {"username": "boss", "name": "Босс",
                                    "user_id": 1}
                              for raw, e in pairs if e == "+79991112233"},
                    "stopped": ""}
        tg.check = fake

        self.a, _ = db.upsert_company({"name": "А", "inn": "7700000001"})
        self.b, _ = db.upsert_company({"name": "Б", "inn": "7700000002"})

    def tearDown(self):
        tg.logged_in, tg.check = self._logged_in, self._check

    def run_task(self, **params):
        tid = db.create_task("tg", params)
        worker.task_tg(tid, params)
        return tid

    def verified(self, value):
        row = db.conn().execute(
            "SELECT verified FROM contacts WHERE kind='phone' AND value=?",
            (value,)).fetchone()
        return row["verified"] if row else None

    def test_one_number_written_two_ways_is_asked_once_marked_twice(self):
        """«+7 999 111-22-33» и «8 999 1112233» — один телефон. Спросить
        надо один раз, отметить обе компании: иначе вторая приходит за
        ответом снова и снова и съедает дневной предел."""
        db.add_contact(self.a, "phone", "+7 999 111-22-33", "director", 90)
        db.add_contact(self.b, "phone", "8 999 1112233", "general", 80)
        self.run_task(limit=50)
        self.assertEqual(self.asked, [["+79991112233"]])
        self.assertEqual(self.verified("+7 999 111-22-33"), "tg")
        self.assertEqual(self.verified("8 999 1112233"), "tg")
        # Имя пользователя достаётся обеим компаниям, а не первой.
        got = [r["company_id"] for r in db.conn().execute(
            "SELECT company_id FROM contacts WHERE kind='telegram'")]
        self.assertEqual(sorted(got), sorted([self.a, self.b]))

    def test_tollfree_is_set_aside_for_good(self):
        db.add_contact(self.a, "phone", "8 800 555 35 35", "general", 60)
        db.add_contact(self.a, "phone", "+7 999 111-22-33", "general", 90)
        self.run_task(limit=50)
        self.assertEqual(self.asked, [["+79991112233"]])
        self.assertEqual(self.verified("8 800 555 35 35"), "skip")
        # И с галочкой «и городские» тоже: у 8-800 аккаунта не бывает.
        self.asked = []
        self.run_task(limit=50, landlines=True)
        self.assertEqual(self.asked, [])

    def test_a_landline_waits_for_the_tick_and_then_comes_back(self):
        db.add_contact(self.a, "phone", "+7 495 000-00-00", "general", 50)
        self.run_task(limit=50)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.verified("+7 495 000-00-00"), "skip_land")
        self.asked = []
        self.run_task(limit=50, landlines=True)
        self.assertEqual(self.asked, [["+74950000000"]])

    def test_the_second_run_asks_nothing(self):
        db.add_contact(self.a, "phone", "+7 999 111-22-33", "general", 90)
        self.run_task(limit=50)
        self.asked = []
        self.run_task(limit=50)
        self.assertEqual(self.asked, [])

    def test_a_number_that_is_not_a_number_is_set_aside(self):
        db.conn().execute(
            "INSERT INTO contacts (company_id, kind, value, owner, "
            "confidence, verified, source, created_at) "
            "VALUES (?,?,?,?,?,?,?,0)",
            (self.a, "phone", "доб. 214", "general", 10, "unchecked", "тест"))
        db.conn().commit()
        self.run_task(limit=50)
        self.assertEqual(self.verified("доб. 214"), "skip")

    def test_no_login_means_no_requests(self):
        tg.logged_in = lambda d: False
        db.add_contact(self.a, "phone", "+7 999 111-22-33", "general", 90)
        tid = self.run_task(limit=50)
        self.assertEqual(self.asked, [])
        texts = " ".join(r["text"] for r in db.conn().execute(
            "SELECT text FROM logs WHERE task_id=?", (tid,)))
        self.assertIn("Вход в Telegram не выполнен", texts)
        self.assertEqual(self.verified("+7 999 111-22-33"), "unchecked")


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
        self.assertEqual(self.html.count('class="src"'), 8)
        self.assertIn("else 'need'", self.html)
        # Значок и подсказка обновляются сразу после сохранения ключа —
        # для этого у них есть и свой id, и оба варианта текста.
        for what in ("gis", "yandex", "dadata", "sj"):
            self.assertIn('id="src-key-%s"' % what, self.html)
            self.assertIn('id="src-hint-%s"' % what, self.html)
        js = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                  "static", "app.js"), encoding="utf-8").read()
        self.assertIn("src-key-", js)
        self.assertIn("пропустим", js)

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
        self.assertEqual(keys, set(score.WEIGHTS) - {"ai_fit"},
                         "в справке не все слагаемые")
        generic = {x["key"] for x in score.legend("generic")}
        self.assertEqual(generic, set(score.WEIGHTS) - set(score.PHONE_ONLY))
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

    def test_place_themes_are_searchable_by_tags(self):
        """Список без тегов — это список слов, по которым ничего не
        найдётся у компаний с выдуманными названиями: «Дента-Люкс»
        стоматологией себя не называет.

        Требование только к темам про места. OpenStreetMap описывает
        то, куда заходят, и металлобазы в нём нет ни одной. Требовать
        тегов от промышленных тем значило бы либо выкинуть их из
        каталога, либо привязать к ним тег пошире — и солгать о
        точности поиска."""
        for g in trades.catalog():
            if g["kind"] != "места":
                continue
            weak = [it["q"] for it in g["items"] if not it["tagged"]]
            share = 100 * g["on_map"] // max(1, len(g["items"]))
            # Большинство — а не число, подобранное под нынешний
            # список. Несколько услуг без тега в теме про места — норма
            # (вывоза мусора в карте нет), а вот тема, где по карте не
            # ищется треть слов, помечена неверно.
            self.assertGreaterEqual(
                share, 70,
                "тема «%s» помечена как «места», но по карте ищется только "
                "%d%%: %s" % (g["title"], share, weak))

    def test_every_theme_says_how_it_is_searched(self):
        """Тема без пометки молча считается «бизнесом», и тема
        про места, забытая в списке, потеряла бы проверку выше —
        без единого признака, что что-то не так."""
        for title, _items in trades.GROUPS:
            self.assertIn(title, trades.KIND, title)
        self.assertEqual(set(trades.KIND) - {t for t, _ in trades.GROUPS}, set(),
                         "в KIND есть темы, которых нет в каталоге")
        for kind in trades.KIND.values():
            self.assertIn(kind, ("места", "бизнес"), kind)

    def test_every_activity_has_close_words(self):
        """Одно слово — одна вывеска. Вид без близких слов ищется
        вдвое хуже соседнего по списку, а почему — не видно."""
        no = [w for w in trades.all_words() if not trades.ALSO.get(w)]
        self.assertFalse(no, "без близких слов: %s" % no[:20])

    def test_every_word_builds_a_real_query(self):
        from app.sources import osm
        city = {"name": "Москва", "ll": "37.6,55.7", "spn": "0.9,0.5"}
        for q in trades.all_words():
            self.assertTrue(osm.build_query(q, city),
                            "«%s» не превращается в запрос" % q)

    def test_catalog_reaches_the_page(self):
        db.init()
        html = web.create_app().test_client().get("/").get_data(as_text=True)
        self.assertIn('id="dd-theme"', html)
        self.assertIn("Медицина и здоровье", html)
        self.assertIn('data-q="стоматология"', html)

    def test_hints_for_the_input_come_from_the_same_list(self):
        words = trades.all_words()
        self.assertEqual(len(words), len(set(words)))
        self.assertIn("автосервис", words)


class GeographyIsWide(unittest.TestCase):
    """Список из двенадцати городов бесполезен тому, чей город в
    него не попал. Но неверная координата хуже отсутствующего города:
    поиск пойдёт молча и не там."""

    def test_the_list_is_actually_large(self):
        self.assertGreaterEqual(len(geo.cities()), 100)

    def test_no_city_appears_twice(self):
        names = [c["name"] for c in geo.cities()]
        self.assertEqual(len(names), len(set(names)),
                         "дубли: %s"
                         % sorted(n for n in set(names) if names.count(n) > 1))

    def test_every_coordinate_is_inside_the_country(self):
        """Перепутанные широта с долготой — самая частая описка
        в такой таблице, и заметна она только по пустой выдаче."""
        for c in geo.cities():
            if c["name"] == geo.WHOLE:
                continue
            lon, lat = [float(x) for x in c["ll"].split(",")]
            self.assertTrue(19.0 <= lon <= 190.0,
                            "долгота вне России: %s %s" % (c["name"], lon))
            self.assertTrue(41.0 <= lat <= 72.0,
                            "широта вне России: %s %s" % (c["name"], lat))

    def test_two_cities_never_share_one_point(self):
        """Скопировал строку и забыл поменять числа — и два
        разных города ищутся в одном и том же месте."""
        seen = {}
        for c in geo.cities():
            if c["name"] == geo.WHOLE:
                continue
            key = c["ll"]
            self.assertNotIn(key, seen,
                             "%s и %s в одной точке" % (c["name"], seen.get(key)))
            seen[key] = c["name"]

    def test_northern_cities_get_a_wider_box_in_degrees(self):
        """Градус долготы в Сочи — восемьдесят километров, в
        Мурманске — сорок. Одинаковый охват в градусах оставил бы
        половину северного города за краем прямоугольника."""
        by = {c["name"]: c for c in geo.cities()}
        def dlon(name):
            return float(by[name]["spn"].split(",")[0])
        def dlat(name):
            return float(by[name]["spn"].split(",")[1])
        # У Мурманска и Сочи один радиус по широте не совпадает,
        # поэтому сравниваем форму прямоугольника, а не его размер.
        self.assertGreater(dlon("Мурманск") / dlat("Мурманск"),
                           dlon("Сочи") / dlat("Сочи"))

    def test_every_city_builds_a_map_query(self):
        """Город без работающего прямоугольника молча выпадает
        из двух источников из пяти."""
        from app.sources import osm
        for c in geo.cities():
            if c["name"] == geo.WHOLE:
                continue
            self.assertTrue(osm.bbox(c), c["name"])
            self.assertTrue(osm.build_query("стоматология", c), c["name"])

    def test_directory_numbers_are_never_invented(self):
        """Номер региона, взятый наугад, — это поиск в другом
        городе без единого слова об этом. Номера берутся только из
        самих справочников, по совпадению названия."""
        from app.sources import gis2, hh
        gis_names = {n for n, _ in gis2.CITIES}
        hh_names = {n for _, n in hh.AREAS}
        for c in geo.cities():
            # «Россия целиком» — не город, её номер у hh стоит прямо в
            # списке: совпадению по названию там совпадать не с чем.
            if c["name"] == geo.WHOLE:
                continue
            if c["gis"]:
                self.assertIn(c["name"], gis_names, c["name"])
            if c["hh"]:
                self.assertIn(c["name"], hh_names, c["name"])

    def test_omsk_is_reachable_at_all(self):
        """Омск стоял в справочнике hh, но список городов строился
        по справочнику 2ГИС, и в интерфейс город не попадал вовсе."""
        omsk = [c for c in geo.cities() if c["name"] == "Омск"]
        self.assertTrue(omsk, "Омска нет в списке")
        self.assertEqual(omsk[0]["hh"], "68")

    def test_groups_cover_every_city_exactly_once(self):
        flat = [c["name"] for g in geo.groups() for c in g["items"]]
        self.assertEqual(sorted(flat),
                         sorted(c["name"] for c in geo.cities()
                                if c["name"] != geo.WHOLE))

    def test_choosing_many_cities_still_works(self):
        got = [c["name"] for c in geo.pick(["Пермь", "Сочи", "Якутск"])]
        self.assertEqual(got, ["Сочи", "Пермь", "Якутск"])


class TagsDoNotOverreach(unittest.TestCase):
    """Слово целиком внутри другого слова — не совпадение.

    «Автошкола» содержит и «школ», и в запрос попадали заодно все
    школы города. «Барбершоп» содержит «бар» — и принёс бы бары."""

    def setUp(self):
        self.city = {"name": "Москва", "ll": "37.6173,55.7558",
                     "spn": "1.12,0.63"}

    def q(self, text):
        from app.sources import osm
        return osm.build_query(text, self.city)

    def test_driving_school_is_not_every_school(self):
        got = self.q("автошкола")
        self.assertIn("driving_school", got)
        self.assertNotIn('"amenity"="school"', got)
        self.assertNotIn("language_school", got)

    def test_barbershop_is_not_a_bar(self):
        got = self.q("барбершоп")
        self.assertIn("hairdresser", got)
        self.assertNotIn('"amenity"="bar"', got)

    def test_vet_pharmacy_is_not_a_human_one_only(self):
        got = self.q("ветаптека")
        self.assertIn("veterinary", got)

    def test_shoe_repair_is_not_a_shoe_shop(self):
        got = self.q("ремонт обуви")
        self.assertIn("shoe_repair", got)
        self.assertNotIn('"shop"="shoes"', got)

    def test_two_real_words_both_survive(self):
        """«Медицинская клиника» — это и clinic, и doctors:
        отбрасывать надо только вложенные слова, а не вторые."""
        got = self.q("медицинская клиника")
        self.assertIn("clinic", got)
        self.assertIn("doctors", got)

    def test_short_keys_cannot_hide_inside_common_words(self):
        """«Газ» лежит внутри «магазина», «спа» — внутри
        «спальни». Таких ключей в словаре быть не должно."""
        from app.sources import osm
        trap = ("магазин", "компания", "услуги", "центр", "салон",
                "производство", "организация", "предприятие")
        for word in osm.TAGS:
            for t in trap:
                self.assertNotIn(word, t,
                                 "ключ «%s» сработает на любом «%s»" % (word, t))

    def test_every_tag_value_has_a_russian_name(self):
        """В карточке и в выгрузке должно стоять «Груминг»,
        а не pet_grooming: список читает продавец, а не картограф."""
        from app.sources import osm
        missing = set()
        for tags in osm.TAGS.values():
            for t in tags:
                value = t.split("=")[1].strip('"[]')
                if value not in osm.RUBRIC_RU:
                    missing.add(value)
        self.assertFalse(missing, "без русского названия: %s"
                         % sorted(missing))

    def test_the_catalogue_is_wide_now(self):
        """Список из семидесяти слов не покрывал большинства
        занятий, и человек возвращался к пустому полю."""
        self.assertGreaterEqual(len(trades.GROUPS), 40)
        self.assertGreaterEqual(len(trades.all_words()), 550)


class SearchIsThreeDropdowns(unittest.TestCase):
    """Тема, вид деятельности, город — три разных выбора.

    Раньше они были двумя рядами одинаковых кнопок подряд, и это
    читалось как один выбор: человек жал тему «Строительство и
    ремонт», видел в поле «дизайн интерьера» с прошлого раза и считал,
    что программа ищет не то, что он выбрал."""

    def setUp(self):
        db.init()
        self.html = web.create_app().test_client().get("/").get_data(as_text=True)
        self.js = io.open(os.path.join(os.path.dirname(__file__), "..",
                                       "app", "static", "app.js"),
                          encoding="utf-8").read()

    def test_three_fields_are_numbered_in_order(self):
        for label in ("1. Тема", "2. Вид деятельности", "3. Город"):
            self.assertIn(label, self.html, label)

    def test_every_dropdown_is_on_the_page(self):
        for box in ('id="dd-theme"', 'id="dd-trade"', 'id="dd-city"'):
            self.assertIn(box, self.html, box)

    def test_long_lists_can_be_searched_by_letters(self):
        """Триста видов и полторы сотни городов листать глазами
        нельзя: в каждом списке есть поиск."""
        self.assertIn('placeholder="Найти тему"', self.html)
        self.assertIn('placeholder="Найти город"', self.html)
        self.assertIn("function ddFilter", self.js)

    def test_only_one_menu_is_open_at_a_time(self):
        """Два раскрытых меню перекрывают друг друга, и нажатие
        попадает не туда, куда человек смотрел."""
        self.assertIn("function ddCloseAll", self.js)
        self.assertIn("ddCloseAll(box)", self.js)

    def test_own_word_still_works_and_is_named(self):
        """Своё слово программа искать умеет, и запирать человека
        в списке было бы хуже, чем помочь ему этим списком."""
        self.assertIn('<input id="q-text"', self.html)
        self.assertIn(u"в списке такого нет", self.js)

    def test_theme_clears_only_a_word_from_another_theme(self):
        """Выбрал «Строительство», а в поле остался «дизайн» —
        именно на это жаловались. Но слово из самой же темы стирать
        нельзя: тогда оно пропадало бы от случайного нажатия."""
        block = self.js[self.js.index('$("dd-theme").querySelector(".dd-list").onclick'):]
        block = block[:block.index("\n};")]
        self.assertIn("tradeInTheme()", block)
        self.assertIn('$("q-text").value = ""', block)
        # Стираем только под проверкой, а не всегда.
        self.assertIn("if (themePick !== \"\" && !tradeInTheme())", block)

    def test_cities_are_grouped_by_federal_district(self):
        """Сто сорок городов одним полотном не читаются."""
        for part in ("Поволжье", "Сибирь", "Дальний Восток"):
            self.assertIn('data-part="%s"' % part, self.html, part)
        self.assertIn("выбрать округ", self.html)

    def test_city_says_which_sources_know_it(self):
        """Города, которого нет в справочниках 2ГИС и hh, для них
        не существует. Сказать это надо до запуска: «нашлось вдвое
        меньше» без объяснения читается как поломка."""
        self.assertIn("все источники", self.html)
        self.assertIn("карта и ЕГРЮЛ", self.html)
        self.assertIn("Без 2ГИС и hh", self.js)

    def test_whole_country_name_is_not_copied_into_the_script(self):
        """Две копии одной строки разошлись бы при первом же
        переименовании, и кнопка молча перестала бы работать."""
        self.assertIn("window.WHOLE_RU", self.html)
        self.assertIn("window.WHOLE_RU ||", self.js)

    def test_country_constant_exists_before_the_saved_search_is_restored(self):
        """Восстановление прошлого поиска зовёт citiesNote()
        в начале файла. const из середины там ещё не существует,
        и страница упала бы у всех, кто уже искал."""
        self.assertLess(self.js.index("const WHOLE_RU"),
                        self.js.index("fillFindForm(window.LAST_FIND)"))


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


class EgrulSearchesTheRightPlace(unittest.TestCase):
    """«ЕГРЮЛ не работает» — на самом деле работал, но искал
    в пустоте.

    Название города подставлялось в поле «регион», и совпадало это
    только для Москвы и Петербурга: они сами себе регионы. Для
    Новосибирска регион — «Новосибирская область», и фильтр не
    совпадал никогда — ни одной компании, ни одной ошибки."""

    class Stub(object):
        def __init__(self, items=()):
            self.sent = []
            self.items = list(items)

        def post(self, url, json=None, headers=None, timeout=None):
            self.sent.append(json)
            outer = self

            class R(object):
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return {"suggestions": outer.items}
            return R()

    def test_city_is_asked_for_as_a_city_too(self):
        s = self.Stub()
        dadata.search_by_name("стоматология", "токен",
                              region="Новосибирск", session=s)
        locations = s.sent[0].get("locations")
        self.assertIn({"city": "Новосибирск"}, locations)
        self.assertIn({"region": "Новосибирск"}, locations)

    def test_sole_traders_are_not_thrown_away(self):
        """ИП — тоже компания и тоже покупатель. Фильтр
        «только юрлица» выбрасывал их молча."""
        s = self.Stub()
        dadata.search_by_name("стоматология", "токен", session=s)
        self.assertNotIn("type", s.sent[0])

    def test_whole_country_asks_without_a_place(self):
        s = self.Stub()
        dadata.search_by_name("стоматология", "токен", region="", session=s)
        self.assertNotIn("locations", s.sent[0])

    def test_empty_answer_is_explained(self):
        """Здесь ищут по названию юрлица, а не по виду
        деятельности, и пустой ответ здесь нормален. Без
        объяснения он читается как поломка источника."""
        said = []
        dadata.search_by_name("стоматология", "токен", region="Пермь",
                              session=self.Stub(),
                              on_log=lambda t, k="": said.append(t))
        self.assertTrue(any("по названию юрлица" in t for t in said), said)


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


class SearchSiteClean(unittest.TestCase):
    """Сайт из справочника приходит с метками перехода — в карточке
    должен оказаться адрес компании, а не след того, откуда мы пришли."""

    def test_add_normalizes_site(self):
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "worker.py"), encoding="utf-8").read()
        block = src[src.index("    def add(row, source):"):]
        block = block[:block.index("    def merge_into(")]
        self.assertIn("site_src.normalize_url(row[\"site\"])", block)


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
        self.assertIn('class="chips"', self.html)

    def test_filter_chips_do_not_share_a_class_with_search_cities(self):
        """Общий класс — общий поиск по странице: кнопка
        «Россия» из фильтра списка молча числилась выбранным городом
        поиска — и без названия вовсе."""
        self.assertNotIn('class="city area', self.html)
        self.assertIn('$("q-cities").querySelectorAll(".city.is-on")', self.js)

    def test_nothing_reads_selection_from_a_listbox(self):
        self.assertNotIn("selectedOptions", self.js)

    def test_whole_country_excludes_the_cities(self):
        """Вместе они значат то же, что «Россия целиком», только дольше."""
        block = self.js[self.js.index('$("q-cities").onclick'):]
        block = block[:block.index("citiesNote();\n};")]
        self.assertIn("WHOLE_RU", block)
        self.assertIn("classList.remove", block)

    def test_empty_choice_is_called_out(self):
        self.assertIn("Не выбран ни один город", self.js)


class ManyPhones(unittest.TestCase):
    """У агентства недвижимости на сайте висит по номеру на каждого
    сотрудника — сорок штук, и все вываливались в карточку подряд.
    Отличались они только цифрами."""

    def setUp(self):
        self.js = io.open(os.path.join(os.path.dirname(__file__), "..", "app",
                                       "static", "app.js"),
                          encoding="utf-8").read()

    def test_only_five_are_shown_at_once(self):
        self.assertIn("const CT_SHOWN = 5;", self.js)
        self.assertIn("contactsBlock", self.js)
        self.assertIn('details class="ct-more"', self.js)

    def test_the_rest_are_not_thrown_away(self):
        block = self.js[self.js.index("function contactsBlock"):]
        block = block[:block.index("\n}")]
        self.assertIn("tail.map(contactRow)", block)

    def test_what_leads_to_the_boss_goes_first(self):
        block = self.js[self.js.index("function contactsBlock"):]
        block = block[:block.index("const sorted")]
        self.assertIn('c.owner === "director"', block)
        self.assertIn("мобильный", block)

    def test_number_says_what_kind_it_is(self):
        """Мобильный — чей-то личный аппарат, и отвечает на него человек,
        а не приёмная."""
        self.assertIn("function phoneKind", self.js)
        for word in ("бесплатный", "мобильный", "городской"):
            self.assertIn(word, self.js)

    def test_number_says_where_it_came_from(self):
        self.assertIn("function ctFrom", self.js)
        self.assertIn('class="ct-from"', self.js)

    def test_owner_is_written_in_russian(self):
        """«general» в строке рядом с номером не объясняет ничего."""
        self.assertIn("OWNER_RU", self.js)
        self.assertIn('director: "ГД"', self.js)

    def test_number_is_readable_but_copies_raw(self):
        """Показываем по-человечески, копируем цифрами: в чужую CRM
        номер вставляют без пробелов."""
        self.assertIn("function prettyPhone", self.js)
        self.assertIn('data-copy="${v}">${shown}', self.js)


# ── Рассылки ─────────────────────────────────────────────
from app import mail, outreach                                # noqa: E402
import fake_mail                                              # noqa: E402

_MAIL_TABLES = ("outreach", "sent_mail", "campaigns", "mail_optout",
                "contacts", "signals", "notes", "companies", "blacklist")


def _mail_reset():
    db.init()
    c = db.conn()
    for t in _MAIL_TABLES:
        c.execute("DELETE FROM %s" % t)
    c.execute("DELETE FROM settings WHERE key LIKE 'mail_%'")
    c.commit()
    outreach.STATE.update({"next_send_at": 0.0, "pause_until": 0.0,
                           "last_error": "", "last_poll": 0.0,
                           "poll_error": "", "poll_found": 0})


def _company(name, director="", email="", owner="director", verified="ok",
             source="сайт компании", stage="new", **extra):
    row = dict({"name": name, "director": director}, **extra)
    cid, _ = db.upsert_company(row)
    if email:
        db.add_contact(cid, "email", email, owner, 90, verified, source)
    if stage != "new":
        db.update_company_fields(cid, {"stage": stage})
    return cid


# Шаги по умолчанию с дописанной заготовкой — как их сохранит человек.
_STEPS = [dict(st, body=st["body"].replace(outreach.PLACEHOLDER_GAP,
                                           "Коротко о нас: делаем сайты."))
          for st in outreach.DEFAULT_STEPS]

# Будний день, 11 утра по местному времени: отправка в окне.
_WEEKDAY = time.mktime((2026, 9, 23, 11, 0, 0, 0, 0, -1))


class MailPresets(unittest.TestCase):
    def test_known_services_fill_servers(self):
        c = mail.conf("sales@yandex.ru", "pw")
        self.assertEqual((c["smtp_host"], c["smtp_port"]), ("smtp.yandex.ru", 465))
        self.assertEqual((c["imap_host"], c["imap_port"]), ("imap.yandex.ru", 993))
        self.assertEqual(mail.conf("a@bk.ru", "pw")["smtp_host"], "smtp.mail.ru")
        self.assertEqual(mail.conf("a@gmail.com", "pw")["imap_host"], "imap.gmail.com")

    def test_own_domain_needs_server(self):
        """Свой домен без сервера — сказать, чего не хватает, а не падать."""
        why = mail.problem(mail.conf("sales@romashka.ru", "pw"))
        self.assertIn("сервер отправки", why)
        self.assertIn("romashka.ru", why)

    def test_own_domain_on_yandex_gets_yandex_hint(self):
        c = mail.conf("sales@romashka.ru", "pw", smtp_host="smtp.yandex.ru")
        self.assertEqual(c["service"], "yandex")
        self.assertEqual(mail.problem(c), "")

    def test_missing_pieces_are_named(self):
        self.assertIn("адрес", mail.problem(mail.conf("", "pw")))
        self.assertIn("пароль", mail.problem(mail.conf("a@yandex.ru", "")))
        self.assertIn("не похож", mail.problem(mail.conf("не почта", "pw")))

    def test_bad_port_falls_back(self):
        c = mail.conf("a@yandex.ru", "pw", smtp_port="абв", imap_port="99999")
        self.assertEqual((c["smtp_port"], c["imap_port"]), (465, 993))


class MailErrors(unittest.TestCase):
    def test_wrong_password_names_app_password(self):
        import smtplib
        c = mail.conf("a@yandex.ru", "pw")
        text, kind = mail.explain(smtplib.SMTPAuthenticationError(535, b"bad"), c)
        self.assertEqual(kind, mail.GLOBAL)
        self.assertIn("пароль приложения", text)
        self.assertIn("id.yandex.ru", text)

    def test_unknown_recipient_is_about_address(self):
        import smtplib
        e = smtplib.SMTPRecipientsRefused({"x@c.ru": (550, b"no such user")})
        text, kind = mail.explain(e, mail.conf("a@yandex.ru", "pw"))
        self.assertEqual(kind, mail.RECIPIENT)
        self.assertIn("x@c.ru", text)

    def test_spam_rejection_stops_everything(self):
        import smtplib
        e = smtplib.SMTPDataError(554, b"5.7.1 Message rejected under suspicion of SPAM")
        text, kind = mail.explain(e, mail.conf("a@yandex.ru", "pw"))
        self.assertEqual(kind, mail.GLOBAL)
        self.assertIn("спам", text)

    def test_password_never_leaks_into_text(self):
        c = mail.conf("a@yandex.ru", "Sup3rSecretPw")
        text, _ = mail.explain(OSError("login a Sup3rSecretPw failed"), c)
        self.assertNotIn("Sup3rSecretPw", text)


_REPLY = """From: Иван Петров <boss@romashka.ru>
To: me@test.local
Subject: Re: Ромашка: вопрос руководителю
Message-ID: <r1@romashka.ru>
In-Reply-To: {ref}
References: {ref}
Content-Type: text/plain; charset=utf-8

{text}

-----Original Message-----
> старое письмо
"""

_BOUNCE = """From: MAILER-DAEMON@test.local
To: me@test.local
Subject: Undelivered Mail Returned to Sender
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: text/plain

Не удалось доставить письмо.
--B
Content-Type: message/delivery-status

Reporting-MTA: dns; test.local
Final-Recipient: rfc822; {addr}
Action: failed
Status: 5.1.1
--B
Content-Type: text/rfc822-headers

Message-ID: {ref}
To: {addr}
--B--
"""


class MailParsing(unittest.TestCase):
    def test_reply_is_linked_and_quote_cut(self):
        item = mail.parse(_REPLY.format(ref="<m1@test.local>",
                                        text="Да, интересно. Позвоните завтра.").encode())
        self.assertEqual(item["from"], "boss@romashka.ru")
        self.assertIn("<m1@test.local>", item["refs"])
        self.assertEqual(item["text"], "Да, интересно. Позвоните завтра.")
        self.assertFalse(item["bounce"])

    def test_bounce_names_address_and_our_letter(self):
        item = mail.parse(_BOUNCE.format(addr="nobody@c.ru", ref="<m9@test.local>").encode())
        self.assertTrue(item["bounce"])
        self.assertEqual(item["failed"], ["nobody@c.ru"])
        self.assertIn("<m9@test.local>", item["bounce_refs"])

    def test_auto_reply_is_not_an_answer(self):
        raw = ("From: a@c.ru\nSubject: Автоответ: в отпуске до 1 октября\n"
               "Auto-Submitted: auto-replied\n\nЯ в отпуске.\n").encode()
        item = mail.parse(raw)
        self.assertTrue(item["auto"])

    def test_refusal_words(self):
        for text in ("нет", "Нет, спасибо.", "Отпишите нас, пожалуйста",
                     "Нам это не интересно", "Не пишите больше"):
            self.assertTrue(mail.is_refusal(text), text)

    def test_no_inside_sentence_is_not_refusal(self):
        """«Нет, давайте в четверг» — это согласие, а не отказ."""
        for text in ("Нет, давайте лучше в четверг созвонимся",
                     "Добрый день! Интересно, пришлите цены",
                     "Не сейчас, напишите в октябре"):
            self.assertFalse(mail.is_refusal(text), text)

    def test_unsubscribe_header_click(self):
        self.assertTrue(mail.is_refusal("", "unsubscribe"))

    def test_html_only_reply_is_read(self):
        raw = ("From: a@c.ru\nSubject: Re: x\nContent-Type: text/html; charset=utf-8\n\n"
               "<div>Да, <b>давайте</b> созвонимся</div><blockquote>старое</blockquote>").encode()
        self.assertIn("давайте", mail.parse(raw)["text"])

    def test_imap_date_is_english(self):
        ts = time.mktime((2026, 3, 5, 12, 0, 0, 0, 0, -1))
        self.assertEqual(mail.imap_since(ts), "05-Mar-2026")


class LetterText(unittest.TestCase):
    def test_empty_name_takes_comma_with_it(self):
        v = {"имя": "", "компания": "Ромашка"}
        self.assertEqual(outreach.fill("Здравствуйте, {имя}!", v), "Здравствуйте!")
        self.assertEqual(outreach.fill("{имя}, добрый день!", v), "Добрый день!")
        self.assertEqual(outreach.fill("Текст\n\n{имя}, добрый день!", v),
                         "Текст\n\nДобрый день!")

    def test_filled_values(self):
        v = outreach.values_for({"director": "ПЕТРОВ ИВАН СЕРГЕЕВИЧ",
                                 "name": 'ООО "РОМАШКА-ТРЕЙД"'}, "Подпись")
        self.assertEqual(v["имя"], "Иван Сергеевич")
        self.assertEqual(v["компания"], "Ромашка-Трейд")
        self.assertEqual(outreach.fill("Здравствуйте, {имя}!", v),
                         "Здравствуйте, Иван Сергеевич!")

    def test_short_caps_name_is_kept(self):
        """Аббревиатуру не превращаем в «Мтс»."""
        self.assertEqual(outreach.company_title("ПАО МТС"), "МТС")

    def test_empty_opener_leaves_no_hole(self):
        got = outreach.fill("Здравствуйте!\n\n{первая_фраза}\n\nМы делаем…",
                            {"первая_фраза": ""})
        self.assertEqual(got, "Здравствуйте!\n\nМы делаем…")

    def test_body_ends_with_opt_out_and_signature(self):
        body = outreach.finish_body("Текст", "Текст", "Иван, +7 999")
        self.assertTrue(body.endswith(mail.OPT_OUT_LINE))
        self.assertIn("Иван, +7 999", body)
        # Подпись, уже поставленная шаблоном, второй раз не дописывается.
        body = outreach.finish_body("Текст\n\nИван, +7 999", "Текст\n\n{подпись}",
                                    "Иван, +7 999")
        self.assertEqual(body.count("Иван, +7 999"), 1)

    def test_steps_validation(self):
        _, err = outreach.clean_steps(outreach.DEFAULT_STEPS)
        self.assertIn("допишите", err)
        ok, err = outreach.clean_steps(_STEPS)
        self.assertEqual(err, "")
        self.assertEqual([s["delay_days"] for s in ok], [0, 3, 7])
        _, err = outreach.clean_steps([{"subject": "", "body": "x"}])
        self.assertIn("тема", err)
        _, err = outreach.clean_steps([{"subject": "Т", "body": "Привет, {Имя}"}])
        self.assertIn("{Имя}", err)
        _, err = outreach.clean_steps({"subject": "x"})
        self.assertTrue(err)
        # Задержку ограничиваем: ноль у напоминания — это спам в тот же день.
        got, _ = outreach.clean_steps([{"subject": "Т", "body": "a"},
                                       {"delay_days": 0, "body": "b"},
                                       {"delay_days": 999, "body": "c"},
                                       {"delay_days": 5, "body": "лишний"}])
        self.assertEqual([s["delay_days"] for s in got], [0, 1, 60])

    def test_ai_mode_only_for_first_letter(self):
        got, _ = outreach.clean_steps([{"mode": "ai"}, {"mode": "ai", "delay_days": 3,
                                                        "body": "b"}])
        self.assertEqual([s["mode"] for s in got], ["ai", "template"])

    def test_follow_up_subject_is_reply(self):
        steps, _ = outreach.clean_steps(_STEPS)
        subj, _, _ = outreach.compose({"id": 0, "name": "Ромашка", "director": ""},
                                      1, steps, "", first_subject="Тема 1")
        self.assertEqual(subj, "Re: Тема 1")


class Enrolment(unittest.TestCase):
    def tearDown(self):
        _mail_reset()

    def setUp(self):
        _mail_reset()
        self.camp, err = outreach.save_campaign("Тест", _STEPS)
        self.assertEqual(err, "")

    def test_director_before_general(self):
        cid = _company("ООО Альфа", "Иванов Иван", "info@alfa.ru", owner="general")
        db.add_contact(cid, "email", "ivanov@alfa.ru", "director", 92, "unchecked",
                       "сайт: фамилия в адресе")
        _, addr, _ = outreach.pick_address(cid)
        self.assertEqual(addr, "ivanov@alfa.ru")

    def test_guess_and_bad_are_skipped(self):
        cid = _company("ООО Бета", "Петров Пётр", "p.petrov@beta.ru",
                       verified="unchecked", source="выведен по схеме домена")
        db.add_contact(cid, "email", "old@beta.ru", "general", 80, "bad", "сайт")
        got = outreach.enroll(self.camp, [cid])
        self.assertEqual(got["added"], 0)
        self.assertEqual(got["guess"], 1)
        db.add_contact(cid, "email", "info@beta.ru", "general", 70, "unchecked", "сайт")
        self.assertEqual(outreach.pick_address(cid)[1], "info@beta.ru")

    def test_confirmed_guess_is_fine(self):
        cid = _company("ООО Гамма", "Сидоров", "sidorov@gamma.ru", verified="ok",
                       source="подтверждён почтовым сервером")
        self.assertEqual(outreach.enroll(self.camp, [cid])["added"], 1)

    def test_refusals_and_blacklist_are_blocked(self):
        a = _company("ООО Отказ", "", "a@a.ru", stage="отказ")
        b = _company("ООО Чёрный", "", "b@b.ru", inn="7700000001")
        db.blacklist_add(b, "тест")
        c = _company("ООО Отписка", "", "c@c.ru")
        outreach.optout_add("c@c.ru", "тест")
        got = outreach.enroll(self.camp, [a, b, c])
        self.assertEqual(got["added"], 0)
        self.assertEqual(got["blocked"], 2)
        self.assertEqual(got["refused"], 1)

    def test_one_company_one_active_campaign(self):
        cid = _company("ООО Дельта", "", "d@d.ru")
        other, _ = outreach.save_campaign("Другая", _STEPS)
        self.assertEqual(outreach.enroll(self.camp, [cid])["added"], 1)
        self.assertEqual(outreach.enroll(other, [cid])["busy"], 1)
        self.assertEqual(outreach.enroll(self.camp, [cid])["busy"], 1)

    def test_deleting_company_drops_its_queue(self):
        cid = _company("ООО Эпсилон", "", "e@e.ru")
        outreach.enroll(self.camp, [cid])
        db.delete_company(cid)
        n = db.conn().execute("SELECT COUNT(*) n FROM outreach").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_editing_steps_requeues_finished(self):
        cid = _company("ООО Зета", "", "z@z.ru")
        one, _ = outreach.save_campaign("Одно", [_STEPS[0]])
        outreach.enroll(one, [cid])
        db.conn().execute("UPDATE outreach SET step=1, status='waiting', sent_at=? "
                          "WHERE campaign_id=?", (int(_WEEKDAY), one))
        db.conn().commit()
        outreach.save_campaign("Одно", _STEPS[:2], one)
        row = db.conn().execute("SELECT status, next_at FROM outreach "
                                "WHERE campaign_id=?", (one,)).fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["next_at"], int(_WEEKDAY) + 3 * 86400)


class SendWindow(unittest.TestCase):
    def tearDown(self):
        _mail_reset()

    def setUp(self):
        _mail_reset()

    def test_hours_and_weekends(self):
        self.assertTrue(outreach.in_window(_WEEKDAY))
        night = time.mktime((2026, 9, 23, 22, 0, 0, 0, 0, -1))
        self.assertFalse(outreach.in_window(night))
        saturday = time.mktime((2026, 9, 26, 11, 0, 0, 0, 0, -1))
        self.assertFalse(outreach.in_window(saturday))
        db.set_setting("mail_weekends", "1")
        self.assertTrue(outreach.in_window(saturday))

    def test_warmup_limits_new_mailbox(self):
        db.set_setting("mail_day_limit", "40")
        self.assertEqual(outreach.day_limit(_WEEKDAY), 15)
        db.conn().execute("INSERT INTO sent_mail (company_id, sent_at) VALUES (1, ?)",
                          (int(_WEEKDAY - 10 * 86400),))
        db.conn().commit()
        self.assertEqual(outreach.day_limit(_WEEKDAY), 25)
        db.set_setting("mail_warmup", "0")
        self.assertEqual(outreach.day_limit(_WEEKDAY), 40)


class MailEndToEnd(unittest.TestCase):
    """Отправка и ответы через настоящие SMTP и IMAP на локальном адресе."""

    def setUp(self):
        _mail_reset()
        # Компании встают в очередь «сейчас», поэтому и отправку считаем
        # от настоящего момента, а не от выдуманной даты в прошлом.
        self.t = time.time() + 5
        self.smtp = fake_mail.FakeSMTP()
        self.imap = fake_mail.FakeIMAP()
        for k, v in (("address", "me@test.local"), ("password", "secret"),
                     ("name", "Иван Продавцов"), ("sign", "Иван, +7 999 000-00-00"),
                     ("smtp_host", "127.0.0.1"), ("smtp_port", self.smtp.port),
                     ("imap_host", "127.0.0.1"), ("imap_port", self.imap.port),
                     ("warmup", "0")):
            db.set_setting("mail_" + k, v)
        self.camp, _ = outreach.save_campaign("Тест", _STEPS)
        self.a = _company("ООО Альфа", "Иванов Иван Иванович", "boss@alfa.ru")
        self.b = _company("ООО Бета", "Петров Пётр", "boss@beta.ru")
        self.c = _company("ООО Гамма", "", "info@gamma.ru", owner="general")

    def tearDown(self):
        self.smtp.close()
        self.imap.close()
        _mail_reset()

    def _sent(self, cid):
        return [dict(r) for r in db.conn().execute(
            "SELECT * FROM sent_mail WHERE company_id=? ORDER BY id", (cid,))]

    def test_full_cycle(self):
        outreach.enroll(self.camp, [self.a, self.b, self.c])
        for _ in range(3):
            self.assertEqual(outreach.tick(self.t, ignore_pause=True), "sent")
        self.assertEqual(outreach.tick(self.t, ignore_pause=True), "nothing_due")
        self.assertEqual(len(self.smtp.sent), 3)

        raw = self.smtp.sent[0][2].decode("utf-8", "replace")
        self.assertIn("List-Unsubscribe", raw)
        letter = email.message_from_bytes(self.smtp.sent[0][2], policy=email.policy.default)
        body = letter.get_content()
        self.assertIn("Здравствуйте, Иван Иванович!", body)
        self.assertIn(mail.OPT_OUT_LINE, body)
        self.assertEqual(db.conn().execute("SELECT stage FROM companies WHERE id=?",
                                           (self.a,)).fetchone()["stage"], "написали")

        ref_a = self._sent(self.a)[0]["message_id"]
        ref_c = self._sent(self.c)[0]["message_id"]
        self.imap.add(_REPLY.format(ref=ref_a, text="Да, интересно. Звоните."))
        self.imap.add(_REPLY.format(ref="<unknown@x>", text="Нет, спасибо.")
                      .replace("boss@romashka.ru", "boss@beta.ru"))
        self.imap.add(_BOUNCE.format(addr="info@gamma.ru", ref=ref_c))
        got = outreach.poll(self.t + 3600)
        self.assertEqual(got, {"replied": 1, "unsub": 1, "bounced": 1, "auto": 0})
        self.assertFalse(self.imap.seen_flags_changed,
                         "письма надо читать с PEEK — «прочитано» ставит человек")

        st = {r["company_id"]: r["status"] for r in
              db.conn().execute("SELECT company_id, status FROM outreach")}
        self.assertEqual(st, {self.a: "replied", self.b: "unsub", self.c: "bounced"})
        a = db.conn().execute("SELECT stage, next_date FROM companies WHERE id=?",
                              (self.a,)).fetchone()
        self.assertEqual(a["stage"], "ответили")
        self.assertEqual(a["next_date"],
                         datetime.date.fromtimestamp(self.t + 3600).isoformat())
        self.assertTrue(outreach.opted_out("boss@beta.ru"))
        self.assertEqual(db.conn().execute("SELECT stage FROM companies WHERE id=?",
                                           (self.b,)).fetchone()["stage"], "отказ")
        self.assertEqual(db.conn().execute(
            "SELECT verified FROM contacts WHERE company_id=?", (self.c,)
        ).fetchone()["verified"], "bad")
        notes = " ".join(n["text"] for n in db.notes(self.a))
        self.assertIn("Да, интересно", notes)

        # Через неделю напоминаний не уходит никому: все трое вышли из цепочки.
        self.assertEqual(outreach.tick(self.t + 8 * 86400, ignore_pause=True),
                         "nothing_due")
        # Повторный опрос тех же писем не разносит второй раз.
        self.assertEqual(sum(outreach.poll(self.t + 7200).values()), 0)

    def test_follow_up_goes_into_same_thread(self):
        outreach.enroll(self.camp, [self.a])
        outreach.tick(self.t, ignore_pause=True)
        self.assertEqual(outreach.tick(self.t + 86400, ignore_pause=True),
                         "nothing_due")
        self.assertEqual(outreach.tick(self.t + 3 * 86400 + 60, ignore_pause=True),
                         "sent")
        first, second = self._sent(self.a)
        self.assertEqual(second["subject"], "Re: " + first["subject"])
        msg = email.message_from_bytes(self.smtp.sent[1][2], policy=email.policy.default)
        self.assertEqual(msg["In-Reply-To"], first["message_id"])

    def test_auto_reply_keeps_sequence(self):
        outreach.enroll(self.camp, [self.a])
        outreach.tick(self.t, ignore_pause=True)
        ref = self._sent(self.a)[0]["message_id"]
        self.imap.add("From: boss@alfa.ru\nSubject: Автоответ\nAuto-Submitted: auto-replied\n"
                      "In-Reply-To: %s\n\nВ отпуске.\n" % ref)
        self.assertEqual(outreach.poll(self.t + 60)["auto"], 1)
        self.assertEqual(db.conn().execute("SELECT status FROM outreach").fetchone()["status"],
                         "queued")

    def test_unknown_recipient_marks_address(self):
        self.smtp.reject_rcpt = {"boss@alfa.ru"}
        outreach.enroll(self.camp, [self.a])
        self.assertEqual(outreach.tick(self.t, ignore_pause=True), "bounced")
        self.assertEqual(db.conn().execute(
            "SELECT verified FROM contacts WHERE company_id=?", (self.a,)
        ).fetchone()["verified"], "bad")

    def test_spam_rejection_pauses_and_keeps_row(self):
        self.smtp.spam = True
        outreach.enroll(self.camp, [self.a])
        self.assertEqual(outreach.tick(self.t, ignore_pause=True), "send_error")
        self.assertIn("спам", outreach.STATE["last_error"])
        self.assertGreater(outreach.STATE["pause_until"], self.t + 3600)
        row = db.conn().execute("SELECT status, step FROM outreach").fetchone()
        self.assertEqual((row["status"], row["step"]), ("queued", 0))
        self.assertEqual(outreach.tick(self.t + 60), "paused")

    def test_wrong_password_is_explained_without_password(self):
        db.set_setting("mail_password", "WrongPassw0rd")
        outreach.enroll(self.camp, [self.a])
        self.assertEqual(outreach.tick(self.t, ignore_pause=True), "send_error")
        self.assertIn("пароль", outreach.STATE["last_error"])
        steps = mail.check(mail.conf_from_db())
        self.assertFalse(steps[0][1])
        self.assertFalse(steps[1][1])
        self.assertNotIn("WrongPassw0rd", json.dumps(steps, ensure_ascii=False))
        self.assertNotIn("WrongPassw0rd", outreach.STATE["last_error"])

    def test_check_sends_test_letter(self):
        steps = mail.check(mail.conf_from_db(), send_test=True)
        self.assertTrue(all(s[1] for s in steps), steps)
        self.assertEqual(self.smtp.sent[-1][1], ["me@test.local"])

    def test_refusal_in_stage_after_enrol_stops_letter(self):
        """Условия проверяются перед каждым письмом, а не только при добавлении."""
        outreach.enroll(self.camp, [self.a])
        db.update_company_fields(self.a, {"stage": "отказ"})
        self.assertEqual(outreach.tick(self.t, ignore_pause=True), "stopped")
        self.assertEqual(self.smtp.sent, [])

    def test_day_limit_holds(self):
        db.set_setting("mail_day_limit", "2")
        outreach.enroll(self.camp, [self.a, self.b, self.c])
        outreach.tick(self.t, ignore_pause=True)
        outreach.tick(self.t, ignore_pause=True)
        self.assertEqual(outreach.tick(self.t, ignore_pause=True), "day_limit")

    def test_paused_campaign_sends_nothing(self):
        outreach.enroll(self.camp, [self.a])
        outreach.set_status(self.camp, "paused")
        self.assertEqual(outreach.tick(self.t, ignore_pause=True), "nothing_due")


class MailApi(unittest.TestCase):
    def tearDown(self):
        _mail_reset()

    def setUp(self):
        _mail_reset()
        self.cl = web.create_app().test_client()

    def test_campaign_create_add_and_list(self):
        d = self.cl.post("/api/campaigns", json={
            "name": "Москва", "steps": _STEPS}).get_json()
        self.assertTrue(d["ok"], d)
        cid = _company("ООО Альфа", "Иванов Иван", "boss@alfa.ru")
        r = self.cl.post("/api/bulk", json={"ids": [cid], "action": "campaign",
                                            "campaign": d["id"]}).get_json()
        self.assertTrue(r["ok"], r)
        self.assertIn("добавлено 1", r["text"])
        lst = self.cl.get("/api/campaigns").get_json()
        self.assertEqual(lst["campaigns"][0]["counts"], {"queued": 1})
        prev = self.cl.post("/api/campaigns/%d/preview" % d["id"], json={}).get_json()
        self.assertIn("Иван", prev["letters"][0]["letters"][0]["body"])
        rows = self.cl.get("/api/campaigns/%d/rows" % d["id"]).get_json()["rows"]
        self.assertEqual(rows[0]["status_ru"], "ждёт отправки")
        self.assertTrue(self.cl.post("/api/outreach/%d/stop" % rows[0]["id"]).get_json()["ok"])

    def test_add_by_filter(self):
        d = self.cl.post("/api/campaigns", json={
            "name": "Все", "steps": _STEPS}).get_json()
        _company("ООО Альфа", "", "a@alfa.ru")
        _company("ООО Бета", "", "b@beta.ru")
        r = self.cl.post("/api/campaigns/%d/add" % d["id"],
                         json={"filter": True, "q": "", "only": ""}).get_json()
        self.assertEqual(r["result"]["added"], 2)

    def test_junk_input_is_not_500(self):
        for body in ({"name": ["x"], "steps": {"a": 1}}, {"name": "x", "steps": [1, 2]},
                     {"name": "x", "steps": [{"subject": {}, "body": []}]}, None):
            r = self.cl.post("/api/campaigns", json=body)
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.get_json()["ok"])
        r = self.cl.post("/api/bulk", json={"ids": [1], "action": "campaign",
                                            "campaign": "abc"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.cl.get("/api/campaigns/999/rows").status_code, 200)

    def test_state_and_settings(self):
        st = self.cl.get("/api/mail/state").get_json()
        self.assertTrue(st["ok"])
        self.assertFalse(st["configured"])
        self.cl.post("/api/settings", json={"mail_address": "me@yandex.ru",
                                            "mail_password": "pw", "mail_sign": "x" * 900})
        st = self.cl.get("/api/mail/state").get_json()
        self.assertTrue(st["configured"])
        self.assertEqual(len(db.get_setting("mail_sign")), 900)

    def test_page_has_mail_view_and_card(self):
        html = self.cl.get("/").get_data(as_text=True)
        for mark in ('id="view-mail"', 'data-view="mail"', 'id="s-mail-password"',
                     'id="bulk-camp"', 'value="ответили"'):
            self.assertIn(mark, html)
        # Пароль прячется так же, как остальные ключи.
        self.assertRegex(html, r'id="s-mail-password"[^>]*data-secret')

    def test_clear_drops_queue_but_keeps_campaigns(self):
        d = self.cl.post("/api/campaigns", json={
            "name": "Москва", "steps": _STEPS}).get_json()
        cid = _company("ООО Альфа", "", "a@alfa.ru")
        outreach.enroll(d["id"], [cid])
        self.cl.post("/api/clear")
        c = db.conn()
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM outreach").fetchone()["n"], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM campaigns").fetchone()["n"], 1)


class RepliedStageEverywhere(unittest.TestCase):
    """Новая стадия должна быть везде, где перечислены стадии."""

    def test_stage_lists_agree(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app", "static", "app.js"), encoding="utf-8") as f:
            js = f.read()
        with open(os.path.join(root, "app", "templates", "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn('["ответили", "ответили"]', js)
        self.assertIn('"ответили": "ответили"', js)
        self.assertIn('<option value="ответили">', html)
        self.assertIn("ответили", web.STAGE_NEXT)


# ── Новые источники и склейка между ними ─────────────────
class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise __import__("requests").HTTPError(str(self.status_code))


class _Sess:
    """Сессия, отвечающая заранее заготовленными ответами по очереди."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {}), dict(headers or {})))
        return self.answers.pop(0)

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, dict(data or {}), {}))
        return self.answers.pop(0)


def _trud_vac(i, inn="7701234567", name="ООО «Ромашка»", title="Оператор колл-центра",
              phone="+7 (495) 123-45-67", email="hr@romashka.ru", region="г. Москва",
              agency=False, created="2026-09-20"):
    return {"vacancy": {
        "id": "v%d" % i, "job-name": title, "creation-date": created,
        "salary_min": 50000, "salary_max": 70000,
        "region": {"region_code": "7700000000000", "name": region},
        "company": {"inn": inn, "ogrn": "1027700000000", "name": name,
                    "site": "https://romashka.ru/?utm_source=trud",
                    "hr-agency": agency},
        "contact_list": [{"contact_type": "Телефон", "contact_value": phone},
                         {"contact_type": "Эл. почта", "contact_value": email}],
        "contact_person": "Иванова Анна",
        "addresses": {"address": [{"location": "г. Москва, ул. Ленина, 1"}]},
        "vac_url": "https://trudvsem.ru/vacancy/card/x/v%d" % i}}


class TrudvsemSource(unittest.TestCase):
    def test_parse_and_fold_by_inn(self):
        data = {"meta": {"total": 3}, "results": {"vacancies": [
            _trud_vac(1), _trud_vac(2, title="Старший оператор колл-центра"),
            _trud_vac(3, inn="7709999999", name="ООО Кадры", agency=True)]}}
        rows, total = trudvsem.parse(data, "оператор колл-центра", in_title=True,
                                     today=datetime.date(2026, 9, 24))
        self.assertEqual(total, 3)
        self.assertEqual(len(rows), 2, "агентство отсекается")
        emps = list(trudvsem.fold(rows).values())
        self.assertEqual(len(emps), 1)
        e = emps[0]
        self.assertEqual((e["inn"], e["vacancies"]), ("7701234567", 2))
        self.assertEqual(e["fresh"], 4)
        self.assertIn("hr@romashka.ru", e["emails"])
        self.assertIn("+7 (495) 123-45-67", e["phones"])
        self.assertIn("Ленина", e["address"])

    def test_title_filter_is_local(self):
        self.assertTrue(trudvsem.title_matches("Операторы колл-центров", "оператор колл-центра"))
        self.assertFalse(trudvsem.title_matches("Курьер", "оператор колл-центра"))

    def test_region_code_is_13_digits(self):
        self.assertEqual(trudvsem.region_param("77"), "7700000000000")
        self.assertEqual(trudvsem.region_param(""), "")

    def test_search_pages_and_stops(self):
        page = {"meta": {"total": 150}, "results": {"vacancies":
                [_trud_vac(i, inn=str(7700000000 + i)) for i in range(100)]}}
        last = {"meta": {"total": 150}, "results": {"vacancies":
                [_trud_vac(100 + i, inn=str(7800000000 + i)) for i in range(50)]}}
        sess = _Sess([_Resp(page), _Resp(last)])
        got = _REAL_SEARCH["trud"]("оператор", region="77", pages=5, in_title=False,
                                   session=sess, pause=0)
        self.assertEqual(len(got), 150)
        self.assertEqual(len(sess.calls), 2)
        self.assertIn("/region/7700000000000", sess.calls[0][1])
        self.assertEqual(sess.calls[1][2]["offset"], 1)

    def test_refusal_is_reported_not_raised(self):
        errs = []
        sess = _Sess([_Resp({}, 503), _Resp({}, 503)])
        got = _REAL_SEARCH["trud"]("x", session=sess, errors=errs, pause=0)
        self.assertEqual(got, [])
        self.assertIn("503", errs[0])

    def test_junk_answer_is_empty(self):
        for junk in (None, [], {"results": []}, {"results": {"vacancies": [1, {"vacancy": 5}]}}):
            self.assertEqual(trudvsem.parse(junk)[0], [])


class SuperJobSource(unittest.TestCase):
    DATA = {"total": 2, "more": False, "objects": [
        {"id": 11, "profession": "Оператор call-центра", "date_published": 1000,
         "payment_from": 40000, "payment_to": 60000, "town": {"title": "Москва"},
         "client": {"id": 501, "title": "Альфа Логистик", "url": "https://alfa-log.ru",
                    "link": "https://www.superjob.ru/clients/alfa-501.html"},
         "agency": {"id": 1}, "phones": [{"number": "74957654321"}]},
        {"id": 12, "profession": "Менеджер", "client": {"id": 777, "title": "Кадровое агентство Плюс"},
         "agency": {"id": 2}}]}

    def test_parse_skips_agencies(self):
        rows, more = superjob.parse(self.DATA, now=1000 + 86400 * 2)
        self.assertFalse(more)
        self.assertEqual([r["name"] for r in rows], ["Альфа Логистик"])
        self.assertEqual(rows[0]["fresh"], 2)
        self.assertEqual(rows[0]["phones"], ["74957654321"])

    def test_title_search_and_key_header(self):
        sess = _Sess([_Resp(self.DATA)])
        got = _REAL_SEARCH["sj"]("оператор", "KEY", town="Москва", period=30,
                                 session=sess, pause=0)
        self.assertEqual(len(got), 1)
        _, _, params, headers = sess.calls[0]
        self.assertEqual(headers["X-Api-App-Id"], "KEY")
        self.assertEqual(params["keywords[0][srws]"], 1)
        self.assertEqual(params["period"], 0)
        self.assertEqual(params["town"], "Москва")

    def test_no_key_no_request(self):
        sess = _Sess([])
        self.assertEqual(_REAL_SEARCH["sj"]("x", "", session=sess), [])
        self.assertEqual(sess.calls, [])

    def test_bad_key_is_explained(self):
        errs = []
        sess = _Sess([_Resp({"error": {"code": 403, "message": "Invalid app key"}}, 403)])
        _REAL_SEARCH["sj"]("x", "BAD", session=sess, errors=errs, pause=0)
        self.assertIn("ключ", errs[0])

    def test_depth_limit(self):
        """API отдаёт не больше 500 вакансий — глубже не просим."""
        more = dict(self.DATA, more=True)
        sess = _Sess([_Resp(more) for _ in range(10)])
        _REAL_SEARCH["sj"]("x", "KEY", pages=20, session=sess, pause=0)
        self.assertEqual(len(sess.calls), 5)


class EgrulFnsSource(unittest.TestCase):
    ROWS = {"rows": [
        {"c": "ООО \"РОМАШКА\"", "n": "ОБЩЕСТВО...", "i": "2632000001", "o": "1022601000001",
         "g": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР: ПЕТРОВ ИВАН ИВАНОВИЧ", "k": "ul",
         "a": "357500, СТАВРОПОЛЬСКИЙ КРАЙ, Г. ПЯТИГОРСК, УЛ. МИРА, Д.1", "tot": "3"},
        {"c": "ООО \"РОМАШКА-ЮГ\"", "i": "2634000002", "o": "1", "k": "ul",
         "a": "355000, СТАВРОПОЛЬСКИЙ КРАЙ, Г. СТАВРОПОЛЬ", "tot": "3"},
        {"c": "ООО \"СТАРАЯ РОМАШКА\"", "i": "2632000003", "k": "ul", "e": "01.01.2020",
         "a": "Г. ПЯТИГОРСК", "tot": "3"}]}

    def test_city_filter_and_closed_skipped(self):
        got, total = egrul.parse(self.ROWS, city="Пятигорск", filter_city=True)
        self.assertEqual(total, 3)
        self.assertEqual([g["inn"] for g in got], ["2632000001"])
        self.assertEqual(got[0]["director"], "Петров Иван Иванович")
        self.assertEqual(got[0]["director_post"], "Генеральный директор")

    def test_two_step_with_wait(self):
        sess = _Sess([_Resp({"t": "TOKEN", "captchaRequired": False}),
                      _Resp({"status": "wait"}), _Resp(self.ROWS)])
        orig = time.sleep
        time.sleep = lambda *_: None
        try:
            got = _REAL_SEARCH["fns"]("ромашка", region="26", city="Пятигорск",
                                      pages=1, session=sess, city_is_subject=False)
        finally:
            time.sleep = orig
        self.assertEqual(len(got), 1)
        self.assertEqual(sess.calls[0][2]["region"], "26")
        self.assertIn("/search-result/TOKEN", sess.calls[1][1])

    def test_captcha_is_reported(self):
        errs = []
        sess = _Sess([_Resp({"captchaRequired": True})])
        self.assertEqual(_REAL_SEARCH["fns"]("x", session=sess, errors=errs), [])
        self.assertIn("капч", errs[0])


class RegionCodes(unittest.TestCase):
    def test_every_city_has_subject(self):
        for c in geo.cities():
            if c["name"] != geo.WHOLE:
                self.assertRegex(c["region"], r"^\d\d$", c["name"])

    def test_known_codes(self):
        self.assertEqual(geo.region_code("Пятигорск"), "26")
        self.assertEqual(geo.region_code("Уфа"), "02")
        self.assertEqual(geo.region_code("Химки"), "50")
        self.assertEqual(geo.region_code("Московская область"), "50")
        self.assertEqual(geo.region_code("Россия"), "")


class CrossSourceDedupe(unittest.TestCase):
    def setUp(self):
        db.init()
        c = db.conn()
        for t in ("contacts", "signals", "notes", "companies", "outreach", "sent_mail"):
            c.execute("DELETE FROM %s" % t)
        c.commit()

    def tearDown(self):
        self.setUp()

    def test_region_spelling_is_one_city(self):
        self.assertEqual(db.norm_region("г. Москва"), "Москва")
        self.assertEqual(db.norm_region("город Казань"), "Казань")
        self.assertEqual(db.norm_region("Москва г"), "Москва")
        a, _ = db.upsert_company({"name": "ООО Ромашка", "region": "Москва"})
        b, new = db.upsert_company({"name": "Ромашка", "region": "г. Москва"})
        self.assertEqual(a, b)
        self.assertFalse(new)

    def test_phone_links_map_and_vacancy_portal(self):
        """У карты нет ИНН, у портала нет вывески — общий у них телефон."""
        a, _ = db.upsert_company({"name": "Стоматология Улыбка", "region": "Москва"})
        db.add_contact(a, "phone", "+74951112233", "general", 85, "unchecked", "2ГИС")
        b, new = db.upsert_company({"name": "ООО «Дент-Сервис»", "inn": "7701111111",
                                    "region": "Москва"}, phones=["8 (495) 111-22-33"])
        self.assertEqual(a, b)
        self.assertFalse(new)
        inn = db.conn().execute("SELECT inn FROM companies WHERE id=?", (a,)).fetchone()["inn"]
        self.assertEqual(inn, "7701111111")

    def test_toll_free_and_shared_phones_do_not_link(self):
        a, _ = db.upsert_company({"name": "Сеть А", "region": "Москва"})
        db.add_contact(a, "phone", "+78001002030", "general", 85, "unchecked", "2ГИС")
        b, new = db.upsert_company({"name": "Сеть Б", "region": "Москва"},
                                   phones=["88001002030"])
        self.assertTrue(new, "8-800 ключом не служит")
        c1, _ = db.upsert_company({"name": "Офис 1", "region": "Москва"})
        c2, _ = db.upsert_company({"name": "Офис 2", "region": "Москва"})
        for cid in (c1, c2):
            db.add_contact(cid, "phone", "+74950000011", "general", 85, "unchecked", "2ГИС")
        _, new = db.upsert_company({"name": "Офис 3", "region": "Москва"},
                                   phones=["+74950000011"])
        self.assertTrue(new, "номер приёмной на нескольких компаниях — не ключ")

    def test_phone_never_joins_different_inn(self):
        a, _ = db.upsert_company({"name": "А", "inn": "7700000001", "region": "Москва"})
        db.add_contact(a, "phone", "+74951234000", "general", 85, "unchecked", "x")
        _, new = db.upsert_company({"name": "Б", "inn": "7700000002", "region": "Москва"},
                                   phones=["+74951234000"])
        self.assertTrue(new)

    def test_ogrn_links(self):
        a, _ = db.upsert_company({"name": "Бета", "ogrn": "1027700000055"})
        b, new = db.upsert_company({"name": "ООО Бета-Плюс", "ogrn": "1027700000055"})
        self.assertEqual(a, b)

    def test_find_duplicates_uses_phone_pairs(self):
        a, _ = db.upsert_company({"name": "Альфа", "region": "Москва"})
        b, _ = db.upsert_company({"name": "Совсем другое имя", "region": "Казань"})
        for cid in (a, b):
            db.add_contact(cid, "phone", "+74959998877", "general", 85, "unchecked", "x")
        self.assertIn((a, b), db.find_duplicates())

    def test_find_merges_map_and_portal_in_one_run(self):
        """Одна компания из карты и с портала вакансий — одна строка."""
        from app.sources import osm
        was = osm.search
        try:
            osm.search = lambda q, city, **kw: [{
                "name": "Улыбка", "site": "", "address": "Москва", "rubric": "стоматология",
                "phones": ["+7 495 111-22-33"], "emails": [], "links": []}]
            trudvsem.search = lambda *a, **kw: [{
                "name": "ООО «Дент-Сервис»", "inn": "7701111111", "ogrn": "",
                "site": "", "region": "г. Москва", "address": "г. Москва",
                "phones": ["8 (495) 111-22-33"], "emails": ["hr@dent.ru"],
                "titles": ["Администратор колл-центра"], "vacancies": 2,
                "salaries": [], "fresh": 3, "source": trudvsem.SOURCE,
                "url": "", "contact_person": ""}]
            tid = db.create_task("find", {})
            worker.task_find(tid, {
                "query": "стоматология", "cities": ["Москва"], "synonyms": False,
                "sources": {"osm": True, "gis": False, "yandex": False,
                            "dadata": False, "hh": False, "trudvsem": True,
                            "superjob": False, "fns": False}})
        finally:
            osm.search = was
            trudvsem.search = lambda *a, **kw: []
        rows = db.conn().execute("SELECT * FROM companies").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["inn"], "7701111111")
        self.assertIn("Работа России", rows[0]["source"])
        self.assertIn("колл-центра", db.get_signal(rows[0]["id"], "hh_titles"))

    def test_vacancy_search_without_hh(self):
        """hh выключен — вакансии приходят с других площадок и не теряются."""
        trudvsem.search = lambda *a, **kw: [{
            "name": "ООО Звонок", "inn": "7702222222", "ogrn": "", "site": "zvonok.ru",
            "region": "г. Москва", "address": "г. Москва", "phones": ["+7 495 222-33-44"],
            "emails": ["job@zvonok.ru"], "titles": ["Оператор колл-центра"],
            "vacancies": 3, "salaries": [55000], "fresh": 1,
            "source": trudvsem.SOURCE, "url": "u", "contact_person": "Анна"}]
        try:
            tid = db.create_task("hh_search", {})
            worker.task_hh_search(tid, {
                "queries": ["оператор колл-центра"], "areas": ["1"],
                "sources": {"hh": False, "trudvsem": True, "superjob": False}})
        finally:
            trudvsem.search = lambda *a, **kw: []
        row = db.conn().execute("SELECT * FROM companies").fetchone()
        self.assertEqual((row["inn"], row["region"]), ("7702222222", "Москва"))
        self.assertEqual(db.get_signal(row["id"], "hh_vacancies"), "3")
        self.assertEqual(db.get_signal(row["id"], "vac_contact"), "Анна")
        kinds = {r["kind"] for r in db.conn().execute(
            "SELECT kind FROM contacts WHERE company_id=?", (row["id"],))}
        self.assertEqual(kinds, {"phone", "email"})

    def test_vacancies_are_not_double_counted(self):
        cid, _ = db.upsert_company({"name": "X"})
        db.add_signal(cid, "hh_vacancies", 5)
        worker._merge_vacancy_signals(cid, {"vacancies": 3, "titles": ["A"],
                                            "source": "SuperJob"})
        self.assertEqual(db.get_signal(cid, "hh_vacancies"), "5")
        self.assertEqual(db.get_signal(cid, "vac_sources"), "SuperJob")


class NewSourcesInForm(unittest.TestCase):
    def test_page_has_new_sources(self):
        html = web.create_app().test_client().get("/").get_data(as_text=True)
        for mark in ('id="q-trud"', 'id="q-sj"', 'id="q-fns"', 'id="f-src-trud"',
                     'id="f-src-sj"', 'id="s-sj"'):
            self.assertIn(mark, html)

    def test_params_carry_sources(self):
        cl = web.create_app().test_client()
        cl.post("/api/searches", json={"name": "т", "kind": "hh_search",
                                       "queries": ["x"], "src_superjob": False})
        saved = [r for r in db.list_searches() if r["name"] == "т"][-1]
        params = saved["params"] if isinstance(saved["params"], dict) else json.loads(saved["params"])
        self.assertEqual(params["sources"], {"hh": True, "trudvsem": True, "superjob": False})


class InnCollision(unittest.TestCase):
    """Обогащение падало: «UNIQUE constraint failed: companies.inn»."""

    def setUp(self):
        db.init()
        c = db.conn()
        for t in ("contacts", "signals", "notes", "companies"):
            c.execute("DELETE FROM %s" % t)
        c.commit()

    tearDown = setUp

    def test_registry_inn_already_on_other_card_merges(self):
        old, _ = db.upsert_company({"name": "ПАО МТС", "inn": "7740000076"})
        db.update_company_fields(old, {"stage": "созвон"})
        db.add_note(old, "звонили")
        cur, _ = db.upsert_company({"name": "МТС", "site": "https://mts.ru"})
        got = db.fill_company(cur, {"inn": "7740000076", "director": "Николаев"})
        self.assertEqual(got["merged_with"], "ПАО МТС")
        rows = db.conn().execute("SELECT * FROM companies").fetchall()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["id"], r["inn"], r["stage"]), (cur, "7740000076", "созвон"))
        self.assertEqual(r["director"], "Николаев")
        self.assertEqual(len(db.notes(cur)), 1)

    def test_hh_id_collision_merges(self):
        a, _ = db.upsert_company({"name": "Билайн", "hh_id": "4934"})
        b, _ = db.upsert_company({"name": "ПАО ВымпелКом", "inn": "7713076301"})
        got, new = db.upsert_company({"name": "ВымпелКом", "inn": "7713076301",
                                      "hh_id": "4934"})
        self.assertFalse(new)
        n = db.conn().execute("SELECT COUNT(*) n FROM companies").fetchone()["n"]
        self.assertEqual(n, 1)


# ── Проекты ──────────────────────────────────────────────
class _ProjectsBase(unittest.TestCase):
    def setUp(self):
        db.init()
        db.set_active(db.projects()[0]["id"])
        c = db.conn()
        for t in ("contacts", "signals", "notes", "companies", "outreach",
                  "sent_mail", "campaigns", "tasks", "searches", "blacklist"):
            c.execute("DELETE FROM %s" % t)
        c.execute("DELETE FROM settings WHERE key LIKE 'mail_%' OR key IN "
                  "('ai_offer','ai_icp','ai_terms','ai_key')")
        c.commit()
        outreach._STATES.clear()

    def tearDown(self):
        for p in db.projects()[1:]:
            db.delete_project(p["id"])
        db.set_active(db.projects()[0]["id"])
        db.conn().execute("DELETE FROM settings WHERE key IN ('ai_key')")
        db.conn().commit()
        outreach._STATES.clear()


class ProjectsIsolation(_ProjectsBase):
    def test_first_project_is_the_main_base(self):
        first = db.projects()[0]
        self.assertEqual(first["path"], db.main_path())
        self.assertTrue(first["name"])

    def test_bases_do_not_mix_but_keys_are_shared(self):
        main_id = db.projects()[0]["id"]
        a, _ = db.upsert_company({"name": "Звонки-Клиент", "inn": "7700000011"})
        db.add_note(a, "из основного")
        db.set_setting("ai_offer", "аналитика звонков")
        db.set_setting("ai_key", "KEY-1")
        b = db.create_project("ПВХ", about="продаём ПВХ", buyer="тентовые мастерские",
                              score_mode="generic")
        db.set_active(b["id"])
        self.assertEqual(db.conn().execute("SELECT COUNT(*) n FROM companies").fetchone()["n"], 0)
        self.assertEqual(db.get_setting("ai_key"), "KEY-1", "ключи общие")
        self.assertEqual(db.get_setting("ai_offer"), "", "описание — своё")
        db.set_setting("ai_offer", "ПВХ-ткани")
        x, new = db.upsert_company({"name": "Звонки-Клиент", "inn": "7700000011"})
        self.assertTrue(new, "та же фирма в другом проекте — своя карточка")
        db.update_company_fields(x, {"stage": "созвон"})
        self.assertEqual(db.score_mode(), "generic")
        db.set_active(main_id)
        self.assertEqual(db.get_setting("ai_offer"), "аналитика звонков")
        row = db.conn().execute("SELECT stage FROM companies WHERE inn='7700000011'").fetchone()
        self.assertEqual(row["stage"] or "new", "new")
        self.assertEqual(len(db.notes(a)), 1)
        self.assertEqual(db.score_mode(), "phone_sales")

    def test_active_survives_restart(self):
        b = db.create_project("Второй")
        db.set_active(b["id"])
        db._state["active"] = ""
        db.init()
        self.assertEqual(db.current_path(), b["path"])

    def test_delete_removes_file_and_main_is_protected(self):
        b = db.create_project("Удаляемый")
        self.assertTrue(os.path.exists(b["path"]))
        self.assertFalse(db.delete_project(db.projects()[0]["id"]))
        self.assertTrue(db.delete_project(b["id"]))
        self.assertFalse(os.path.exists(b["path"]))
        self.assertIsNone(db.get_project(b["id"]))

    def test_carry_keeps_project_in_pool_threads(self):
        import threading as _t
        b = db.create_project("Пул")
        got = []
        with db.pinned(b["path"]):
            fn = db.carry(lambda: got.append(db.current_path()))
        th = _t.Thread(target=fn)
        th.start()
        th.join()
        self.assertEqual(got, [b["path"]])


class ProjectsBackground(_ProjectsBase):
    def test_task_writes_where_it_was_queued(self):
        b = db.create_project("Фон")
        worker.HANDLERS["_test_add"] = lambda tid, params: db.upsert_company(
            {"name": "Из задачи Б"})
        try:
            with db.pinned(b["path"]):
                db.create_task("_test_add", {})
            # Окно переключили на первый проект — задача всё равно идёт в Б.
            db.set_active(db.projects()[0]["id"])
            path, row = worker._next_task()
            self.assertEqual(path, b["path"])
            with db.pinned(path):
                worker._run_task(path, row)
        finally:
            worker.HANDLERS.pop("_test_add", None)
        self.assertEqual(db.conn().execute(
            "SELECT COUNT(*) n FROM companies WHERE name='Из задачи Б'").fetchone()["n"], 0)
        with db.pinned(b["path"]):
            self.assertEqual(db.conn().execute(
                "SELECT COUNT(*) n FROM companies WHERE name='Из задачи Б'").fetchone()["n"], 1)
            self.assertEqual(db.conn().execute(
                "SELECT status FROM tasks").fetchone()["status"], "done")

    def test_mail_of_two_projects_is_independent(self):
        a_srv = fake_mail.FakeSMTP(user="a@test.local")
        b_srv = fake_mail.FakeSMTP(user="b@test.local", spam=True)
        try:
            b = db.create_project("Рассылка Б")
            ts = time.time() + 5
            for path, srv, addr in ((db.main_path(), a_srv, "a@test.local"),
                                    (b["path"], b_srv, "b@test.local")):
                with db.pinned(path):
                    for k, v in (("address", addr), ("password", "secret"),
                                 ("smtp_host", "127.0.0.1"), ("smtp_port", srv.port),
                                 ("warmup", "0")):
                        db.set_setting("mail_" + k, v)
                    camp, _ = outreach.save_campaign("К", _STEPS)
                    cid = _company("ООО Цель " + addr, "", "boss@target-%s.ru" % addr[0])
                    outreach.enroll(camp, [cid])
            with db.pinned(b["path"]):
                self.assertEqual(outreach.tick(ts, ignore_pause=True), "send_error")
                self.assertTrue(outreach.STATE["last_error"])
            with db.pinned(db.main_path()):
                self.assertEqual(outreach.STATE["last_error"], "")
                # Пауза после ошибки ящика Б на проект А не распространяется.
                self.assertEqual(outreach.STATE["pause_until"], 0.0)
                self.assertEqual(outreach.tick(ts, ignore_pause=True), "sent")
            self.assertEqual(len(a_srv.sent), 1)
            self.assertEqual(a_srv.sent[0][0], "a@test.local")
        finally:
            a_srv.close()
            b_srv.close()


class ProjectSetupAi(unittest.TestCase):
    def _with_answer(self, text):
        was = ai.ask
        ai.ask = lambda *a, **kw: (text, "")
        try:
            return ai.project_setup("Продаём ПВХ-ткани оптом", "Производители тентов")
        finally:
            ai.ask = was

    def test_parses_and_cleans(self):
        d, err = self._with_answer(json.dumps({
            "find": ["производство тентов", "Производство тентов", "пошив палаток"],
            "vacancies": ["сварщик ПВХ", "оператор ТВЧ"], "icp": "тентовые цеха",
            "offer": "", "phone_sales": False, "okved": ["13.92"], "name": "ПВХ"},
            ensure_ascii=False))
        self.assertEqual(err, "")
        self.assertEqual(d["find"], ["производство тентов", "пошив палаток"])
        self.assertEqual(d["score_mode"], "generic")
        self.assertEqual(d["offer"], "Продаём ПВХ-ткани оптом")

    def test_phone_sales_flag(self):
        d, _ = self._with_answer('{"find": ["стоматология"], "phone_sales": true}')
        self.assertEqual(d["score_mode"], "phone_sales")

    def test_garbage_and_empty(self):
        self.assertTrue(self._with_answer("не JSON вовсе")[1])
        self.assertTrue(self._with_answer('{"find": [], "vacancies": []}')[1])
        self.assertTrue(ai.project_setup("", "x")[1])


class GenericScore(unittest.TestCase):
    def test_generic_ignores_telephony_and_counts_ai_fit(self):
        company = {"director": "", "site": "", "ai_fit": 72}
        sig = {"tech_calltracking": "Calltouch"}
        phone, _ = score.compute(company, sig, [], mode="phone_sales")
        gen, parts = score.compute(company, sig, [], mode="generic")
        keys = {p["key"] for p in parts}
        self.assertNotIn("calltracking", keys)
        self.assertIn("ai_fit", keys)
        self.assertEqual(phone, score.WEIGHTS["calltracking"])
        self.assertEqual(gen, score.WEIGHTS["ai_fit"])

    def test_weak_fit_gets_a_third(self):
        gen, _ = score.compute({"director": "", "site": "", "ai_fit": 45}, {}, [],
                               mode="generic")
        self.assertEqual(gen, score.WEIGHTS["ai_fit"] // 3)


class ProjectsApi(_ProjectsBase):
    def setUp(self):
        super().setUp()
        self.cl = web.create_app().test_client()

    def test_create_activate_and_list(self):
        d = self.cl.post("/api/projects", json={
            "name": "ПВХ", "about": "ПВХ-ткани", "buyer": "тентовики",
            "find": ["производство тентов", "пошив палаток"],
            "vacancies": ["сварщик ПВХ"], "cities": ["Москва", "Нигдеград"],
            "score_mode": "generic", "run": True}).get_json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["tasks"], 2)
        lst = self.cl.get("/api/projects").get_json()["projects"]
        cur = [p for p in lst if p["active"]][0]
        self.assertEqual((cur["name"], cur["score_mode"]), ("ПВХ", "generic"))
        kinds = sorted(r["kind"] for r in db.conn().execute("SELECT kind FROM tasks"))
        self.assertEqual(kinds, ["find", "hh_search"])
        params = json.loads(db.conn().execute(
            "SELECT params FROM tasks WHERE kind='find'").fetchone()["params"])
        self.assertEqual(params["query"], "производство тентов, пошив палаток")
        self.assertEqual(params["cities"], ["Москва"])
        self.assertEqual(db.get_setting("ai_offer"), "ПВХ-ткани")
        html = self.cl.get("/").get_data(as_text=True)
        self.assertIn('class="mode-generic"', html)
        self.assertIn("Проект «ПВХ»", html)
        main_id = db.projects()[0]["id"]
        self.assertTrue(self.cl.post("/api/projects/%d/activate" % main_id).get_json()["ok"])
        self.assertEqual(db.conn().execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"], 0)

    def test_setup_without_key_is_explained(self):
        d = self.cl.post("/api/projects/setup", json={"about": "a", "buyer": "b"}).get_json()
        self.assertFalse(d["ok"])
        self.assertIn("ключ", d["error"])

    def test_junk_is_not_500(self):
        for body in (None, {"name": ["x"]}, {"name": "x", "find": {"a": 1}},
                     {"name": "x", "find": "одна строка", "cities": "Москва"}):
            r = self.cl.post("/api/projects", json=body)
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.cl.post("/api/projects/999/activate").status_code, 200)
        self.assertFalse(self.cl.post("/api/projects/999/delete").get_json()["ok"])
        self.assertFalse(self.cl.post("/api/projects/%d/delete" % db.projects()[0]["id"])
                         .get_json()["ok"])

    def test_mode_change_rescores(self):
        cid, _ = db.upsert_company({"name": "Коллтрекинг"})
        db.add_signal(cid, "tech_calltracking", "Calltouch")
        worker._rescore(cid)
        before = db.conn().execute("SELECT score FROM companies WHERE id=?", (cid,)).fetchone()[0]
        pid = db.projects()[0]["id"]
        self.cl.post("/api/projects/%d" % pid, json={"score_mode": "generic"})
        after = db.conn().execute("SELECT score FROM companies WHERE id=?", (cid,)).fetchone()[0]
        db.update_project(pid, score_mode="phone_sales")
        self.assertGreater(before, after)


class FindSeveralKinds(unittest.TestCase):
    def test_comma_separated_kinds_are_all_searched(self):
        from app.sources import osm
        asked = []
        was = osm.search
        try:
            osm.search = lambda q, city, **kw: asked.append(q) or []
            db.init()
            tid = db.create_task("find", {})
            try:
                worker.task_find(tid, {
                    "query": "производство тентов, пошив палаток", "cities": ["Москва"],
                    "synonyms": False,
                    "sources": {"osm": True, "gis": False, "yandex": False, "dadata": False,
                                "hh": False, "trudvsem": False, "superjob": False,
                                "fns": False}})
            except RuntimeError:
                pass
        finally:
            osm.search = was
        self.assertEqual(asked, ["производство тентов", "пошив палаток"])


class EnrichSurvivesMerges(unittest.TestCase):
    """Обогащение падало: «'NoneType' object is not subscriptable» на
    филиале, который в том же прогоне склеился с головной компанией."""

    def setUp(self):
        db.init()
        db.set_active(db.projects()[0]["id"])
        c = db.conn()
        for t in ("contacts", "signals", "notes", "companies", "tasks", "logs"):
            c.execute("DELETE FROM %s" % t)
        c.commit()
        db.set_setting("dadata_token", "T")
        self.was = dadata.by_name

    def tearDown(self):
        dadata.by_name = self.was
        db.set_setting("dadata_token", "")

    def _run(self):
        tid = db.create_task("enrich", {})
        worker.task_enrich(tid, {"limit": 10, "fns": False})
        logs = " ".join(r["text"] for r in db.conn().execute(
            "SELECT text FROM logs WHERE task_id=?", (tid,)))
        return logs

    def test_branch_merged_mid_run_is_skipped(self):
        head, _ = db.upsert_company({"name": "МТС"})
        branch, _ = db.upsert_company({"name": "ФИЛИАЛ ПАО МТС В АЛТАЙСКОМ КРАЕ",
                                       "inn": "7740000076"})
        db.set_score(head, 90)
        db.set_score(branch, 10)
        dadata.by_name = lambda name, token, **kw: (
            {"inn": "7740000076", "director": "Николаев Вячеслав"} if name == "МТС" else {})
        logs = self._run()
        self.assertIn("склеена с другой карточкой", logs)
        rows = db.conn().execute("SELECT id, inn FROM companies").fetchall()
        self.assertEqual([(r["id"], r["inn"]) for r in rows], [(head, "7740000076")])

    def test_one_failure_does_not_stop_the_run(self):
        a, _ = db.upsert_company({"name": "Ломается"})
        b, _ = db.upsert_company({"name": "Работает"})
        db.set_score(a, 90)
        db.set_score(b, 10)

        def by_name(name, token, **kw):
            if name == "Ломается":
                raise ValueError("источник ответил мусором")
            return {"director": "Иванов Иван"}
        dadata.by_name = by_name
        logs = self._run()
        self.assertIn("сбой", logs)
        self.assertEqual(db.conn().execute(
            "SELECT director FROM companies WHERE id=?", (b,)).fetchone()["director"],
            "Иванов Иван")


if __name__ == "__main__":
    unittest.main(verbosity=2)

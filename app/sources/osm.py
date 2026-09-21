# -*- coding: utf-8 -*-
"""OpenStreetMap — справочник организаций без ключей и без денег.

Зачем понадобился. 2ГИС и Яндекс отдают больше и точнее, но оба требуют
регистрации с ИНН и реквизитами, а у Яндекса поиск по организациям стоит
двадцать тысяч в месяц. Для программы, которой человек хочет
воспользоваться сегодня вечером, это стена.

OSM — открытая карта, которую ведут люди. Ключей нет, регистрации нет,
лимитов по деньгам нет. Данные беднее: где-то не указан телефон, где-то
сайт, названия бывают написаны как попало. Зато их можно взять прямо
сейчас, и по крупным городам их много.

Отдельная ценность: в OSM есть тег contact:vk — ссылка на сообщество
ВКонтакте. Это ровно то, по чему программа потом выходит на контактных
лиц компании, то есть на живого руководителя.

Запросы идут к Overpass API — открытому поисковику по данным OSM.
Зеркал несколько: они бесплатные, иногда перегружены, и при отказе
одного пробуем следующее.
"""
import re
import time

import requests

from .. import settings

# Зеркала Overpass. Порядок случайным не является: первое обычно самое
# быстрое, остальные — на случай, когда оно занято чужими запросами.
MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Вид деятельности словами — в теги OSM.
#
# Список намеренно короткий: он покрывает то, что ищут чаще всего, а всё
# остальное всё равно находится поиском по названию. Гнаться за полнотой
# здесь бессмысленно — теги OSM исчисляются тысячами, а бизнес называет
# себя как хочет.
TAGS = {
    "стоматолог": ['["amenity"="dentist"]', '["healthcare"="dentist"]'],
    "клиник": ['["amenity"="clinic"]', '["amenity"="doctors"]'],
    "медицин": ['["amenity"="clinic"]', '["amenity"="doctors"]'],
    "медцентр": ['["amenity"="clinic"]', '["amenity"="doctors"]'],
    "лаборатор": ['["healthcare"="laboratory"]'],
    "автосервис": ['["shop"="car_repair"]', '["shop"="car_parts"]'],
    "автосалон": ['["shop"="car"]'],
    "автомойк": ['["amenity"="car_wash"]'],
    "автошкол": ['["amenity"="driving_school"]'],
    "шиномонтаж": ['["shop"="tyres"]'],
    "запчаст": ['["shop"="car_parts"]'],
    "азс": ['["amenity"="fuel"]'],
    "парикмахер": ['["shop"="hairdresser"]'],
    "барбершоп": ['["shop"="hairdresser"]'],
    "салон красоты": ['["shop"="beauty"]'],
    "космето": ['["shop"="beauty"]'],
    "массаж": ['["shop"="massage"]'],
    "фитнес": ['["leisure"="fitness_centre"]'],
    "спортзал": ['["leisure"="fitness_centre"]'],
    "юрид": ['["office"="lawyer"]'],
    "адвокат": ['["office"="lawyer"]'],
    "нотариус": ['["office"="notary"]'],
    "бухгалтер": ['["office"="accountant"]'],
    "аудит": ['["office"="accountant"]'],
    "страхов": ['["office"="insurance"]'],
    "турист": ['["shop"="travel_agency"]'],
    "турагент": ['["shop"="travel_agency"]'],
    "недвижимост": ['["office"="estate_agent"]'],
    "риэлт": ['["office"="estate_agent"]'],
    "кадров": ['["office"="employment_agency"]'],
    "рекрут": ['["office"="employment_agency"]'],
    "реклам": ['["office"="advertising_agency"]'],
    "дизайн": ['["office"="graphic_design"]', '["shop"="interior_decoration"]'],
    "интерьер": ['["shop"="interior_decoration"]'],
    "архитект": ['["office"="architect"]'],
    "маркетинг": ['["office"="advertising_agency"]'],
    "ветеринар": ['["amenity"="veterinary"]'],
    "аптек": ['["amenity"="pharmacy"]'],
    "оптик": ['["shop"="optician"]'],
    "типограф": ['["shop"="copyshop"]', '["craft"="printer"]'],
    "полиграф": ['["shop"="copyshop"]', '["craft"="printer"]'],
    "мебел": ['["shop"="furniture"]', '["craft"="carpenter"]'],
    "кух": ['["shop"="kitchen"]'],
    "окн": ['["shop"="windows"]', '["craft"="window_construction"]'],
    "двер": ['["shop"="doors"]'],
    "потолк": ['["craft"="plasterer"]'],
    "ремонт квартир": ['["craft"="builder"]', '["shop"="doityourself"]'],
    "отделк": ['["craft"="builder"]'],
    "строительн": ['["office"="construction_company"]', '["craft"="builder"]'],
    "стройматериал": ['["shop"="doityourself"]', '["shop"="hardware"]'],
    "сантехник": ['["craft"="plumber"]'],
    "электрик": ['["craft"="electrician"]'],
    "кондиционер": ['["craft"="hvac"]'],
    "вентиляц": ['["craft"="hvac"]'],
    "кафе": ['["amenity"="cafe"]'],
    "ресторан": ['["amenity"="restaurant"]'],
    "пекарн": ['["shop"="bakery"]'],
    "кондитер": ['["shop"="confectionery"]'],
    "гостиниц": ['["tourism"="hotel"]'],
    "отел": ['["tourism"="hotel"]'],
    "хостел": ['["tourism"="hostel"]'],
    "банк": ['["amenity"="bank"]'],
    "логист": ['["office"="logistics"]'],
    "грузоперевоз": ['["office"="logistics"]', '["office"="moving_company"]'],
    "перевозк": ['["office"="logistics"]', '["office"="moving_company"]'],
    "переезд": ['["office"="moving_company"]'],
    "транспортн": ['["office"="logistics"]'],
    "школ": ['["amenity"="school"]', '["amenity"="language_school"]'],
    "курсы": ['["amenity"="language_school"]', '["office"="educational_institution"]'],
    "учебный центр": ['["office"="educational_institution"]'],
    "детский сад": ['["amenity"="kindergarten"]'],
    "ит-компан": ['["office"="it"]'],
    "айти": ['["office"="it"]'],
    "разработка по": ['["office"="it"]'],
    "компьютер": ['["shop"="computer"]'],
    "химчист": ['["shop"="dry_cleaning"]'],
    "прачечн": ['["shop"="laundry"]'],
    "клининг": ['["office"="cleaning"]', '["craft"="cleaning"]'],
    "уборк": ['["office"="cleaning"]', '["craft"="cleaning"]'],
    "ритуальн": ['["shop"="funeral_directors"]'],
    "ювелир": ['["shop"="jewelry"]'],
    "цветочн": ['["shop"="florist"]'],
    "цветы": ['["shop"="florist"]'],
    "салон связи": ['["shop"="mobile_phone"]'],
    "одежд": ['["shop"="clothes"]'],
    "продукт": ['["shop"="convenience"]', '["shop"="supermarket"]'],
    "супермаркет": ['["shop"="supermarket"]'],

    # Дополнение словаря: чем больше видов деятельности ищется по тегу, а
    # не только по названию, тем больше улов. «Дента-Люкс» не называет
    # себя стоматологией, «Бегемот» — магазином игрушек, и без тега такие
    # компании не находятся вообще.
    #
    # Ключ — основа слова в нижнем регистре, её ищут внутри запроса.
    # Слишком короткие основы здесь опасны: «газ» лежит внутри «магазина»,
    # а «спа» — внутри «спальни». Поэтому короткого ключа нет там, где он
    # мог бы совпасть со случайным словом.
    "ветаптек": ['["amenity"="veterinary"]', '["amenity"="pharmacy"]'],
    "груминг": ['["shop"="pet_grooming"]'],
    "зоомагазин": ['["shop"="pet"]'],
    "зоотовар": ['["shop"="pet"]'],
    "больниц": ['["amenity"="hospital"]'],
    "анализ": ['["healthcare"="sample_collection"]', '["healthcare"="laboratory"]'],
    "физиотерап": ['["healthcare"="physiotherapist"]'],
    "реабилитац": ['["healthcare"="rehabilitation"]'],
    "психолог": ['["healthcare"="psychotherapist"]'],
    "психотерап": ['["healthcare"="psychotherapist"]'],
    "логопед": ['["healthcare"="speech_therapist"]'],
    "остеопат": ['["healthcare"="alternative"]'],
    "офтальмолог": ['["healthcare"="optometrist"]', '["shop"="optician"]'],
    "окулист": ['["healthcare"="optometrist"]', '["shop"="optician"]'],
    "медтехник": ['["shop"="medical_supply"]'],
    "ортопед": ['["shop"="medical_supply"]'],
    "слухов": ['["shop"="hearing_aids"]'],

    "маникюр": ['["shop"="beauty"]'],
    "педикюр": ['["shop"="beauty"]'],
    "ногт": ['["shop"="beauty"]'],
    "брови": ['["shop"="beauty"]'],
    "эпиляц": ['["shop"="beauty"]'],
    "визаж": ['["shop"="beauty"]'],
    "солярий": ['["shop"="beauty"]'],
    "спа-салон": ['["shop"="beauty"]', '["leisure"="sauna"]'],
    "косметик": ['["shop"="cosmetics"]'],
    "парфюм": ['["shop"="perfumery"]'],
    "тату": ['["shop"="tattoo"]'],
    "пирсинг": ['["shop"="tattoo"]'],
    "баня": ['["leisure"="sauna"]'],
    "сауна": ['["leisure"="sauna"]'],
    "тренажерн": ['["leisure"="fitness_centre"]'],
    "йога": ['["leisure"="fitness_centre"]'],
    "спортивный клуб": ['["leisure"="sports_centre"]'],
    "спортклуб": ['["leisure"="sports_centre"]'],
    "единоборств": ['["leisure"="sports_centre"]'],
    "бассейн": ['["leisure"="swimming_pool"]'],
    "аквапарк": ['["leisure"="water_park"]'],
    "танцевальн": ['["leisure"="dance"]'],
    "хореограф": ['["leisure"="dance"]'],
    "боулинг": ['["leisure"="bowling_alley"]'],
    "квест": ['["leisure"="escape_game"]'],
    "батут": ['["leisure"="trampoline_park"]'],
    "конн": ['["leisure"="horse_riding"]'],

    "колледж": ['["amenity"="college"]'],
    "техникум": ['["amenity"="college"]'],
    "университет": ['["amenity"="university"]'],
    "репетитор": ['["office"="educational_institution"]'],
    "детский центр": ['["office"="educational_institution"]'],
    "библиотек": ['["amenity"="library"]'],
    "музе": ['["tourism"="museum"]'],
    "кинотеатр": ['["amenity"="cinema"]'],
    "театр": ['["amenity"="theatre"]'],
    "ночной клуб": ['["amenity"="nightclub"]'],
    "караоке": ['["amenity"="nightclub"]'],

    "кофейн": ['["amenity"="cafe"]', '["shop"="coffee"]'],
    "чайн": ['["shop"="tea"]'],
    "пиццери": ['["amenity"="fast_food"]'],
    "шаурм": ['["amenity"="fast_food"]'],
    "бургер": ['["amenity"="fast_food"]'],
    "фастфуд": ['["amenity"="fast_food"]'],
    "столов": ['["amenity"="restaurant"]'],
    "суши": ['["amenity"="restaurant"]'],
    "бар": ['["amenity"="bar"]', '["amenity"="pub"]'],
    "кейтеринг": ['["craft"="caterer"]'],
    "банкет": ['["craft"="caterer"]'],
    "мороженое": ['["amenity"="ice_cream"]'],
    "кулинар": ['["shop"="deli"]'],
    "мясн": ['["shop"="butcher"]'],
    "рыбн": ['["shop"="seafood"]'],
    "молочн": ['["shop"="dairy"]'],
    "овощ": ['["shop"="greengrocer"]'],
    "хлеб": ['["shop"="bakery"]'],
    "пивн": ['["shop"="alcohol"]', '["amenity"="pub"]'],
    "алкогол": ['["shop"="alcohol"]'],
    "винный": ['["shop"="wine"]', '["shop"="alcohol"]'],
    "табач": ['["shop"="tobacco"]'],
    "вейп": ['["shop"="tobacco"]'],
    "рынок": ['["amenity"="marketplace"]'],
    "торговый центр": ['["shop"="mall"]'],
    "универмаг": ['["shop"="department_store"]'],
    "оптов": ['["shop"="wholesale"]', '["shop"="trade"]'],
    "киоск": ['["shop"="kiosk"]'],
    "товары для дома": ['["shop"="variety_store"]', '["shop"="houseware"]'],

    "книжн": ['["shop"="books"]'],
    "канцтовар": ['["shop"="stationery"]'],
    "канцеляр": ['["shop"="stationery"]'],
    "игрушк": ['["shop"="toys"]'],
    "спорттовар": ['["shop"="sports"]'],
    "снаряжен": ['["shop"="outdoor"]'],
    "рыболов": ['["shop"="fishing"]'],
    "охотнич": ['["shop"="hunting"]'],
    "велосипед": ['["shop"="bicycle"]'],
    "мотосалон": ['["shop"="motorcycle"]'],
    "мототехник": ['["shop"="motorcycle"]'],
    "обув": ['["shop"="shoes"]'],
    "ремонт обуви": ['["shop"="shoe_repair"]', '["craft"="shoemaker"]'],
    "сумк": ['["shop"="bag"]'],
    "ткан": ['["shop"="fabric"]'],
    "ателье": ['["shop"="tailor"]', '["craft"="tailor"]'],
    "швейн": ['["craft"="tailor"]'],
    "художеств": ['["shop"="art"]'],
    "музыкальн": ['["shop"="musical_instrument"]'],
    "сувенир": ['["shop"="gift"]'],
    "подарк": ['["shop"="gift"]'],
    "часов": ['["shop"="watches"]'],
    "изготовление ключей": ['["craft"="locksmith"]', '["shop"="locksmith"]'],
    "ломбард": ['["shop"="pawnbroker"]'],
    "обмен валют": ['["amenity"="bureau_de_change"]'],
    "букмекер": ['["shop"="bookmaker"]'],
    "лотере": ['["shop"="lottery"]'],
    "антиквар": ['["shop"="antiques"]'],
    "секонд": ['["shop"="second_hand"]'],
    "комиссионн": ['["shop"="second_hand"]'],

    "бытовая техника": ['["shop"="appliance"]', '["shop"="electronics"]'],
    "электроник": ['["shop"="electronics"]'],
    "ремонт техники": ['["shop"="electronics_repair"]', '["craft"="electronics_repair"]'],
    "ремонт телефонов": ['["shop"="mobile_phone"]'],
    "электротовар": ['["shop"="electrical"]'],
    "кабел": ['["shop"="electrical"]'],
    "светильник": ['["shop"="lighting"]'],
    "люстр": ['["shop"="lighting"]'],
    "посуд": ['["shop"="houseware"]'],
    "хозтовар": ['["shop"="houseware"]', '["shop"="hardware"]'],
    "ковр": ['["shop"="carpet"]'],
    "штор": ['["shop"="curtain"]'],
    "жалюзи": ['["shop"="window_blind"]'],
    "рольставни": ['["shop"="window_blind"]'],
    "матрас": ['["shop"="bed"]'],
    "постельн": ['["shop"="bed"]'],
    "магазин сантехники": ['["shop"="bathroom_furnishing"]', '["shop"="hardware"]'],
    "плитк": ['["shop"="tiles"]'],
    "плиточн": ['["craft"="tiler"]'],
    "обои": ['["shop"="wallpaper"]'],
    "краск": ['["shop"="paint"]'],
    "малярн": ['["craft"="painter"]'],
    "ламинат": ['["shop"="flooring"]'],
    "линолеум": ['["shop"="flooring"]'],
    "напольн": ['["shop"="flooring"]'],
    "инструмент": ['["shop"="hardware"]', '["shop"="trade"]'],
    "крепеж": ['["shop"="hardware"]'],
    "металлопрокат": ['["shop"="trade"]'],

    "кровл": ['["craft"="roofer"]'],
    "кровельн": ['["craft"="roofer"]'],
    "фасадн": ['["craft"="plasterer"]'],
    "штукатур": ['["craft"="plasterer"]'],
    "утеплен": ['["craft"="insulation"]'],
    "гидроизоляц": ['["craft"="insulation"]'],
    "сварочн": ['["craft"="metal_construction"]'],
    "сварк": ['["craft"="metal_construction"]'],
    "металлоконструкц": ['["craft"="metal_construction"]'],
    "ворота": ['["craft"="metal_construction"]'],
    "забор": ['["craft"="metal_construction"]'],
    "ковк": ['["craft"="blacksmith"]'],
    "стекл": ['["craft"="glaziery"]', '["shop"="glaziery"]'],
    "зеркал": ['["craft"="glaziery"]'],
    "столярн": ['["craft"="carpenter"]', '["craft"="joiner"]'],
    "пиломатериал": ['["craft"="sawmilling"]'],
    "строительные леса": ['["craft"="scaffolder"]'],
    "бурение": ['["craft"="well_drilling"]'],
    "скважин": ['["craft"="well_drilling"]'],
    "каменщик": ['["craft"="stonemason"]'],
    "памятник": ['["craft"="stonemason"]', '["shop"="funeral_directors"]'],
    "перетяжка мебели": ['["craft"="upholsterer"]'],
    "газовое оборудование": ['["craft"="gasfitter"]'],
    "отоплен": ['["craft"="hvac"]', '["craft"="plumber"]'],
    "водоснабжен": ['["craft"="plumber"]'],
    "домофон": ['["craft"="electrician"]'],
    "электромонтаж": ['["craft"="electrician"]'],
    "ландшафт": ['["craft"="gardener"]'],
    "озеленен": ['["craft"="gardener"]'],
    "садовый центр": ['["shop"="garden_centre"]'],
    "теплиц": ['["shop"="garden_centre"]'],
    "агро": ['["shop"="agrarian"]'],
    "семена": ['["shop"="agrarian"]'],
    "удобрен": ['["shop"="agrarian"]'],
    "сельхоз": ['["shop"="agrarian"]'],
    "фермер": ['["shop"="farm"]'],

    "застройщик": ['["office"="construction_company"]'],
    "управляющая компания": ['["office"="property_management"]'],
    "бизнес-центр": ['["office"="property_management"]'],
    "проектн": ['["office"="engineer"]', '["office"="architect"]'],
    "проектирован": ['["office"="engineer"]', '["office"="architect"]'],
    "инженерн": ['["office"="engineer"]'],
    "изыскан": ['["office"="engineer"]'],
    "геодез": ['["office"="surveyor"]'],
    "кадастр": ['["office"="surveyor"]'],
    "межеван": ['["office"="surveyor"]'],
    "оценочн": ['["office"="financial_advisor"]'],
    "лизинг": ['["office"="financial"]'],
    "микрозайм": ['["office"="financial"]'],
    "инвестиц": ['["office"="financial"]'],
    "налогов": ['["office"="tax_advisor"]'],
    "консалтинг": ['["office"="consulting"]'],
    "исследован": ['["office"="research"]'],
    "энергосбыт": ['["office"="energy_supplier"]'],
    "энергоснабжен": ['["office"="energy_supplier"]'],
    "телеком": ['["office"="telecommunication"]'],
    "провайдер": ['["office"="telecommunication"]'],
    "хостинг": ['["office"="it"]'],
    "разработка сайтов": ['["office"="it"]', '["office"="graphic_design"]'],
    "веб-студи": ['["office"="it"]', '["office"="graphic_design"]'],
    "программн": ['["office"="it"]'],
    "сео": ['["office"="advertising_agency"]'],
    "смм": ['["office"="advertising_agency"]'],
    "seo": ['["office"="advertising_agency"]'],
    "наружная реклама": ['["craft"="signmaker"]', '["office"="advertising_agency"]'],
    "вывеск": ['["craft"="signmaker"]'],
    "издательств": ['["office"="newspaper"]'],
    "газета": ['["office"="newspaper"]'],
    "фотограф": ['["craft"="photographer"]', '["shop"="photo"]'],
    "фотостуди": ['["craft"="photographer"]', '["shop"="photo"]'],
    "видеосъемк": ['["craft"="photographer"]'],
    "копицентр": ['["shop"="copyshop"]'],
    "экспеди": ['["office"="logistics"]'],
    "курьер": ['["office"="logistics"]'],
    "почт": ['["amenity"="post_office"]'],
    "такси": ['["amenity"="taxi"]'],
    "каршеринг": ['["amenity"="car_rental"]'],
    "прокат авто": ['["amenity"="car_rental"]'],
    "аренда авто": ['["amenity"="car_rental"]'],
    "туроператор": ['["office"="travel_agent"]', '["shop"="travel_agency"]'],
    "гостевой дом": ['["tourism"="guest_house"]'],
    "апартамент": ['["tourism"="apartment"]'],
    "мотел": ['["tourism"="motel"]'],
    "база отдыха": ['["tourism"="camp_site"]'],
    "кемпинг": ['["tourism"="camp_site"]'],

    # Производство, опт и услуги бизнесу.
    #
    # Тегов у карты здесь меньше: OpenStreetMap описывает места, куда
    # заходят, а металлобаза и цех по обработке — это не магазин. Что
    # есть, тем и пользуемся: craft для ремёсел, shop=trade для оптовых
    # баз, man_made=works для заводов. Остальное честно ищется только по
    # названию — в таком деле оно как раз и стоит в названии.
    "металлообработ": ['["craft"="metal_construction"]'],
    "токарн": ['["craft"="metal_construction"]'],
    "фрезерн": ['["craft"="metal_construction"]'],
    "лазерная резка": ['["craft"="metal_construction"]'],
    "порошковая покраска": ['["craft"="painter"]'],
    "завод": ['["man_made"="works"]'],
    "комбинат": ['["man_made"="works"]'],
    "пилорам": ['["craft"="sawmilling"]'],
    "деревообработ": ['["craft"="sawmilling"]'],
    "сруб": ['["craft"="carpenter"]'],
    "каркасные дома": ['["craft"="carpenter"]', '["office"="construction_company"]'],
    "столярный цех": ['["craft"="joiner"]'],
    "арматур": ['["shop"="trade"]'],
    "профнастил": ['["shop"="trade"]'],
    "металлочерепиц": ['["shop"="trade"]', '["craft"="roofer"]'],
    "сэндвич-панел": ['["shop"="trade"]'],
    "нержавей": ['["shop"="trade"]'],
    "трубы": ['["shop"="trade"]'],
    "элеватор": ['["shop"="agrarian"]'],
    "комбикорм": ['["shop"="agrarian"]'],
    "ветпрепарат": ['["amenity"="veterinary"]'],
    "пчеловод": ['["shop"="agrarian"]'],
    "рыбоводств": ['["shop"="agrarian"]'],
    "мясопереработ": ['["shop"="butcher"]'],
    "колбасн": ['["shop"="butcher"]'],
    "молокозавод": ['["shop"="dairy"]', '["man_made"="works"]'],
    "хлебозавод": ['["shop"="bakery"]', '["man_made"="works"]'],
    "пивовар": ['["craft"="brewery"]'],
    "винодельн": ['["craft"="winery"]'],
    "гофротар": ['["shop"="trade"]'],
    "этикетк": ['["craft"="printer"]', '["shop"="copyshop"]'],
    "упаковочн": ['["shop"="trade"]'],
    "автокран": ['["shop"="trade"]'],
    "экскаватор": ['["shop"="trade"]'],
    "погрузчик": ['["shop"="trade"]'],
    "компрессор": ['["shop"="trade"]'],
    "насосное оборудование": ['["shop"="trade"]'],
    "станк": ['["shop"="trade"]'],
    "промышленное оборудование": ['["shop"="trade"]'],
    "видеонаблюден": ['["craft"="electrician"]'],
    "пожарная сигнализац": ['["craft"="electrician"]'],
    "огнетушител": ['["shop"="trade"]'],
    "охрана труда": ['["office"="consulting"]'],
    "инкассац": ['["office"="financial"]'],
    "металлолом": ['["amenity"="recycling"]'],
    "макулатур": ['["amenity"="recycling"]'],
    "переработка отходов": ['["amenity"="recycling"]'],
    "прокат оборудован": ['["shop"="trade"]'],
    "аренда инструмента": ['["shop"="hardware"]', '["shop"="trade"]'],
    "воздушные шар": ['["shop"="party"]'],
    "фейерверк": ['["shop"="fireworks"]'],
    "спецодежд": ['["shop"="clothes"]', '["shop"="trade"]'],
    "широкоформатная печать": ['["craft"="printer"]', '["shop"="copyshop"]'],
    "шелкограф": ['["craft"="printer"]'],
    "сувенирная продукц": ['["shop"="gift"]'],
    "таблички": ['["craft"="signmaker"]'],
    "банкротств": ['["office"="lawyer"]'],
    "арбитраж": ['["office"="lawyer"]'],
    "регистрация фирм": ['["office"="lawyer"]'],
    "миграционн": ['["office"="lawyer"]'],
    "патентн": ['["office"="lawyer"]'],
    "сертификац": ['["office"="consulting"]'],
    "метролог": ['["office"="research"]'],
    "поверка приборов": ['["office"="research"]'],
    "экспертиз": ['["office"="engineer"]'],
    "телефони": ['["office"="telecommunication"]'],
    "медицинское оборудование": ['["shop"="medical_supply"]'],
    "лабораторное оборудование": ['["shop"="trade"]'],
    "детские товар": ['["shop"="baby_goods"]'],
    "коляск": ['["shop"="baby_goods"]'],
    "робототехник": ['["office"="educational_institution"]'],
    "спортивное питание": ['["shop"="nutrition_supplements"]'],
    "рукодели": ['["shop"="craft"]'],
    "товары для творчества": ['["shop"="craft"]'],
    "настольные игр": ['["shop"="games"]'],
    "моделизм": ['["shop"="model"]'],
    "коллекцион": ['["shop"="collector"]'],
    "профессиональная косметика": ['["shop"="cosmetics"]'],
    "гончарн": ['["craft"="pottery"]'],
    "керамическая мастерская": ['["craft"="pottery"]'],
}

# Теги OSM по-русски. В карточку и в выгрузку должно попадать
# «Агентство недвижимости», а не estate_agent: список читает продавец, а
# не картограф.
RUBRIC_RU = {
    "dentist": "Стоматология", "clinic": "Клиника", "doctors": "Медцентр",
    "hospital": "Больница", "pharmacy": "Аптека", "veterinary": "Ветклиника",
    "car_repair": "Автосервис", "car": "Автосалон", "tyres": "Шиномонтаж",
    "car_parts": "Автозапчасти", "fuel": "АЗС",
    "hairdresser": "Парикмахерская", "beauty": "Салон красоты",
    "massage": "Массажный салон", "fitness_centre": "Фитнес-клуб",
    "lawyer": "Юридические услуги", "accountant": "Бухгалтерские услуги",
    "insurance": "Страхование", "estate_agent": "Агентство недвижимости",
    "travel_agency": "Турагентство", "employment_agency": "Кадровое агентство",
    "advertising_agency": "Рекламное агентство", "it": "ИТ-компания",
    "company": "Компания", "logistics": "Логистика", "moving_company": "Переезды",
    "bank": "Банк", "cafe": "Кафе", "restaurant": "Ресторан", "bar": "Бар",
    "fast_food": "Быстрое питание", "hotel": "Гостиница",
    "school": "Школа", "language_school": "Языковая школа",
    "driving_school": "Автошкола", "kindergarten": "Детский сад",
    "copyshop": "Типография", "printer": "Типография",
    "furniture": "Мебель", "doityourself": "Стройматериалы",
    "hardware": "Хозтовары", "supermarket": "Супермаркет",
    "convenience": "Магазин у дома", "clothes": "Одежда",
    "laundry": "Прачечная", "dry_cleaning": "Химчистка",
    "funeral_directors": "Ритуальные услуги", "optician": "Оптика",
    "florist": "Цветы", "bakery": "Пекарня", "butcher": "Мясная лавка",
    "jewelry": "Ювелирный", "mobile_phone": "Салон связи",
    "computer": "Компьютерный магазин", "electronics": "Электроника",
    "notary": "Нотариус", "construction_company": "Строительная компания",
    "educational_institution": "Учебный центр", "laboratory": "Лаборатория",
    "car_wash": "Автомойка", "windows": "Окна", "doors": "Двери",
    "kitchen": "Кухни", "confectionery": "Кондитерская", "hostel": "Хостел",
    "builder": "Строительство", "plumber": "Сантехник",
    "electrician": "Электрик", "hvac": "Вентиляция и кондиционеры",
    "carpenter": "Столярные работы", "window_construction": "Окна",
    "plasterer": "Отделочные работы", "graphic_design": "Дизайн-студия",
    "interior_decoration": "Дизайн интерьера", "architect": "Архитектурное бюро",
    "cleaning": "Клининг",
    # Продолжение: у каждого тега из словаря выше должно быть русское
    # название. Иначе в карточке и в выгрузке оказывается «pet_grooming»,
    # а список читает продавец, а не картограф.
    "hospital": "Больница", "sample_collection": "Забор анализов",
    "physiotherapist": "Физиотерапия", "rehabilitation": "Реабилитация",
    "psychotherapist": "Психолог", "speech_therapist": "Логопед",
    "alternative": "Нетрадиционная медицина", "optometrist": "Офтальмология",
    "medical_supply": "Медтехника", "hearing_aids": "Слуховые аппараты",
    "pet": "Зоомагазин", "pet_grooming": "Груминг",
    "cosmetics": "Косметика", "perfumery": "Парфюмерия",
    "tattoo": "Тату-салон", "sauna": "Баня и сауна",
    "sports_centre": "Спортивный клуб", "swimming_pool": "Бассейн",
    "water_park": "Аквапарк", "dance": "Танцевальная студия",
    "bowling_alley": "Боулинг", "escape_game": "Квесты",
    "trampoline_park": "Батутный центр", "horse_riding": "Конный клуб",
    "college": "Колледж", "university": "Университет",
    "library": "Библиотека", "museum": "Музей", "cinema": "Кинотеатр",
    "theatre": "Театр", "nightclub": "Ночной клуб",
    "coffee": "Кофе", "tea": "Чай", "ice_cream": "Мороженое",
    "pub": "Паб", "caterer": "Кейтеринг", "deli": "Кулинария",
    "seafood": "Рыбный магазин", "dairy": "Молочный магазин",
    "greengrocer": "Овощи и фрукты", "alcohol": "Алкоголь",
    "wine": "Винный магазин", "tobacco": "Табак",
    "marketplace": "Рынок", "mall": "Торговый центр",
    "department_store": "Универмаг", "wholesale": "Оптовая торговля",
    "kiosk": "Киоск", "variety_store": "Товары для дома",
    "books": "Книжный магазин", "stationery": "Канцтовары",
    "toys": "Игрушки", "sports": "Спорттовары", "outdoor": "Снаряжение",
    "fishing": "Рыболовный магазин", "hunting": "Охотничий магазин",
    "bicycle": "Велосипеды", "motorcycle": "Мототехника",
    "shoes": "Обувь", "shoe_repair": "Ремонт обуви",
    "shoemaker": "Ремонт обуви", "bag": "Сумки", "fabric": "Ткани",
    "tailor": "Ателье", "art": "Товары для художников",
    "musical_instrument": "Музыкальные инструменты", "gift": "Подарки",
    "watches": "Часы", "locksmith": "Изготовление ключей",
    "pawnbroker": "Ломбард", "bureau_de_change": "Обмен валют",
    "bookmaker": "Букмекер", "lottery": "Лотереи",
    "antiques": "Антиквариат", "second_hand": "Комиссионный магазин",
    "appliance": "Бытовая техника", "electronics_repair": "Ремонт техники",
    "electrical": "Электротовары", "lighting": "Светильники",
    "houseware": "Посуда и хозтовары", "carpet": "Ковры",
    "curtain": "Шторы", "window_blind": "Жалюзи", "bed": "Матрасы",
    "bathroom_furnishing": "Сантехника", "tiles": "Плитка",
    "tiler": "Плиточные работы", "wallpaper": "Обои", "paint": "Краски",
    "painter": "Малярные работы", "flooring": "Напольные покрытия",
    "trade": "Оптовая база", "roofer": "Кровельные работы",
    "insulation": "Утепление и гидроизоляция",
    "metal_construction": "Металлоконструкции", "blacksmith": "Ковка",
    "glaziery": "Стекло и зеркала", "joiner": "Столярные работы",
    "sawmilling": "Пиломатериалы", "scaffolder": "Строительные леса",
    "stonemason": "Камень и памятники", "upholsterer": "Перетяжка мебели",
    "gasfitter": "Газовое оборудование", "gardener": "Озеленение",
    "garden_centre": "Садовый центр", "agrarian": "Товары для села",
    "farm": "Фермерское хозяйство",
    "property_management": "Управление недвижимостью",
    "engineer": "Проектирование", "surveyor": "Геодезия и кадастр",
    "financial": "Финансовые услуги", "financial_advisor": "Оценка и финансы",
    "tax_advisor": "Налоговый консультант", "consulting": "Консалтинг",
    "research": "Исследования", "energy_supplier": "Энергоснабжение",
    "telecommunication": "Связь и интернет", "newspaper": "Издательство",
    "signmaker": "Вывески", "photographer": "Фотограф", "photo": "Фотоуслуги",
    "post_office": "Почта", "taxi": "Такси", "car_rental": "Аренда авто",
    "travel_agent": "Туроператор", "guest_house": "Гостевой дом",
    "apartment": "Апартаменты", "motel": "Мотель", "camp_site": "База отдыха",
    "well_drilling": "Бурение скважин",
    "works": "Завод", "winery": "Винодельня", "brewery": "Пивоварня", "recycling": "Приём вторсырья",
    "party": "Товары для праздника", "fireworks": "Пиротехника",
    "baby_goods": "Детские товары", "nutrition_supplements": "Спортивное питание",
    "craft": "Товары для творчества", "games": "Настольные игры",
    "model": "Моделизм", "collector": "Коллекционирование",
    "pottery": "Гончарная мастерская",
}


def rubric_ru(tags):
    """Понятное название рубрики из тегов OSM."""
    for key in ("amenity", "shop", "office", "healthcare", "leisure",
                "tourism", "craft", "man_made"):
        v = (tags.get(key) or "").strip()
        if v:
            return RUBRIC_RU.get(v, v.replace("_", " "))
    return ""


# Признаки того, что объект — организация, а не дом и не улица.
BIZ_KEYS = ("shop", "office", "amenity", "craft", "healthcare", "company")

# Теги со ссылками на соцсети. Ради contact:vk всё и затевалось.
LINK_TAGS = ("contact:vk", "contact:telegram", "contact:instagram",
             "contact:facebook", "contact:youtube", "contact:ok")


def stem(query):
    """Основа слова для поиска по названию.

    «Стоматология» должна находить и «стоматологическую клинику», и
    «стоматологию», поэтому ищем по основе, а не по слову целиком.
    Отрезаем два последних знака у слов длиннее шести — грубо, но для
    русских окончаний работает, а перемудрить здесь опаснее: слишком
    короткая основа притащит всё подряд.
    """
    q = (query or "").strip().lower()
    if len(q) > 6 and " " not in q:
        q = q[:-2]
    return re.sub(r'["\\\\\\[\\]()|]', "", q)


def bbox(city):
    """Прямоугольник города в порядке, который ждёт Overpass."""
    try:
        lon, lat = [float(x) for x in (city.get("ll") or "").split(",")]
        dlon, dlat = [float(x) for x in (city.get("spn") or "0.5,0.4").split(",")]
    except Exception:
        return ""
    return "%.4f,%.4f,%.4f,%.4f" % (lat - dlat / 2, lon - dlon / 2,
                                    lat + dlat / 2, lon + dlon / 2)


def quarters(box):
    """Прямоугольник на четыре — для обхода города по частям.

    Overpass отвечает на один запрос не больше чем заданным числом
    объектов, и в миллионнике этот предел наступает раньше, чем
    кончаются компании. Обойти город четвертями — единственный способ
    забрать остальных: сервер отдаёт столько же, но четыре раза по
    меньшей площади.
    """
    try:
        s, w, n, e = [float(x) for x in box.split(",")]
    except Exception:
        return []
    mid_lat = (s + n) / 2.0
    mid_lon = (w + e) / 2.0
    return ["%.4f,%.4f,%.4f,%.4f" % (a, b, c, d) for a, b, c, d in (
        (s, w, mid_lat, mid_lon), (s, mid_lon, mid_lat, e),
        (mid_lat, w, n, mid_lon), (mid_lat, mid_lon, n, e))]


def build_query(query, city, limit=400, box=None):
    """Запрос на языке Overpass.

    Ищем двумя способами сразу: по тегам вида деятельности, если он нам
    знаком, и по названию всегда. Первое находит организации, которые
    никак не назвали себя в названии («Дента-Люкс» — стоматология),
    второе — те, у кого тег не проставлен, а в названии всё написано.
    """
    box = box or bbox(city)
    if not box:
        return ""
    low = (query or "").lower()
    parts = []
    # Совпавших слов может быть несколько: «медицинская клиника» — это и
    # clinic, и doctors. Раньше брали первое попавшееся и выходили, и
    # половина подходящих тегов терялась. Больше трёх групп не берём:
    # Overpass отвечает отказом на слишком широкий запрос.
    seen_tags = []
    hits = [w for w in TAGS if w in low]
    # Совпадение внутри совпадения не считается.
    #
    # «Автошкола» содержит и «автошкол», и «школ», и раньше в запрос
    # попадали заодно все школы города. «Барбершоп» содержит «бар» — и
    # принёс бы бары. Слово, целиком лежащее внутри другого совпавшего,
    # выбрасываем: оно говорит о том же, только грубее. Остальные
    # сортируем от длинного к короткому, потому что тегов берём не
    # больше четырёх, и уйти должны самые точные.
    hits = [w for w in hits if not any(w != o and w in o for o in hits)]
    hits.sort(key=len, reverse=True)
    for word in hits:
        for t in TAGS[word]:
            if t not in seen_tags:
                seen_tags.append(t)
        if len(seen_tags) >= 4:
            break
    for t in seen_tags[:4]:
        parts.append('nwr%s(%s);' % (t, box))
    name = stem(query)
    if name:
        # Поиск по названию — только среди организаций.
        #
        # Голое ["name"~"дизайн"] заставляет сервер просмотреть все
        # объекты города: дома, улицы, остановки. Он отвечает на такое
        # отказом 504, а если отвечает — приносит переулок Дизайнеров
        # вместо студии. Пара «название + признак организации» ищется по
        # указателю и стоит дёшево.
        for key in BIZ_KEYS:
            parts.append('nwr["name"~"%s",i]["%s"](%s);' % (name, key, box))
    if not parts:
        return ""
    return ("[out:json][timeout:50];(%s);out center tags %d;"
            % ("".join(parts), int(limit)))


def ask(q, session=None, on_log=None, should_stop=None):
    """Один запрос к Overpass с перебором зеркал и одним повтором.

    Зеркала бесплатные и перегружаются пачками, но отпускает их быстро.
    Повтор через полминуты спасает большую часть прогонов; без него
    город просто выпадал из поиска.
    """
    s = session or requests.Session()
    for url in MIRRORS:
        if should_stop and should_stop():
            return None
        try:
            r = s.post(url, data={"data": q}, timeout=70,
                       headers={"User-Agent": settings.USER_AGENT})
        except Exception as e:
            if on_log:
                on_log("OSM: %s не ответил (%s)" % (_host(url), str(e)[:90]), "warn")
            continue
        if r.status_code == 429 or r.status_code == 504:
            # Зеркало занято чужими запросами — это нормально, идём к
            # следующему, а не объявляем источник сломанным.
            if on_log:
                on_log("OSM: %s занят (%s), пробую другое зеркало"
                       % (_host(url), r.status_code), "warn")
            continue
        if r.status_code != 200:
            if on_log:
                on_log("OSM: %s ответил %s" % (_host(url), r.status_code), "warn")
            continue
        try:
            return r.json()
        except Exception:
            continue

    if should_stop and should_stop():
        return None
    if on_log:
        on_log("OSM: все зеркала заняты, жду полминуты и пробую ещё раз", "warn")
    time.sleep(30)
    for url in MIRRORS:
        if should_stop and should_stop():
            break
        try:
            r = s.post(url, data={"data": q}, timeout=70,
                       headers={"User-Agent": settings.USER_AGENT})
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    return None


def _org(t):
    """Организация из тегов OSM, или None, если это не организация."""
    name = (t.get("name") or "").strip()
    if not name:
        return None
    phones = []
    for key in ("phone", "contact:phone", "contact:mobile"):
        for p in (t.get(key) or "").split(";"):
            p = p.strip()
            if p and p not in phones:
                phones.append(p)
    links = [t[k].strip() for k in LINK_TAGS if (t.get(k) or "").strip()]
    return {
        "name": name,
        "address": _address(t),
        "site": (t.get("website") or t.get("contact:website") or "").strip(),
        "phones": phones[:4],
        "links": [_full(l) for l in links][:6],
        "rubric": rubric_ru(t),
        "emails": [e.strip() for e in (t.get("email") or
                                       t.get("contact:email") or "").split(";")
                   if e.strip()][:2],
    }


# Сколько запросов к карте тратим на один город за раз.
#
# Обход четвертями снимает потолок, но каждая четверть — это отдельный
# запрос к бесплатному общему серверу. Целый город плюс четыре его
# четверти плюс четыре четверти самой густой из них — девять запросов,
# и это предел приличия: дальше начинается отказ по превышению.
MAX_CALLS = 9

# Пауза между запросами по частям города: сервер общий и
# бесплатный, и очередь из девяти запросов подряд он справедливо
# считает злоупотреблением.
PAUSE = 1.2


def search(query, city, pages=1, session=None, on_log=None, should_stop=None,
           limit=400):
    """Организации по виду деятельности в городе. Ключ не нужен.

    Про потолок. Overpass отдаёт не больше `limit` объектов на запрос, и
    в миллионнике этот предел наступал раньше, чем кончались компании:
    выдача молча обрывалась, а по виду ответа это было неотличимо от
    «больше и нет». Теперь ответ, упёршийся в предел, считается
    обрезанным, и та же площадь переспрашивается четвертями — там, где
    густо, и только там. Где компаний мало, всё остаётся как было:
    один запрос, один ответ.
    """
    if not bbox(city):
        if on_log:
            on_log("OSM: для «%s» нет координат — пропускаю"
                   % (city or {}).get("name", "?"), "warn")
        return []

    http = session or requests.Session()
    out, seen, calls, split, answered = [], set(), [0], [0], [False]

    def take(data):
        """Разобрать ответ и сказать, был ли он обрезан пределом."""
        els = [el for el in (data.get("elements") or []) if isinstance(el, dict)]
        for el in els:
            org = _org(el.get("tags") or {})
            if not org or org["name"].lower() in seen:
                continue
            seen.add(org["name"].lower())
            out.append(org)
        return len(els) >= limit

    def walk(box, level):
        if should_stop and should_stop():
            return
        if calls[0] >= MAX_CALLS:
            return
        q = build_query(query, city, limit=limit, box=box)
        if not q:
            return
        if calls[0] and PAUSE:
            time.sleep(PAUSE)
        calls[0] += 1
        data = ask(q, session=http, on_log=on_log, should_stop=should_stop)
        if data is None:
            return
        answered[0] = True
        if not take(data) or level >= 2:
            return
        # Ответ упёрся в предел — значит, за ним было ещё.
        split[0] += 1
        for part in quarters(box):
            walk(part, level + 1)

    walk(bbox(city), 0)

    # Пустой ответ и молчание сервера — разные беды, и путать их нельзя:
    # в первом случае таких компаний в карте нет, во втором они есть, но
    # их не отдали. Человеку это решает, менять слово или подождать.
    if not answered[0] and not (should_stop and should_stop()):
        if on_log:
            on_log("OSM: ни одно зеркало не ответило и со второго раза. "
                   "Это проходит само — попробуйте через несколько минут.",
                   "warn")
        return []
    if on_log:
        on_log("OSM: найдено организаций %d%s"
               % (len(out),
                  (" (город обошёл по частям — в одном запросе они не "
                   "помещались)" if split[0] else "")))
    return out


def _address(t):
    parts = [t.get("addr:city"), t.get("addr:street"), t.get("addr:housenumber")]
    return ", ".join(p for p in parts if p)


def _full(link):
    """В OSM ссылки пишут и полностью, и просто именем сообщества."""
    link = link.strip()
    if link.startswith("http"):
        return link
    if link.startswith("@"):
        return "https://t.me/" + link[1:]
    return "https://vk.com/" + link


def _host(url):
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except Exception:
        return url

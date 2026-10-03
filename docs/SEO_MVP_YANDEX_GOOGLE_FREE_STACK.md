# SEO MVP — Яндекс + Google без обязательного Keys.so

Статус: planning. Не влияет на текущую Phase 4 и не меняет main.

## Решение

Keys.so исключен из обязательного MVP-контура из-за стоимости. На старте используем Yandex Wordstat как основной внешний источник спроса, Google Keyword Planner как дополнительный источник Google-спроса, а после запуска — фактические данные из Yandex Webmaster и Google Search Console.

## 1. До запуска: выбрать первые 10 000

1. Кандидаты: действующие ЮЛ из Master Registry.
2. Top-1000 по выручке и крупные consumer brands получают navigation-risk flag; общий брендовый спрос не должен поднимать их в топ автоматически.
3. Для каждого ИНН строим alias dictionary: юрназвание, краткое название, бренд, старые названия, варианты написания, ИНН/ОГРН/КПП, регион.
4. Генерируем high-intent кластеры из `config/seo/company_intent_clusters.yml`: проверить, надежность, риски, отзывы, контакты, реквизиты, директор, учредители, суды, долги, банкротство, финансы, лицензии, РНП, санкции, сотрудничество и т. д.
5. Wordstat снимаем каскадом: сначала название/ИНН/сильные high-intent модификаторы, затем расширяем только компании с подтвержденным спросом.
6. Google Keyword Planner применяем к shortlist из Wordstat, а не ко всей базе.
7. Считаем score на уровне ИНН: verification_demand + identification_demand + risk_demand + reputation_demand + company_data_demand + finance_demand + contact_demand + google_demand + card_completeness - navigation_noise.

## 2. Общая техническая база индексации

- canonical URL `/company/{slug}-{inn}`;
- полезный HTML доступен роботу без обязательного клиентского JS;
- уникальные title/H1/meta description;
- self-canonical;
- корректные HTTP 200/301/404/410;
- `index,follow` только после completeness/privacy gate;
- внутренняя перелинковка;
- `robots.txt` со ссылкой на sitemap index;
- `sitemap-index.xml` и отдельные company sitemaps;
- `lastmod` меняется только при реальном содержательном обновлении.

## 3. Яндекс

1. Добавить домен в Yandex Webmaster и подтвердить права.
2. Создать один счетчик Yandex Metrica на Next Profile и все company pages.
3. Связать Webmaster с Metrica и включить обход по данным счетчика.
4. Отправить sitemap index в Webmaster.
5. Использовать IndexNow для новых, существенно обновленных и удаленных карточек.
6. Следить за индексированием и исключениями.

## 4. Google

1. Создать Domain property в Google Search Console и подтвердить через DNS.
2. Отправить тот же sitemap index в Search Console.
3. Указать sitemap в robots.txt.
4. Для массовых company pages использовать sitemap + внутренние ссылки; URL Inspection — только для отдельных критичных страниц.
5. Google Indexing API для company pages не использовать: официальный API предназначен только для JobPosting и BroadcastEvent/VideoObject.
6. Следить за Page indexing и Performance reports.

## 5. После запуска

Yandex Webmaster и Google Search Console становятся главными источниками фактического SEO-спроса:

- query
- page
- impressions
- clicks
- CTR
- average position
- indexing status

Все данные привязываем к URL и ИНН, затем к semantic cluster.

Публикация партиями: 50 → 200 → 1 000 → 2 000 → 5 000 → 10 000. После каждой партии анализируем discovery/crawl/indexation/impressions/queries/CTR и обновляем рейтинг следующей партии.

## 6. Секреты

Не хранить секреты в GitHub.

Обязательные:

- `YANDEX_METRIKA_COUNTER_ID`
- `YANDEX_WORDSTAT_API_KEY` / параметры Yandex Cloud, требуемые выбранной авторизацией
- `PUBLIC_BASE_URL`
- `INDEXNOW_KEY`

Опциональные позднее:

- Google Search Console API credentials
- Google Ads API credentials
- `KEYS_SO_TOKEN` только если Keys.so будет подключен в будущем

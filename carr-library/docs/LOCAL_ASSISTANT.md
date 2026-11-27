# Локальний асистент

Відкрийте **Tools → Local assistant** або натисніть **Ctrl+Alt+J**. Це керовані локальні команди та версійовані JSON-плани. Вільномовний LLM, онлайн-планування й переклад залишаються окремими незавершеними можливостями платформи.

## Перегляд і виконання

Виберіть бібліотеку та дозволені вхідну й вихідну папки. Для створення нової бібліотеки виберіть відповідний перший пункт. Асистент перевіряє папки знову перед виконанням; зміна шляху через символічне посилання не переносить дозвіл.

У полі Goal введіть одну підтримувану команду на рядок, наприклад:

```text
Створи бібліотеку "Історія"
Імпортуй "C:/Books/source.txt"
Знайди "залізничний транспорт"
```

Інші приклади доступні через Help: список бібліотек і книг, імпорт папки, експорт тексту за ідентифікатором видання, повна резервна копія та озвучення.

Натисніть **Preview plan**. Перегляд показує операції, вибрані папки, бібліотеку та ліміти. Він не виконує самі операції. Зміна інструкції або дозволів скасовує попередній перегляд. **Run / resume** запускає підтверджений план. Статус містить кількість завершених кроків і фактичні результати.

Поля лімітів задають загальну кількість файлів, обсяг імпортованих даних і створених результатів та час завдання. Завершені кроки враховуються повторно при продовженні, тому перезапуск не поновлює вже витрачений ліміт. Перевірка наявних результатів включає їхні контрольні суми.

## Озвучення

Команда **Озвуч:** передає наступний текст як звичайну оповідь. Заголовки, мітки персонажів і квадратні дужки залишаються текстом. Структурований багатоголосий сценарій задається окремим типізованим планом.

Перед виконанням виберіть доступний локальний голос. Для багатомовного голосу потрібно окремо вибрати оголошену ним мову. Асистент не підміняє її першою мовою зі списку. Рендер використовує той самий перевірений FFmpeg і локальні голосові рушії, що й Audio Studio.

Виберіть вихідну папку поза приватними даними бібліотеки. Результат містить усі опубліковані файли рендера: основне аудіо, розділи, часові позначки, субтитри та замовлені доріжки. Наявні файли іншої операції не приймаються за результат цього завдання.

## Пауза, скасування й історія

**Pause** та **Cancel job** передають запит на зупинку. Час реакції залежить від поточної операції; деякі локальні дії завершують поточний етап перед перевіркою скасування. Завершені кроки та придатні аудіофрагменти зберігаються. Під час роботи закриття діалогу спочатку запитує паузу; дочекайтеся повідомлення про збережений стан.

**Saved jobs** відкриває збережені завдання. Продовження перевіряє старий план, дозволи та фактичні результати. Зміна бібліотеки або пошкодження результату потребує перегляду; асистент не перезаписує його мовчки.

## Повна резервна копія

Команда Backup потребує окремої позначки **Allow a complete backup of all my libraries for this plan**. Вона включає всі бібліотеки цього каталогу даних. Загальні ліміти охоплюють базу, оригінали, збережений стан і маніфест; перевищення виявляється до публікації завершеної копії.

Типізовані завдання асистента та їхня історія входять до резервної копії. Після відновлення початкові байти планів і результатів зберігаються для читання. Кожне відновлене завдання отримує окрему перевірку: старий дозвіл на запуск не відновлюється автоматично, навіть якщо шлях каталогу той самий.

Для незавершеного відновленого завдання відкрийте Saved jobs, заново виберіть поточну бібліотеку й папки та натисніть Preview plan. Створиться новий план незавершених кроків. Завершена історія доступна без повторного запуску.

## Межі цієї можливості

Ці локальні інструменти не схвалюють OCR-цитати й не змінюють статус перевірки джерел. Перевірка OCR та імпорту, керування зовнішніми API, приватний транскриптор і повний LLM-асистент мають власні правила та окремі перевірки готовності.


## Optional local model proposals

The **Propose with local model** button reuses an explicitly configured loopback
OpenAI-compatible profile from AI and API connections. The selected local service
receives only the entered goal and fixed planning instructions. It does not receive
library contents, credentials, or permission to invoke tools.

The service must already be installed and running. Select its profile and authorize
the exact one-call request. A returned proposal is bounded, decoded with duplicate
and non-finite JSON fields rejected, and checked by the existing assistant scope
and argument validators. Unknown tools and out-of-scope inputs are rejected.

A successful response fills an editable plan. **Run remains disabled until a
separate Preview succeeds**; receiving a proposal never creates or executes a job.
Pause or closing the busy dialog requests cancellation, and an uncertain request
is not automatically sent again. No remote fallback or model download occurs.

The required test_local_llm suite uses a synthetic local response with the actual
provider gateway and durable budget ledger. The installed Windows assistant probe
also checks the native proposal/Preview boundary, with explicitly synthetic model
transport and approval decisions. These checks do not qualify an actual local
model, language coverage, or model quality. Real engine inference remains a
separate acceptance requirement.

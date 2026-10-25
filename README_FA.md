<p align="center">
  <img src="https://upload.wikimedia.org/wikipedia/commons/f/fd/State_flag_of_Iran_%281964%E2%80%931980%29.svg" width="110" alt="Lion and Sun Flag of Iran">
</p>

# 🐦 ربات تلگرام فالوور توییتر / X

> بدون X API هم کار می‌کند: با یک RSSHub اختصاصی (فقط با کوکی یک اکانت معمولی X) فیدهای پایدار بگیر، یا به RSSHub عمومی و آینه‌های RSS تکیه کن. X API رسمی اختیاری است.
> هر وقت اکانت‌هایی که اضافه کردی توییت جدید بزنن، مستقیم داخل تلگرام برات میفرسته. با متن + عکس + ترجمه فارسی.

---

### ✨ امکانات

- 🔔 ارسال خودکار توییت‌ها به تلگرام با عکس
- 🛰 پشتیبانی از RSSHub اختصاصی — فید پایدار بدون نیاز به X API (دیپلوی روی Railway یا Docker Compose)
- 🦁 ترجمه فارسی خودکار (با هوش مصنوعی یا Google Translate)
- 🚫 فیلتر خودکار ریتوییت‌ها / repostها (بدون افتادن اکانت‌هایی که نام نمایشی‌شان شبیه یوزرنیم است، مثل RippleX)
- 📊 داشبورد وب زنده (PWA)
- ⚡ پشتیبانی از چندین اکانت با دریافت و ترجمه کنترل‌شده برای جلوگیری از محدودیت سرویس‌ها
- 🔄 بررسی خودکار هر ۵ دقیقه

---

### ۱. ساخت ربات تلگرام

1. برو به [@BotFather](https://t.me/BotFather) در تلگرام
2. بزن `/newbot` و اسم و یوزرنیم بده
3. توکنی که بهت میده رو کپی کن (چیزی مثل: `123456:ABC-DEF...`)

---

### ۲. نصب و راه‌اندازی

```bash
# کلون کردن پروژه
git clone https://github.com/YOUR_USER/twitter-telegram-bot.git
cd twitter-telegram-bot

# نصب وابستگی‌ها
pip install -r requirements.txt
```

یک فایل `.env` بساز و این داخلش بزار:

```env
TELEGRAM_BOT_TOKEN=توکنی که از BotFather گرفتی
DATABASE_URL=آدرس PostgreSQL
# اختیاری: فقط اگر دسترسی X API داری
# X_BEARER_TOKEN=توکن Bearer از X Developer Portal
# اختیاری: آدرس RSSHub اختصاصی خودت (پیشنهادی — بخش بعدی)
# RSSHUB_BASE_URL=https://rsshub.app
# اختیاری: اگر روی RSSHub خودت متغیر ACCESS_KEY گذاشتی، همین مقدار را اینجا هم بگذار
# RSSHUB_ACCESS_KEY=
# ترجمه فارسی (به‌صورت پیش‌فرض فعال است)
TRANSLATE_FA=true
# پیشنهاد: فقط کلید Gemini را بگذار؛ برنامه endpoint/model را خودش انتخاب می‌کند
GEMINI_API_KEY=your_gemini_api_key
# برای سرویس OpenAI-compatible دیگر، هر سه مقدار را متناسب با همان سرویس بگذار:
# REQUESTY_API_KEY=your_provider_key
# REQUESTY_BASE_URL=https://provider.example/v1
# REQUESTY_MODEL=your_active_model
```

`X_BEARER_TOKEN` اجباری نیست؛ بدون آن ربات از RSSHub عمومی (`https://rsshub.app/twitter/user/<username>`) و سپس آینه‌های دیگر استفاده می‌کند. این سرویس‌های عمومی ممکن است محدود یا قطع شوند. راه پایدار و بدون توکن: **RSSHub اختصاصی** (بخش «🛰 RSSHub اختصاصی» در پایین همین متن). اگر X API داری، می‌توانی Bearer Token را در Railway Variables تنظیم کنی؛ هرگز آن را در چت یا Git قرار نده. برای استفاده از RSSHub دیگری، `RSSHUB_BASE_URL` را تغییر بده.

---

### ۳. تست لوکال

```bash
# اجرای مستقیم
python twitter_telegram_bot.py
```

ربات روشن میشه. یه پیام `/start` بفرست به رباتت توی تلگرام.

**دستورات تلگرام:**

| دستور | توضیح |
|-------|-------|
| `/start` | راهنما |
| `/add vitalikbuterin` | اضافه کردن اکانت |
| `/add user1 user2` | اضافه چند اکانت همزمان |
| `/del vitalikbuterin` | حذف اکانت |
| `/list` | لیست اکانت‌های دنبال‌شده |
| `/test` | تست سریع با @ElonMusk |

---

### ۴. دیتابیس (PostgreSQL)

برای استفاده از Railway رایگان:

1. Railway.app → یه پروژه بساز
2. PostgreSQL دیتابیس اضافه کن
3. متغیر `DATABASE_URL` خودکار ست میشه
4. در Variables سرویس، `TELEGRAM_BOT_TOKEN` را تنظیم کن. `X_BEARER_TOKEN` اختیاری است؛ بدون آن ربات RSSHub و RSSهای عمومی را امتحان می‌کند.
5. برای ترجمهٔ مطمئن‌تر، `TRANSLATE_FA=true` و `GEMINI_API_KEY` معتبر را بگذار. بدون کلید، برنامه از Google/MyMemory عمومی استفاده می‌کند که ممکن است روی Railway محدود یا مسدود شود. اگر از Gemini استفاده می‌کنی، متغیرهای `REQUESTY_BASE_URL` یا `REQUESTY_MODEL` قدیمی/متناقض را پاک کن تا تنظیم خودکار اعمال شود.
6. (پیشنهادی) یک سرویس RSSHub هم **داخل همین پروژه** اضافه کن: **New → GitHub Repo → `DIYgod/RSSHub`** با متغیرهای `NODE_ENV`، `TWITTER_AUTH_TOKEN` و `ACCESS_KEY`، بعد در ربات `RSSHUB_BASE_URL` و `RSSHUB_ACCESS_KEY` را ست کن — بدون هیچ X API.

---

### ۵. هاست ۲۴ ساعته رایگان

برای اینکه همیشه روشن باشه، روی یکی از اینها رایگان دیپلوی کن:

- **Railway.app** — ساده‌ترین (۵$ credit رایگان ماهانه)
- **Render.com** — plan رایگان
- یا روی یک سرور VPS خودت — با یک دستور `docker compose -f deploy/docker-compose.yml up -d --build` کل استک (RSSHub + دیتابیس + ربات) بالا می‌آید

فقط کافیه فایل‌ها رو آپلود کنی و متغیرهای محیطی رو ست کنی.

---

## 🛰 RSSHub اختصاصی — فید پایدار بدون X API

پایدارترین راه اجرای این ربات، داشتن **RSSHub خودت** است. مسیر `/twitter/user/:username` در RSSHub تایم‌لاین را با **کوکیِ یک اکانت معمولیِ لاگین‌شدهٔ X** می‌خواند — نه اکانت توسعه‌دهنده لازم است، نه API پولی، نه هیچ Bearer Token ای. ربات هم اول سراغ RSSHub اختصاصی تو می‌رود و آینه‌های عمومی فقط به‌عنوان پشتیبان می‌مانند.

### ۱. گرفتن کوکی اکانت X (`TWITTER_AUTH_TOKEN`)

1. با یک مرورگر وارد [x.com](https://x.com) شو — حتماً با **اکانت جدا/مخصوص** (اتوماسیون ممکن است اکانت را محدود کند و این کوکی دسترسی کامل به اکانت را می‌دهد).
2. DevTools را باز کن (`F12`) → بخش **Application** → **Cookies** → `https://x.com`.
3. مقدار `auth_token` را کپی کن (یک رشته هگز حدوداً ۴۰ کاراکتری).

این مقدار را مثل رمز عبور نگه دار؛ از آن **session مرورگر Log out نکن** (کوکی باطل می‌شود) و بدان که می‌توانی چند کوکی از چند اکانت را با کاما جدا کنی تا نوبتی استفاده شوند.

### ۲. راه‌اندازی سرور RSSHub

**راه A — Railway (همان پروژهٔ ربات):**

1. در پروژهٔ Railway خودت: **New** → **GitHub Repo** → مخزن عمومی [`DIYgod/RSSHub`](https://github.com/DIYgod/RSSHub) را مستقیم دیپلوی کن (Railway می‌تواند مخازن عمومی را بدون fork بسازد؛ خود repo دارای Dockerfile است).
2. روی سرویس RSSHub این متغیرها را بگذار:
   - `NODE_ENV=production`
   - `TWITTER_AUTH_TOKEN` — کوکی مرحلهٔ ۱
   - `ACCESS_KEY` — یک رشتهٔ تصادفی بلند تا غریبه‌ها نتوانند از سرورت استفاده کنند
3. **Settings → Networking → Generate Domain** بزن تا آدرس عمومی RSSHub ساخته شود.
4. روی سرویس **ربات** این متغیرها را ست کن:
   - `RSSHUB_BASE_URL=https://<آدرس-rsshub-تو>.up.railway.app`
   - `RSSHUB_ACCESS_KEY` — همان مقدار `ACCESS_KEY`

**راه B — هر سروری که Docker دارد (کل استک با یک دستور):**

```bash
cp deploy/.env.example deploy/.env   # مقادیر را پر کن
docker compose -f deploy/docker-compose.yml up -d --build
```

با این کار **RSSHub + Redis + PostgreSQL + خودِ ربات** به‌صورت یک استک خصوصی بالا می‌آیند و ربات خودکار به `http://rsshub:1200` وصل می‌شود. داشبورد روی پورت `8080` سرو می‌شود.

### ۳. تست

```bash
curl "https://<آدرس-rsshub>/twitter/user/nasa?key=<ACCESS_KEY>"
```

- اگر XML توییت‌ها را برگرداند → درست است.
- لاگ شروع ربات باید بنویسد: `RSSHub <url> is up (HTTP 200 on /healthz)`.
- در تلگرام `/status` بفرست → باید `📡 منبع فید: ✅ RSSHub اختصاصی` ببینی.

### رفع اشکال

| علامت | راه‌حل |
|-------|--------|
| فید می‌گوید `Twitter authentication cookie is not valid` | کوکی منقضی/عوض شده — دوباره وارد شو و `TWITTER_AUTH_TOKEN` را آپدیت کن (تغییر رمز یا Log out کوکی را باطل می‌کند) |
| سرور بالاست ولی تایم‌لاین‌ها خالی‌اند | X معمولاً IPهای دیتاسنتر را مسدود می‌کند — روی سرویس RSSHub متغیر `PROXY_URI` را ست کن (مثلاً `http://user:pass@host:port` یا `socks5h://host:port`) یا از `TWITTER_THIRD_PARTY_API` استفاده کن |
| `RSSHUB_ACCESS_KEY` ربات با سرور یکی نیست | درخواست‌ها 403 می‌گیرند؛ ربات لاگ می‌زند و تا اصلاح، به آینه‌های عمومی برمی‌گردد |
| سرور موقتاً خاموش است | کاری لازم نیست — ربات خودکار به آینه‌های عمومی برمی‌گردد و با برگشتن سرور، دوباره از آن استفاده می‌کند |
| لاگ می‌گوید `Conflict: terminated by other getUpdates` | دو نمونه از ربات با یک توکن poll می‌زنند — نمونه دوم را خاموش کن: دیپلوی قبلی Railway که هنوز در حال drain است، سرویس/replica دوم از همین ریپو در Railway، یا `python twitter_telegram_bot.py` محلی که هنوز باز است |

> ⚠️ خواندن X با کوکی اکانت، خلاف قوانین X است؛ از اکانتی استفاده کن که از دست دادنش مشکلی نداشته باشد.

---

### ⚙️ تنظیمات

| متغیر | پیش‌فرض | توضیح |
|-------|---------|-------|
| `X_BEARER_TOKEN` | خالی | اختیاری؛ با تنظیم آن دریافت timeline از X API انجام می‌شود |
| `X_API_BASE_URL` | `https://api.x.com/2` | ریشهٔ API نسخهٔ ۲ X |
| `X_USER_ID_CACHE_TTL` | `86400` | مدت نگهداری شناسهٔ کاربر X در حافظه (ثانیه) |
| `RSSHUB_BASE_URL` | `https://rsshub.app` | آدرس RSSHub که در حالت بدون X API همراه دیگر منابع RSS امتحان می‌شود |
| `HTTP_MAX_CONNECTIONS` | `50` | اندازهٔ pool اتصال HTTP؛ برای درخواست‌های RSS ظرفیت اضافه نگه می‌دارد |
| `RSS_FETCH_TIMEOUT` | `12` | حداکثر زمان بررسی آینه‌های RSS برای هر حساب (ثانیه) |
| `RSS_MERGE_GRACE` | `0.25` | زمان کوتاه برای ادغام پاسخ آینه‌های سریع پس از اولین فید قابل‌استفاده |
| `MIRROR_BREAKER_SAMPLE` | `12` | حداقل خطاهای سختِ متوالی برای کنار گذاشتن آینه عمومی؛ هنگام بررسی دوره‌ای، آستانه خودکار حداقل ۲ برابر تعداد اکانت‌های دنبال‌شده می‌شود. خطای 404 مخصوص یک اکانت و پاسخ خالی 200 خطای سخت حساب نمی‌شوند. |
| `MIRROR_BREAKER_COOLDOWN` | `1800` | مدت کنار گذاشته‌شدن آینه شکسته (ثانیه)؛ RSSHub اختصاصی خودت هرگز کنار گذاشته نمی‌شود |
| `MIRROR_RATE_LIMIT_COOLDOWN` | `120` | وقفه بعد از HTTP 429 (محدودیت نرخ). آینه‌ای که ما را rate-limit کرده **زنده است**، پس فقط این وقفه‌ی کوتاه را می‌گیرد (نه بنچ ۳۰ دقیقه‌ای) و در شمارش فیل‌ها حساب نمی‌شود |
| `CHECK_INTERVAL` | `300` | فاصله بررسی (ثانیه) |
| `REQUESTY_API_KEY` / `GEMINI_API_KEY` / `OPENAI_API_KEY` | خالی | کلید سرویس AI برای ترجمه باکیفیت‌تر |
| `CONCURRENT_LIMIT` | `3` | تعداد حساب‌هایی که هم‌زمان بررسی می‌شوند |
| `RSS_FETCH_CONCURRENCY` | `12` | سقف کلی درخواست‌های هم‌زمان RSS برای جلوگیری از PoolTimeout |
| `TRANSLATION_TIMEOUT` | `20` | تایم‌اوت هر تلاش ترجمه (ثانیه) |
| `TRANSLATION_DELIVERY_TIMEOUT` | `30` | حداکثر انتظار برای ترجمه؛ بعدش متن اصلی ارسال می‌شود. مقدار پیش‌فرض از تایم‌اوت یک تلاشِ provider بیشتر است تا فرصت ترجمه یا fallback داشته باشد |
| `TRANSLATION_RETRIES` | `2` | تعداد تلاش دوباره برای تلاش‌های اصلی AI/Google |
| `TRANSLATION_CONCURRENT_LIMIT` | `1` | تعداد ترجمه همزمان (برای جلوگیری از محدودیت گوگل کم نگه دار) |
| `TRANSLATION_BACKFILL_LIMIT` | `15` | تعداد توییت‌های بدون ترجمه که در هر نوبت ترمیم می‌شوند |
| `MAX_TWEETS_PER_CHECK` | `10` | تعداد پست‌های ارسال‌شده برای هر حساب در هر نوبت؛ بقیهٔ موارد در نوبت بعد ادامه می‌یابد |
| `MYMEMORY_SOURCE_LANG` | `en` | زبان مبدأ برای fallback غیرگوگلی ترجمه |
| `MYMEMORY_EMAIL` | خالی | ایمیل اختیاری برای سقف رایگان بالاتر MyMemory |
| `LOG_LEVEL` | `INFO` | روی `DEBUG` بگذار تا دلیل نگه‌داشتن یا حذف هر آیتم فید لاگ شود |

---

### 📝 نکات

- اکانت‌های **خصوصی / Private** بدون دسترسی مناسب X API/RSS قابل‌خواندن نیستند
- با تنظیم `X_BEARER_TOKEN` ربات از X API رسمی استفاده می‌کند؛ بدون آن RSSHub عمومی و RSSهای دیگر را امتحان می‌کند، اما دسترسی این منابع عمومی تضمینی نیست
- 🛰 بهترین حالتِ بدون X API: **RSSHub اختصاصی** — سلامتش موقع استارت چک می‌شود و در `/status` هم نمایش داده می‌شود
- ریتوییت‌ها/repostها فیلتر میشن و ارسال نمیشن؛ فیلتر فقط به یوزرنیم واقعی (یعنی لینک استاتوس یا `@handle` موجود در فید) نگاه می‌کنه و نام نمایشی هرگز جای یوزرنیم حساب نمیشه
- با `LOG_LEVEL=DEBUG` دلیل حذف هر توییت لاگ میشه (مثلاً `Skip @nasa 123: status link is @nasa`)
- هر توییت به صورت یک کارت تکی در تلگرام ارسال می‌شود؛ در صورت طولانی شدن کپشن، متن و ترجمه به صورت خودکار خلاصه می‌شوند و هرگز پیام جداگانه‌ای ارسال نمی‌شود
- ترجمه با چند بار تلاش دوباره انجام میشه و اگر AI خطا بده، از Google/MyMemory fallback استفاده میشه
- توییت‌های ذخیره‌شده‌ای که ترجمه خالی دارند، به صورت خودکار در پس‌زمینه برای دیتابیس و داشبورد بک‌فیل می‌شوند و پیام جداگانه به تلگرام فرستاده نمی‌شود
- توییت‌ها با عکس هم ارسال میشن
- تصاویر با lazy loading در وب داشبورد بارگذاری میشن
- Service Worker برای حالت آفلاین فعاله

---

### 🛠 ساختار پروژه

```
twitter-telegram-bot/
├── twitter_telegram_bot.py   # ربات اصلی + وب سرور
├── database.py               # مدیریت PostgreSQL
├── templates/index.html      # قالب داشبورد وب
├── sw.js                     # Service Worker
├── manifest.json             # PWA Manifest
├── requirements.txt          # وابستگی‌ها
└── Procfile                  # فایل هاست
```

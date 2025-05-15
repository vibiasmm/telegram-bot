import os
import time
import base64
import logging
import threading
import asyncio
import re
from datetime import datetime, time as dt_time, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from twocaptcha import TwoCaptcha
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# === LOG AYARLARI ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# APScheduler için debug logları
logging.getLogger('apscheduler').setLevel(logging.DEBUG)

# === AYARLAR ===
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")
CAPTCHA_KEY = os.getenv("TWOCAPTCHA_API_KEY")
SITES_FILE_PATH = os.getenv("SITES_FILE_PATH", "sites_v2.txt")

# Token ve diğer zorunlu değişkenlerin kontrolü
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_TOKEN çevre değişkeni tanımlı değil. Lütfen .env dosyasını kontrol edin.")
if not GROUP_ID:
    raise ValueError("GROUP_ID çevre değişkeni tanımlı değil. Lütfen .env dosyasını kontrol edin.")
if not CAPTCHA_KEY:
    raise ValueError("TWOCAPTCHA_API_KEY çevre değişkeni tanımlı değil. Lütfen .env dosyasını kontrol edin.")

app = Application.builder().token(BOT_TOKEN).build()
solver = TwoCaptcha(CAPTCHA_KEY)
scheduler = AsyncIOScheduler(timezone="Europe/Istanbul")

# === SABİTLER ===
class BotConfig:
    BTK_URL = "https://internet.btk.gov.tr/sitesorgu/"
    MAX_ATTEMPTS = 3
    PAGE_LOAD_TIMEOUT = 20
    ELEMENT_TIMEOUT = 20
    SCROLL_PAUSE = 2
    QUERY_INTERVAL = 2 * 60  # 2 dakika (saniye cinsinden)

# === GLOBAL DURUM ===
current_site_index = 0
next_query_time = time.time()
is_query_running = False

def is_working_hours() -> bool:
    """Botun çalışma saatleri ve günleri içinde olup olmadığını kontrol et."""
    now = datetime.now(ZoneInfo("Europe/Istanbul"))
    is_weekday = now.weekday() < 5  # Pazartesi-Cuma: 0-4
    current_time = now.time()
    start_time = dt_time(8, 0)  # 08:00
    end_time = dt_time(21, 0)   # 21:00
    result = is_weekday and start_time <= current_time <= end_time
    logger.info(f"is_working_hours: {result}, Gün: {now.weekday()}, Saat: {current_time}")
    return result

def setup_driver(headless: bool = True) -> webdriver.Chrome:
    """Selenium WebDriver'ı anti-detection seçenekleriyle kur."""
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--enable-unsafe-swiftshader")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--disable-webgl")
    chrome_options.add_argument("--disable-gpu-sandbox")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=chrome_options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    logger.info("Chromedriver başlatıldı.")
    return driver

def solve_captcha(driver, captcha_image) -> Optional[str]:
    """2Captcha kullanarak görsel captchayı çöz."""
    logger.info("Captcha çözülüyor...")
    try:
        WebDriverWait(driver, BotConfig.ELEMENT_TIMEOUT).until(
            lambda d: d.execute_script(
                "return arguments[0].complete && arguments[0].naturalWidth > 0 && arguments[0].naturalHeight > 0",
                captcha_image
            )
        )
        logger.info("Captcha görseli yüklendi.")

        captcha_base64 = driver.execute_script("""
            var img = arguments[0];
            var canvas = document.createElement('canvas');
            canvas.width = img.naturalWidth;
            canvas.height = img.naturalHeight;
            var ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0);
            return canvas.toDataURL('image/png').split(',')[1];
        """, captcha_image)

        if not captcha_base64:
            raise ValueError("Captcha görseli base64 formatında alınamadı.")

        captcha_image_path = "captcha.png"
        with open(captcha_image_path, "wb") as f:
            f.write(base64.b64decode(captcha_base64))
        logger.info(f"Captcha görseli kaydedildi: {captcha_image_path}")

        if os.path.getsize(captcha_image_path) == 0:
            raise ValueError("Captcha görsel dosyası boş.")

        result = solver.solve(
            file=captcha_image_path,
            caseSensitive=True,
            hintText="Enter exactly as shown, uppercase/lowercase matters."
        )
        captcha_code = result["code"]
        logger.info(f"Captcha çözüldü: {captcha_code}")
        os.remove(captcha_image_path)
        logger.info(f"Captcha dosyası silindi: {captcha_image_path}")
        return captcha_code
    except Exception as e:
        logger.error(f"CAPTCHA çözüm hatası: {e}")
        return None

def check_site_status(domain: str) -> Tuple[str, Optional[str]]:
    """BTK sitesinde bir domainin durumunu kontrol et."""
    logger.info(f"Site sorgulanıyor: {domain}")
    driver = None
    try:
        driver = setup_driver()
        driver.get(BotConfig.BTK_URL)
        WebDriverWait(driver, BotConfig.PAGE_LOAD_TIMEOUT).until(
            EC.presence_of_element_located((By.ID, "deger"))
        )
        logger.info("BTK sayfası yüklendi.")

        for attempt in range(BotConfig.MAX_ATTEMPTS):
            input_box = driver.find_element(By.ID, "deger")
            input_box.clear()
            input_box.send_keys(domain)
            logger.info("Domain girildi.")

            captcha_image = WebDriverWait(driver, BotConfig.ELEMENT_TIMEOUT).until(
                EC.presence_of_element_located((By.XPATH, "//img[@alt='Güvenlik Kodu']"))
            )
            captcha_code = solve_captcha(driver, captcha_image)
            if not captcha_code:
                logger.warning(f"CAPTCHA çözülemedi. Deneme {attempt+1}/{BotConfig.MAX_ATTEMPTS}")
                driver.refresh()
                WebDriverWait(driver, BotConfig.PAGE_LOAD_TIMEOUT).until(
                    EC.presence_of_element_located((By.ID, "deger"))
                )
                continue

            driver.find_element(By.ID, "security_code").clear()
            driver.find_element(By.ID, "security_code").send_keys(captcha_code)
            driver.find_element(By.ID, "submit1").click()
            logger.info("Sorgu gönderildi.")

            WebDriverWait(driver, BotConfig.PAGE_LOAD_TIMEOUT).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(BotConfig.SCROLL_PAUSE)

            if "Güvenlik kodunu yanlış girdiniz" in driver.page_source:
                logger.info(f"Geçersiz CAPTCHA. Deneme {attempt+1}/{BotConfig.MAX_ATTEMPTS}")
                driver.refresh()
                WebDriverWait(driver, BotConfig.PAGE_LOAD_TIMEOUT).until(
                    EC.presence_of_element_located((By.ID, "deger"))
                )
                continue
            break
        else:
            return f"CAPTCHA {BotConfig.MAX_ATTEMPTS} denemede de çözülemedi.", None

        try:
            WebDriverWait(driver, BotConfig.ELEMENT_TIMEOUT).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(translate(text(), ' ', ''), 'BilgiTeknolojileriveİletişimKurumutarafındanuygulananbirkararbulunamadı') or contains(translate(., ' ', ''), 'erişim') and contains(translate(., ' ', ''), 'engellen')]")
                )
            )
        except Exception as e:
            logger.error(f"Sonuç elementi bulunamadı: {e}")

        total_height = driver.execute_script("return Math.max(document.body.scrollHeight, document.body.offsetHeight, document.documentElement.clientHeight, document.documentElement.scrollHeight, document.documentElement.offsetHeight);")
        total_width = driver.execute_script("return Math.max(document.body.scrollWidth, document.body.offsetWidth, document.documentElement.clientWidth, document.documentElement.scrollWidth, document.documentElement.offsetWidth);")

        driver.set_window_size(total_width, total_height)
        time.sleep(1)

        screenshot_path = f"screenshot_{int(datetime.now().timestamp())}.png"
        try:
            driver.save_screenshot(screenshot_path)
            logger.info(f"Tam sayfa ekran görüntüsü kaydedildi: {screenshot_path}")
        except Exception as e:
            logger.error(f"Ekran görüntüsü hatası: {e}")
            screenshot_path = None

        page_source = driver.page_source
        if "uygulanan bir karar bulunamadı" in page_source:
            result = "Erişim serbest"
        elif "erişim" in page_source and "engellen" in page_source:
            result = "Erişim engelli"
        else:
            result = "Sonuç alınamadı"

        return result, screenshot_path

    except Exception as e:
        logger.error(f"Site sorgulama hatası: {e}")
        screenshot_path = f"error_screenshot_{int(datetime.now().timestamp())}.png"
        if driver:
            driver.save_screenshot(screenshot_path)
            logger.info(f"Hata ekran görüntüsü kaydedildi: {screenshot_path}")
        return f"Hata: {str(e)}", screenshot_path
    finally:
        if driver:
            driver.quit()
            logger.info("Chromedriver kapatıldı.")

async def send_to_telegram(message: str, screenshot: Optional[str] = None, parse_mode: str = None) -> None:
    """Mesajı ve isteğe bağlı ekran görüntüsünü Telegram grubuna gönder."""
    logger.info(f"Mesaj gönderiliyor: {message}")
    try:
        formatted_message = f"{message}\nZaman: {datetime.now(ZoneInfo('Europe/Istanbul')).strftime('%Y-%m-%d %H:%M:%S')}"
        await app.bot.send_message(chat_id=GROUP_ID, text=formatted_message, parse_mode=parse_mode)
        if screenshot and os.path.exists(screenshot):
            logger.info(f"Ekran görüntüsü gönderiliyor: {screenshot}")
            with open(screenshot, 'rb') as photo:
                await app.bot.send_photo(chat_id=GROUP_ID, photo=photo)
            os.remove(screenshot)
            logger.info(f"Ekran görüntüsü silindi: {screenshot}")
    except Exception as e:
        logger.error(f"Telegram gönderim hatası: {e}")

def load_sites() -> List[str]:
    """sites_v2.txt dosyasından siteleri yükle."""
    logger.info(f"Siteler yükleniyor: {SITES_FILE_PATH}")
    if not os.path.exists(SITES_FILE_PATH):
        raise FileNotFoundError(f"Siteler dosyası bulunamadı: {SITES_FILE_PATH}")
    if not os.access(SITES_FILE_PATH, os.R_OK):
        raise PermissionError(f"Siteler dosyası okunamıyor: {SITES_FILE_PATH}")
    
    with open(SITES_FILE_PATH, "r", encoding="utf-8") as file:
        sites = [line.strip() for line in file if line.strip()]
    if not sites:
        raise ValueError("Siteler dosyası boş.")
    logger.info(f"Yüklenen siteler: {sites}")
    return sites

def update_sites_file(sites: List[str]) -> None:
    """sites_v2.txt dosyasını güncelle."""
    logger.info(f"Siteler dosyası güncelleniyor: {SITES_FILE_PATH}")
    try:
        with open(SITES_FILE_PATH, "w", encoding="utf-8") as file:
            for site in sites:
                file.write(f"{site}\n")
        logger.info(f"Siteler dosyası güncellendi: {sites}")
    except Exception as e:
        logger.error(f"Siteler dosyası güncelleme hatası: {e}")
        raise

def is_valid_domain(domain: str) -> bool:
    """Basit domain formatı kontrolü."""
    pattern = r'^[a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9]\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, domain))

async def add_site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /add komutu ile site ekle."""
    logger.info("/add komutu alındı.")
    if not context.args:
        await update.message.reply_text("Lütfen bir domain belirtin. Örnek: /add example.com")
        return

    domain = context.args[0].strip()
    if not is_valid_domain(domain):
        await update.message.reply_text(f"Hata: '{domain}' geçerli bir domain formatında değil.")
        return

    try:
        sites = load_sites()
        if domain in sites:
            await update.message.reply_text(f"Hata: '{domain}' zaten listede mevcut.")
            return

        sites.append(domain)
        update_sites_file(sites)
        await update.message.reply_text(f"Başarılı: '{domain}' siteler listesine eklendi.")
        logger.info(f"Domain eklendi: {domain}")
    except Exception as e:
        await update.message.reply_text(f"Hata: Site eklenemedi: {str(e)}")
        logger.error(f"Site ekleme hatası: {e}")

async def remove_site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /remove komutu ile site sil."""
    logger.info("/remove komutu alındı.")
    if not context.args:
        await update.message.reply_text("Lütfen bir domain belirtin. Örnek: /remove example.com")
        return

    domain = context.args[0].strip()
    try:
        sites = load_sites()
        if domain not in sites:
            await update.message.reply_text(f"Hata: '{domain}' listede bulunamadı.")
            return

        sites.remove(domain)
        update_sites_file(sites)
        global current_site_index
        if current_site_index >= len(sites) and sites:
            current_site_index = 0
        elif not sites:
            current_site_index = 0
        await update.message.reply_text(f"Başarılı: '{domain}' siteler listesinden silindi.")
        logger.info(f"Domain silindi: {domain}")
    except Exception as e:
        await update.message.reply_text(f"Hata: Site silinemedi: {str(e)}")
        logger.error(f"Site silme hatası: {e}")

async def next_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /next komutu ile bir sonraki sorguya kalan süreyi göster."""
    logger.info("/next komutu alındı.")
    if not is_working_hours():
        await update.message.reply_text(
            "Bot şu anda çalışma saatleri dışında. Çalışma saatleri: Hafta içi 08:00-21:00.\n"
            f"Zaman: {datetime.now(ZoneInfo('Europe/Istanbul')).strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return

    current_time = time.time()
    remaining = next_query_time - current_time
    if remaining > 0 and not is_query_running:
        minutes, seconds = divmod(int(remaining), 60)
        await update.message.reply_text(
            f"Bir sonraki sorguya kalan süre: {minutes:02d}:{seconds:02d}\n"
            f"Zaman: {datetime.now(ZoneInfo('Europe/Istanbul')).strftime('%Y-%m-%d %H:%M:%S')}"
        )
    else:
        await update.message.reply_text(
            f"Sorgulama şu anda yapılıyor...\n"
            f"Zaman: {datetime.now(ZoneInfo('Europe/Istanbul')).strftime('%Y-%m-%d %H:%M:%S')}"
        )

def countdown_timer():
    """Terminalde bir sonraki sorguya kalan süreyi göster."""
    while True:
        if not is_working_hours():
            print("\rBot çalışma saatleri dışında. (Hafta içi 08:00-21:00)", end="")
            time.sleep(60)
            continue

        current_time = time.time()
        remaining = next_query_time - current_time
        if remaining > 0 and not is_query_running:
            minutes, seconds = divmod(int(remaining), 60)
            print(f"\rBir sonraki sorguya kalan süre: {minutes:02d}:{seconds:02d}", end="")
        else:
            print("\rSorgulama yapılıyor...                    ", end="")
        time.sleep(1)

async def test_job(context: ContextTypes.DEFAULT_TYPE = None) -> None:
    """Site sorgulama işini çalıştır."""
    global next_query_time, current_site_index, is_query_running
    if not is_working_hours():
        logger.info("Sorgulama çalışma saatleri dışında, atlanıyor.")
        return

    is_query_running = True
    logger.info("Sorgulama işlemi başlatılıyor.")
    try:
        sites = load_sites()
        if not sites:
            await send_to_telegram("Hata: Siteler dosyası boş. Bot durduruluyor.")
            raise ValueError("Siteler dosyası boş.")

        # Mevcut siteyi kontrol et
        current_site = sites[current_site_index]
        await send_to_telegram(f"Sorgulama yapılıyor: {current_site}")
        result, screenshot = check_site_status(current_site)
        await send_to_telegram(f"Sonuç: {current_site} - {result}", screenshot)

        # Sonuca göre işlem yap
        if result == "Erişim serbest":
            logger.info(f"{current_site} erişim serbest, aynı site tekrar kontrol edilecek.")
            # current_site_index değişmez, aynı site tekrar kontrol edilir
        elif result == "Erişim engelli":
            logger.info(f"{current_site} erişim engelli, listeden siliniyor.")
            # Mevcut siteyi listeden sil
            sites.pop(current_site_index)
            update_sites_file(sites)
            # Erişim engeli protokolü mesajı
            await send_to_telegram(
                f"**EERİŞİM ENGELİ PROTOKOLÜ: {current_site} değiştirildi. Sorgulanacak domain güncellendi.**",
                parse_mode="Markdown"
            )
            # İndeks sabit kalır, çünkü bir sonraki site artık mevcut indekste
            if not sites:
                await send_to_telegram("Hata: Tüm siteler erişim engelli, liste boş. Bot durduruluyor.")
                raise ValueError("Siteler dosyası boş.")
        else:
            logger.info(f"{current_site} için sonuç alınamadı, aynı site tekrar kontrol edilecek.")
            # Sonuç alınamadıysa, aynı siteyi tekrar kontrol et

    except FileNotFoundError as e:
        await send_to_telegram(f"Hata: Siteler dosyası bulunamadı: {e}")
    except PermissionError as e:
        await send_to_telegram(f"Hata: Siteler dosyası okunamıyor: {e}")
    except ValueError as e:
        await send_to_telegram(f"Hata: {e}")
    except Exception as e:
        await send_to_telegram(f"Hata: Sorgulama sırasında bir sorun oluştu: {e}")
    finally:
        is_query_running = False
        # Bir sonraki sorgu zamanını güncelle (21:00'ı geçmemek kaydıyla)
        now = datetime.now(ZoneInfo("Europe/Istanbul"))
        end_of_day = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if now.time() < dt_time(21, 0):
            next_query_time = time.time() + BotConfig.QUERY_INTERVAL
        else:
            # Ertesi gün 08:00 için zamanı ayarla
            next_day = now.replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
            while next_day.weekday() > 4:  # Hafta sonunu atla
                next_day += timedelta(days=1)
            next_query_time = next_day.timestamp()
        logger.info("Sorgulama işlemi tamamlandı.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /start komutunu işle."""
    logger.info("/start komutu alındı.")
    if not is_working_hours():
        await update.message.reply_text(
            "Bot şu anda çalışma saatleri dışında. Çalışma saatleri: Hafta içi 08:00-21:00.\n"
            f"Zaman: {datetime.now(ZoneInfo('Europe/Istanbul')).strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return

    await update.message.reply_text("Sorgulama işlemi başlatılıyor...")
    await test_job(context)

def schedule_jobs():
    """Hafta içi her gün 08:00-21:00 arasında 2 dakikada bir sorgulama yap."""
    logger.info("Zamanlama işleri ayarlanıyor...")
    # Her 2 dakikada bir sorgulama, sadece hafta içi 08:00-20:58
    scheduler.add_job(
        test_job,
        trigger=CronTrigger(
            day_of_week='mon-fri',
            hour='8-20',
            minute='*/2',
            second=0,
            timezone='Europe/Istanbul'
        ),
        id='query_job',
        replace_existing=True
    )
    # Son sorgu için 21:00'da bir iş ekle
    scheduler.add_job(
        test_job,
        trigger=CronTrigger(
            day_of_week='mon-fri',
            hour=21,
            minute=0,
            second=0,
            timezone='Europe/Istanbul'
        ),
        id='last_query_job',
        replace_existing=True
    )
    logger.info("Zamanlama başlatıldı: Hafta içi her gün 08:00-21:00, her 2 dakikada bir sorgulama.")

async def main() -> None:
    """Ana fonksiyon, botu çalıştırır."""
    logger.info("Bot başlatılıyor...")
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_site))
    app.add_handler(CommandHandler("remove", remove_site))
    app.add_handler(CommandHandler("next", next_query))
    
    # Çalışma saatleri içindeyse hemen bir sorgu çalıştır
    if is_working_hours():
        logger.info("Çalışma saatleri içinde, başlangıç sorgusu çalıştırılıyor.")
        await test_job(None)
    else:
        logger.info("Çalışma saatleri dışında, başlangıç sorgusu atlanıyor.")
    
    # Zamanlayıcıyı başlat
    schedule_jobs()
    scheduler.start()
    logger.info("Zamanlayıcı başlatıldı.")
    
    # Zamanlayıcıyı ayrı bir iş parçacığında başlat
    timer_thread = threading.Thread(target=countdown_timer, daemon=True)
    timer_thread.start()
    
    # Botu başlat
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Bot çalışmaya başladı.")
    
    # Botun çalışmasını sürdürmek için bir olay oluştur
    stop_event = asyncio.Event()
    
    try:
        # Botun çalışmasını sürdürmek için stop_event'i bekle
        await stop_event.wait()
    except KeyboardInterrupt:
        logger.info("Bot durduruluyor...")
    finally:
        scheduler.shutdown()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info("Bot durduruldu.")

if __name__ == "__main__":
    import asyncio
    # Yeni olay döngüsü oluştur
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
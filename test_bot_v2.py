import os
import logging
from typing import List
from datetime import datetime
from zoneinfo import ZoneInfo
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from twocaptcha import TwoCaptcha
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Logging yapılandırması
logging.basicConfig(
    filename="/app/bot.log",
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Ortam değişkenlerini yükle
load_dotenv()

# Sabitler
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")
TWOCAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY")
SITES_FILE_PATH = os.getenv("SITES_FILE_PATH", "/app/sites_v2.txt")
ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")

class BotConfig:
    QUERY_INTERVAL = 2 * 60  # 2 dakika
    MAX_RETRIES = 3
    WORKING_HOURS = {"start": 8, "end": 21}  # 08:00-21:00
    WORKING_DAYS = ["mon", "tue", "wed", "thu", "fri"]

def load_sites() -> List[str]:
    """Siteleri dosyadan veya ortam değişkeninden yükle."""
    logger.info("Siteler yükleniyor...")
    sites_str = os.getenv("SITES_LIST")
    if sites_str:
        sites = [site.strip() for site in sites_str.split(",") if site.strip()]
        logger.info(f"Ortam değişkeninden yüklenen siteler: {sites}")
        return sites
    if not os.path.exists(SITES_FILE_PATH):
        raise FileNotFoundError(f"Siteler dosyası bulunamadı: {SITES_FILE_PATH}")
    with open(SITES_FILE_PATH, "r", encoding="utf-8") as file:
        sites = [line.strip() for line in file if line.strip()]
    logger.info(f"Dosyadan yüklenen siteler: {sites}")
    return sites

def update_sites_file(sites: List[str]) -> None:
    """Siteleri dosyaya yaz."""
    logger.info("Siteler dosyaya yazılıyor: %s", sites)
    with open(SITES_FILE_PATH, "w", encoding="utf-8") as file:
        file.write("\n".join(sites) + "\n")

def setup_driver(headless: bool = True) -> webdriver.Chrome:
    """Selenium WebDriver'ı anti-detection seçenekleriyle kur."""
    logger.info("Chromedriver başlatılıyor...")
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)

    # Render için Chrome yolu
    chrome_path = "/usr/bin/google-chrome"
    if not os.path.exists(chrome_path):
        logger.error("Google Chrome bulunamadı: %s", chrome_path)
        raise RuntimeError("Google Chrome yüklü değil. Render buildpack'lerini kontrol edin.")

    chrome_options.binary_location = chrome_path
    logger.debug("Chrome yolu: %s", chrome_path)

    # ChromeDriver yolu
    chromedriver_path = "/usr/lib/chromium/chromedriver"
    if not os.path.exists(chromedriver_path):
        logger.error("Chromedriver bulunamadı: %s", chromedriver_path)
        raise RuntimeError("Chromedriver yüklü değil.")

    try:
        driver = webdriver.Chrome(executable_path=chromedriver_path, options=chrome_options)
        logger.info("Chromedriver başarıyla başlatıldı.")
    except Exception as e:
        logger.error("Chromedriver başlatma hatası: %s", e, exc_info=True)
        raise

    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver

async def check_site_status(context: ContextTypes.DEFAULT_TYPE, site: str) -> tuple[str, str]:
    """Siteyi BTK'da sorgula."""
    logger.info("Site sorgulanıyor: %s", site)
    driver = None
    status = "Erişim serbest"
    screenshot_path = f"/app/{site}_screenshot.png"

    try:
        driver = setup_driver()
        driver.get("https://internet.btk.gov.tr/tr/sorgu/sorgula")

        # Sorgu alanına siteyi gir
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "domainInput"))
        ).send_keys(site)

        # reCAPTCHA'yı çöz
        solver = TwoCaptcha(TWOCAPTCHA_API_KEY)
        recaptcha = driver.find_element(By.CLASS_NAME, "g-recaptcha")
        site_key = recaptcha.get_attribute("data-sitekey")
        captcha_result = solver.recaptcha(
            sitekey=site_key,
            url=driver.current_url
        )
        driver.execute_script(
            f'document.getElementById("g-recaptcha-response").innerHTML="{captcha_result["code"]}";'
        )

        # Sorgula butonuna tıkla
        driver.find_element(By.ID, "sorgulaButton").click()

        # Sonucu kontrol et
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "sonucMesaji"))
        )
        result_text = driver.find_element(By.CLASS_NAME, "sonucMesaji").text
        if "erişimi engellenmiştir" in result_text.lower():
            status = "Erişim engelli"

        # Ekran görüntüsü al
        driver.save_screenshot(screenshot_path)

    except Exception as e:
        logger.error("Sorgulama hatası: %s", e, exc_info=True)
        status = f"Hata: {str(e)}"
        if driver:
            driver.save_screenshot(screenshot_path)

    finally:
        if driver:
            driver.quit()

    return status, screenshot_path

async def test_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Planlanmış sorgulama görevi."""
    if not GROUP_ID:
        logger.error("GROUP_ID tanımlı değil.")
        return

    sites = load_sites()
    if not sites:
        logger.warning("Sorgulanacak site yok.")
        await context.bot.send_message(GROUP_ID, "Sorgulanacak site yok.")
        return

    current_site = sites[0]
    logger.info("Sorgulama yapılıyor: %s", current_site)
    await context.bot.send_message(
        GROUP_ID,
        f"Sorgulama yapılıyor: {current_site}\nZaman: {datetime.now(ISTANBUL_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
    )

    status, screenshot_path = await check_site_status(context, current_site)
    await context.bot.send_photo(
        GROUP_ID,
        photo=open(screenshot_path, "rb"),
        caption=(
            f"Sonuç: {current_site} - {status}\n"
            f"Zaman: {datetime.now(ISTANBUL_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
        )
    )

    if "Erişim engelli" in status:
        logger.info("Erişim engelli: %s", current_site)
        sites.pop(0)
        if sites:
            next_site = sites[0]
            update_sites_file(sites)
            await context.bot.send_message(
                GROUP_ID,
                (
                    f"**EERİŞİM ENGELİ PROTOKOLÜ: {current_site} değiştirildi. "
                    f"Sorgulanacak domain güncellendi.**\n"
                    f"Zaman: {datetime.now(ISTANBUL_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
                )
            )
        else:
            await context.bot.send_message(
                GROUP_ID,
                "Tüm siteler engellendi. Yeni site ekleyin."
            )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Başlangıç komutu."""
    await update.message.reply_text("Bot çalışıyor! Komutlar: /add, /remove, /next")

async def add_site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Yeni site ekle."""
    if not context.args:
        await update.message.reply_text("Lütfen bir site belirtin: /add example.com")
        return

    site = context.args[0].strip()
    sites = load_sites()
    if site in sites:
        await update.message.reply_text(f"{site} zaten listede.")
        return

    sites.append(site)
    update_sites_file(sites)
    logger.info("Site eklendi: %s", site)
    await update.message.reply_text(f"{site} listeye eklendi.")

async def remove_site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Siteyi listeden çıkar."""
    if not context.args:
        await update.message.reply_text("Lütfen bir site belirtin: /remove example.com")
        return

    site = context.args[0].strip()
    sites = load_sites()
    if site not in sites:
        await update.message.reply_text(f"{site} listede değil.")
        return

    sites.remove(site)
    update_sites_file(sites)
    logger.info("Site silindi: %s", site)
    await update.message.reply_text(f"{site} listeden çıkarıldı.")

async def next_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bir sonraki sorguya kalan süreyi göster."""
    scheduler = context.bot_data.get("scheduler")
    if not scheduler:
        await update.message.reply_text("Planlanmış görev bulunamadı.")
        return

    next_run = scheduler.get_job("query_job").next_run_time
    time_diff = (next_run - datetime.now(ISTANBUL_TZ)).total_seconds()
    minutes, seconds = divmod(int(time_diff), 60)
    await update.message.reply_text(
        f"Bir sonraki sorguya kalan süre: {minutes:02d}:{seconds:02d}"
    )

def schedule_jobs(app: Application) -> None:
    """Planlanmış görevleri ayarla."""
    scheduler = AsyncIOScheduler(timezone=ISTANBUL_TZ)
    scheduler.add_job(
        test_job,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="8-20",
            minute="*/2",  # 2 dakikada bir
            second=0,
            timezone=ISTANBUL_TZ
        ),
        id="query_job",
        replace_existing=True,
        args=[app],
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info("Planlanmış görevler başlatıldı.")

async def main() -> None:
    """Ana fonksiyon, botu çalıştırır."""
    logger.info("Bot başlatılıyor...")
    app = Application.builder().token(BOT_TOKEN).post_init(lambda app: app.bot.set_webhook(None)).analytics(False).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_site))
    app.add_handler(CommandHandler("remove", remove_site))
    app.add_handler(CommandHandler("next", next_query))

    schedule_jobs(app)
    await app.run_polling()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
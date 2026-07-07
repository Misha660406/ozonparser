import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException
import pandas as pd
import time
import re
import os
import sys
from urllib.parse import urljoin
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium.webdriver.common.action_chains import ActionChains


# Сопоставление URL категорий с поисковыми запросами для обхода заглушек
CATEGORY_SEARCH_QUERIES = {
    "holodilniki-10502": "холодильники",
    "stiralnye-mashiny-10537": "стиральные машины",
    "kuhonnye-plity-10515": "кухонные плиты",
    "morozilnye-kamery-10504": "морозильные камеры",
    "posudomoechnye-mashiny-10534": "посудомоечные машины",
    "vstraivaemaya-krupnaya-bytovaya-tehnika-10543": "встраиваемая крупная бытовая техника"
}


# ──────────────────────────── УТИЛИТЫ ────────────────────────────

def load_existing_data(filename):
    if os.path.exists(filename):
        try:
            df = pd.read_excel(filename)
            inns = set(df['ИНН'].dropna().astype(str)) if 'ИНН' in df.columns else set()
            names = set(df['Наименование'].dropna().astype(str).str.upper()) if 'Наименование' in df.columns else set()
            return inns, names
        except Exception as e:
            print(f"Ошибка чтения старого файла: {e}")
    return set(), set()


def is_session_alive(driver):
    try:
        _ = driver.current_window_handle
        return True
    except Exception:
        return False


def safe_get(driver, url, retries=3):
    for attempt in range(retries):
        if not is_session_alive(driver):
            return False
        try:
            driver.get(url)
            time.sleep(1)
            _ = driver.find_element(By.TAG_NAME, "body")
            return True
        except TimeoutException:
            pass
        except Exception:
            time.sleep(3)
    return False


def handle_error_stub(driver, max_retries=3):
    for attempt in range(max_retries):
        if not is_session_alive(driver):
            return False
        try:
            page_source = driver.page_source or ""
        except Exception:
            return False

        low_source = page_source.lower()
        if "cloudflare" in low_source or "verify you are human" in low_source \
           or "подтвердите, что вы человек" in page_source or "captcha" in low_source:
            print("\n🛑 Найдена проверка Cloudflare / Капча! Пройдите вручную...")
            time.sleep(15)
            return True

        if "Произошла ошибка" not in page_source and "Обновить страницу" not in page_source:
            return True

        try:
            driver.refresh()
        except Exception:
            pass
        time.sleep(5)
    return True


def save_results(results, excel_filename, lock):
    """Сохраняет результаты, используя блокировку потока для безопасности файла."""
    if not results:
        return
    
    with lock:  # Защита от одновременной записи разными браузерами
        new_df = pd.DataFrame(results)
        if os.path.exists(excel_filename):
            try:
                old_df = pd.read_excel(excel_filename)
                combined_df = pd.concat([old_df, new_df], ignore_index=True)
                if 'ИНН' in combined_df.columns:
                    combined_df.drop_duplicates(subset=['ИНН'], inplace=True, keep='last')
            except Exception as e:
                print(f"Ошибка чтения старого файла при сохранении: {e}")
                combined_df = new_df
        else:
            combined_df = new_df
        combined_df.to_excel(excel_filename, index=False)


# ──────────────────────────── CHECKO ────────────────────────────

def find_inn_by_ogrn(driver, ogrn):
    if not is_session_alive(driver):
        return "Не найден"
    original_window = driver.current_window_handle
    try:
        driver.switch_to.new_window('tab')
        driver.get(f"https://checko.ru/search?query={ogrn}")
        time.sleep(3)
        page_text = driver.find_element(By.TAG_NAME, "body").text
        inn_match = re.search(r'(?i)ИНН[\s\:\.\-]*(\d{10}|\d{12})\b', page_text)
        return inn_match.group(1) if inn_match else "Не найден"
    except Exception:
        return "Не найден"
    finally:
        try:
            driver.close()
            driver.switch_to.window(original_window)
        except Exception:
            pass


# ──────────────────────────── ТОВАР ────────────────────────────

def process_product(driver, product_url, seller_name):
    """seller_name передается из меню фильтров, на случай если ООО не найдется"""
    if not safe_get(driver, product_url, retries=2):
        return None

    time.sleep(2)

    # 1. Сбиваем всплывающие подсказки Озона (нажатием ESC)
    try:
        body = driver.find_element(By.TAG_NAME, 'body')
        body.send_keys(Keys.ESCAPE)
        time.sleep(0.3)
        body.send_keys(Keys.ESCAPE)
    except Exception:
        pass

    seller_link = "Не найдена"
    try:
        link_elements = driver.find_elements(By.XPATH, "//a[contains(@href, '/seller/')]")
        for elem in link_elements:
            href = elem.get_attribute("href")
            if href and '/seller/' in href:
                seller_link = urljoin("https://www.ozon.ru", href.split('?')[0])
                break
    except Exception:
        pass

    # 2. ИЩЕМ И НАВОДИМ МЫШКУ НА "О МАГАЗИНЕ"
    info_btn = None
    window_opened = False

    # Делаем до 15 небольших скроллов
    for step in range(15):
        try:
            buttons = driver.find_elements(
                By.XPATH, 
                "//*[normalize-space(text())='О магазине' or normalize-space(text())='О продавце' or contains(normalize-space(text()), 'Информация о продавце')]"
            )
            
            valid_buttons = [b for b in buttons if b.is_displayed()]
            
            if valid_buttons:
                info_btn = valid_buttons[-1]
                
                # Центрируем элемент на экране
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", info_btn)
                time.sleep(1)
                
                # Пытаемся навести мышку (HOVER) до 3 раз
                for hover_attempt in range(3):
                    try:
                        # 1. Физическое наведение курсора мыши (ActionChains)
                        actions = ActionChains(driver)
                        actions.move_to_element(info_btn).perform()
                        time.sleep(0.5)
                        
                        # 2. На всякий случай клик через JS
                        driver.execute_script("arguments[0].click();", info_btn)
                        
                        # 3. Принудительный JS-hover (запасной вариант, если ActionChains сбоит)
                        driver.execute_script("""
                            var evObj = document.createEvent('MouseEvents');
                            evObj.initMouseEvent('mouseover', true, false, window, 0, 0, 0, 0, 0, false, false, false, false, 0, null);
                            arguments[0].dispatchEvent(evObj);
                        """, info_btn)
                    except Exception:
                        pass
                    
                    time.sleep(1.5) # Ждем, пока Озон подгрузит окошко
                    
                    # Проверяем, появилось ли черное окно с реквизитами
                    page_text = driver.find_element(By.TAG_NAME, "body").text
                    if "Режим работы" in page_text or "ОГРН" in page_text or re.search(r'\b([15]\d{12}|3\d{14})\b', page_text):
                        window_opened = True
                        break # Окно открылось!
                    
                if window_opened:
                    break # Выходим из цикла скроллинга

        except Exception:
            pass
        
        # Если окно так и не открылось — скроллим еще чуть-чуть вниз
        driver.execute_script("window.scrollBy(0, 300);")
        time.sleep(0.5)

    # 3. --- ПАРСИНГ РЕКВИЗИТОВ ИЗ ОТКРЫТОГО ОКНА ---
    ogrn = None
    inn = "Не найден"
    jur_name = seller_name

    if window_opened:
        for attempt in range(5):
            time.sleep(1)
            try:
                page_text = driver.find_element(By.TAG_NAME, "body").text
                html = driver.page_source or ""
            except Exception:
                continue

            clean_html = re.sub(r'<[^>]+>', ' ', html)
            search_text = page_text + " " + clean_html

            ogrn_match = re.search(r'(?i)ОГРН(?:ИП)?[\s\:-]*(\d{13}|\d{15})\b', search_text)
            if ogrn_match:
                ogrn = ogrn_match.group(1)
            else:
                fallback_match = re.search(r'\b([15]\d{12}|3\d{14})\b', search_text)
                if fallback_match:
                    ogrn = fallback_match.group(1)

            if ogrn:
                inn_match = re.search(r'(?i)ИНН[\s\:-]*(\d{10}|\d{12})\b', search_text)
                if inn_match:
                    inn = inn_match.group(1)

                name_match = re.search(r'(ООО|ИП|АО|ПАО|ЗАО)\s+["«]?([А-Яа-яA-Za-z0-9\s\-]+)["»]?', search_text)
                if name_match:
                    jur_name = name_match.group(0).strip()
                break 

    if not ogrn:
        return None

    if inn == "Не найден":
        inn = find_inn_by_ogrn(driver, ogrn)

    report_link = f"https://checko.ru/search?query={ogrn}"

    return {
        "Наименование": jur_name,
        "СсылкаНаМагазин": seller_link,
        "ИНН": inn,
        "ОГРН": ogrn,
        "СсылкаНаТовар": product_url,
        "СсылкаНаОтчет": report_link
    }
# ──────────────────────────── ФИЛЬТР МАГАЗИНОВ ────────────────────────────

OTHER_FILTER_KEYWORDS = [
    'скидки недели', 'рассрочка', 'бренд', 'производитель', 'страна',
    'рейтинг', 'наличие', 'с фото', 'с видео', 'самовывоз',
    'быстрая доставка', 'ozon card', 'озон карта', 'новинки', 'цена'
]

_STORE_HEADER_XPATH = (
    "(local-name()='div' or local-name()='span' or local-name()='h3' or local-name()='font' or local-name()='p' or local-name()='label') "
    "and (contains(., 'Продавец') or contains(., 'Магазин') "
    "or contains(., 'продавец') or contains(., 'магазин'))"
)


def is_inside_header_or_nav(element):
    try:
        curr = element
        for _ in range(8):
            curr = curr.find_element(By.XPATH, "./..")
            tag = curr.tag_name.lower()
            cls = (curr.get_attribute("class") or "").lower()
            id_attr = (curr.get_attribute("id") or "").lower()
            if tag in ['header', 'nav', 'footer'] or any(k in cls or k in id_attr for k in ['header', 'nav', 'menu', 'topbar', 'global', 'footer', 'head']):
                return True
    except Exception:
        pass
    return False


def find_store_filter_block(driver):
    driver.implicitly_wait(0)
    try:
        for scroll_step in range(40):
            if not is_session_alive(driver):
                return None
            try:
                headers = driver.find_elements(By.XPATH, f"//*[{_STORE_HEADER_XPATH}]")
            except Exception:
                return None

            leaf_headers = []
            for h in headers:
                try:
                    if not h.find_elements(By.XPATH, f".//*[{_STORE_HEADER_XPATH}]"):
                        leaf_headers.append(h)
                except Exception:
                    leaf_headers.append(h)

            for h in leaf_headers:
                try:
                    txt = h.text or h.get_attribute("textContent") or ""
                    if txt.strip().lower() not in ['магазин', 'магазины', 'продавец', 'продавцы']:
                        continue
                    if is_inside_header_or_nav(h) or not h.is_displayed():
                        continue

                    container = h
                    for _ in range(6):
                        container = container.find_element(By.XPATH, "./..")
                        cls = (container.get_attribute("class") or "").lower()
                        if any(k in cls for k in ['sidebar', 'layout', 'grid', 'page', 'body', 'root', 'split']):
                            continue

                        full_text = container.get_attribute("textContent") or ""
                        crossed = [kw for kw in OTHER_FILTER_KEYWORDS if kw in full_text.lower()]
                        if crossed:
                            break

                        lines = [line.strip() for line in full_text.split('\n') if line.strip()]
                        if len(lines) >= 2 or "посмотреть все" in full_text.lower() or "показать все" in full_text.lower():
                            return container, txt.strip()
                except Exception:
                    continue

            try:
                driver.execute_script("window.scrollBy(0, 350);")
            except Exception:
                return None
            time.sleep(0.3)
    finally:
        try:
            driver.implicitly_wait(3)
        except Exception:
            pass
    return None


def extract_seller_items(container):
    driver = container.parent
    driver.implicitly_wait(0) 
    try:
        items = container.find_elements(By.XPATH, ".//a | .//label | .//div[not(div)]")
        if items:
            return items
    except Exception:
        pass
    finally:
        driver.implicitly_wait(3)
    return []


def try_expand_store_list(driver, container):
    try:
        see_all = container.find_elements(By.XPATH, ".//*[contains(text(),'Посмотреть все') or contains(text(),'Показать все')]")
        for btn in see_all:
            if btn.is_displayed():
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(1)
                return True
    except Exception:
        pass
    return False


def try_search_fallback(driver, cat_url):
    term = next((val for key, val in CATEGORY_SEARCH_QUERIES.items() if key in cat_url), None)
    if not term:
        return False

    if not safe_get(driver, "https://www.ozon.ru/", retries=2):
        return False
    time.sleep(3)

    search_xpaths = [
        "//input[contains(@placeholder, 'Искать на')]",
        "//input[contains(@placeholder, 'Search')]",
        "//input[@type='text' and @name='search']",
        "//input[@type='text' and @placeholder]"
    ]

    driver.implicitly_wait(0)
    search_input = None
    try:
        for xpath in search_xpaths:
            try:
                search_input = driver.find_element(By.XPATH, xpath)
                if search_input.is_displayed():
                    break
            except NoSuchElementException:
                continue
    finally:
        driver.implicitly_wait(3)

    if not search_input:
        return False

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", search_input)
        time.sleep(0.5)
        search_input.click()
        search_input.clear()
        search_input.send_keys(term)
        time.sleep(0.5)
        search_input.send_keys(Keys.ENTER)
        time.sleep(4)
        return True
    except Exception:
        return False


def filter_by_seller_name(driver, seller_name):
    result = find_store_filter_block(driver)
    if not result:
        return False
    container, _ = result

    try:
        search_inputs = container.find_elements(By.XPATH, ".//input")
        text_input = next((inp for inp in search_inputs if inp.get_attribute("type") not in ["checkbox", "radio"]), None)
        
        if text_input:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", text_input)
            time.sleep(0.3)
            text_input.click()
            text_input.send_keys(Keys.CONTROL + "a")
            text_input.send_keys(Keys.DELETE)
            time.sleep(0.3)
            text_input.send_keys(seller_name)
            time.sleep(1.5)
        else:
            try_expand_store_list(driver, container)
    except Exception:
        try_expand_store_list(driver, container)

    if "'" in seller_name:
        longest_part = max(seller_name.split("'"), key=len)
        xpaths = [f".//*[contains(text(), \"{longest_part}\")]"]
    else:
        xpaths = [
            f".//*[normalize-space(text())='{seller_name}']",
            f".//*[contains(normalize-space(text()), '{seller_name}')]"
        ]

    driver.implicitly_wait(0)
    try:
        for xpath in xpaths:
            try:
                elements = container.find_elements(By.XPATH, xpath)
                for el in elements:
                    if el.is_displayed():
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        time.sleep(0.3)
                        driver.execute_script("arguments[0].click();", el)
                        return True
            except Exception:
                continue
    finally:
        driver.implicitly_wait(3)

    return False


# ──────────────────────────── РАБОТА ОДНОГО БРАУЗЕРА (ДЛЯ ПОТОКА) ────────────────────────────

def process_category(cat_url, excel_filename, file_lock, global_seen_inns, global_seen_names, stats, thread_id):
    """Функция, которую выполняет каждый отдельный браузер в своем потоке."""
    
    time.sleep(thread_id * 5)
    
    print(f"[Поток {thread_id}] Запускаем браузер для категории: {cat_url}")
    options = uc.ChromeOptions()
    options.add_argument('--start-maximized')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.page_load_strategy = 'eager'

    try:
        driver = uc.Chrome(options=options, version_main=149)
    except Exception:
        try:
            driver = uc.Chrome(options=options)
        except Exception as e:
            print(f"[Поток {thread_id}] ❌ Ошибка запуска браузера: {e}")
            return

    driver.set_page_load_timeout(45)
    driver.implicitly_wait(3)

    results = []
    try:
        active_base_url = cat_url

        if not safe_get(driver, cat_url, retries=3):
            if try_search_fallback(driver, cat_url):
                active_base_url = driver.current_url
            else:
                return

        time.sleep(4)
        if not handle_error_stub(driver, max_retries=3):
            if try_search_fallback(driver, cat_url):
                active_base_url = driver.current_url
            else:
                return

        result = find_store_filter_block(driver)
        if not result and active_base_url == cat_url:
            if try_search_fallback(driver, cat_url):
                active_base_url = driver.current_url
                result = find_store_filter_block(driver)

        if not result:
            print(f"[Поток {thread_id}] ❌ Блок 'Магазин' не найден.")
            return

        container, matched_header_text = result
        try_expand_store_list(driver, container)
        seller_items = extract_seller_items(container)

        seller_names_to_process = []
        ignore_words = [
            'магазин', 'магазины', 'продавец', 'продавцы', 
            'посмотреть все', 'показать все', 'фильтры', 
            'очистить', 'найти', 'свернуть', 'развернуть'
        ]

        for item in seller_items:
            try:
                raw_text = item.get_attribute("textContent") or ""
                lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
                if not lines: continue
                
                name = re.sub(r'\s*\(\d+\)$', '', lines[0])
                if not name or len(name) < 2 or name.lower() in ignore_words or name.lower() in OTHER_FILTER_KEYWORDS:
                    continue
                if name.upper() in global_seen_names:
                    continue
                if name not in seller_names_to_process:
                    seller_names_to_process.append(name)
            except Exception:
                continue

        print(f"[Поток {thread_id}] Найдено продавцов: {len(seller_names_to_process)}")

        for seller_name in seller_names_to_process:
            if not is_session_alive(driver):
                break

            if not re.search(r'[А-Яа-яЁё]', seller_name):
                print(f"[Поток {thread_id}] ⏭️ Пропуск (нет русских букв): {seller_name}")
                continue
            
            print(f"\n[Поток {thread_id}] 🎯 Обрабатываем: {seller_name}")

            if not safe_get(driver, active_base_url, retries=2):
                continue
            time.sleep(1.5)

            if not filter_by_seller_name(driver, seller_name):
                print(f"[Поток {thread_id}] ❌ Не удалось кликнуть на фильтр продавца.")
                continue

            time.sleep(2.5)

            try:
                product_links = driver.find_elements(By.XPATH, "//a[contains(@href, '/product/') and not(contains(@href, 'reviews'))]")
            except Exception:
                product_links = []

            if not product_links:
                print(f"[Поток {thread_id}] ❌ Товары не найдены после фильтрации.")
                continue

            first_product_url = product_links[0].get_attribute("href")
            original_window = driver.current_window_handle
            data = None
            try:
                driver.switch_to.new_window('tab')
                data = process_product(driver, first_product_url, seller_name)
            except Exception:
                pass
            finally:
                try:
                    if len(driver.window_handles) > 1:
                        driver.close()
                    driver.switch_to.window(original_window)
                except Exception:
                    pass

            if data:
                inn = data["ИНН"]
                if inn not in global_seen_inns and inn != "Не найден":
                    global_seen_inns.add(inn)
                    global_seen_names.add(data["Наименование"].upper())
                    results.append(data)
                    
                    with file_lock:
                        stats['added'] += 1
                        current_count = stats['added']
                        
                    print(f"[Поток {thread_id}] ✅ Успех: {data['Наименование']} | ИНН: {inn} | [Всего добавлено за сессию: {current_count}]")
                    save_results(results, excel_filename, file_lock)
                    results = [] 
                elif inn == "Не найден":
                    print(f"[Поток {thread_id}] ❌ ИНН не определён (Checko.ru не нашел ОГРН: {data['ОГРН']}).")
                else:
                    print(f"[Поток {thread_id}] ⚠️ ИНН {inn} уже есть в базе.")
            else:
                print(f"[Поток {thread_id}] ❌ Данные магазина не найдены (в карточке нет ОГРН).")

        if results:
            save_results(results, excel_filename, file_lock)

    except Exception as e:
        print(f"[Поток {thread_id}] ❌ Ошибка: {e}")
        if results:
            save_results(results, excel_filename, file_lock)
    finally:
        try:
            if is_session_alive(driver):
                driver.quit()
        except Exception:
            pass


# ──────────────────────────── MAIN ────────────────────────────

def main():
    excel_filename = 'Ozon_Sellers.xlsx'
    seen_inns, seen_names = load_existing_data(excel_filename)
    
    if seen_names:
        print(f"В базе уже есть продавцы ({len(seen_names)} шт.), будем их пропускать.")

    category_urls = [
        "https://www.ozon.ru/category/holodilniki-10502/",
        "https://www.ozon.ru/category/stiralnye-mashiny-10537/",
        "https://www.ozon.ru/category/kuhonnye-plity-10515/",
        "https://www.ozon.ru/category/morozilnye-kamery-10504/",
        "https://www.ozon.ru/category/posudomoechnye-mashiny-10534/",
        "https://www.ozon.ru/category/vstraivaemaya-krupnaya-bytovaya-tehnika-10543/"
    ]

    file_lock = threading.Lock()
    
    # -----------------------------------
    # ИЗМЕНЕНО: 2 ОКНА БРАУЗЕРА 
    # -----------------------------------
    MAX_BROWSERS = 1
    
    # Словарь со статистикой, который будут менять все потоки
    stats = {'added': 0}

    print(f"🚀 ЗАПУСКАЕМ {MAX_BROWSERS} ПАРАЛЛЕЛЬНЫХ БРАУЗЕРА(ОВ) 🚀")
    
    with ThreadPoolExecutor(max_workers=MAX_BROWSERS) as executor:
        futures = []
        for i, cat_url in enumerate(category_urls):
            future = executor.submit(
                process_category, 
                cat_url, 
                excel_filename, 
                file_lock, 
                seen_inns, 
                seen_names,
                stats,     # Передаем счетчик
                i + 1      # Номер потока
            )
            futures.append(future)

        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"Ошибка в одном из потоков: {e}")

    print(f"\n🎉 ВСЕ КАТЕГОРИИ ОБРАБОТАНЫ! За эту сессию добавлено: {stats['added']} магазинов.")
    
    if os.path.exists(excel_filename):
        df = pd.read_excel(excel_filename)
        print(f"📊 Итого в файле {excel_filename}: {len(df)} записей.")

if __name__ == '__main__':
    main()
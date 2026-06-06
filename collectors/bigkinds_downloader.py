import argparse
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


def _bool_env(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def build_driver(download_dir: Path, headless: bool, session_dir: Path) -> webdriver.Chrome:
    download_dir.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)

    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--window-size=1600,1000")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    # 동일한 사용자 프로필을 재사용해 로그인 세션(쿠키/스토리지)을 유지한다.
    chrome_options.add_argument(f"--user-data-dir={str(session_dir.resolve())}")
    chrome_options.add_argument("--profile-directory=Default")

    prefs = {
        "download.default_directory": str(download_dir.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }
    chrome_options.add_experimental_option("prefs", prefs)

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


def _split_date_ranges(start: datetime, end: datetime, step_days: int) -> list[tuple[datetime, datetime]]:
    ranges: list[tuple[datetime, datetime]] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=step_days - 1), end)
        ranges.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return ranges


def _add_one_month(dt: datetime) -> datetime:
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1)
    return dt.replace(month=dt.month + 1)


def _is_logged_in(driver: webdriver.Chrome) -> bool:
    if driver.find_elements(By.CSS_SELECTOR, ".logout-btn"):
        return True
    return len(driver.find_elements(By.LINK_TEXT, "로그인")) == 0


def _wait_present_input(wait: WebDriverWait, input_id: str):
    return wait.until(EC.presence_of_element_located((By.ID, input_id)))


def _safe_set_date_input(driver: webdriver.Chrome, wait: WebDriverWait, input_id: str, value: str) -> None:
    input_el = _wait_present_input(wait, input_id)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", input_el)

    try:
        input_el.click()
        input_el.send_keys(Keys.COMMAND, "a")
        input_el.send_keys(Keys.BACKSPACE)
        input_el.send_keys(value)
    except Exception:
        # 일부 케이스(오버레이/datepicker 충돌)에서는 JS 값 설정이 더 안정적이다.
        driver.execute_script(
            """
            const el = arguments[0];
            const v = arguments[1];
            el.value = v;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            input_el,
            value,
        )


def _ensure_news_page_ready(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    # 비로그인/세션 만료 시 검색 화면 요소가 없어 타임아웃이 발생하므로 먼저 판별한다.
    if driver.find_elements(By.LINK_TEXT, "로그인") and not driver.find_elements(By.ID, "search-begin-date"):
        raise RuntimeError("로그인 상태가 아닙니다. 브라우저에서 로그인 후 다시 실행하세요.")

    # STEP 01이 접혀 있으면 날짜 입력창이 비노출될 수 있어 펼친다.
    collapse_btn = driver.find_elements(By.ID, "collapse-step-1")
    if collapse_btn and "open" not in (collapse_btn[0].get_attribute("class") or ""):
        collapse_btn[0].click()
        time.sleep(0.5)

    wait.until(EC.presence_of_element_located((By.ID, "search-begin-date")))
    wait.until(EC.presence_of_element_located((By.ID, "search-end-date")))


def _click_visible(driver: webdriver.Chrome, wait: WebDriverWait, css_selector: str) -> None:
    def _pick_clickable(_):
        for el in driver.find_elements(By.CSS_SELECTOR, css_selector):
            if el.is_displayed() and el.is_enabled():
                return el
        return False

    target = wait.until(_pick_clickable)
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target)
        target.click()
    except Exception:
        driver.execute_script("arguments[0].click();", target)


def _open_data_download_panel(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    # 하단 배너/푸터가 클릭을 가로채지 않도록 먼저 화면을 상단으로 이동
    driver.execute_script("window.scrollTo(0, 0);")

    # STEP 03 펼치기
    step3 = wait.until(EC.presence_of_element_located((By.ID, "collapse-step-3")))
    if "open" not in (step3.get_attribute("class") or ""):
        try:
            step3.click()
        except Exception:
            driver.execute_script("arguments[0].click();", step3)

    # 데이터 다운로드 탭 활성화
    preview_tab = wait.until(EC.presence_of_element_located((By.ID, "analytics-preview-tab")))
    if "active" not in (preview_tab.get_attribute("class") or ""):
        # 좌표 클릭이 아닌 JS 클릭으로 하단 링크 오버레이 간섭을 회피한다.
        driver.execute_script("arguments[0].click();", preview_tab)

    wait.until(
        lambda d: (
            len(d.find_elements(By.CSS_SELECTOR, "#analytics-data-download.active")) > 0
            and len(d.find_elements(By.CSS_SELECTOR, "#analytics-data-download .news-download-btn.mobile-excel-download")) > 0
        )
    )


def _apply_provider_filter_national_daily(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    # 언론사 탭 열기
    provider_tab_candidates = driver.find_elements(By.CSS_SELECTOR, "a.search-tab_group")
    provider_tab = None
    for tab in provider_tab_candidates:
        if (tab.text or "").strip() == "언론사":
            provider_tab = tab
            break
    if provider_tab is None:
        raise RuntimeError("언론사 탭을 찾지 못했습니다.")

    driver.execute_script("arguments[0].click();", provider_tab)
    wait.until(EC.presence_of_element_located((By.ID, "srch-tab2")))

    # 상단 분류 체크박스 중 '전국일간지'만 체크
    applied = driver.execute_script(
        """
        const tab = document.querySelector('#srch-tab2');
        if (!tab) return false;

        const labels = Array.from(tab.querySelectorAll('label'));
        const normalize = (s) => (s || '').replace(/\\s+/g, '').trim();
        const targetLabel = labels.find((l) => normalize(l.textContent) === '전국일간지');
        if (!targetLabel) return false;

        const targetInput = targetLabel.control
            || (targetLabel.getAttribute('for') ? document.getElementById(targetLabel.getAttribute('for')) : null)
            || targetLabel.closest('li,div,span')?.querySelector('input[type="checkbox"]');
        if (!targetInput) return false;

        const allChecks = Array.from(tab.querySelectorAll('input[type="checkbox"]'));
        for (const c of allChecks) {
            if (c !== targetInput && c.checked) {
                c.click();
            }
        }
        if (!targetInput.checked) {
            targetInput.click();
        }

        return true;
        """
    )
    if not applied:
        raise RuntimeError("'전국일간지' 체크박스를 찾지 못했습니다.")


def _has_login_required_popup(driver: webdriver.Chrome) -> bool:
    scripts = """
    const pop = document.querySelector('.alert-pop.open');
    if (!pop) return false;
    const txt = (pop.querySelector('.alert-txt')?.textContent || '').trim();
    return txt.includes('로그인');
    """
    try:
        return bool(driver.execute_script(scripts))
    except Exception:
        return False


def _close_login_required_popup(driver: webdriver.Chrome) -> None:
    try:
        driver.execute_script(
            """
            const btn = document.querySelector('.alert-pop.open .alert-confirm')
                || document.querySelector('.alert-pop.open .alert-close');
            if (btn) btn.click();
            """
        )
    except Exception:
        pass


def _confirm_download_popup(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    def _has_download_confirm(_):
        script = """
        const pop = document.querySelector('.alert-pop.open');
        if (!pop) return false;
        const txt = (pop.querySelector('.alert-txt')?.textContent || '').trim();
        return txt.includes('다운로드') && txt.includes('하시겠');
        """
        return bool(driver.execute_script(script))

    try:
        wait.until(_has_download_confirm)
    except Exception:
        # 사이트 상황에 따라 확인 팝업이 없을 수도 있으므로 조용히 통과한다.
        return

    driver.execute_script(
        """
        const btn = document.querySelector('.alert-pop.open .alert-confirm');
        if (btn) btn.click();
        """
    )


def _accept_browser_confirm(driver: webdriver.Chrome, timeout_seconds: int = 5) -> None:
    # 브라우저 네이티브 confirm/alert(예: "다운로드 하시겠습니까?") 처리
    try:
        alert = WebDriverWait(driver, timeout_seconds).until(EC.alert_is_present())
        alert.accept()
    except Exception:
        pass


def _wait_for_manual_login(driver: webdriver.Chrome, wait_seconds: int) -> None:
    driver.get("https://www.bigkinds.or.kr/")
    print("브라우저가 열렸습니다. 수동 로그인 완료 후 터미널에서 Enter를 누르세요.")
    try:
        input()
    except EOFError:
        # 비대화형 환경에서는 지정 시간만큼 대기 후 상태를 확인한다.
        print(f"대화형 입력이 불가하여 {wait_seconds}초 대기 후 로그인 상태를 확인합니다.")
        time.sleep(wait_seconds)

    if not _is_logged_in(driver):
        raise RuntimeError("수동 로그인 확인에 실패했습니다. 로그인 후 다시 실행하세요.")
    print("수동 로그인 확인 완료. 자동화를 이어서 실행합니다.")


def ensure_logged_in(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    user_id: str,
    user_pw: str,
    manual_login_wait_seconds: int,
    login_provider: str,
) -> None:
    driver.get("https://www.bigkinds.or.kr/")

    if _is_logged_in(driver):
        print("기존 세션이 유효합니다. 로그인 상태를 재사용합니다.")
        return

    if user_id and user_pw:
        print("저장된 세션이 없어, ID/PW로 로그인 시도합니다.")
        wait.until(EC.element_to_be_clickable((By.LINK_TEXT, "로그인"))).click()
        wait.until(EC.presence_of_element_located((By.ID, "loginId"))).send_keys(user_id)
        wait.until(EC.presence_of_element_located((By.ID, "loginPw"))).send_keys(user_pw)
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))).click()
        wait.until(lambda d: _is_logged_in(d))
        print("로그인 완료. 이후 실행부터는 세션 유지 방식으로 동작합니다.")
        return

    if manual_login_wait_seconds < 1:
        raise ValueError("MANUAL_LOGIN_WAIT_SECONDS는 1 이상이어야 합니다.")

    print(
        "ID/PW가 없어 수동 로그인 대기 모드로 전환합니다. "
        f"{manual_login_wait_seconds}초 안에 브라우저에서 로그인하세요."
    )
    wait.until(EC.element_to_be_clickable((By.LINK_TEXT, "로그인"))).click()

    normalized_provider = login_provider.strip().lower()
    if normalized_provider in {"google", "kakao", "naver"}:
        provider_selector = f".oauth-login-btn[data-type='{normalized_provider}']"
        try:
            wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, provider_selector))).click()
            print(f"{normalized_provider} 로그인 버튼을 클릭했습니다. 인증을 완료하세요.")
        except Exception:
            print(f"{normalized_provider} 로그인 버튼 클릭에 실패했습니다. 브라우저에서 직접 클릭하세요.")

    WebDriverWait(driver, manual_login_wait_seconds).until(lambda d: _is_logged_in(d))
    print("수동 로그인 완료. 세션이 저장되었습니다.")


def run(
    years: list[int] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    # 통합 .env(루트) 로딩 — db.config 가 있으면 공유 로더, 없으면 dotenv 폴백.
    try:
        from db.config import load_shared_env
        load_shared_env()
    except Exception:
        load_dotenv()

    # 통합 .env 규약: BigKinds 전용 키는 BIGKINDS_* 접두어(다른 단계와 충돌 방지).
    # 구 키(DOWNLOAD_DIR/SESSION_DIR/HEADLESS/STEP_DAYS)도 폴백으로 허용.
    user_id = os.getenv("BIGKINDS_ID", "").strip()
    user_pw = os.getenv("BIGKINDS_PW", "").strip()
    download_dir = Path(os.getenv("BIGKINDS_DOWNLOAD_DIR") or os.getenv("DOWNLOAD_DIR") or "data/news")
    session_dir = Path(os.getenv("BIGKINDS_SESSION_DIR") or os.getenv("SESSION_DIR") or ".selenium_profile")
    headless = _bool_env(os.getenv("BIGKINDS_HEADLESS") or os.getenv("HEADLESS"), default=False)
    step_days = int((os.getenv("BIGKINDS_STEP_DAYS") or os.getenv("STEP_DAYS") or "2").strip())
    manual_login_wait_seconds = int(os.getenv("MANUAL_LOGIN_WAIT_SECONDS", "180").strip())
    login_provider = os.getenv("LOGIN_PROVIDER", "google").strip()
    pause_for_manual_login = _bool_env(os.getenv("PAUSE_FOR_MANUAL_LOGIN"), default=False)

    if step_days < 1:
        raise ValueError("STEP_DAYS는 1 이상이어야 합니다.")

    # 우선순위: (--start-date/--end-date) > --year > .env START_DATE/END_DATE
    period_ranges: list[tuple[datetime, datetime, int | None]] = []
    cli_start = (start_date or "").strip()
    cli_end = (end_date or "").strip()
    if cli_start or cli_end:
        if not cli_start or not cli_end:
            raise ValueError("--start-date와 --end-date는 둘 다 지정해야 합니다. (형식: YYYY-MM-DD)")
        if years:
            raise ValueError("--year와 (--start-date/--end-date)는 함께 사용할 수 없습니다.")
        try:
            start_dt = datetime.strptime(cli_start, "%Y-%m-%d")
            end_dt = datetime.strptime(cli_end, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError("--start-date/--end-date는 YYYY-MM-DD 형식이어야 합니다.") from e
        if start_dt > end_dt:
            raise ValueError("시작일이 종료일보다 늦을 수 없습니다.")
        period_ranges.append((start_dt, end_dt, None))
        print(f"명령줄 구간 적용: {cli_start} ~ {cli_end}")
    elif years:
        unique_years = sorted(set(years))
        for y in unique_years:
            if y < 1990 or y > 2100:
                raise ValueError("--year 값은 1990~2100 사이 정수여야 합니다.")
            period_ranges.append((datetime(y, 1, 1), datetime(y, 12, 31), y))
        print(
            "연도 인자 적용: "
            + ", ".join(str(y) for y in unique_years)
            + " (각 연도 1월 1일~12월 31일, 순서대로 다운로드)"
        )
    else:
        env_start = os.getenv("START_DATE", "2024-01-01").strip()
        env_end = os.getenv("END_DATE", "").strip()
        if not env_start:
            raise ValueError(
                "START_DATE를 .env에 설정하거나 --year / --start-date·--end-date 로 범위를 지정하세요."
            )
        start_dt = datetime.strptime(env_start, "%Y-%m-%d")
        if env_end:
            end_dt = datetime.strptime(env_end, "%Y-%m-%d")
        else:
            end_dt = _add_one_month(start_dt) - timedelta(days=1)
        if start_dt > end_dt:
            raise ValueError("START_DATE가 END_DATE보다 늦을 수 없습니다.")
        period_ranges.append((start_dt, end_dt, None))

    driver = build_driver(download_dir, headless, session_dir)
    wait = WebDriverWait(driver, 20)

    try:
        if pause_for_manual_login:
            _wait_for_manual_login(driver, manual_login_wait_seconds)
        else:
            # 항상 세션 재사용을 먼저 시도하고, 없을 때만 1회 수동 로그인한다.
            ensure_logged_in(driver, wait, user_id, user_pw, manual_login_wait_seconds, login_provider)

        for period_start, period_end, period_year in period_ranges:
            date_ranges = _split_date_ranges(period_start, period_end, step_days)
            year_tag = f"[연도 {period_year}] " if period_year is not None else ""
            print(
                f"{year_tag}구간 {period_start.strftime('%Y-%m-%d')} ~ {period_end.strftime('%Y-%m-%d')} "
                f"({len(date_ranges)}회 분할)"
            )
            for idx, (chunk_start, chunk_end) in enumerate(date_ranges, start=1):
                chunk_start_str = chunk_start.strftime("%Y-%m-%d")
                chunk_end_str = chunk_end.strftime("%Y-%m-%d")
                print(f"{year_tag}[{idx}/{len(date_ranges)}] {chunk_start_str} ~ {chunk_end_str} 다운로드 시작")

                # 1) 뉴스검색 페이지 직접 접속
                driver.get("https://www.bigkinds.or.kr/v2/news/index.do")
                _ensure_news_page_ready(driver, wait)

                # 2), 3) 시작/종료일 입력
                _safe_set_date_input(driver, wait, "search-begin-date", chunk_start_str)
                _safe_set_date_input(driver, wait, "search-end-date", chunk_end_str)
                _apply_provider_filter_national_daily(driver, wait)

                # 4) 검색 버튼 클릭
                wait.until(
                    EC.element_to_be_clickable(
                        (By.CSS_SELECTOR, ".btn.btn-search.news-search-btn.news-report-search-btn")
                    )
                ).click()

                # 5) 10초 대기
                time.sleep(10)

                # 6) 다운로드 버튼 클릭
                _open_data_download_panel(driver, wait)
                _click_visible(driver, wait, "#analytics-data-download .news-download-btn.mobile-excel-download")
                _accept_browser_confirm(driver)
                _confirm_download_popup(driver, wait)

                # 로그인 필요 팝업이 뜨면 자동 재로그인 후 1회 재시도
                if _has_login_required_popup(driver):
                    print("로그인 필요 팝업 감지: 세션 재확인 후 다운로드를 재시도합니다.")
                    _close_login_required_popup(driver)
                    ensure_logged_in(driver, wait, user_id, user_pw, manual_login_wait_seconds, login_provider)
                    driver.get("https://www.bigkinds.or.kr/v2/news/index.do")
                    _ensure_news_page_ready(driver, wait)
                    _safe_set_date_input(driver, wait, "search-begin-date", chunk_start_str)
                    _safe_set_date_input(driver, wait, "search-end-date", chunk_end_str)
                    _apply_provider_filter_national_daily(driver, wait)
                    wait.until(
                        EC.element_to_be_clickable(
                            (By.CSS_SELECTOR, ".btn.btn-search.news-search-btn.news-report-search-btn")
                        )
                    ).click()
                    time.sleep(10)
                    _open_data_download_panel(driver, wait)
                    _click_visible(driver, wait, "#analytics-data-download .news-download-btn.mobile-excel-download")
                    _accept_browser_confirm(driver)
                    _confirm_download_popup(driver, wait)

                # 7) 10초 대기
                time.sleep(10)
                print(f"{year_tag}[{idx}/{len(date_ranges)}] 완료")

        print(f"다운로드 디렉터리 확인: {download_dir.resolve()}")
        print("작업이 완료되었습니다.")
    finally:
        driver.quit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="빅카인즈 뉴스 엑셀 다운로드")
    parser.add_argument(
        "--year",
        type=int,
        nargs="+",
        metavar="YYYY",
        help="다운로드 대상 연도(복수 가능). 각 연도는 1/1~12/31로 처리됩니다. (--start-date/--end-date보다 우선하지 않음)",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        metavar="YYYY-MM-DD",
        default=None,
        help="다운로드 시작일. --end-date와 함께 지정. --year·.env 범위보다 우선합니다.",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        metavar="YYYY-MM-DD",
        default=None,
        help="다운로드 종료일. --start-date와 함께 지정.",
    )
    args = parser.parse_args()
    run(years=args.year, start_date=args.start_date, end_date=args.end_date)

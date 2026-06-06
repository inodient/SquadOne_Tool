"""Unit tests for react_agent tools using a mock BrowserSession."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixture: mock BrowserSession
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_session(tmp_path):
    """BrowserSession with a fully mocked Playwright page."""
    session = MagicMock()
    session._config = MagicMock()
    session._config.browser_headless = True
    session._config.browser_timeout_ms = 30_000
    session._config.react_outputs_dir = tmp_path

    page = MagicMock()
    page.url = "https://example.com"
    page.title.return_value = "Example Domain"
    page.goto.return_value = MagicMock(status=200)
    page.is_closed.return_value = False
    session.page = page
    return session


@pytest.fixture()
def tools(mock_session):
    from react_agent.tools import make_tools
    return {t.name: t for t in make_tools(mock_session)}


# ---------------------------------------------------------------------------
# navigate
# ---------------------------------------------------------------------------

def test_navigate_returns_url_and_title(tools, mock_session):
    result = tools["navigate"].invoke({"url": "https://example.com"})
    assert "https://example.com" in result
    assert "Example Domain" in result


def test_navigate_returns_error_on_exception(tools, mock_session):
    mock_session.page.goto.side_effect = Exception("timeout")
    result = tools["navigate"].invoke({"url": "https://example.com"})
    assert "error" in result.lower()


# ---------------------------------------------------------------------------
# get_page_content
# ---------------------------------------------------------------------------

def test_get_page_content_calls_build_page_perception(tools, mock_session):
    base = json.dumps({"a11y": "tree", "html_summary": "<body></body>"})
    mock_session.page.evaluate.return_value = ["/article/* (×5)", "/news/* (×3)"]
    with patch("tools.parser.build_page_perception", return_value=base):
        result = tools["get_page_content"].invoke({})
    data = json.loads(result)
    assert "a11y" in data
    assert "link_patterns" in data
    assert data["link_patterns"][0] == "/article/* (×5)"
    # /article/ keyword detected → suggested_selector auto-populated
    assert data.get("suggested_selector") == "a[href*='/article/']"
    assert data.get("suggested_fields") == {"title": "", "url": "[href]"}


# ---------------------------------------------------------------------------
# click
# ---------------------------------------------------------------------------

def test_click_returns_confirmation(tools, mock_session):
    result = tools["click"].invoke({"selector": "a.link", "index": 0})
    mock_session.page.locator.assert_called_with("a.link")
    assert "Clicked" in result


def test_click_returns_error_on_exception(tools, mock_session):
    mock_session.page.locator.side_effect = Exception("not found")
    result = tools["click"].invoke({"selector": "a.missing"})
    assert "error" in result.lower()


# ---------------------------------------------------------------------------
# type_text
# ---------------------------------------------------------------------------

def test_type_text_fills_field(tools, mock_session):
    result = tools["type_text"].invoke({"selector": "input#q", "text": "python"})
    mock_session.page.locator.assert_called_with("input#q")
    assert "Typed" in result


def test_type_text_presses_enter_when_requested(tools, mock_session):
    tools["type_text"].invoke({"selector": "input", "text": "hello", "press_enter": True})
    mock_session.page.keyboard.press.assert_called_with("Enter")


# ---------------------------------------------------------------------------
# scroll
# ---------------------------------------------------------------------------

def test_scroll_down(tools, mock_session):
    result = tools["scroll"].invoke({"direction": "down", "amount": 2})
    mock_session.page.evaluate.assert_called_once()
    assert "down" in result


def test_scroll_up(tools, mock_session):
    result = tools["scroll"].invoke({"direction": "up", "amount": 1})
    assert "up" in result


# ---------------------------------------------------------------------------
# extract_structured_data
# ---------------------------------------------------------------------------

def test_extract_returns_items(tools, mock_session):
    el = MagicMock()
    el.inner_text.return_value = "Article Title"
    el.get_attribute.return_value = "https://example.com/article"

    container = MagicMock()
    container.query_selector.return_value = el
    mock_session.page.query_selector_all.return_value = [container, container]

    result = tools["extract_structured_data"].invoke({
        "item_selector": "article",
        "fields": {"title": "h2", "url": "a[href]"},
        "max_items": 10,
    })
    data = json.loads(result)
    assert data["count"] == 2
    assert data["items"][0]["title"] == "Article Title"
    assert data["items"][0]["url"] == "https://example.com/article"


def test_extract_self_text_and_attr(tools, mock_session):
    """item_selector selects <a> directly; fields use "" and "[href]" forms."""
    container = MagicMock()
    container.inner_text.return_value = "뉴스 제목"
    container.get_attribute.return_value = "https://n.news.naver.com/article/001"
    mock_session.page.query_selector_all.return_value = [container]

    result = tools["extract_structured_data"].invoke({
        "item_selector": "a[href*='naver']",
        "fields": {"title": "", "url": "[href]"},
        "max_items": 10,
    })
    data = json.loads(result)
    assert data["count"] == 1
    assert data["items"][0]["title"] == "뉴스 제목"
    assert data["items"][0]["url"] == "https://n.news.naver.com/article/001"


def test_extract_returns_empty_on_no_match(tools, mock_session):
    mock_session.page.query_selector_all.return_value = []
    result = tools["extract_structured_data"].invoke({
        "item_selector": "article.missing",
        "fields": {"title": "h2"},
    })
    data = json.loads(result)
    assert data["count"] == 0
    assert data["items"] == []


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------

def test_screenshot_saves_file(tools, mock_session, tmp_path):
    result = tools["screenshot"].invoke({"label": "test"})
    assert "Screenshot saved" in result
    mock_session.page.screenshot.assert_called_once()


# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------

def test_done_returns_json_sentinel(tools):
    result = tools["done"].invoke({
        "result": {"items": [{"title": "Test"}]},
        "success": True,
        "summary": "extracted 1 item",
    })
    data = json.loads(result)
    assert data["__done__"] is True
    assert data["success"] is True
    assert data["result"]["items"][0]["title"] == "Test"


def test_done_success_false(tools):
    result = tools["done"].invoke({
        "result": {},
        "success": False,
        "summary": "blocked by login",
    })
    data = json.loads(result)
    assert data["success"] is False


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------

def test_web_search_returns_results(tools, mock_session):
    """web_search가 {"results": [...], "count": N} 형태를 반환해야 한다."""
    mock_session.page.goto.return_value = MagicMock(status=200)
    mock_session.page.evaluate.return_value = [
        {"title": "네이버 스포츠", "url": "https://m.sports.naver.com/index"},
        {"title": "스포츠 뉴스", "url": "https://sports.news.naver.com/"},
    ]
    result = tools["web_search"].invoke({"query": "네이버 스포츠", "max_results": 5})
    data = json.loads(result)
    assert data["count"] == 2
    assert data["results"][0]["url"] == "https://m.sports.naver.com/index"
    assert data["results"][0]["title"] == "네이버 스포츠"


def test_web_search_navigates_to_naver(tools, mock_session):
    """검색 시 search.naver.com 으로 이동해야 한다."""
    mock_session.page.evaluate.return_value = []
    tools["web_search"].invoke({"query": "스포츠 뉴스"})
    call_url = mock_session.page.goto.call_args[0][0]
    assert "search.naver.com" in call_url
    assert "스포츠+뉴스" in call_url or "%EC%8A%A4%ED%8F%AC%EC%B8%A0" in call_url


def test_web_search_returns_empty_on_no_results(tools, mock_session):
    """결과가 없을 때 count=0, results=[] 반환."""
    mock_session.page.evaluate.return_value = []
    result = tools["web_search"].invoke({"query": "존재하지않는검색어xyzabc"})
    data = json.loads(result)
    assert data["count"] == 0
    assert data["results"] == []


def test_web_search_handles_error(tools, mock_session):
    """page.goto 예외 발생 시 error 필드가 있는 JSON 반환."""
    mock_session.page.goto.side_effect = Exception("timeout")
    result = tools["web_search"].invoke({"query": "test"})
    data = json.loads(result)
    assert data["count"] == 0
    assert "error" in data

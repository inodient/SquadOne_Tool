"""Stage0 수집기 패키지.

외부 소스에서 원천 데이터를 수집한다.
- bigkinds_downloader: 빅카인즈 뉴스 엑셀(Selenium) → data/news/*.xlsx
- naver_categories: 네이버 쇼핑 카테고리 분류체계 → newstrend.naver_categories
- agentic_scraper(P5): ReAct+Playwright 범용 웹 수집기
수집 산출물은 steps/qdrant_ingest.py 로 임베딩→Qdrant 적재된다.
"""

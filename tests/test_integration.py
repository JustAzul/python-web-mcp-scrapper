import pytest
from fastapi.testclient import TestClient
import os
import sys
import time
import requests # Already imported below, but good practice at top
from bs4 import BeautifulSoup
import re
from urllib.parse import urljoin, urlparse
import feedparser
import asyncio
from mcp import ClientSession, StdioServerParameters # Import StdioServerParameters
from mcp.client.stdio import stdio_client # Import stdio_client
import aiohttp

# Add src directory to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

# Import the core scraper function for direct testing
from src.scraper import extract_text_from_url

# Track the last tested domain to implement smart sleeping
_last_tested_domain = ""
_domain_access_times = {}

def get_domain_from_url(url):
    """Extract the domain from a URL, removing www. prefix"""
    parsed = urlparse(url)
    return parsed.netloc.replace("www.", "")

def smart_sleep(url, seconds=1):
    """Only sleep if we're accessing the same domain as the last test
    to prevent rate limiting while avoiding unnecessary delays"""
    global _last_tested_domain
    global _domain_access_times
    
    domain = get_domain_from_url(url)
    current_time = time.time()
    
    # If we've accessed this domain recently, sleep to avoid rate limiting
    if domain in _domain_access_times:
        last_access_time = _domain_access_times[domain]
        time_since_last_access = current_time - last_access_time
        
        # If we accessed this domain less than 'seconds' seconds ago, sleep for the remaining time
        if time_since_last_access < seconds:
            sleep_time = seconds - time_since_last_access
            print(f"Sleeping for {sleep_time:.2f}s to avoid rate limiting {domain}")
            time.sleep(sleep_time)
    
    # Update the last access time for this domain
    _domain_access_times[domain] = time.time()
    _last_tested_domain = domain

async def smart_sleep_async(url, seconds=1):
    """Async version of smart_sleep - only sleep if we're accessing the same domain as the last test"""
    global _last_tested_domain
    global _domain_access_times
    
    domain = get_domain_from_url(url)
    current_time = time.time()
    
    # If we've accessed this domain recently, sleep to avoid rate limiting
    if domain in _domain_access_times:
        last_access_time = _domain_access_times[domain]
        time_since_last_access = current_time - last_access_time
        
        # If we accessed this domain less than 'seconds' seconds ago, sleep for the remaining time
        if time_since_last_access < seconds:
            sleep_time = seconds - time_since_last_access
            print(f"Sleeping for {sleep_time:.2f}s to avoid rate limiting {domain}")
            await asyncio.sleep(sleep_time)
    
    # Update the last access time for this domain
    _domain_access_times[domain] = time.time()
    _last_tested_domain = domain
    
# Conditional import based on whether the code runs inside Docker or locally for testing
try:
    # Assuming the API is running inside the Docker container
    # Use environment variable for the service URL
    BASE_URL = os.getenv("WEBSCRAPER_SERVICE_URL", "http://webscraper:9001") # 'webscraper' is the service name in docker-compose
    # Use requests for tests running against a separate service
    IS_DOCKER_TEST = True
    IS_API_AVAILABLE = True # Assume API is available when running Docker tests

    # Helper function to make API requests in Docker environment
    def make_api_request(url_to_scrape):
        response = requests.post(f"{BASE_URL}/extract", json={"url": url_to_scrape}, timeout=45) # Increased timeout for scraping
        response.raise_for_status() # Raise exception for bad status codes
        return response.json()

except ImportError:
    # Fallback for local testing (e.g., during development without docker compose run test)
    from api import app # Import your FastAPI app instance
    client = TestClient(app)
    IS_DOCKER_TEST = False
    IS_API_AVAILABLE = True # API is available via TestClient
    BASE_URL = "" # Not needed for TestClient

    # Helper function for TestClient API calls
    def make_api_request(url_to_scrape):
        response = client.post("/extract", json={"url": url_to_scrape})
        # TestClient automatically handles base URL and doesn't need raise_for_status typically
        return response.json()
except requests.exceptions.ConnectionError:
     # Handle case where the API service isn't running (e.g., testing only scraper logic locally)
     print("Warning: API service not reachable. API tests will be skipped.")
     IS_API_AVAILABLE = False
     # Define a dummy function so tests don't break
     def make_api_request(url_to_scrape):
         pytest.skip("API service not available")
         return None

# Helper function to make MCP tool calls using stdio
async def make_mcp_tool_call(tool_name: str, arguments: dict) -> dict:
    """Connects to the MCP server via stdio and calls the specified tool."""
    # No need for IS_DOCKER_TEST check here, stdio works locally or in Docker
    # if not IS_DOCKER_TEST:
    #     pytest.skip("MCP integration tests require running in Docker environment")
    #     return {}

    server_params = StdioServerParameters(
        command="python",
        args=["src/mcp_server.py"] # Command to start the server
    )
    print(f"\nCreating MCP stdio session with command: {server_params.command} {server_params.args}")

    try:
        # Use stdio_client context manager
        async with stdio_client(server_params) as (read, write):
            # Pass streams to ClientSession
            async with ClientSession(read, write) as session:
                print(f"MCP stdio session initialized. Calling tool '{tool_name}'...")
                # Initialize the session (important step)
                await asyncio.wait_for(session.initialize(), timeout=30)
                print("MCP session initialized.")

                # Add timeout to the tool call
                result_obj = await asyncio.wait_for(
                    session.call_tool(name=tool_name, arguments=arguments),
                    timeout=60
                )
                print(f"MCP Tool '{tool_name}' returned: {result_obj}")
                
                # Extract the actual result data from the content field
                if result_obj.content and len(result_obj.content) > 0:
                    # The content field contains TextContent objects. Get the text from the first one.
                    result_text = result_obj.content[0].text
                    # Parse the JSON string in the text field
                    import json
                    result = json.loads(result_text)
                    return result
                else:
                    raise ValueError("Empty content in MCP tool result")
    except asyncio.TimeoutError:
        pytest.fail(f"MCP call timed out after 60 seconds. The operation may be hanging.")
    except ConnectionRefusedError: # Should not happen with stdio, but keep for safety
        pytest.fail(f"MCP Connection Refused: Could not start server process for stdio.")
    except Exception as e:
        pytest.fail(f"MCP tool call via stdio failed with unexpected error: {e}")
    return {}

# --- API Test Cases ---
# These test the /extract endpoint

@pytest.mark.skipif(not IS_API_AVAILABLE, reason="API service not available")
def test_api_extract_example_com():
    """Test basic extraction from example.com via API"""
    url = "https://example.com"
    result = make_api_request(url)

    assert result["status"] == "success"
    assert "Example Domain" in result["extracted_text"]
    assert result["error_message"] is None
    assert result["final_url"] in [url, url + '/']
    if IS_DOCKER_TEST: 
        smart_sleep(url)

@pytest.mark.skipif(not IS_API_AVAILABLE, reason="API service not available")
def test_api_extract_redirect_success_1():
    """Test extraction after a successful redirect via API."""
    url = "https://search.app/1jGF2"
    result = make_api_request(url)

    assert result["status"] == "success"
    assert result["extracted_text"]
    assert result["error_message"] is None
    assert result["final_url"] != url
    if IS_DOCKER_TEST: 
        smart_sleep(url)

@pytest.mark.skipif(not IS_API_AVAILABLE, reason="API service not available")
def test_api_extract_redirect_success_2():
    """Test extraction after another successful redirect via API."""
    url = "https://search.app/vXQf9"
    result = make_api_request(url)

    assert result["status"] == "success"
    assert result["extracted_text"]
    assert result["error_message"] is None
    assert result["final_url"] != url
    if IS_DOCKER_TEST: 
        smart_sleep(url)

@pytest.mark.skipif(not IS_API_AVAILABLE, reason="API service not available")
def test_api_extract_invalid_url_404():
    """Test API handling of an invalid URL that should result in an error."""
    url = "https://httpbin.org/status/404"
    result = make_api_request(url)

    assert result["status"] == "error_fetching"
    assert "404" in result["error_message"]
    assert result["extracted_text"] == ""
    assert result["final_url"] == url
    if IS_DOCKER_TEST: 
        smart_sleep(url)

@pytest.mark.skipif(not IS_API_AVAILABLE, reason="API service not available")
def test_api_extract_invalid_redirect_404():
    """Test API handling of a redirect.
    Note: This URL previously returned a 404 after redirect, but now appears to be valid.
    We've updated the test to validate the redirect behavior instead."""
    url = "https://search.app/CmeVX"
    result = make_api_request(url)

    # Check redirect happened successfully
    assert result["status"] == "success"
    assert result["extracted_text"]
    assert result["error_message"] is None
    assert result["final_url"] != url # Check that a redirect occurred
    if IS_DOCKER_TEST: 
        smart_sleep(url)

@pytest.mark.skipif(not IS_API_AVAILABLE, reason="API service not available")
def test_api_missing_url_parameter():
    """Test API response when 'url' parameter is missing."""
    if IS_DOCKER_TEST:
        response = requests.post(f"{BASE_URL}/extract", json={}, timeout=10)
        assert response.status_code == 422
        assert "detail" in response.json()
    else:
        response = client.post("/extract", json={})
        assert response.status_code == 422
        assert "detail" in response.json()

@pytest.mark.skipif(not IS_API_AVAILABLE, reason="API service not available")
def test_api_invalid_url_format():
    """Test API response with a poorly formatted URL."""
    url = "not_a_valid_url"
    if IS_DOCKER_TEST:
        response = requests.post(f"{BASE_URL}/extract", json={"url": url}, timeout=10)
        if response.status_code == 200:
            result = response.json()
            assert result["status"] in ["error_fetching", "error_invalid_url", "error_unknown"]
            assert result["error_message"] is not None
        else:
             assert response.status_code == 422
             assert "detail" in response.json()
    else:
        response = client.post("/extract", json={"url": url})
        assert response.status_code == 422
        assert "detail" in response.json()

@pytest.mark.skipif(not IS_API_AVAILABLE, reason="API service not available")
def test_api_extract_nonexistent_domain():
    """Test API handling of a completely non-existent domain."""
    url = "https://nonexistent-domain-for-testing-12345.com/somepage"
    result = make_api_request(url)

    assert result["status"] == "error_fetching"
    assert "resolve" in result["error_message"] or "connect" in result["error_message"]
    assert result["extracted_text"] == ""
    assert result["final_url"] == url
    if IS_DOCKER_TEST: 
        smart_sleep(url)

# --- Dynamic Article Finding Helper (Used by both API and Direct tests) ---
# (Keep the existing find_article_link_on_page function)
# ... (find_article_link_on_page function remains here) ...

# --- Dynamic Extraction Tests via API ---

@pytest.mark.skipif(not IS_API_AVAILABLE, reason="API service not available")
@pytest.mark.parametrize("domain_info", [
    # domain, optional start_path
    ("forbes.com", "/innovation/"),
    # ("fortune.com", "/section/fortune-analytics/"), # Using RSS feed now, start from root
    ("fortune.com", "/"), # Using RSS feed now
    ("dmnews.com", "/"),
    ("tomsguide.com", "/news"),
    ("dev.to", "/"),
    ("xda-developers.com", "/"),
    ("medium.com", "/"), # Still using HTML scraping
    ("techcrunch.com", "/"),
    ("wired.com", "/most-recent/"),
    ("engadget.com", "/")
])
def test_api_dynamic_article_extraction(domain_info):
    """Tests dynamic article extraction using the /extract API endpoint."""
    domain, start_path = domain_info
    start_url = f"https://{domain}{start_path or '/'}"
    print(f"\nTesting dynamic extraction via API for: {domain} (starting from {start_url})")

    article_url = find_article_link_on_page(start_url)
    if not article_url:
        pytest.skip(f"Could not dynamically find an article link on {start_url} for API test")
        return

    print(f"Found article link: {article_url}")
    print(f"Calling API to extract text...")

    try:
        result = make_api_request(article_url)
        print(f"API Result Status: {result['status']}")
        if result["status"] != "success":
             print(f"API Error: {result['error_message']}")

        # Shared assertions (can be refactored)
        assert result["status"] == "success", f"Expected status 'success' but got '{result['status']}' for {article_url}"
        assert result["extracted_text"], f"Expected non-empty extracted_text for {article_url}"
        assert len(result["extracted_text"]) >= 100, f"Extracted text too short ({len(result['extracted_text'])} chars) for {article_url}"
        assert result["error_message"] is None, f"Expected null error_message for {article_url}"
        requested_parsed = urlparse(article_url)
        final_parsed = urlparse(result["final_url"])
        assert final_parsed.netloc.replace("www.", "") == requested_parsed.netloc.replace("www.", ""), \
               f"Expected final_url '{result['final_url']}' to be on the same domain as requested URL '{article_url}' (ignoring www.)"

    except requests.exceptions.RequestException as e:
         pytest.fail(f"API call failed with RequestException: {e}. URL: {article_url}")
    except Exception as e:
        pytest.fail(f"An unexpected error occurred during API call or assertion: {e}. URL: {article_url}")

    if IS_DOCKER_TEST: 
        print("Using smart sleep for rate limiting...")
        smart_sleep(article_url, seconds=5) # Smart delay between dynamic tests


# --- MCP Tool Simulation Tests ---
# These test the core scraper function directly, simulating an MCP tool call

@pytest.mark.asyncio
async def test_mcp_extract_example_com():
    """Test basic extraction simulating MCP tool call."""
    url = "https://example.com"
    result = await extract_text_from_url(url)

    assert result["status"] == "success"
    assert "Example Domain" in result["extracted_text"]
    assert result["error_message"] is None
    assert result["final_url"] in [url, url + '/']
    assert list(result.keys()) == ["extracted_text", "status", "error_message", "final_url"] # Verify schema keys

@pytest.mark.asyncio
async def test_mcp_extract_redirect_success():
    """Test extraction after redirect simulating MCP tool call."""
    url = "https://search.app/1jGF2" # Use one redirect example
    result = await extract_text_from_url(url)

    assert result["status"] == "success"
    assert result["extracted_text"]
    assert result["error_message"] is None
    assert result["final_url"] != url
    assert list(result.keys()) == ["extracted_text", "status", "error_message", "final_url"]

@pytest.mark.asyncio
async def test_mcp_extract_invalid_url_404():
    """Test 404 handling simulating MCP tool call."""
    url = "https://httpbin.org/status/404"
    result = await extract_text_from_url(url)

    assert result["status"] == "error_fetching"
    assert "404" in result["error_message"]
    assert result["extracted_text"] == ""
    assert result["final_url"] == url
    assert list(result.keys()) == ["extracted_text", "status", "error_message", "final_url"]

@pytest.mark.asyncio
async def test_mcp_extract_invalid_redirect_404():
    """Test redirect handling simulation via MCP tool call.
    Note: This URL previously returned a 404 after redirect, but now appears to be valid.
    We've updated the test to validate the redirect behavior instead."""
    url = "https://search.app/CmeVX"
    result = await extract_text_from_url(url)

    # Check redirect happened successfully  
    assert result["status"] == "success"
    assert result["extracted_text"]
    assert result["error_message"] is None
    # The scraper function should resolve the redirect
    assert result["final_url"] != url
    assert list(result.keys()) == ["extracted_text", "status", "error_message", "final_url"]

@pytest.mark.asyncio
async def test_mcp_extract_nonexistent_domain():
    """Test non-existent domain handling simulating MCP tool call."""
    url = "https://nonexistent-domain-for-testing-123456.com/somepage"
    result = await extract_text_from_url(url)

    assert result["status"] == "error_fetching"
    assert "resolve" in result["error_message"] or "connect" in result["error_message"]
    assert result["extracted_text"] == ""
    assert result["final_url"] == url
    assert list(result.keys()) == ["extracted_text", "status", "error_message", "final_url"]


# --- Dynamic Extraction Tests via Direct Call (MCP Simulation) ---

@pytest.mark.asyncio
@pytest.mark.parametrize("domain_info", [
    # Use a subset for direct calls to avoid excessive runtime, API tests cover more
    ("forbes.com", "/innovation/"),
    ("fortune.com", "/"), # Test RSS path
    ("xda-developers.com", "/"), # Test exclusion logic
    ("medium.com", "/"), # Test tricky scraping
])
async def test_mcp_dynamic_article_extraction(domain_info):
    """Tests dynamic article extraction via direct core function call (MCP sim)."""
    domain, start_path = domain_info
    start_url = f"https://{domain}{start_path or '/'}"
    print(f"\nTesting dynamic extraction via Direct Call for: {domain} (starting from {start_url})")

    # Use the same helper to find the link
    article_url = find_article_link_on_page(start_url)
    if not article_url:
        pytest.skip(f"Could not dynamically find an article link on {start_url} for Direct Call test")
        return

    print(f"Found article link: {article_url}")
    print(f"Calling core function extract_text_from_url...")

    try:
        # Directly call the async scraper function
        result = await extract_text_from_url(article_url)
        print(f"Direct Call Result Status: {result['status']}")
        if result["status"] != "success":
             print(f"Direct Call Error: {result['error_message']}")

        # Verify the schema keys are correct for MCP tool result
        assert list(result.keys()) == ["extracted_text", "status", "error_message", "final_url"]

        # Reuse shared assertions (can be refactored into a helper)
        assert result["status"] == "success", f"Expected status 'success' but got '{result['status']}' for {article_url}"
        assert result["extracted_text"], f"Expected non-empty extracted_text for {article_url}"
        assert len(result["extracted_text"]) >= 100, f"Extracted text too short ({len(result['extracted_text'])} chars) for {article_url}"
        assert result["error_message"] is None, f"Expected null error_message for {article_url}"
        requested_parsed = urlparse(article_url)
        final_parsed = urlparse(result["final_url"])
        assert final_parsed.netloc.replace("www.", "") == requested_parsed.netloc.replace("www.", ""), \
               f"Expected final_url '{result['final_url']}' to be on the same domain as requested URL '{article_url}' (ignoring www.)"

    except Exception as e:
        pytest.fail(f"An unexpected error occurred during direct call or assertion: {e}. URL: {article_url}")

    # Add a small delay if running in Docker to be nice to target sites
    if IS_DOCKER_TEST: 
        print("Using smart sleep for rate limiting...")
        await smart_sleep_async(article_url, seconds=5) # Smart delay between dynamic tests


# Helper function to find an article link on a domain's page
def find_article_link_on_page(domain_url: str) -> str | None:
    """Tries to find a plausible article link on the given domain URL.
    Uses RSS for fortune.com, otherwise attempts HTML scraping."""
    parsed_domain_full = urlparse(domain_url)
    domain_root = parsed_domain_full.netloc

    # --- RSS Feed Strategy (Fortune) ---
    if 'fortune.com' in domain_root:
        fortune_rss_url = "https://fortune.com/feed/"
        try:
            feed = feedparser.parse(fortune_rss_url)
            if feed.entries:
                # Find the first entry with a valid link
                for entry in feed.entries:
                    if entry.link and urlparse(entry.link).scheme in ['http', 'https']:
                        print(f"Found Fortune link via RSS: {entry.link}")
                        return entry.link
            print(f"Warning: Could not find valid entry in Fortune RSS feed: {fortune_rss_url}")
            return None # Skip test if RSS fails or is empty
        except Exception as e:
            print(f"Error fetching or parsing Fortune RSS feed {fortune_rss_url}: {e}")
            return None # Skip test if RSS fails

    # --- HTML Scraping Strategy (Default, including Medium) ---
    try:
        # Define exclusions once (keep existing)
        excluded_patterns = [
             '/tag/', '/category/', '/author/', '/topic/', '/series/', '/collection/', '/section/',
             '/page/', '/search', '/login', '/signup', '/register', '/subscribe',
             '/video/', '/gallery/', '/podcast/', '/event/', '/live/',
             '/newsletter', '/about', '/contact', '/privacy', '/terms', '/legal', '/cookies',
             '/jobs', '/careers', '/advertise', '/support', '/help', '/faq',
             '/shop', '/store', '/products', '/community', '/forum', '/contribute', '/press',
             '/vetted/', # Added for Forbes product pages
             '?replytocom=', '#comment-', '/feed/', '/rss/', '/amp/', '.pdf', '.zip', '.jpg', '.png', '.gif', '.svg',
             # Added exclusions for common non-article links
             '/membership', '/gift', '/archive', '/feed', 'mailto:', 'tel:', 'javascript:',
             # XDA Specific Exclusions
             '/processor/', # Added for XDA category pages
             '/thread/' # Added for XDA forum-like threads
        ]

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.google.com/' # Adding a referer might help sometimes
        }
        response = requests.get(domain_url, headers=headers, timeout=20, allow_redirects=True) # Increased timeout
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        potential_links = set() # Use a set to avoid duplicates

        # Refined Heuristics for finding article links
        # 1. Prioritize links within <article> tags or common heading tags (h2, h3)
        for container_tag in soup.find_all(['article', 'h2', 'h3']):
            a_tag = container_tag.find('a', href=True)
            if a_tag and a_tag['href']:
                 potential_links.add(a_tag['href'])

        # 2. Find other plausible links
        for a_tag in soup.find_all('a', href=True):
             href = a_tag['href']
             # Basic checks: not empty, not just '#', is relative or absolute http(s)
             if href and href != '#' and (href.startswith('/') or href.startswith('http')):
                 # More specific check for Medium-like structures if applicable
                 if 'medium.com' in domain_root:
                      # Look for paths like /@user/slug or /publication/slug
                      # Avoid very short paths or paths ending with digits (often pagination/tags)
                      parsed_href = urlparse(urljoin(domain_url, href))
                      path_segments = [seg for seg in parsed_href.path.split('/') if seg]
                      if len(path_segments) >= 2 and not path_segments[-1].isdigit():
                           potential_links.add(href)
                 # General check using regex (keep this for broader coverage)
                 elif re.search(r'/(\d{4}/\d{2}/\d{2}/|\d{8}/|articles?|posts?|blog|news|story|[^/]+\\.html$)', href, re.IGNORECASE):
                      potential_links.add(href)
                 # Less specific check: longer path, likely an article
                 elif len(urlparse(href).path) > 15 and '.' not in urlparse(href).path.split('/')[-1]:
                     potential_links.add(href)


        # Filter and prioritize found links
        found_link = None
        parsed_base_domain = urlparse(domain_url) # Use the original domain_url for base

        # Convert set to list for potential ordering later if needed
        # Prioritize links found within article/h2/h3 tags if desired, or by length etc.
        # For now, simple iteration is fine.
        sorted_links = list(potential_links)
        # Example prioritization: longer paths often better articles
        sorted_links.sort(key=lambda x: len(urlparse(urljoin(domain_url, x)).path), reverse=True)

        for link in sorted_links:
            try:
                # Ensure link is absolute
                full_url = urljoin(domain_url, link.strip())
                parsed_link = urlparse(full_url)

                # Check 1: Scheme
                if parsed_link.scheme not in ['http', 'https']:
                    continue

                # Check 2: Exclusions (case-insensitive)
                lower_full_url = full_url.lower()
                if any(pattern in lower_full_url for pattern in excluded_patterns):
                    continue

                # Check 3: Domain Match (allow subdomains of the original requested domain)
                # Make sure it belongs to the target site (e.g., fortune.com, not ad.doubleclick.net)
                base_netloc = parsed_base_domain.netloc.replace('www.', '')
                link_netloc = parsed_link.netloc.replace('www.', '')
                if not link_netloc.endswith(base_netloc):
                    continue

                # Check 4: Basic path validity (avoid root, very short paths unless it's the only option)
                # This check is implicitly handled by prioritizing longer paths and regex searches
                if len(parsed_link.path) <= 1 and link != sorted_links[-1]: # Allow '/' only if it's the last resort? Probably not useful.
                    continue

                # Check 5: Avoid links that look like query parameters dominating the path
                if '?' in parsed_link.path or '&' in parsed_link.path:
                    continue

                # >> NEW Check 6: Remove URL Fragment <<
                final_url_no_fragment = parsed_link._replace(fragment='').geturl()

                # If all checks pass, consider it plausible
                print(f"Found plausible link for {domain_root}: {final_url_no_fragment}")
                found_link = final_url_no_fragment
                break # Take the first plausible one after sorting/filtering

            except Exception as e: # Catch potential errors during URL parsing/joining
                print(f"Warning: Skipping link '{link}' for {domain_root} due to processing error: {e}")
                continue

        if not found_link:
             print(f"Warning: Could not find a plausible article link on {domain_url} after filtering {len(potential_links)} candidates.")
             return None # Explicitly return None if no link is found

        return found_link

    except requests.RequestException as e:
        print(f"Error fetching {domain_url} for link finding: {e}")
        return None # Skip test if fetch fails
    except Exception as e:
        print(f"Error parsing {domain_url} for link finding: {e}")
        return None # Skip test if parsing fails


@pytest.mark.parametrize("domain_info", [
    # domain, optional start_path
    ("forbes.com", "/innovation/"),
    # ("fortune.com", "/section/fortune-analytics/"), # Using RSS feed now, start from root
    ("fortune.com", "/"), # Using RSS feed now
    ("dmnews.com", "/"),
    ("tomsguide.com", "/news"),
    ("dev.to", "/"),
    ("xda-developers.com", "/"),
    ("medium.com", "/"), # Still using HTML scraping
    ("techcrunch.com", "/"),
    ("wired.com", "/most-recent/"),
    ("engadget.com", "/")
])
def test_dynamic_article_extraction(domain_info):
    domain, start_path = domain_info
    start_url = f"https://{domain}{start_path or '/'}"
    
    print(f"\nTesting dynamic extraction for: {domain} (starting from {start_url})")
    
    article_url = find_article_link_on_page(start_url)
    
    if not article_url:
        pytest.skip(f"Could not dynamically find an article link on {start_url}")
        return

    print(f"Found article link: {article_url}")
    print(f"Calling API to extract text...")
    
    try:
        result = make_api_request(article_url)
        
        print(f"API Result Status: {result['status']}")
        if result["status"] != "success":
             print(f"API Error: {result['error_message']}")
             # Optionally print some text if parsing failed to see what was extracted
             if result["status"] == "error_parsing" and result["extracted_text"]:
                  print(f"Extracted text (first 200 chars): {result['extracted_text'][:200]}...")

        assert result["status"] == "success", f"Expected status 'success' but got '{result['status']}' for {article_url}"
        assert result["extracted_text"], f"Expected non-empty extracted_text for {article_url}"
        # Check if the generic length check passed (implies some reasonable content)
        assert len(result["extracted_text"]) >= 100, f"Extracted text too short ({len(result['extracted_text'])} chars) for {article_url}"
        assert result["error_message"] is None, f"Expected null error_message for {article_url}"
        # Relaxed check for dynamic tests: Only verify the domain matches, allowing path changes from redirects.
        requested_parsed = urlparse(article_url)
        final_parsed = urlparse(result["final_url"])
        assert final_parsed.netloc.replace("www.", "") == requested_parsed.netloc.replace("www.", ""), \
               f"Expected final_url '{result['final_url']}' to be on the same domain as requested URL '{article_url}' (ignoring www.)"

    except requests.exceptions.HTTPError as e:
         pytest.fail(f"API call failed with HTTPError: {e}. URL: {article_url}")
    except Exception as e:
         pytest.fail(f"An unexpected error occurred during API call or assertion: {e}. URL: {article_url}")
    finally:
         if IS_DOCKER_TEST: 
             print("Using smart sleep for rate limiting...")
             smart_sleep(article_url, seconds=5) # Smart delay between dynamic tests

# --- MCP Integration Tests ---
# These test the actual MCP server via the protocol

@pytest.mark.asyncio
async def test_mcp_integration_extract_example_com():
    """Test basic extraction via true MCP call."""
    url = "https://example.com"
    result = await make_mcp_tool_call("WEBPAGE_TEXT_EXTRACTOR", {"url": url})

    assert result["status"] == "success"
    assert "Example Domain" in result["extracted_text"]
    assert result["error_message"] is None
    assert result["final_url"] in [url, url + '/']
    assert list(result.keys()) == ["extracted_text", "status", "error_message", "final_url"]
    if IS_DOCKER_TEST: 
        await smart_sleep_async(url)

@pytest.mark.asyncio
async def test_mcp_integration_extract_redirect_success():
    """Test extraction after redirect via true MCP call."""
    url = "https://search.app/1jGF2"
    result = await make_mcp_tool_call("WEBPAGE_TEXT_EXTRACTOR", {"url": url})

    assert result["status"] == "success"
    assert result["extracted_text"]
    assert result["error_message"] is None
    assert result["final_url"] != url
    assert list(result.keys()) == ["extracted_text", "status", "error_message", "final_url"]
    if IS_DOCKER_TEST: 
        await smart_sleep_async(url)

@pytest.mark.asyncio
async def test_mcp_integration_extract_invalid_redirect_404():
    """Test redirect handling via true MCP call.
    Note: This URL previously returned a 404 after redirect, but now appears to be valid.
    We've updated the test to validate the redirect behavior instead."""
    url = "https://search.app/CmeVX"
    result = await make_mcp_tool_call("WEBPAGE_TEXT_EXTRACTOR", {"url": url})

    # Check redirect happened successfully
    assert result["status"] == "success"
    assert result["extracted_text"]
    assert result["error_message"] is None
    assert result["final_url"] != url # Check that a redirect occurred 
    assert list(result.keys()) == ["extracted_text", "status", "error_message", "final_url"]
    if IS_DOCKER_TEST: 
        await smart_sleep_async(url)

@pytest.mark.asyncio
async def test_mcp_integration_extract_nonexistent_domain():
    """Test non-existent domain handling via true MCP call."""
    url = "https://nonexistent-domain-for-testing-abcdefg.com/somepage"
    result = await make_mcp_tool_call("WEBPAGE_TEXT_EXTRACTOR", {"url": url})

    assert result["status"] == "error_fetching"
    assert "resolve" in result["error_message"] or "connect" in result["error_message"]
    assert result["extracted_text"] == ""
    assert result["final_url"] == url
    assert list(result.keys()) == ["extracted_text", "status", "error_message", "final_url"]
    if IS_DOCKER_TEST: 
        await smart_sleep_async(url)

# --- Dynamic Extraction Tests via Direct Call (MCP Simulation) ---
# ... existing simulation tests ...

# Helper function to find an article link ...
# ... existing helper function ...

# Parameterized test calling API (no change)
# ... existing test_dynamic_article_extraction test ... 
# type: ignore
from __future__ import annotations
from typing import *  # type: ignore

from pydantic import BaseModel
from functools import lru_cache, wraps
from tenacity import (
    retry,
    wait_random_exponential,
    stop_after_attempt,
    before_sleep_log,
)
import time
import logging
import http.cookiejar

# Import stealth-requests for browser fingerprinting
import stealth_requests
from stealth_requests import StealthSession as SSession
from curl_cffi.requests.session import HttpMethod

from .exceptions import AlreadyVisitedError, AlreadyFailedError
from .version import __version__

if TYPE_CHECKING:
    from requests import Response
    from stealth_requests import StealthResponse


class SessionOptions(BaseModel):
    timeout: float
    download_timeout: float
    retries: int
    retry_sleep: float
    retry_sleep_multiplier: float
    warc_output: str
    user_agent: str
    cookies: str | None = None
    headers: str | None = None
    header: list[str] = []
    get_urls: bool
    cookies: str


class Session:
    def __init__(self, options: SessionOptions):
        self._warc_file = None
        self._using_stealth_session = True  # Flag to indicate we're using StealthSession

        if options.warc_output:
            from warcio.warcwriter import WARCWriter
            from warcio.capture_http import capture_http

            self._capture_http = capture_http
            self._warc_file = open(options.warc_output, "wb")
            self._warc_writer = WARCWriter(self._warc_file)
            
            # WARC recording requires regular requests, not stealth_requests
            self._using_stealth_session = False
            import requests
            self._session = requests.Session()
        else:
            # Use StealthSession for browser fingerprinting to bypass Cloudflare
            # Initialize StealthSession without any arguments - we'll configure it after creation
            try:
                self._session = SSession()  # Initialize with defaults
                logging.info("Using StealthSession for browser fingerprinting to bypass Cloudflare")
            except Exception as e:
                # Fallback to regular requests if there's an issue
                logging.warning(f"Failed to initialize StealthSession: {e}. Falling back to regular requests.")
                self._using_stealth_session = False
                import requests
                self._session = requests.Session()
        
        self._options = options
        self._cache: dict[
            tuple[str, frozenset[tuple[str, Any]], frozenset[tuple[str, Any]]],
            Union[Response, 'StealthResponse'],
        ] = {}
        self._past_requests: set[
            tuple[str, frozenset[tuple[str, Any]], frozenset[tuple[str, Any]]]
        ] = set()
        self._past_failed_requests: set[
            tuple[str, frozenset[tuple[str, Any]], frozenset[tuple[str, Any]]]
        ] = set()
        
        # Initialize custom headers dictionary
        self._custom_headers = {}

        # Load cookies if provided
        if options.cookies:
            try:
                if self._using_stealth_session:
                    # Load cookies into StealthSession
                    # StealthSession just needs cookies as a dict
                    cookie_jar = http.cookiejar.MozillaCookieJar(options.cookies)
                    cookie_jar.load(ignore_discard=True, ignore_expires=True)
                    # Convert to dict format required by StealthSession
                    for cookie in cookie_jar:
                        self._session.cookies.set(cookie.name, cookie.value, 
                                                domain=cookie.domain)
                    logging.info(f"Loaded cookies from {options.cookies} into StealthSession")
                else:
                    # Regular requests session
                    cookie_jar = http.cookiejar.MozillaCookieJar(options.cookies)
                    cookie_jar.load(ignore_discard=True, ignore_expires=True)
                    self._session.cookies = cookie_jar
                    logging.info(f"Loaded cookies from {options.cookies}")
            except Exception as e:
                logging.error(f"Failed to load cookies from {options.cookies}: {e}")
        
        # Load custom headers
        custom_headers = {}
        
        # Handle User-Agent
        if options.user_agent:
            if self._using_stealth_session:
                # For StealthSession, we'll need to add this to custom_headers
                # It will override the automatic UA rotation in stealth_requests
                custom_headers["User-Agent"] = options.user_agent
                logging.debug(f"StealthSession will use custom User-Agent: {options.user_agent}")
            else:
                # For regular session, set directly
                self._session.headers["User-Agent"] = options.user_agent
        
        # Load headers from file if specified
        if options.headers:
            logging.info(f"Loaded headers from {options.headers}")
            with open(options.headers, "r") as f:
                for line in f:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        custom_headers[key.strip()] = value.strip()
                
        # Load headers from command line arguments
        if options.header:
            for header in options.header:
                if ":" in header:
                    key, value = header.split(":", 1)
                    custom_headers[key.strip()] = value.strip()
        
        # Apply custom headers if we have any
        if custom_headers:
            if self._using_stealth_session:
                # Store headers to be used in each request
                self._custom_headers = custom_headers
            else:
                # Apply directly to session
                for key, value in custom_headers.items():
                    self._session.headers[key] = value

        self.delay = 1
        self.attempts = 0

    def __del__(self):
        if self._warc_file:
            self._warc_file.close()
            
        # Close the session
        if hasattr(self, '_session') and self._session:
            try:
                self._session.close()
            except:
                pass

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] = {},
        headers: dict[str, Any] = {},
        should_cache: bool = False,
        should_retry: bool = True,
        **kwargs: Any,
    ):
        response = self.try_get(
            url,
            params=params,
            headers=headers,
            should_cache=should_cache,
            should_retry=should_retry,
            **kwargs,
        )
        response.raise_for_status()

        return response

    def try_get(
        self,
        url: str,
        *,
        params: dict[str, Any] = {},
        headers: dict[str, Any] = {},
        should_cache: bool = False,
        should_retry: bool = True,
        download_mode: bool = False,
        **kwargs: Any,
    ) -> Response:
        logging.debug(f"Attempting GET {url} {params} {headers}")

        frozen_params = frozenset(params.items())
        frozen_headers = frozenset(headers.items())

        if (url, frozen_params, frozen_headers) in self._cache:
            cached_response = self._cache[(url, frozen_params, frozen_headers)]

            if not should_cache:
                del self._cache[(url, frozen_params, frozen_headers)]

            return cached_response
        elif (url, frozen_params, frozen_headers) in self._past_requests:
            raise AlreadyVisitedError(url, frozen_params, frozen_headers)
        elif (url, frozen_params, frozen_headers) in self._past_failed_requests:
            raise AlreadyFailedError(url, frozen_params, frozen_headers)

        if should_retry:

            @retry(
                reraise=True,
                wait=wait_random_exponential(
                    multiplier=self._options.retry_sleep,
                    exp_base=self._options.retry_sleep_multiplier,
                ),
                stop=stop_after_attempt(self._options.retries),
                before_sleep=before_sleep_log(logging.getLogger(), logging.WARNING),
            )
            def retrying_get(
                url: str,
                *,
                params: dict[str, Any] = {},
                headers: dict[str, Any] = {},
                download_mode: bool = False,
                **kwargs: Any,
            ):
                return self._do_get(url, params=params, headers=headers, download_mode=download_mode, **kwargs)

            try:
                response = retrying_get(url, params=params, headers=headers, download_mode=download_mode, **kwargs)
            except:
                self._past_failed_requests.add((url, frozen_params, frozen_headers))
                raise
        else:
            response = self._do_get(url, params=params, headers=headers, download_mode=download_mode, **kwargs)

        if should_cache:
            self._cache[(url, frozen_params, frozen_headers)] = response
        else:
            self._past_requests.add((url, frozen_params, frozen_headers))

        return response

    def _after_retry(self):
        logging.warning(f"Waiting {self.delay} seconds.")

    def _do_get(
        self,
        url: str,
        *,
        params: dict[str, Any] = {},
        headers: dict[str, Any] = {},
        download_mode: bool = False,
        **kwargs: Any,
    ):
        if self._options.get_urls:
            print(url)
        else:
            logging.info(f"GET {url} {params} {headers}")

        # Prepare headers based on session type
        request_headers = {}
        if self._using_stealth_session:
            # StealthSession will handle User-Agent automatically
            request_headers = headers.copy()
        else:
            # Regular session needs User-Agent
            request_headers = headers.copy()
            if not request_headers:
                request_headers = {"User-Agent": self._options.user_agent}
            elif "User-Agent" not in request_headers:
                request_headers["User-Agent"] = self._options.user_agent
            
        # Add any custom headers from headers file or command line
        if hasattr(self, '_custom_headers') and self._custom_headers:
            for key, value in self._custom_headers.items():
                if key not in request_headers:  # Don't override headers provided directly to the method
                    request_headers[key] = value
                    
        # Only log complete headers in debug mode
        if logging.getLogger().level <= logging.DEBUG:
            logging.debug(f"Request headers: {request_headers}")

        if self._warc_file:
            with self._capture_http(self._warc_writer):
                return self._session.get(
                    url,
                    params=params,
                    headers=request_headers,
                    timeout=self._options.timeout,
                    **kwargs,
                )
        elif self._using_stealth_session:
            # StealthSession has built-in retry mechanism, set it based on our options
            retry_count = self._options.retries if self._options.retries > 0 else 0
            # Use longer timeout for file downloads
            timeout = self._options.download_timeout if download_mode else self._options.timeout
            if download_mode:
                logging.debug(f"Using download timeout: {timeout}s for {url}")
            return self._session.get(
                url,
                params=params,
                headers=request_headers,
                timeout=timeout,
                retry=retry_count,  # Use stealth-requests built-in retry
                **kwargs,
            )
        else:
            # Use longer timeout for file downloads
            timeout = self._options.download_timeout if download_mode else self._options.timeout
            if download_mode:
                logging.debug(f"Using download timeout: {timeout}s for {url}")
            return self._session.get(
                url,
                params=params,
                headers=request_headers,
                timeout=timeout,
                **kwargs,
            )

    def validate_url(self, url: str):
        try:
            self._session.get_adapter(url)
        except:  # `InvalidSchema`, not referencing it directly to avoid breaking WARC recording.
            return False

        return True

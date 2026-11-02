"""
Cliente HTTP Stealth con evasión de bloqueos Cloudflare / Akamai / DataDome
utilizando curl_cffi para suplantación de huella TLS (JA3/JA4) y HTTP/2 de Chrome.
"""
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("GlitchHunter.StealthClient")

# Intentar importar curl_cffi para máxima evasión anti-bot
_CURL_CFFI_AVAILABLE = False
try:
    from curl_cffi import requests as curl_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    import requests as std_requests
    _CURL_CFFI_AVAILABLE = False

DEFAULT_CHROME_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


class StealthHTTPClient:
    """Cliente HTTP sigiloso para scraping en retail con bypass TLS."""

    def __init__(self, impersonate: str = "chrome124", timeout: int = 12):
        self.impersonate = impersonate
        self.timeout = timeout
        self.use_curl_cffi = _CURL_CFFI_AVAILABLE
        self._session = None
        self._init_session()

    def _init_session(self):
        if self.use_curl_cffi:
            self._session = curl_requests.Session(impersonate=self.impersonate)
            self._session.headers.update(DEFAULT_CHROME_HEADERS)
            logger.debug("StealthHTTPClient inicializado con curl_cffi (impersonate=%s)", self.impersonate)
        else:
            import requests as std_requests
            self._session = std_requests.Session()
            self._session.headers.update(DEFAULT_CHROME_HEADERS)
            logger.warning("curl_cffi no disponible; usando requests estándar con cabeceras Chrome emuladas.")

    def get(self, url: str, headers: Optional[Dict[str, str]] = None, timeout: Optional[int] = None, **kwargs) -> Any:
        merged_headers = dict(DEFAULT_CHROME_HEADERS)
        if headers:
            merged_headers.update(headers)
        
        req_timeout = timeout or self.timeout

        if self.use_curl_cffi:
            try:
                return self._session.get(url, headers=merged_headers, timeout=req_timeout, impersonate=self.impersonate, **kwargs)
            except Exception as e:
                logger.warning("Fallo en sesión curl_cffi; reintentando petición directa get(%s): %s", url, e)
                return curl_requests.get(url, headers=merged_headers, timeout=req_timeout, impersonate=self.impersonate, **kwargs)
        else:
            return self._session.get(url, headers=merged_headers, timeout=req_timeout, **kwargs)

    def post(self, url: str, data: Any = None, json: Any = None, headers: Optional[Dict[str, str]] = None, timeout: Optional[int] = None, **kwargs) -> Any:
        merged_headers = dict(DEFAULT_CHROME_HEADERS)
        if headers:
            merged_headers.update(headers)
            
        req_timeout = timeout or self.timeout

        if self.use_curl_cffi:
            try:
                return self._session.post(url, data=data, json=json, headers=merged_headers, timeout=req_timeout, impersonate=self.impersonate, **kwargs)
            except Exception as e:
                logger.warning("Fallo en sesión curl_cffi; reintentando petición directa post(%s): %s", url, e)
                return curl_requests.post(url, data=data, json=json, headers=merged_headers, timeout=req_timeout, impersonate=self.impersonate, **kwargs)
        else:
            return self._session.post(url, data=data, json=json, headers=merged_headers, timeout=req_timeout, **kwargs)


# Instancia singleton para reuso en toda la suite de scrapers
default_stealth_client = StealthHTTPClient()

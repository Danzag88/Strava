"""Paced Strava API client."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import requests

from strava_backup.rate_limit import DailyBudgetExhausted, RateLimit, parse_rate_limit

logger = logging.getLogger(__name__)

SHORT_WINDOW_SECONDS = 15 * 60 + 5
PAGE_SIZE = 200


class StravaError(Exception):
    def __init__(self, status: int, path: str, message: str):
        self.status = status
        self.path = path
        super().__init__(f"{status} {path}: {message}")


class StravaClient:
    def __init__(
        self,
        access_token: str,
        api_base: str,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        short_budget: int = 90,
        daily_budget: int = 950,
        on_response: Callable[[str, str, int, RateLimit, str | None], None] | None = None,
        refresh_access_token: Callable[[], str] | None = None,
    ):
        self.access_token = access_token
        self.api_base = api_base.rstrip("/")
        self.session = session or requests.Session()
        self.sleep = sleep
        self.short_budget = short_budget
        self.daily_budget = daily_budget
        self.on_response = on_response
        self.refresh_access_token = refresh_access_token
        self.limits = RateLimit()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._request("GET", path, params=params)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise StravaError(response.status_code, path, "Response was not JSON") from exc

    def get_text(self, path: str, params: dict[str, Any] | None = None) -> str:
        return self._request("GET", path, params=params).text

    def get_pages(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        page = 1
        while page <= 1000:
            query = dict(params or {})
            query["page"] = page
            query["per_page"] = PAGE_SIZE
            batch = self.get_json(path, query)
            if not isinstance(batch, list) or not batch:
                return
            yield from batch
            if len(batch) < PAGE_SIZE:
                return
            page += 1

    def get_comments(self, activity_id: str) -> list[dict]:
        comments: list[dict] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": PAGE_SIZE}
            if cursor:
                params["after_cursor"] = cursor
            batch = self.get_json(f"/activities/{activity_id}/comments", params)
            if not isinstance(batch, list) or not batch:
                return comments
            comments.extend(batch)
            next_cursor = batch[-1].get("cursor") if isinstance(batch[-1], dict) else None
            if not next_cursor or next_cursor in seen_cursors or len(batch) < PAGE_SIZE:
                return comments
            seen_cursors.add(str(next_cursor))
            cursor = str(next_cursor)

    def get_kudos(self, activity_id: str) -> list[dict]:
        return list(self.get_pages(f"/activities/{activity_id}/kudos"))

    def _before_request(self) -> None:
        if self.limits.daily_usage is not None and self.limits.daily_usage >= self.daily_budget:
            raise DailyBudgetExhausted(
                f"Daily read usage is {self.limits.daily_usage}, at the {self.daily_budget} headroom stop"
            )
        if self.limits.short_usage is not None and self.limits.short_usage >= self.short_budget:
            logger.info(
                "15-minute read usage is %s. Sleeping %s seconds for the window to reset",
                self.limits.short_usage,
                SHORT_WINDOW_SECONDS,
            )
            self.sleep(SHORT_WINDOW_SECONDS)
            self.limits = RateLimit(daily_usage=self.limits.daily_usage)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        allow_refresh: bool = True,
    ) -> requests.Response:
        self._before_request()
        url = f"{self.api_base}{path}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        response: requests.Response | None = None
        error_message: str | None = None
        try:
            response = self._send(method, url, headers, params)
        except requests.RequestException as exc:
            error_message = str(exc)
            self._record(method, path, 0, error_message)
            raise StravaError(0, path, error_message) from exc

        self.limits = parse_rate_limit(dict(response.headers))
        if response.status_code == 429 and allow_refresh:
            self._record(method, path, 429, "rate limited")
            self._sleep_for_429(response)
            return self._request(method, path, params, allow_refresh=False)
        if response.status_code == 401 and allow_refresh and self.refresh_access_token:
            self._record(method, path, 401, "unauthorized")
            self.access_token = self.refresh_access_token()
            return self._request(method, path, params, allow_refresh=False)
        if response.status_code >= 400:
            error_message = response.text[:300]
            self._record(method, path, response.status_code, error_message)
            raise StravaError(response.status_code, path, error_message)
        self._record(method, path, response.status_code, None)
        return response

    def _send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None,
    ) -> requests.Response:
        last_error: requests.RequestException | None = None
        for attempt in range(3):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    timeout=(30, 120),
                )
            except requests.RequestException as exc:
                last_error = exc
                self.sleep(2**attempt)
                continue
            if response.status_code < 500 or attempt == 2:
                return response
            self.sleep(2**attempt)
        if last_error:
            raise last_error
        raise requests.RequestException(f"Request failed for {url}")

    def _sleep_for_429(self, response: requests.Response) -> None:
        retry_after = response.headers.get("Retry-After")
        try:
            delay = int(retry_after) if retry_after else SHORT_WINDOW_SECONDS
        except ValueError:
            delay = SHORT_WINDOW_SECONDS
        delay = max(1, min(delay, SHORT_WINDOW_SECONDS))
        logger.info("Strava returned 429. Sleeping %s seconds", delay)
        self.sleep(delay)
        self.limits = RateLimit(daily_usage=self.limits.daily_usage)

    def _record(self, method: str, path: str, status: int, error_message: str | None) -> None:
        if self.on_response:
            self.on_response(method, path, status, self.limits, error_message)

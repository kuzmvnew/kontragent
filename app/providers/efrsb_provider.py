"""Credential-aware client for the official EFRSB publications REST API."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import re

import httpx


EFRSB_PRODUCTION_BASE_URL = "https://bank-publications-prod.fedresurs.ru"
EFRSB_PUBLIC_MESSAGE_URL = "https://fedresurs.ru/bankruptmessages/{guid}"


class EfrsbProviderError(Exception):
    def __init__(self, *, kind: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status


class EfrsbRestProvider:
    """Bounded exact-INN access to Fedresurs' documented production service.

    Credentials are only accepted from explicit arguments or environment
    variables and are never included in URLs, returned evidence, or errors.
    """

    code = "efrsb_official_rest"

    def __init__(
        self,
        *,
        login: str | None = None,
        password: str | None = None,
        base_url: str = EFRSB_PRODUCTION_BASE_URL,
        client=None,
    ):
        self.login = login or os.getenv("EFRSB_API_LOGIN")
        self.password = password or os.getenv("EFRSB_API_PASSWORD")
        self.base_url = base_url.rstrip("/")
        self.client = client or (
            httpx.Client(
                timeout=45,
                follow_redirects=True,
                http2=False,
                headers={"User-Agent": "Kontragent/1.0 (official EFRSB REST client)"},
            )
            if self.configured else None
        )

    @property
    def configured(self) -> bool:
        return bool(self.login and self.password)

    def _payload(self, response, *, operation: str) -> dict:
        if response.status_code != 200:
            kind = "access_denied" if response.status_code in {401, 403} else "http_error"
            raise EfrsbProviderError(
                kind=kind,
                message=f"EFRSB {operation} returned HTTP {response.status_code}",
                http_status=response.status_code,
            )
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise EfrsbProviderError(
                kind="invalid_response", message=f"EFRSB {operation} returned invalid JSON",
            ) from error
        if not isinstance(payload, dict):
            raise EfrsbProviderError(
                kind="invalid_response", message=f"EFRSB {operation} returned an invalid payload",
            )
        if payload.get("code") is not None and payload.get("message"):
            raise EfrsbProviderError(
                kind="provider_error", message=f"EFRSB {operation} rejected the request",
            )
        return payload

    def _token(self) -> str:
        if not self.configured:
            raise EfrsbProviderError(
                kind="access_pending",
                message="EFRSB_API_LOGIN and EFRSB_API_PASSWORD are not configured",
            )
        try:
            response = self.client.post(
                f"{self.base_url}/v1/auth",
                json={"login": self.login, "password": self.password},
            )
        except httpx.TimeoutException as error:
            raise EfrsbProviderError(kind="timeout", message="EFRSB authentication timed out") from error
        except httpx.RequestError as error:
            raise EfrsbProviderError(kind="network_error", message="EFRSB authentication network error") from error
        token = self._payload(response, operation="authentication").get("jwt")
        if not isinstance(token, str) or not token:
            raise EfrsbProviderError(
                kind="invalid_response", message="EFRSB authentication returned no JWT",
            )
        return token

    def _get(self, path: str, *, token: str, params: dict) -> dict:
        try:
            response = self.client.get(
                f"{self.base_url}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as error:
            raise EfrsbProviderError(kind="timeout", message="EFRSB request timed out") from error
        except httpx.RequestError as error:
            raise EfrsbProviderError(kind="network_error", message="EFRSB network error") from error
        return self._payload(response, operation=path)

    def search_company(self, *, inn: str) -> dict:
        inn = re.sub(r"\D", "", str(inn or ""))
        if not re.fullmatch(r"\d{10}|\d{12}", inn):
            raise ValueError("Некорректный ИНН")
        token = self._token()
        entity_type = "Company" if len(inn) == 10 else "Person"
        bankrupts = self._get(
            "/v1/bankrupts", token=token,
            params={"type": entity_type, "inn": inn, "limit": 1000, "offset": 0},
        )
        raw_debtors = bankrupts.get("pageData") or []
        exact_debtors = []
        for row in raw_debtors:
            if not isinstance(row, dict):
                continue
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            if re.sub(r"\D", "", str(data.get("inn") or "")) == inn:
                exact_debtors.append({
                    "guid": row.get("guid"), "type": row.get("type"),
                    "name": data.get("name") or " ".join(filter(None, (
                        data.get("lastName"), data.get("firstName"), data.get("middleName"),
                    ))),
                    "inn": data.get("inn"), "ogrn": data.get("ogrn") or data.get("ogrnip"),
                    "address": data.get("address"),
                })

        events = []
        messages_complete = True
        for debtor in exact_debtors:
            guid = debtor.get("guid")
            if not guid:
                messages_complete = False
                continue
            messages = self._get(
                "/v1/messages", token=token,
                params={
                    "bankruptGUID": guid, "isLocked": False, "isAnnulled": False,
                    "includeBankruptInfo": False, "sort": "DatePublish:desc",
                    "limit": 1000, "offset": 0,
                },
            )
            rows = messages.get("pageData") or []
            messages_complete = messages_complete and int(messages.get("total") or 0) <= len(rows)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                message_guid = row.get("guid")
                events.append({
                    "debtor": debtor.get("name"), "debtor_inn": debtor.get("inn"),
                    "case_number": None, "procedure": None, "status": None,
                    "publication_number": row.get("number"),
                    "publication_date": row.get("datePublish"), "event_date": None,
                    "message_type": row.get("type"), "source_identifier": message_guid,
                    "source_url": (
                        EFRSB_PUBLIC_MESSAGE_URL.format(guid=message_guid)
                        if message_guid else self.base_url + "/v1/messages"
                    ),
                })

        bankrupts_complete = int(bankrupts.get("total") or 0) <= len(raw_debtors)
        total_debtors = int(bankrupts.get("total") or 0)
        return {
            "exact_identifier_match": bool(exact_debtors) or total_debtors == 0,
            "debtor": exact_debtors[0] if len(exact_debtors) == 1 else None,
            "debtors": exact_debtors,
            "events": events,
            "coverage_complete": bankrupts_complete and messages_complete,
            "source_as_of": datetime.now(timezone.utc),
            "source_url": self.base_url + "/v1/bankrupts",
        }

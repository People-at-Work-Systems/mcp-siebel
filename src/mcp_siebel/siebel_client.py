"""Async Siebel REST API client ported from the TypeScript MCP server."""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal, overload
from urllib.parse import parse_qs, quote, urlparse

import httpx

log = logging.getLogger("mcp-siebel")


SiebelRecord = dict[str, Any]


class SiebelError(Exception):
    def __init__(self, message: str, status_code: int, response_body: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


@dataclass
class SiebelFieldDescriptor:
    name: str
    type: str
    max_length: int | None = None
    required: bool = False
    read_only: bool = False
    precision: int | None = None
    scale: int | None = None
    validation: str | None = None


@dataclass
class SiebelBCDescriptor:
    business_object: str
    business_component: str
    fields: list[SiebelFieldDescriptor]


@dataclass
class SiebelQueryResult:
    items: list[SiebelRecord]
    total_count: int | None
    has_more: bool
    offset: int
    page_size: int
    next_start_row: int | None = None


class SiebelClient:
    def __init__(
        self,
        *,
        base_url: str,
        auth_mode: str = "basic",
        username: str | None = None,
        password: str | None = None,
        sso_header_name: str = "SBL_USER",
        sso_username: str | None = None,
        page_size: int = 20,
        request_timeout_ms: int = 30_000,
        max_concurrent_requests: int = 5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_mode = auth_mode
        self.username = username
        self.password = password
        self.sso_header_name = sso_header_name
        self.sso_username = sso_username
        self.page_size = page_size
        self.timeout = request_timeout_ms / 1000
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)
        # Populated by login() for auth_mode == "token"
        self._session_token: str | None = None

    async def login(self) -> None:
        """Exchange username+password for a Siebel session token (auth_mode='token').

        Calls POST /auth with Basic credentials. Siebel 26.x returns
        {"siebel-session-token": "<token>"} which is then used as Bearer on
        every subsequent request.
        """
        basic = base64.b64encode(
            f"{self.username or ''}:{self.password or ''}".encode()
        ).decode()
        async with httpx.AsyncClient(verify=False, timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/auth",
                headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
            )
        if response.status_code >= 400:
            log.error(
                "Siebel login failed: POST %s/auth → HTTP %s — %s",
                self.base_url,
                response.status_code,
                response.text[:500] if response.text else "(no body)",
            )
            raise SiebelError(
                f"Siebel login returned {response.status_code}",
                response.status_code,
                response.text,
            )
        data = response.json()
        token = data.get("siebel-session-token") or data.get("token")
        if not token:
            raise SiebelError("Siebel login response missing session token", 0, str(data))
        self._session_token = token
        log.info("Siebel session token acquired (mode=token)")

    def _auth_headers(self) -> dict[str, str]:
        if self.auth_mode == "sso":
            return {self.sso_header_name: self.sso_username or ""}
        if self.auth_mode == "token":
            return {"Authorization": f"Bearer {self._session_token or ''}"}
        # basic (default)
        token = base64.b64encode(f"{self.username or ''}:{self.password or ''}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    @overload
    async def _request(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        query_params: dict[str, str] | None = None,
        *,
        include_headers: Literal[False] = False,
    ) -> Any: ...

    @overload
    async def _request(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        query_params: dict[str, str] | None = None,
        *,
        include_headers: Literal[True],
    ) -> tuple[Any, httpx.Headers]: ...

    async def _request(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        query_params: dict[str, str] | None = None,
        *,
        include_headers: bool = False,
    ) -> Any | tuple[Any, httpx.Headers]:
        async with self._semaphore:
            headers = {**self._auth_headers(), "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"

            log.debug(
                "Siebel request: %s %s%s | auth_mode=%s | headers=%s",
                method,
                self.base_url,
                path,
                self.auth_mode,
                {k: (v[:8] + "…" if k.lower() in {"authorization"} and len(v) > 8 else v)
                 for k, v in headers.items()},
            )

            async with httpx.AsyncClient(verify=False, timeout=self.timeout) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    params=query_params,
                    json=body,
                    headers=headers,
                )
                if response.status_code == 401 and self.auth_mode == "sso":
                    response = await client.request(
                        method,
                        f"{self.base_url}{path}",
                        params=query_params,
                        json=body,
                        headers=headers,
                    )

            if response.status_code == 204:
                return (None, response.headers) if include_headers else None
            if response.status_code >= 400:
                log.error(
                    "Siebel API error: %s %s → HTTP %s — %s",
                    method,
                    f"{self.base_url}{path}",
                    response.status_code,
                    response.text if response.text else "(no body)",
                )
                raise SiebelError(f"Siebel API returned {response.status_code}", response.status_code, response.text)
            data = response.json()
            return (data, response.headers) if include_headers else data

    async def list_business_objects(self) -> list[str]:
        all_objects: list[str] = []
        start_row = 0
        page_size = 100
        while True:
            params = {"PageSize": str(page_size), "pagination": "y"}
            if start_row > 0:
                params["StartRowNum"] = str(start_row)
            result = await self._request("GET", "/data/describe", query_params=params)
            for path in (result.get("paths") or {}).keys():
                prefix = "/data/"
                suffix = "/describe"
                if path.startswith(prefix) and path.endswith(suffix):
                    name = path[len(prefix) : -len(suffix)]
                    if "/" not in name:
                        all_objects.append(name)
            has_more, next_start_row = _parse_pagination_links(result.get("Link"))
            if not has_more or next_start_row is None:
                break
            start_row = next_start_row
        return sorted(set(all_objects))

    async def describe_bc(self, business_object: str, business_component: str) -> SiebelBCDescriptor:
        bo = _enc(business_object)
        bc = _enc(business_component)
        try:
            result = await self._request("GET", f"/data/{bo}/{bc}/describe")
        except SiebelError as exc:
            if "SBL-EAI-50257" not in exc.response_body:
                raise
            primary_bc = _enc(business_object)
            result = await self._request("GET", f"/data/{bo}/{primary_bc}/-/{bc}/describe")
        return SiebelBCDescriptor(business_object, business_component, _parse_fields_from_describe(result))

    async def query(
        self,
        business_object: str,
        business_component: str,
        *,
        search_spec: str | None = None,
        fields: list[str] | None = None,
        sort_spec: str | None = None,
        page_size: int | None = None,
        offset: int | None = None,
        childlinks: str | None = None,
    ) -> SiebelQueryResult:
        params = _query_params(search_spec, fields, sort_spec, page_size or self.page_size, offset)
        params["childlinks"] = childlinks or "None"
        try:
            data, headers = await self._request(
                "GET",
                f"/data/{_enc(business_object)}/{_enc(business_component)}",
                query_params=params,
                include_headers=True,
            )
        except SiebelError as exc:
            if "SBL-EAI-50257" not in exc.response_body or business_component.lower() == business_object.lower():
                raise
            data, headers = await self._request(
                "GET",
                f"/data/{_enc(business_component)}/{_enc(business_component)}",
                query_params=params,
                include_headers=True,
            )
        return _query_result(data, headers, offset or 0, int(params["PageSize"]))

    async def get_record(
        self, business_object: str, business_component: str, record_id: str,
        fields: list[str] | str | None = None,
        *,
        childlinks: str | None = "None",
    ) -> SiebelRecord | None:
        """Fetch one record. `childlinks` defaults to "None" so Siebel omits the
        100+ child-BC Link entries it would otherwise return — _format_record
        turns those into a massive "Children:" line that floods the chat and the
        record form. Pass childlinks="ALL" (or use get_record_with_children) when
        child data is actually wanted. Mirrors the query() default."""
        clean = _normalise_fields(fields)
        params: dict[str, str] = {"childlinks": childlinks or "None"}
        if clean:
            params["fields"] = ",".join(clean)
        try:
            return await self._request(
                "GET",
                f"/data/{_enc(business_object)}/{_enc(business_component)}/{_enc(record_id)}",
                query_params=params,
            )
        except SiebelError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def get_record_with_children(
        self, business_object: str, business_component: str, record_id: str
    ) -> SiebelRecord | None:
        try:
            result = await self._request(
                "GET",
                f"/data/{_enc(business_object)}/{_enc(business_component)}/{_enc(record_id)}",
                query_params={"getChildren": "ALL"},
            )
            record = result.get("items", result) if isinstance(result, dict) else result
            return {"__childrenAvailable": True, **record}
        except SiebelError as exc:
            if exc.status_code == 404:
                return None
            if exc.status_code == 500 and "SBL-EAI-04392" in exc.response_body:
                flat = await self.get_record(business_object, business_component, record_id)
                return {"__childrenAvailable": False, "__fallbackReason": "SBL-EAI-04392", **(flat or {})}
            raise

    async def create_record(self, business_object: str, business_component: str, data: SiebelRecord) -> SiebelRecord:
        dummy_id = "NEW" + uuid.uuid4().hex[:16]
        return await self._request(
            "PUT",
            f"/data/{_enc(business_object)}/{_enc(business_component)}/{_enc(dummy_id)}",
            data,
        )

    async def update_record(
        self, business_object: str, business_component: str, record_id: str, data: SiebelRecord
    ) -> SiebelRecord:
        return await self._request(
            "PUT",
            f"/data/{_enc(business_object)}/{_enc(business_component)}/{_enc(record_id)}",
            data,
        )

    async def delete_record(self, business_object: str, business_component: str, record_id: str) -> None:
        await self._request("DELETE", f"/data/{_enc(business_object)}/{_enc(business_component)}/{_enc(record_id)}")

    async def query_child_records(
        self,
        business_object: str,
        business_component: str,
        parent_id: str,
        child_business_component: str,
        *,
        search_spec: str | None = None,
        fields: list[str] | None = None,
        sort_spec: str | None = None,
        page_size: int | None = None,
        offset: int | None = None,
    ) -> SiebelQueryResult:
        params = _query_params(search_spec, fields, sort_spec, page_size or self.page_size, offset)
        data, headers = await self._request(
            "GET",
            f"/data/{_enc(business_object)}/{_enc(business_component)}/{_enc(parent_id)}/"
            f"{_enc(child_business_component)}",
            query_params=params,
            include_headers=True,
        )
        return _query_result(data, headers, offset or 0, int(params["PageSize"]))

    async def create_child_record(
        self,
        business_object: str,
        business_component: str,
        parent_id: str,
        child_business_component: str,
        data: SiebelRecord | list[SiebelRecord],
    ) -> SiebelRecord:
        records = data if isinstance(data, list) else [data]
        with_ids = [
            record if record.get("Id") else {**record, "Id": f"dummy{idx:03d}"}
            for idx, record in enumerate(records, 1)
        ]
        return await self._request(
            "PUT",
            f"/data/{_enc(business_object)}/{_enc(business_component)}/{_enc(parent_id)}",
            {child_business_component: with_ids},
        )

    async def upload_attachment(
        self,
        business_object: str,
        business_component: str,
        parent_id: str,
        attachment_bc: str,
        file_name_field: str,
        file_ext_field: str,
        file_name: str,
        file_ext: str,
        base64_content: str,
        comment: str | None = None,
    ) -> SiebelRecord:
        body = {file_name_field: file_name, file_ext_field: file_ext, "Attachment Id": base64_content}
        if comment:
            body["Comment"] = comment
        return await self._request(
            "PUT",
            f"/data/{_enc(business_object)}/{_enc(business_component)}/{_enc(parent_id)}/{_enc(attachment_bc)}",
            body,
        )

    async def describe_repository_bc(
        self, business_object: str, business_component: str
    ) -> list[dict[str, Any]]:
        """Return full field metadata from the Siebel workspace repository.

        Richer than describe_bc: includes pre/post defaults, LOV types,
        calculated flags, join info, and fields that only appear in searchspec.

        Tries the workspace metadata API first (Siebel 17+/26.x). Falls back
        to querying the Repository Business Component Field BC via the data API
        on older or restricted instances.
        """
        bo = _enc(business_object)
        bc = _enc(business_component)

        # Primary: workspace metadata endpoint (returns all repository fields)
        for workspace in ("MAIN", "main"):
            try:
                result = await self._request("GET", f"/workspace/{workspace}/BO/{bo}/BC/{bc}/fields")
                items = result.get("items") if isinstance(result, dict) else None
                if isinstance(items, list) and items:
                    return items
            except SiebelError:
                continue

        # Fallback: query Repository Field BC through the standard data API
        safe_bc = business_component.replace("'", "''")
        try:
            result = await self._request(
                "GET",
                "/data/Repository Business Object/Repository Business Component Field",
                query_params={
                    "searchspec": f"[Business Component Name]='{safe_bc}'",
                    "fields": (
                        "Name,Type,Length,Required,ReadOnly,Calculated,"
                        "PreDefaultValue,PostDefaultValue,LOVType,JoinName,PickList,Active"
                    ),
                    "PageSize": "200",
                    "pagination": "y",
                    "uniformresponse": "y",
                },
            )
            return result.get("items") or []
        except SiebelError:
            pass

        return []

    async def call_business_service(self, service: str, method: str, inputs: Any) -> Any:
        return await self._request(
            "POST", f"/service/{_enc(service)}/{_enc(method)}", inputs, query_params={"uniformresponse": "Y"}
        )


def _normalise_fields(fields: list[str] | str | None) -> list[str] | None:
    """Accept a list or a comma-separated string; return a clean list or None."""
    if not fields:
        return None
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",")]
    else:
        fields = [f.strip() for f in fields]
    clean = [f for f in fields if f]
    return clean if clean else None


def _query_params(
    search_spec: str | None,
    fields: list[str] | str | None,
    sort_spec: str | None,
    page_size: int,
    offset: int | None,
) -> dict[str, str]:
    params = {
        "PageSize": str(min(page_size, 100)),
        "pagination": "y",
        "recordcountneeded": "true",
        "uniformresponse": "y",
    }
    if search_spec:
        params["searchspec"] = search_spec
    clean_fields = _normalise_fields(fields)
    if clean_fields:
        params["fields"] = ",".join(clean_fields)
    if sort_spec:
        params["sortspec"] = sort_spec
    if offset:
        params["StartRowNum"] = str(offset)
    return params


def _query_result(data: Any, headers: httpx.Headers, offset: int, page_size: int) -> SiebelQueryResult:
    items = data.get("items") if isinstance(data, dict) else []
    if not isinstance(items, list):
        items = []
    total_header = headers.get("total-record-count")
    has_more, next_start_row = _parse_pagination_links(data.get("Link") if isinstance(data, dict) else None)
    return SiebelQueryResult(
        items=items,
        total_count=int(total_header) if total_header is not None else None,
        has_more=has_more,
        next_start_row=next_start_row,
        offset=offset,
        page_size=page_size,
    )


def _parse_fields_from_describe(spec: dict[str, Any]) -> list[SiebelFieldDescriptor]:
    schemas = ((spec.get("components") or {}).get("schemas") or {})
    for schema in schemas.values():
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not properties:
            continue
        required = schema.get("required") or []
        return [
            SiebelFieldDescriptor(
                name=name,
                type=definition.get("x-siebel-datatype") or definition.get("type") or "string",
                max_length=_optional_int(definition.get("x-maxLength")),
                required=name in required,
                read_only=definition.get("x-siebel-readOnly") == "true",
                precision=_optional_int(definition.get("x-siebel-precision")),
                scale=_optional_int(definition.get("x-siebel-scale")),
                validation=definition.get("x-siebel-validation"),
            )
            for name, definition in properties.items()
            if isinstance(definition, dict)
        ]
    return []


def _parse_pagination_links(links: Any) -> tuple[bool, int | None]:
    if not isinstance(links, list):
        return False, None
    next_link = next((link for link in links if isinstance(link, dict) and link.get("rel") == "nextSet"), None)
    href = next_link.get("href") if next_link else None
    if not href:
        return False, None
    try:
        values = parse_qs(urlparse(href).query).get("StartRowNum")
        return (True, int(values[0])) if values else (False, None)
    except (TypeError, ValueError):
        return False, None


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _enc(value: str) -> str:
    return quote(value, safe="")

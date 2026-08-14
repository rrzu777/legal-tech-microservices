from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from http.cookiejar import Cookie, CookieJar
import math
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class CookieRecord:
    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = False
    expires: int | None = None
    http_only: bool = False
    same_site: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str) or not self.name
            or not isinstance(self.value, str)
            or not isinstance(self.domain, str) or not self.domain
            or not isinstance(self.path, str) or not self.path.startswith("/")
            or not isinstance(self.secure, bool)
            or (
                self.expires is not None
                and (not isinstance(self.expires, int) or isinstance(self.expires, bool))
            )
            or not isinstance(self.http_only, bool)
            or (self.same_site is not None and not isinstance(self.same_site, str))
        ):
            raise ValueError("invalid_cookie_record")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def normalize_cookie_records(records: Iterable[CookieRecord]) -> tuple[CookieRecord, ...]:
    result: list[CookieRecord] = []
    by_scope: dict[tuple[str, str, str], CookieRecord] = {}
    for record in records:
        if not isinstance(record, CookieRecord):
            raise ValueError("invalid_cookie_record")
        key = (record.name, record.domain, record.path)
        previous = by_scope.get(key)
        if previous is not None:
            if previous != record:
                raise ValueError("ambiguous_cookie_scope")
            continue
        by_scope[key] = record
        result.append(record)
    return tuple(result)


def cookie_record_from_json(value: Mapping[str, Any]) -> CookieRecord:
    if not isinstance(value, Mapping) or set(value) != {
        "name", "value", "domain", "path", "secure", "expires", "http_only", "same_site",
    }:
        raise ValueError("invalid_cookie_record")
    return CookieRecord(**value)


def playwright_cookie_records(records: Sequence[Mapping[str, Any]]) -> tuple[CookieRecord, ...]:
    converted: list[CookieRecord] = []
    for value in records:
        try:
            expires = value.get("expires")
            if isinstance(expires, bool):
                raise ValueError("invalid_cookie_record")
            normalized_expires = None
            if isinstance(expires, (int, float)) and expires > 0:
                if not math.isfinite(expires):
                    raise ValueError("invalid_cookie_record")
                normalized_expires = int(expires)
            converted.append(CookieRecord(
                name=value["name"],
                value=value["value"],
                domain=value["domain"],
                path=value.get("path") or "/",
                secure=value.get("secure", False),
                expires=normalized_expires,
                http_only=value.get("httpOnly", False),
                same_site=value.get("sameSite"),
            ))
        except (KeyError, OverflowError, TypeError, ValueError):
            raise ValueError("invalid_cookie_record") from None
    return normalize_cookie_records(converted)


def cookie_jar_from_records(records: Iterable[CookieRecord]) -> CookieJar:
    jar = CookieJar()
    for record in normalize_cookie_records(records):
        rest: dict[str, str | None] = {}
        if record.http_only:
            rest["HttpOnly"] = None
        if record.same_site is not None:
            rest["SameSite"] = record.same_site
        jar.set_cookie(Cookie(
            version=0, name=record.name, value=record.value,
            port=None, port_specified=False,
            domain=record.domain, domain_specified=True,
            domain_initial_dot=record.domain.startswith("."),
            path=record.path, path_specified=True,
            secure=record.secure, expires=record.expires,
            discard=record.expires is None,
            comment=None, comment_url=None, rest=rest, rfc2109=False,
        ))
    return jar


def cookie_records_from_jar(jar: CookieJar) -> tuple[CookieRecord, ...]:
    records: list[CookieRecord] = []
    for cookie in jar:
        same_site = cookie.get_nonstandard_attr("SameSite")
        records.append(CookieRecord(
            name=cookie.name,
            value=cookie.value,
            domain=cookie.domain,
            path=cookie.path or "/",
            secure=bool(cookie.secure),
            expires=cookie.expires,
            http_only=cookie.has_nonstandard_attr("HttpOnly"),
            same_site=same_site if isinstance(same_site, str) else None,
        ))
    return normalize_cookie_records(records)


def legacy_cookie_records(
    cookies: Mapping[str, str], *, domain: str, secure: bool,
) -> tuple[CookieRecord, ...]:
    if not isinstance(cookies, Mapping):
        raise ValueError("invalid_cookie_record")
    try:
        return normalize_cookie_records(
            CookieRecord(name=name, value=value, domain=domain, path="/", secure=secure)
            for name, value in cookies.items()
        )
    except (TypeError, ValueError):
        raise ValueError("invalid_cookie_record") from None


def legacy_cookie_scope(base_url: str) -> tuple[str, bool]:
    parsed = urlparse(base_url)
    if not parsed.hostname or parsed.scheme not in {"http", "https"}:
        raise ValueError("invalid_cookie_scope")
    return parsed.hostname, parsed.scheme == "https"

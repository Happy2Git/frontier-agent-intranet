"""Fail-closed network policy for intranet deployments.

The agent has several HTTP clients in different layers.  Checking only the
search tool therefore leaves an easy path for a page, a summary model, OCR,
or a model-authored subprocess to send task data elsewhere.  This module is a
small shared policy seam.  It does not create a proxy and it does not inspect
payloads; callers must invoke :func:`validate_outbound_url` immediately before
opening a connection.

``FRONTIER_AGENT_INTRANET_ONLY=1`` enables the policy.  The default is enabled
so a deployment has to opt out explicitly.  In this mode hostnames must resolve
only to private, loopback, or link-local addresses.  Operators may add exact
trusted hostnames and CIDRs with ``FRONTIER_AGENT_ALLOWED_NETWORK_HOSTS`` and
``FRONTIER_AGENT_ALLOWED_NETWORK_CIDRS``.  A hostname allowlist is still
checked through DNS; it is not a bypass for a public answer.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from functools import lru_cache
from urllib.parse import urlsplit


INTRANET_ONLY_ENV = "FRONTIER_AGENT_INTRANET_ONLY"
ALLOWED_HOSTS_ENV = "FRONTIER_AGENT_ALLOWED_NETWORK_HOSTS"
ALLOWED_CIDRS_ENV = "FRONTIER_AGENT_ALLOWED_NETWORK_CIDRS"
EXTERNAL_TELEMETRY_ENV = "FRONTIER_AGENT_TELEMETRY"


class NetworkPolicyError(ValueError):
    """Raised when an outbound destination is outside the configured policy."""


def intranet_only() -> bool:
    """Return whether outbound destinations must stay on the private network.

    The secure default is deliberate.  Setting the variable to a false-like
    value is an explicit operator decision for public-network deployments.
    """
    raw = os.environ.get(INTRANET_ONLY_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled", "public"}


def external_telemetry_enabled() -> bool:
    """Return whether an optional out-of-process telemetry hook may run.

    The repository has local usage and trace records, which are useful for
    audit and do not open a network connection.  Protocol emitters,
    cross-process usage aggregators, and third-party observers are a separate
    capability.  They stay disabled unless an operator explicitly opts in.
    """
    raw = os.environ.get(EXTERNAL_TELEMETRY_ENV, "off").strip().lower()
    return raw in {"1", "true", "yes", "on", "enabled"}


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(
        item.strip().lower().rstrip(".")
        for item in os.environ.get(name, "").split(",")
        if item.strip()
    )


def _host_allowed(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True
    for entry in _csv_env(ALLOWED_HOSTS_ENV):
        if entry.startswith("."):
            entry = entry[1:]
        if host == entry or host.endswith("." + entry):
            return True
    return False


@lru_cache(maxsize=16)
def _configured_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise NetworkPolicyError(
                f"invalid {ALLOWED_CIDRS_ENV} entry {item!r}"
            ) from exc
    return tuple(networks)


def _allowed_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    # RFC1918, loopback, link-local, and IPv6 ULA are the normal private
    # destinations.  Extra enterprise ranges must be explicitly configured.
    defaults = (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
    return _configured_networks(",".join((*defaults, *_csv_env(ALLOWED_CIDRS_ENV))))


def _is_allowed_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in network for network in _allowed_networks())


def _resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise NetworkPolicyError(f"cannot resolve network endpoint host {host!r}") from exc
    addresses = tuple(dict.fromkeys(str(row[4][0]) for row in rows))
    if not addresses:
        raise NetworkPolicyError(f"network endpoint host {host!r} has no addresses")
    return addresses


def validate_outbound_url(
    url: str,
    *,
    purpose: str = "outbound",
    require_path: str | None = None,
) -> str:
    """Validate *url* and return it unchanged for the caller to request.

    In public-network mode only syntax and credentials are checked.  In
    intranet mode every resolved address must be in the private-network policy.
    URL credentials and fragments are rejected in both modes because they are
    unnecessary data channels and frequently appear in copied links.
    """
    raw = (url or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NetworkPolicyError(f"{purpose} endpoint must use http:// or https://")
    if parsed.username or parsed.password:
        raise NetworkPolicyError(f"{purpose} endpoint must not contain URL credentials")
    if parsed.fragment:
        raise NetworkPolicyError(f"{purpose} endpoint must not contain a URL fragment")
    if require_path and not parsed.path.rstrip("/").endswith(require_path.rstrip("/")):
        raise NetworkPolicyError(
            f"{purpose} endpoint must end with {require_path!r}"
        )
    if not intranet_only():
        return raw

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise NetworkPolicyError(f"{purpose} endpoint has an invalid port") from exc

    host = parsed.hostname.rstrip(".").lower()
    # An explicit hostname allowlist documents operator intent, but DNS is
    # still checked to prevent an accidental public or split-horizon answer.
    if not _host_allowed(host):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # The resolution itself is allowed only to establish the policy;
            # no request is made until every resulting address passes below.
            pass

    addresses = _resolve_addresses(host, port)
    if not all(_is_allowed_address(address) for address in addresses):
        raise NetworkPolicyError(
            f"{purpose} endpoint {host!r} resolves outside the intranet network"
        )
    return raw


def endpoint_allowed(url: str, *, purpose: str = "outbound") -> bool:
    """Best-effort predicate for optional candidates such as summary models."""
    try:
        validate_outbound_url(url, purpose=purpose)
    except NetworkPolicyError:
        return False
    return True


def require_isolated_sandbox(backend: str, *, inner_bwrap: bool = False) -> None:
    """Reject subprocess backends that cannot enforce network isolation."""
    if not intranet_only():
        return
    normalized = (backend or "").strip().lower()
    if normalized in {"bwrap", "local"} or (normalized == "container" and inner_bwrap):
        return
    raise NetworkPolicyError(
        "intranet-only mode requires a network-isolated sandbox; use "
        "SANDBOX_BACKEND=bwrap or enable FRONTIER_AGENT_CONTAINER_INNER_BWRAP=1"
    )


__all__ = [
    "ALLOWED_CIDRS_ENV",
    "ALLOWED_HOSTS_ENV",
    "EXTERNAL_TELEMETRY_ENV",
    "INTRANET_ONLY_ENV",
    "NetworkPolicyError",
    "endpoint_allowed",
    "external_telemetry_enabled",
    "intranet_only",
    "require_isolated_sandbox",
    "validate_outbound_url",
]

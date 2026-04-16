"""
LDAP group membership resolution service.

This module resolves nested LDAP group members using the external LDAP API
and caches results to reduce repeated requests.
"""

import logging
import hashlib
import urllib.parse
from typing import Optional

import requests
from decouple import config
from django.core.cache import cache

from users.models import CustomUser

logger = logging.getLogger("powerbi_report")

LDAP_GROUP_MEMBERS_URL = config("LDAP_GROUP_MEMBERS_URL")
LDAP_API_TOKEN = config("LDAP_API_TOKEN")
_REPORTED_CIRCULAR_GROUPS: set[str] = set()


def _cache_key_for_group(group_name: str) -> str:
    """Build a memcache-safe cache key for arbitrary LDAP group names."""
    digest = hashlib.sha256(group_name.encode("utf-8")).hexdigest()
    return f"group_members:{digest}"


def _group_members_url(group_name: str) -> str:
    """Build the LDAP API URL without leaving raw group names in the path."""
    encoded_group = urllib.parse.quote(group_name, safe="")
    return f"{LDAP_GROUP_MEMBERS_URL}/{encoded_group}?token={LDAP_API_TOKEN}"


def _log_group_fetch_error(group_name: str, exc: requests.RequestException) -> None:
    """Log LDAP API failures without exposing token-bearing URLs."""
    status_code = None
    if getattr(exc, "response", None) is not None:
        status_code = exc.response.status_code

    if status_code:
        logger.warning(
            "Error fetching LDAP group '%s' from group-members API (status=%s).",
            group_name,
            status_code,
        )
    else:
        logger.warning(
            "Error fetching LDAP group '%s' from group-members API: %s",
            group_name,
            exc.__class__.__name__,
        )


def get_group_members(
    group_name: str,
    visited_groups: Optional[set[str]] = None,
    reported_cycles: Optional[set[str]] = None,
    depth: int = 0,
    max_depth: int = 10,
) -> set[str]:
    """
    Resolve a group's members recursively while preventing loops.
    """
    if visited_groups is None:
        visited_groups = set()
    if reported_cycles is None:
        reported_cycles = set()

    cache_key = _cache_key_for_group(group_name)
    cached_members = cache.get(cache_key)
    if cached_members is not None:
        logger.debug("LDAP group cache hit for '%s'.", group_name)
        return set(cached_members)

    if group_name in visited_groups:
        group_key = group_name.casefold()
        if group_key not in reported_cycles and group_key not in _REPORTED_CIRCULAR_GROUPS:
            logger.warning("Circular LDAP group reference detected for '%s'.", group_name)
            reported_cycles.add(group_key)
            _REPORTED_CIRCULAR_GROUPS.add(group_key)
        else:
            logger.debug("Circular LDAP group reference detected for '%s'.", group_name)
        return set()

    if depth >= max_depth:
        logger.warning("Max LDAP group recursion depth reached for '%s'.", group_name)
        return set()

    visited_groups.add(group_name)
    url = _group_members_url(group_name)

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        members = data.get("members", [])
        if not members:
            cache.set(cache_key, [], timeout=86400)
            return set()

        unique_members: set[str] = set()
        for member in members:
            cleaned_member = member.replace("GROUPE-HASNAOUI\\", "")

            if CustomUser.objects.filter(ad2000=cleaned_member).exists():
                unique_members.add(cleaned_member)
                continue

            is_potential_group = cleaned_member.isupper() or "-" in cleaned_member
            if not is_potential_group:
                unique_members.add(cleaned_member)
                continue

            sub_url = _group_members_url(cleaned_member)
            try:
                sub_response = requests.get(sub_url, timeout=10)
                if sub_response.status_code == 200 and "members" in sub_response.json():
                    sub_members = get_group_members(
                        cleaned_member,
                        visited_groups.copy(),
                        reported_cycles,
                        depth + 1,
                        max_depth,
                    )
                    unique_members.update(sub_members)
                else:
                    unique_members.add(cleaned_member)
            except requests.RequestException:
                unique_members.add(cleaned_member)
                logger.debug(
                    "Failed to validate nested LDAP group '%s'; treating as user.",
                    cleaned_member,
                )

        def resolve_groups(
            members_set: set[str],
            visited: set[str],
            current_depth: int,
            max_allowed_depth: int,
        ) -> set[str]:
            final_members: set[str] = set()
            groups_found = False

            for member in members_set:
                if CustomUser.objects.filter(ad2000=member).exists():
                    final_members.add(member)
                    continue

                is_potential_group = member.isupper() or "-" in member
                if not is_potential_group:
                    final_members.add(member)
                    continue

                sub_url = _group_members_url(member)
                try:
                    sub_response = requests.get(sub_url, timeout=10)
                    if sub_response.status_code == 200 and "members" in sub_response.json():
                        groups_found = True
                        sub_members = get_group_members(
                            member,
                            visited.copy(),
                            reported_cycles,
                            current_depth + 1,
                            max_allowed_depth,
                        )
                        final_members.update(sub_members)
                    else:
                        final_members.add(member)
                except requests.RequestException:
                    final_members.add(member)
                    logger.debug(
                        "Failed final LDAP group check for '%s'; treating as user.",
                        member,
                    )

            if groups_found:
                return resolve_groups(
                    final_members, visited, current_depth, max_allowed_depth
                )
            return final_members

        final_members = resolve_groups(unique_members, visited_groups.copy(), depth, max_depth)
        cache.set(cache_key, list(final_members), timeout=86400)
        logger.debug("Cached resolved LDAP members for group '%s'.", group_name)
        return final_members
    except requests.RequestException as exc:
        _log_group_fetch_error(group_name, exc)
        return set()
    finally:
        visited_groups.discard(group_name)

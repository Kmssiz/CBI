"""
LDAP group membership resolution service.

This module resolves nested LDAP group members using the external LDAP API
and caches results to reduce repeated requests.
"""

import logging
from typing import Optional

import requests
from decouple import config
from django.core.cache import cache

from users.models import CustomUser

logger = logging.getLogger("powerbi_report")

LDAP_GROUP_MEMBERS_URL = config("LDAP_GROUP_MEMBERS_URL")
LDAP_API_TOKEN = config("LDAP_API_TOKEN")


def get_group_members(
    group_name: str,
    visited_groups: Optional[set[str]] = None,
    depth: int = 0,
    max_depth: int = 10,
) -> set[str]:
    """
    Resolve a group's members recursively while preventing loops.
    """
    if visited_groups is None:
        visited_groups = set()

    cache_key = f"group_members:{group_name}"
    cached_members = cache.get(cache_key)
    if cached_members is not None:
        logger.debug("LDAP group cache hit for '%s'.", group_name)
        return set(cached_members)

    if group_name in visited_groups:
        logger.warning("Circular LDAP group reference detected for '%s'.", group_name)
        return set()

    if depth >= max_depth:
        logger.warning("Max LDAP group recursion depth reached for '%s'.", group_name)
        return set()

    visited_groups.add(group_name)
    url = f"{LDAP_GROUP_MEMBERS_URL}/{group_name}?token={LDAP_API_TOKEN}"

    try:
        response = requests.get(url)
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

            sub_url = f"{LDAP_GROUP_MEMBERS_URL}/{cleaned_member}?token={LDAP_API_TOKEN}"
            try:
                sub_response = requests.get(sub_url)
                if sub_response.status_code == 200 and "members" in sub_response.json():
                    sub_members = get_group_members(
                        cleaned_member, visited_groups.copy(), depth + 1, max_depth
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

                sub_url = f"{LDAP_GROUP_MEMBERS_URL}/{member}?token={LDAP_API_TOKEN}"
                try:
                    sub_response = requests.get(sub_url)
                    if sub_response.status_code == 200 and "members" in sub_response.json():
                        groups_found = True
                        sub_members = get_group_members(
                            member,
                            visited.copy(),
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
        logger.error("Error fetching LDAP group '%s': %s", group_name, exc)
        return set()
    finally:
        visited_groups.discard(group_name)

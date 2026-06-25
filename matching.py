from __future__ import annotations
import json
import math
import random
from collections import defaultdict

GROUP_SIZE_RANGES = {
    "small":  (4, 5),
    "medium": (6, 8),
    "large":  (8, 10),
}

W_AVAILABILITY   = 2.0
W_NEIGHBOURHOOD  = 5.0
W_DINNER_FORMAT  = 4.0
W_AGE_PROXIMITY  = 3.0
W_GROUP_SIZE     = 2.0
W_DISTANCE       = 5.0


def _parse(person: dict) -> dict:
    p = dict(person)
    for field in ("dietary", "availability"):
        if isinstance(p.get(field), str):
            try:
                p[field] = json.loads(p[field])
            except (json.JSONDecodeError, TypeError):
                p[field] = []
    for field in ("lat", "lng"):
        if p.get(field) is not None:
            try:
                p[field] = float(p[field])
            except (ValueError, TypeError):
                p[field] = None
    for field in ("age", "age_range_pref", "max_travel_km"):
        if p.get(field) is not None:
            try:
                p[field] = int(p[field])
            except (ValueError, TypeError):
                p[field] = None
    p.setdefault("dinner_format", "any")
    p.setdefault("dinner_format_is_must", 0)
    p.setdefault("group_size_pref", "medium")
    p.setdefault("age_range_pref", 10)
    p.setdefault("age_range_is_must", 0)
    p.setdefault("max_travel_km", 10)
    p.setdefault("neighbourhood", "")
    p.setdefault("can_host", 0)
    return p


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def score_pair(a: dict, b: dict) -> float:
    score = 0.0

    avail_a = set(a.get("availability") or [])
    avail_b = set(b.get("availability") or [])
    score += len(avail_a & avail_b) * W_AVAILABILITY

    if a.get("neighbourhood") and b.get("neighbourhood"):
        if a["neighbourhood"].lower() == b["neighbourhood"].lower():
            score += W_NEIGHBOURHOOD

    fmt_a = a.get("dinner_format", "any")
    fmt_b = b.get("dinner_format", "any")
    if fmt_a == fmt_b and fmt_a != "any":
        score += W_DINNER_FORMAT
    elif fmt_a == "any" or fmt_b == "any":
        score += W_DINNER_FORMAT * 0.5

    age_a = a.get("age")
    age_b = b.get("age")
    if age_a and age_b:
        pref = max(a.get("age_range_pref") or 10, b.get("age_range_pref") or 10)
        diff = abs(age_a - age_b)
        if diff <= pref:
            score += W_AGE_PROXIMITY * (1 - diff / pref)

    if a.get("group_size_pref") == b.get("group_size_pref"):
        score += W_GROUP_SIZE

    lat_a, lng_a = a.get("lat"), a.get("lng")
    lat_b, lng_b = b.get("lat"), b.get("lng")
    if lat_a and lng_a and lat_b and lng_b:
        max_km = max(a.get("max_travel_km") or 10, b.get("max_travel_km") or 10)
        dist = _haversine_km(lat_a, lng_a, lat_b, lng_b)
        if dist <= max_km:
            score += W_DISTANCE * (1 - dist / max_km)

    return score


def hard_constraints_compatible(candidate: dict, group: list[dict], past_pairs: set[tuple] = None) -> bool:
    # Prevent rematching
    if past_pairs:
        for member in group:
            pair = (min(candidate["id"], member["id"]), max(candidate["id"], member["id"]))
            if pair in past_pairs:
                return False

    if candidate.get("dinner_format_is_must") and candidate.get("dinner_format", "any") != "any":
        candidate_fmt = candidate["dinner_format"]
        for member in group:
            if (member.get("dinner_format_is_must")
                    and member.get("dinner_format", "any") not in ("any", candidate_fmt)):
                return False

    if candidate.get("age_range_is_must") and candidate.get("age"):
        c_age = candidate["age"]
        c_pref = candidate.get("age_range_pref") or 10
        members_with_age = [m for m in group if m.get("age")]
        if members_with_age:
            if not any(abs(m["age"] - c_age) <= c_pref for m in members_with_age):
                return False

    c_lat, c_lng = candidate.get("lat"), candidate.get("lng")
    if c_lat and c_lng:
        c_km = candidate.get("max_travel_km") or 10
        for member in group:
            m_lat, m_lng = member.get("lat"), member.get("lng")
            if m_lat and m_lng:
                max_km = max(c_km, member.get("max_travel_km") or 10)
                if _haversine_km(c_lat, c_lng, m_lat, m_lng) > max_km:
                    return False

    return True


def _resolve_linked_units(people: list[dict]) -> tuple[list[list[dict]], list[dict]]:
    by_code: dict[str, list[dict]] = defaultdict(list)
    unlinked = []
    for p in people:
        code = (p.get("link_code") or "").strip().upper()
        if code:
            by_code[code].append(p)
        else:
            unlinked.append(p)
    locked_units = []
    for members in by_code.values():
        if len(members) >= 2:
            locked_units.append(members)
        else:
            unlinked.extend(members)
    return locked_units, unlinked


def _resolve_group_format(group: list[dict]) -> str:
    votes: dict[str, int] = defaultdict(int)
    for p in group:
        fmt = p.get("dinner_format", "any")
        if fmt != "any":
            votes[fmt] += 1
    if not votes:
        return "potluck"
    max_votes = max(votes.values())
    top = [f for f, v in votes.items() if v == max_votes]
    return top[0] if len(top) == 1 else "potluck"


def _assign_host(group: list[dict]) -> tuple[dict | None, bool]:
    """Return (host, needs_host_flag). host is None if nobody can host."""
    hosts = [m for m in group if m.get("can_host")]
    if not hosts:
        return None, True
    # prefer whoever has "hosted" as dinner format, else pick randomly
    preferred = [h for h in hosts if h.get("dinner_format") == "hosted"]
    host = preferred[0] if preferred else random.choice(hosts)
    return host, False


def run_matching(
    people: list[dict],
    past_pairs: set[tuple] | None = None,
    default_min: int = 4,
    default_max: int = 8,
) -> list[dict]:
    """
    Returns a list of group dicts, each with:
      - members: list of person dicts
      - dinner_format: resolved format string
      - host: person dict or None
      - needs_host: bool
    """
    people = [_parse(p) for p in people]
    past_pairs = past_pairs or set()
    random.shuffle(people)

    locked_units, unlinked = _resolve_linked_units(people)
    raw_groups: list[list[dict]] = []
    assigned: set = set()

    # Seed groups from locked pairs/units first
    for unit in locked_units:
        _, max_s = GROUP_SIZE_RANGES.get(unit[0].get("group_size_pref", "medium"), (default_min, default_max))
        for i in range(0, len(unit), max_s):
            chunk = unit[i:i + max_s]
            if len(chunk) >= 2:
                raw_groups.append(chunk)
                assigned.update(m["id"] for m in chunk)
            else:
                unlinked.extend(chunk)

    remaining = [p for p in unlinked if p["id"] not in assigned]

    # Pass 1: neighbourhood grouping
    by_neighbourhood: dict[str, list[dict]] = defaultdict(list)
    for p in remaining:
        by_neighbourhood[p.get("neighbourhood", "").lower()].append(p)

    for hood, hood_people in by_neighbourhood.items():
        if not hood:
            continue
        min_s, max_s = GROUP_SIZE_RANGES.get(
            hood_people[0].get("group_size_pref", "medium"), (default_min, default_max)
        )
        if len(hood_people) < min_s:
            continue
        group: list[dict] = []
        for p in hood_people:
            if p["id"] in assigned:
                continue
            if len(group) >= max_s:
                if len(group) >= min_s:
                    raw_groups.append(group)
                    assigned.update(m["id"] for m in group)
                group = []
                min_s, max_s = GROUP_SIZE_RANGES.get(
                    p.get("group_size_pref", "medium"), (default_min, default_max)
                )
            if hard_constraints_compatible(p, group, past_pairs):
                group.append(p)
        if len(group) >= min_s:
            raw_groups.append(group)
            assigned.update(m["id"] for m in group)

    # Pass 2: greedy score-based for remainder
    pool = [p for p in remaining if p["id"] not in assigned]

    while pool:
        seed = pool.pop(0)
        if seed["id"] in assigned:
            continue
        min_s, max_s = GROUP_SIZE_RANGES.get(
            seed.get("group_size_pref", "medium"), (default_min, default_max)
        )
        group = [seed]

        candidates = [p for p in pool if p["id"] not in assigned and hard_constraints_compatible(p, group, past_pairs)]
        candidates.sort(key=lambda p: score_pair(seed, p), reverse=True)

        for candidate in candidates:
            if len(group) >= max_s:
                break
            if hard_constraints_compatible(candidate, group, past_pairs):
                group.append(candidate)

        if len(group) >= min_s:
            raw_groups.append(group)
            assigned.update(m["id"] for m in group)
        else:
            for existing in raw_groups:
                _, max_s_ex = GROUP_SIZE_RANGES.get(
                    existing[0].get("group_size_pref", "medium"), (default_min, default_max)
                )
                if (len(existing) + len(group) <= max_s_ex
                        and all(hard_constraints_compatible(m, existing, past_pairs) for m in group)):
                    existing.extend(group)
                    assigned.update(m["id"] for m in group)
                    break
            else:
                assigned.update(m["id"] for m in group)

        pool = [p for p in pool if p["id"] not in assigned]

    # Annotate each group with format, host, needs_host
    groups = []
    for members in raw_groups:
        fmt = _resolve_group_format(members)
        host, needs_host = _assign_host(members)
        for m in members:
            m["_resolved_format"] = fmt
        groups.append({
            "members": members,
            "dinner_format": fmt,
            "host": host,
            "needs_host": needs_host,
        })

    return groups

"""Shared deterministic allocation policy for saved effective search specs."""
PRIORITY = 'official_priority_once'
OFFICIAL = 'official_career'
BASE_OTHER = 5
FAILURE_ALLOWANCE = 1


def other_quota(channels, per_channel):
    """非官网渠道的单渠道额度：显式给了就按显式值，没给用 BASE_OTHER。

    官网优先分配下，官网额度永远是"城市总预算扣除其它渠道"的余量，
    所以官网不参与这里，也不接受单独指定。
    """
    return {c: per_channel.get(c, BASE_OTHER) for c in channels if c != OFFICIAL}


def validate_allocation(cities, channels, per_city, per_channel, policy):
    if policy != PRIORITY:
        return
    if OFFICIAL in per_channel:
        raise ValueError('官网优先分配下官网额度由城市预算扣除其它渠道后得出，不能单独指定；请改城市预算')
    quota = other_quota(channels, per_channel)
    minimum = sum(quota.values())
    if not minimum:
        return
    for city in cities:
        if per_city[city] < minimum:
            detail = '、'.join(f'{c}×{n}' for c, n in quota.items())
            raise ValueError(f'{city}基础预算至少需要{minimum}次，才能给其它已选渠道配额（{detail}）；请提高预算或减少渠道')


def base_allocation(cities, channels, per_city, per_channel=None):
    quota = other_quota(channels, per_channel or {})
    other_total = sum(quota.values())
    return {city: {channel: max(0, per_city[city] - other_total) if channel == OFFICIAL else quota[channel]
                   for channel in channels} for city in cities}

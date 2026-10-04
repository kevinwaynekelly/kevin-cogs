"""Bound the named-game catalog while preserving lifetime activity counters."""

MAX_GAME_NAMES = 100
MAX_GAME_NAME_LENGTH = 128


def bounded_games(names):
    """Keep the last 100 catalog entries; merge names truncated to the same title."""
    retained = {}
    for name, count in names.items():
        if not isinstance(name, str) or type(count) is not int or count < 0:
            continue
        name = name[:MAX_GAME_NAME_LENGTH]
        if not name:
            continue
        retained[name] = retained.pop(name, 0) + count
        if len(retained) > MAX_GAME_NAMES:
            retained.pop(next(iter(retained)))
    return retained


def record_game(names, name):
    """Promote a launched game to the end of the bounded catalog."""
    name = str(name)[:MAX_GAME_NAME_LENGTH]
    if name:
        names[name] = names.pop(name, 0) + 1
    return bounded_games(names)

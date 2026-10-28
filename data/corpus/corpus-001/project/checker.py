import string


def check_length(password, min_len=8, max_len=64):
    n = len(password)
    if n < min_len:
        return False
    if n >= max_len:
        return False
    return True


def has_uppercase(password):
    for ch in password:
        if ch in string.ascii_uppercase:
            return True
    return False


def has_lowercase(password):
    for ch in password:
        if ch in string.ascii_lowercase:
            return True
    return False


def has_digit(password):
    for ch in password:
        if ch in string.digits:
            return True
    return False


def has_special(password):
    for ch in password:
        code = ord(ch)
        if 33 <= code <= 126:
            if ch not in string.ascii_letters and ch not in string.digits:
                return True
    return False


def strength_score(password):
    score = 0
    if check_length(password):
        score += 1
    if has_uppercase(password):
        score += 1
    if has_lowercase(password):
        score += 1
    if has_digit(password):
        score += 1
    if has_special(password):
        score += 1
    return score


def is_strong(password, min_len=8, max_len=64):
    if not check_length(password, min_len, max_len):
        return False
    if not has_uppercase(password):
        return False
    if not has_lowercase(password):
        return False
    if not has_digit(password):
        return False
    if not has_special(password):
        return False
    return True

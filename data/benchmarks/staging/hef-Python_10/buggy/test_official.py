from mod_make_palindrome import make_palindrome





def check(make_palindrome):
    assert make_palindrome('') == ''
    assert make_palindrome('x') == 'x'
    assert make_palindrome('xyz') == 'xyzyx'
    assert make_palindrome('xyx') == 'xyx'
    assert make_palindrome('jerry') == 'jerryrrej'

check(make_palindrome)
check(make_palindrome)
print('OFFICIAL-OK')

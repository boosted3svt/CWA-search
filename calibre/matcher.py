import re
import unicodedata


def normalize(text):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', text).casefold()))


def matches(book, title=None, author=None, exact=False):
    def compare(wanted, actual):
        wanted, actual = normalize(wanted), normalize(actual)
        return bool(wanted) and (wanted == actual if exact else f' {wanted} ' in f' {actual} ')
    return ((not title or compare(title, book.title)) and
            (not author or any(compare(author, name) for name in book.authors)))

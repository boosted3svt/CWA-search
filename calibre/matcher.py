import re
import unicodedata


def normalize(text):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', text).casefold()))


def normalize_author(text):
    """Treat explicit 'Family, Given' metadata as 'Given Family'."""
    parts = text.split(',')
    if len(parts) == 2 and all(part.strip() for part in parts):
        # A trailing suffix is not a surname-first author name.
        if normalize(parts[1]) not in ('jr', 'sr', 'ii', 'iii', 'iv'):
            text = parts[1] + ' ' + parts[0]
    return normalize(text)


def matches(book, title=None, author=None, exact=False):
    def compare(wanted, actual, normalizer=normalize):
        wanted, actual = normalizer(wanted), normalizer(actual)
        return bool(wanted) and (wanted == actual if exact else f' {wanted} ' in f' {actual} ')
    return ((not title or compare(title, book.title)) and
            (not author or any(compare(author, name, normalize_author) for name in book.authors)))

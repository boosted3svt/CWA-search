"""Conservative library-name policy; this does not detect individual book languages."""
import re
import unicodedata


def library_language(settings, source, identifier, name):
    overrides = settings.library_language_overrides
    override = overrides.get(source+'?library_id='+identifier, overrides.get(identifier))
    if override:
        return override, 'configured override'
    text = unicodedata.normalize('NFKC', name or identifier)
    if any(ch.isalpha() and not unicodedata.name(ch, '').startswith('LATIN') for ch in text):
        return 'non-english', 'non-Latin lettering in library name'
    words = set(re.findall(r'[^\W\d_]+', text.casefold()))
    foreign = {'bibliothek','bücher','bucher','buecher','deutsch','englisch',
               'bibliothèque','bibliotheque','livres','français','francais','anglais',
               'biblioteca','libros','livros','español','espanol'}
    if words & foreign:
        return 'non-english', 'non-English library-name wording: '+', '.join(sorted(words & foreign))
    if words & {'library','libraries','books','ebooks','english','fiction','nonfiction'}:
        return 'english', 'English library-name wording'
    return 'unknown', 'library name is too ambiguous to identify a language'


def enforce_language(settings, library, identifier, names):
    from .client import ClassifiedError
    name = names.get(identifier, identifier)
    language, reason = library_language(settings, library.url, identifier, name)
    if language == 'non-english' or (language == 'unknown' and settings.reject_unknown_library_language):
        raise ClassifiedError(f'Library language policy rejected {name!r}: {reason}', 'language')
    return language

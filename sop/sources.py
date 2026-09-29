"""Read-only, bounded Skill source snapshots.

Only explicit Markdown references are followed. Source text is never executed;
paths are retained for freshness checks and must be removed from model context.
All references are confined to the main source's directory. Unsupported or
unreadable references remain diagnostics instead of disappearing from coverage.
"""
import hashlib
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit


MAX_FILES = 32
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
_SUFFIXES = {'.md', '.txt', '.json', '.yaml', '.yml', '.py', '.sh'}
_MARKDOWN_SUFFIXES = {'.md', '.txt'}
_ESCAPE = re.compile(r'\\([!"#$%&\'()*+,\-./:;<=>?@\[\]\\^_`{|}~])')


def _blank(value):
    return ''.join('\n' if char == '\n' else ' ' for char in value)


def _prose(text):
    """Mask fenced/indented and inline code without changing line locations."""
    result = []
    fence = None
    list_body = False
    for line in text.splitlines(keepends=True):
        if re.match(r'^ {0,3}(?:[-+*]|\d+[.)])\s', line):
            list_body = True
        elif line.strip() and not line.startswith((' ', '\t')):
            list_body = False
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line.rstrip('\r\n'))
        if fence:
            result.append(_blank(line))
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
        elif marker and (marker[1][0] != '`' or '`' not in marker[2]):
            fence = (marker[1][0], len(marker[1]))
            result.append(_blank(line))
        elif line.startswith(('    ', '\t')) and not list_body:
            result.append(_blank(line))
        else:
            result.append(line)
    prose = re.sub(r'<!--.*?(?:-->|\Z)', lambda match: _blank(match[0]), ''.join(result), flags=re.S)
    # CommonMark code spans close with the same run length of backticks.
    cursor = 0
    while cursor < len(prose):
        if prose[cursor] != '`':
            cursor += 1
            continue
        end = cursor
        while end < len(prose) and prose[end] == '`':
            end += 1
        marker = prose[cursor:end]
        closing = re.search(r'(?<!`)' + re.escape(marker) + r'(?!`)', prose[end:])
        if closing:
            last = end + closing.end()
            prose = prose[:cursor] + _blank(prose[cursor:last]) + prose[last:]
            cursor = last
        else:
            cursor = end
    return prose


def _destination(value):
    """Parse a destination with an optional standard Markdown title."""
    value = value.strip()
    if not value:
        return ''
    if value.startswith('<'):
        closing = value.find('>')
        if closing == -1 or '\n' in value[:closing] or '<' in value[1:closing]:
            raise ValueError('Malformed angle-bracket link destination')
        target, rest = value[1:closing], value[closing + 1:].strip()
    else:
        depth = 0
        cursor = 0
        while cursor < len(value):
            char = value[cursor]
            if char == '\\' and cursor + 1 < len(value):
                cursor += 2
                continue
            if char.isspace() and depth == 0:
                break
            depth += (char == '(') - (char == ')')
            if depth < 0:
                raise ValueError('Unbalanced link destination')
            cursor += 1
        if depth:
            raise ValueError('Unbalanced link destination')
        target, rest = value[:cursor], value[cursor:].strip()
    if rest and not (len(rest) >= 2 and (rest[0], rest[-1]) in {('"', '"'), ("'", "'"), ('(', ')')}):
        raise ValueError('Unsupported text after link destination')
    return _ESCAPE.sub(r'\1', target)


def _markdown_links(text):
    """Return (line, destination, error) for supported explicit references."""
    prose = _prose(text)
    definitions = {}
    diagnostic = []
    lines = prose.splitlines(keepends=True)
    for number, line in enumerate(lines, 1):
        match = re.match(r'^ {0,3}\[([^\]\n]+)\]:[ \t]*(.*)$', line.rstrip('\r\n'))
        if not match:
            continue
        label = ' '.join(match[1].split()).casefold()
        try:
            target = _destination(match[2])
            if label in definitions and definitions[label] != target:
                diagnostic.append((number, match[1], 'Conflicting Markdown reference definitions'))
            else:
                definitions[label] = target
        except ValueError as error:
            diagnostic.append((number, match[2], str(error)))
        lines[number - 1] = _blank(line)
    prose = ''.join(lines)
    found = list(diagnostic)
    cursor = 0
    while (begin_label := prose.find('[', cursor)) != -1:
        # An escaped opening bracket is ordinary text, not a source reference.
        prefix = prose[:begin_label]
        if (len(prefix) - len(prefix.rstrip('\\'))) % 2:
            cursor = begin_label + 1
            continue
        end_label = begin_label + 1
        depth = 1
        while end_label < len(prose):
            if prose[end_label] == '\\':
                end_label += 2
                continue
            depth += (prose[end_label] == '[') - (prose[end_label] == ']')
            if depth == 0:
                break
            end_label += 1
        if depth:
            cursor = begin_label + 1
            continue
        cursor = end_label + 1
        label = prose[begin_label + 1:end_label]
        number = prose.count('\n', 0, begin_label) + 1
        if cursor < len(prose) and prose[cursor] == '(':
            begin = cursor + 1
            end = begin
            depth = 1
            angle = False
            quote = None
            while end < len(prose):
                char = prose[end]
                if char == '\\':
                    end += 2
                    continue
                if quote:
                    if char == quote:
                        quote = None
                elif char in ('"', "'") and end > begin and prose[end - 1].isspace():
                    quote = char
                elif char == '<' and end == begin:
                    angle = True
                elif char == '>' and angle:
                    angle = False
                elif not angle:
                    depth += (char == '(') - (char == ')')
                    if depth == 0:
                        break
                end += 1
            if depth:
                found.append((number, prose[begin:].splitlines()[0], 'Unclosed Markdown link'))
                continue
            try:
                found.append((number, _destination(prose[begin:end]), None))
            except ValueError as error:
                found.append((number, prose[begin:end], str(error)))
            cursor = end + 1
        elif cursor < len(prose) and prose[cursor] == '[':
            end = prose.find(']', cursor + 1)
            if end == -1:
                found.append((number, label, 'Unclosed Markdown reference link'))
                continue
            key = ' '.join((prose[cursor + 1:end] or label).split()).casefold()
            if key not in definitions:
                found.append((number, key, 'Missing Markdown reference definition'))
            else:
                found.append((number, definitions[key], None))
            cursor = end + 1
        else:
            key = ' '.join(label.split()).casefold()
            if key in definitions:
                found.append((number, definitions[key], None))
    return found


def _read_regular(path, limit):
    """Open without following links and detect replacement during the read."""
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            except OSError:
                # Identify links without following them, including ancestor links.
                if stat.S_ISLNK(os.stat(component, dir_fd=parent, follow_symlinks=False).st_mode):
                    raise ValueError('Symbolic links are not allowed in source paths')
                raise
            os.close(parent)
            parent = child
        if stat.S_ISLNK(os.stat(path.name, dir_fd=parent, follow_symlinks=False).st_mode):
            raise ValueError('Symbolic links are not allowed in source paths')
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(descriptor, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError('Source must be a regular file with no hard links')
        if before.st_size > limit:
            raise OverflowError('Source exceeds the per-file byte limit')
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
        current = path.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise ValueError('Source changed while being imported')
        if len(raw) > limit:
            raise OverflowError('Source exceeds the per-file byte limit')
        return raw


def collect_sources(source_path):
    """Snapshot a Skill and its explicit local text references without executing.

    Returns ``root_id``, ``files`` and structured ``diagnostics``. Any diagnostic
    blocks complete authoring; successfully read material remains reviewable.
    IDs and relative paths are relative to the root Skill's containing directory.
    """
    source = Path(os.path.abspath(os.fspath(source_path)))
    root = source.parent
    result = {'root_id': source.name, 'files': [], 'diagnostics': []}
    pending = [(source, source.name, None)]
    seen = set()
    total = 0

    def problem(code, owner, line, reference, message):
        result['diagnostics'].append({'code': code, 'source_id': owner, 'line': line,
                                      'reference': reference, 'message': message})

    while pending:
        path, owner, line = pending.pop(0)
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        if len(result['files']) >= MAX_FILES:
            problem('source_limit', owner, line, relative, 'Source collection exceeds the file count limit')
            continue
        if path.suffix.lower() not in _SUFFIXES:
            problem('unsupported_source', owner, line, relative, 'Source file type is not supported')
            continue
        try:
            raw = _read_regular(path, MAX_FILE_BYTES)
            if total + len(raw) > MAX_TOTAL_BYTES:
                problem('source_limit', owner, line, relative, 'Source collection exceeds the total byte limit')
                continue
            text = raw.decode('utf-8')
            if any(ord(char) < 32 and char not in '\t\r\n' for char in text):
                raise UnicodeError('Binary control bytes in text source')
        except FileNotFoundError:
            problem('missing_source', owner, line, relative, 'Referenced source does not exist')
            continue
        except OverflowError as error:
            problem('source_limit', owner, line, relative, str(error))
            continue
        except UnicodeError:
            problem('invalid_source_encoding', owner, line, relative, 'Source must contain UTF-8 text without binary control bytes')
            continue
        except (OSError, ValueError) as error:
            problem('unsafe_source' if isinstance(error, ValueError) else 'unreadable_source', owner, line, relative, str(error))
            continue
        total += len(raw)
        result['files'].append({'id': relative, 'relative_path': relative, 'path': str(path),
                                'sha256': hashlib.sha256(raw).hexdigest(), 'text': text,
                                'lines': [{'line': index, 'text': value}
                                          for index, value in enumerate(text.splitlines(), 1)]})
        if path.suffix.lower() not in _MARKDOWN_SUFFIXES:
            continue
        for link_line, target, error in _markdown_links(text):
            if error:
                problem('invalid_source_reference', relative, link_line, target, error)
                continue
            if not target or target.startswith('#'):
                continue  # An in-document reference requires no additional file.
            try:
                parts = urlsplit(target)
            except ValueError:
                problem('invalid_source_reference', relative, link_line, target, 'Malformed link target')
                continue
            decoded = unquote(parts.path)
            if (parts.scheme or parts.netloc or parts.query or parts.fragment or '?' in target or '#' in target
                    or '?' in decoded or '#' in decoded or '\\' in decoded or '\x00' in decoded or Path(decoded).is_absolute()):
                problem('unsupported_source_reference', relative, link_line, target,
                        'Only relative local file references without URI queries or fragments are supported')
                continue
            candidate = Path(os.path.abspath(path.parent / decoded))
            if not candidate.is_relative_to(root):
                problem('unsafe_source', relative, link_line, target, 'Reference escapes the Skill source directory')
                continue
            pending.append((candidate, relative, link_line))
    return result
